#!/usr/bin/env python3
"""
risk_classifier.py — Tier 0–3 risk classification for Intent Objects.

Tiers:
  Tier 0 — Read-only operations. Auto-execute. No prompt. No notification.
  Tier 1 — Low-risk writes (user home dir, user services). Auto-execute + audit log.
  Tier 2 — Medium-risk (system services, package install). Auto-execute + notification.
              QB verification hook fires here (verifier.py).
  Tier 3 — High/critical (outside home dir, firewall, /etc/sudoers, /boot, /root). BLOCK + HITL prompt.
              Majority vote (voting.py) + QB verification + COW dry-run + HITL.

Usage:
  from risk_classifier import classify, Tier
  result = classify(intent)   # returns ClassificationResult

  # After PB generates a command, run post-generation hooks:
  from risk_classifier import run_post_generation_hooks
  hooks_result = run_post_generation_hooks(tier, ref_id, command, intent_description)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional


class Tier(IntEnum):
    READ_ONLY = 0   # auto-execute, no UI
    LOW       = 1   # auto-execute + audit log
    MEDIUM    = 2   # auto-execute + notification
    HIGH      = 3   # BLOCK — requires explicit user approval


@dataclass
class ClassificationResult:
    tier: Tier
    reason: str
    reversible: bool
    blocked_pattern: Optional[str] = None

    @property
    def requires_hitl(self) -> bool:
        return self.tier == Tier.HIGH

    @property
    def auto_execute(self) -> bool:
        return self.tier in (Tier.READ_ONLY, Tier.LOW, Tier.MEDIUM)


# ── Tier 0: Always read-only — never need a prompt ───────────────────────────
_TIER0_TOOLS = frozenset({
    "system.status", "system.uptime", "system.cpu",
    "system.memory", "system.disk",
    "process.list", "process.inspect",
    "service.logs", "service.status",
    "fs.read", "fs.list", "fs.stat",
    "network.status", "network.interfaces", "network.dns",
    "package.list", "package.search", "package.info",
})

# ── Tier 3: Always block — these touch sensitive system locations ──────────────
_CRITICAL_PATHS = re.compile(
    r"(/boot|/etc/sudoers|/etc/shadow|/etc/passwd|/root|"
    r"/proc/sys/kernel|/dev/sd[a-z]|/dev/nvme|/sys/firmware)",
    re.IGNORECASE,
)

_DESTRUCTIVE_TOOLS = frozenset({
    "fs.delete",
    "network.firewall_flush",
    "network.firewall_delete",
    "package.purge",
})

_SYSTEM_WRITE_TOOLS = frozenset({
    "network.firewall_add",
    "network.firewall_modify",
    "package.install",
    "package.remove",
    "package.upgrade",
})

_USER_HOME = os.path.expanduser("~")


def _target_is_in_user_home(target: str) -> bool:
    """True if the target path is within the current user's home directory."""
    if not target:
        return False
    try:
        return os.path.abspath(target).startswith(_USER_HOME)
    except Exception:
        return False


def _target_is_critical(target: str) -> bool:
    """True if the target is a sensitive system path."""
    return bool(_CRITICAL_PATHS.search(target))


def classify(intent: dict) -> ClassificationResult:
    """
    Classify an Intent Object into a risk tier.

    Args:
        intent: Validated Intent Object (must pass JSON schema validation first).

    Returns:
        ClassificationResult with tier, reason, reversibility.
    """
    action     = intent.get("action", "")
    target     = intent.get("target", "")
    risk_level = intent.get("risk_level", "")

    # ── Explicit critical override from intent ────────────────────────────────
    if risk_level == "critical":
        return ClassificationResult(
            tier=Tier.HIGH,
            reason="Intent explicitly marked critical risk level",
            reversible=False,
        )

    # ── Tier 0: Pure read-only tools ─────────────────────────────────────────
    if action in _TIER0_TOOLS:
        return ClassificationResult(
            tier=Tier.READ_ONLY,
            reason=f"{action} is a read-only operation",
            reversible=True,
        )

    # ── Tier 3: Any operation touching a critical path ────────────────────────
    if _target_is_critical(target):
        return ClassificationResult(
            tier=Tier.HIGH,
            reason=f"Target '{target}' is a protected system path",
            reversible=False,
            blocked_pattern=_CRITICAL_PATHS.search(target).group(0) if _CRITICAL_PATHS.search(target) else None,
        )

    # ── Tier 3: Destructive tools ─────────────────────────────────────────────
    if action in _DESTRUCTIVE_TOOLS:
        return ClassificationResult(
            tier=Tier.HIGH,
            reason=f"{action} is a destructive operation — requires explicit approval",
            reversible=False,
        )

    # ── Tier 3: Writes outside user home ─────────────────────────────────────
    if action.startswith("fs.write") and target and not _target_is_in_user_home(target):
        return ClassificationResult(
            tier=Tier.HIGH,
            reason=f"fs.write to '{target}' is outside user home directory",
            reversible=False,
        )

    # ── Tier 3: Network/firewall mutations ───────────────────────────────────
    if action in _SYSTEM_WRITE_TOOLS:
        return ClassificationResult(
            tier=Tier.MEDIUM,
            reason=f"{action} modifies system-level configuration",
            reversible=action.startswith("package."),
        )

    # ── Tier 2: Service restarts for system (non-user) services ──────────────
    if action in ("service.restart", "service.stop", "service.start"):
        user_service = target.endswith(".service") and "/" in target
        if not user_service:
            return ClassificationResult(
                tier=Tier.MEDIUM,
                reason=f"Service operation on '{target}' — notify user",
                reversible=action != "service.stop",
            )

    # ── Tier 1: Low-risk writes inside user home ──────────────────────────────
    if action.startswith("fs.write") and _target_is_in_user_home(target):
        return ClassificationResult(
            tier=Tier.LOW,
            reason=f"Write to user home directory — auto-execute with audit",
            reversible=True,
        )

    # ── Default: Medium — log and notify ─────────────────────────────────────
    return ClassificationResult(
        tier=Tier.MEDIUM,
        reason=f"Unclassified action '{action}' — applying medium risk tier",
        reversible=True,
    )


# ── Post-generation hooks (Arch 2: QB verify, Arch 3: majority vote) ─────────

from dataclasses import dataclass as _dc, field as _field

@_dc
class PostGenerationResult:
    """Result of the post-generation hook pipeline."""
    command: str               # final command to use (may differ from input if voting ran)
    ref_id: str                # may be a revised ref_id if QB flagged a correction
    requires_revision: bool    # True if PB should regenerate with revised Intent Object
    low_confidence: bool       # True if majority vote found no consensus (warn in HITL)
    verification_skipped: bool # True if QB was unreachable (fail-open)
    vote_candidates: list      # all k vote candidates (empty if voting didn't run)


def run_post_generation_hooks(
    tier: Tier,
    ref_id: str,
    command: str,
    intent_description: str,
) -> PostGenerationResult:
    """Run accuracy improvement hooks after the Privileged Brain generates a command.

    Tier 2 (MEDIUM): QB verification only.
    Tier 3 (HIGH):   Majority vote (k=3) first, then QB verification on the winner.

    Returns PostGenerationResult. The Controller uses this to decide whether to:
      - Proceed with the command (requires_revision=False)
      - Regenerate with a revised Intent Object (requires_revision=True, revised ref_id set)
      - Warn the user in the HITL prompt (low_confidence=True)

    This function is fail-open: if any hook fails, the pipeline continues.
    """
    final_command = command
    final_ref_id = ref_id
    low_confidence = False
    requires_revision = False
    verification_skipped = False
    vote_candidates: list = []

    # ── Tier 3: majority vote across k=3 parallel PB inferences ──────────────
    if tier == Tier.HIGH:
        try:
            from .voting import vote, Confidence
            vote_result = vote(intent_description)
            vote_candidates = vote_result.candidates
            if vote_result.command:
                final_command = vote_result.command
            low_confidence = vote_result.confidence == Confidence.LOW
        except Exception:
            # voting unavailable — continue with single-inference command
            pass

    # ── Tier 2+: QB verification ──────────────────────────────────────────────
    if tier >= Tier.MEDIUM:
        try:
            from .verifier import verify
            verdict = verify(final_ref_id, final_command)
            if not verdict.matches and verdict.revised_ref_id:
                final_ref_id = verdict.revised_ref_id
                requires_revision = True
        except Exception:
            verification_skipped = True

    return PostGenerationResult(
        command=final_command,
        ref_id=final_ref_id,
        requires_revision=requires_revision,
        low_confidence=low_confidence,
        verification_skipped=verification_skipped,
        vote_candidates=vote_candidates,
    )


# ── CLI for testing ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json, sys

    test_cases = [
        {"action": "system.status",    "target": "",                    "params": {}, "reason": "user_requested", "risk_level": "read_only"},
        {"action": "fs.read",          "target": "/etc/hosts",          "params": {}, "reason": "user_requested", "risk_level": "read_only"},
        {"action": "fs.write",         "target": f"{_USER_HOME}/notes", "params": {}, "reason": "user_requested", "risk_level": "low"},
        {"action": "fs.write",         "target": "/etc/nginx/nginx.conf","params": {}, "reason": "user_requested", "risk_level": "medium"},
        {"action": "fs.delete",        "target": "/var/log/app.log",    "params": {}, "reason": "user_requested", "risk_level": "high"},
        {"action": "service.restart",  "target": "nginx",               "params": {}, "reason": "user_requested", "risk_level": "medium"},
        {"action": "package.install",  "target": "curl",                "params": {}, "reason": "user_requested", "risk_level": "medium"},
        {"action": "fs.write",         "target": "/boot/grub/grub.cfg", "params": {}, "reason": "user_requested", "risk_level": "critical"},
        {"action": "network.firewall_flush", "target": "",              "params": {}, "reason": "user_requested", "risk_level": "critical"},
    ]

    print(f"{'Action':<30} {'Target':<35} {'Tier':<12} {'HITL':<6} {'Reason'}")
    print("-" * 120)
    for intent in test_cases:
        result = classify(intent)
        print(
            f"{intent['action']:<30} "
            f"{intent['target']:<35} "
            f"Tier {result.tier} {result.tier.name:<6} "
            f"{'YES' if result.requires_hitl else 'no':<6} "
            f"{result.reason}"
        )
