"""Tests for the Tier 0–3 risk classifier.

The classifier is the gate that decides which intents auto-execute, which
get notified, and which require HITL. Drift between this logic and mcpd's
catalogue is dangerous; the test_mcpd_catalogue suite covers that surface.
This suite focuses on the tier-decision rules themselves.
"""

import os

import pytest

from controller._mcpd_tools import ALL_TOOLS
from controller.risk_classifier import ClassificationResult, Tier, classify


HOME = os.path.expanduser("~")


def _make_intent(action: str, target: str = "", risk_level: str = "low") -> dict:
    return {
        "action": action,
        "target": target,
        "params": {},
        "reason": "test",
        "risk_level": risk_level,
    }


# ── Tier 0: read-only ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", [
    "system.status", "system.uptime", "system.cpu",
    "system.memory", "system.disk",
    "process.list", "process.inspect",
    "fs.read", "fs.list", "fs.stat",
    "service.logs",
    "network.status", "network.dns.read",
    "package.query",
])
def test_tier0_read_only(action):
    r = classify(_make_intent(action))
    assert r.tier == Tier.READ_ONLY
    assert r.auto_execute
    assert not r.requires_hitl
    assert r.reversible


# ── Tier 1: fs.write inside $HOME ────────────────────────────────────────────

def test_fs_write_in_home_is_tier1():
    r = classify(_make_intent("fs.write", f"{HOME}/notes.md"))
    assert r.tier == Tier.LOW
    assert r.auto_execute
    assert not r.requires_hitl


def test_fs_write_deep_in_home_is_tier1():
    r = classify(_make_intent("fs.write", f"{HOME}/projects/icebreaker/scratch"))
    assert r.tier == Tier.LOW


# ── Tier 2: system writes ────────────────────────────────────────────────────

@pytest.mark.parametrize("action,target", [
    ("package.install", "curl"),
    ("package.remove",  "vim"),
    ("package.upgrade", "openssl"),
    ("service.start",   "nginx.service"),
    ("service.stop",    "nginx.service"),
    ("service.restart", "nginx.service"),
])
def test_tier2_system_write(action, target):
    r = classify(_make_intent(action, target, risk_level="medium"))
    assert r.tier == Tier.MEDIUM
    assert r.auto_execute
    assert not r.requires_hitl


def test_package_actions_are_reversible():
    r = classify(_make_intent("package.install", "htop"))
    assert r.reversible is True


def test_service_actions_marked_irreversible():
    r = classify(_make_intent("service.restart", "nginx"))
    assert r.reversible is False


# ── Tier 3: destructive tools ────────────────────────────────────────────────

def test_fs_delete_is_tier3():
    r = classify(_make_intent("fs.delete", "/var/log/app.log"))
    assert r.tier == Tier.HIGH
    assert r.requires_hitl
    assert not r.auto_execute


# ── Tier 3: fs.write outside $HOME ───────────────────────────────────────────

def test_fs_write_outside_home_is_tier3():
    r = classify(_make_intent("fs.write", "/etc/nginx/nginx.conf"))
    assert r.tier == Tier.HIGH
    assert r.requires_hitl


# ── Tier 3: critical path targets ────────────────────────────────────────────

@pytest.mark.parametrize("target", [
    "/boot/grub/grub.cfg",
    "/etc/sudoers",
    "/etc/shadow",
    "/etc/passwd",
    "/root/.ssh/authorized_keys",
    "/proc/sys/kernel/random/boot_id",
    "/dev/sda",
    "/dev/nvme0n1",
    "/sys/firmware/efi/efivars/anything",
])
def test_critical_paths_force_tier3(target):
    # Even with a Tier 0 read action, a critical-path target escalates to HIGH.
    r = classify(_make_intent("fs.read", target))
    assert r.tier == Tier.HIGH
    assert r.requires_hitl
    assert r.blocked_pattern is not None


# ── Tier 3: explicit risk_level=="critical" ──────────────────────────────────

def test_explicit_critical_risk_level_forces_tier3():
    r = classify(_make_intent("fs.write", f"{HOME}/notes", risk_level="critical"))
    assert r.tier == Tier.HIGH
    assert r.requires_hitl


# ── Coverage: no tool falls into the "Unclassified" fallback ─────────────────

def test_every_mcpd_tool_classifies_without_fallback():
    fallback_msg = "Unclassified action"
    unclassified = []
    for action in sorted(ALL_TOOLS):
        # For fs.write, supply an in-home target so it lands in Tier 1
        # (the alternate Tier 3 outside-home path is exercised above).
        target = f"{HOME}/scratch" if action == "fs.write" else ""
        r = classify(_make_intent(action, target))
        if fallback_msg in r.reason:
            unclassified.append(action)
    assert not unclassified, f"Tools landed in 'Unclassified' fallback: {unclassified}"


# ── Result invariants ────────────────────────────────────────────────────────

def test_classification_result_invariants():
    # requires_hitl exactly when tier == HIGH; auto_execute exactly otherwise.
    for action in ALL_TOOLS:
        target = f"{HOME}/x" if action == "fs.write" else ""
        r = classify(_make_intent(action, target))
        assert isinstance(r, ClassificationResult)
        assert r.requires_hitl == (r.tier == Tier.HIGH)
        assert r.auto_execute != r.requires_hitl
