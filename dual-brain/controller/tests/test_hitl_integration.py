"""Integration tests for hitl — crosses module boundaries.

Verifies that hitl.py glues correctly with:
  - risk_classifier: classify() → HitlPrompt constructible
  - audit: Decision.to_outcome() → AuditFields → AuditLog
  - intent_schema: validate() → classify() → _render() pipeline
  - config: HitlConfig.lockout_seconds flows through to presenter
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from controller._mcpd_tools import DESTRUCTIVE_TOOLS
from controller.audit import AuditFields, AuditLog, Outcome
from controller.config import HitlConfig
from controller.hitl import (
    Decision,
    HitlDisplayData,
    HitlPresenter,
    HitlPrompt,
    _ANSI_ESCAPE,
)
from controller.intent_schema import validate
from controller.risk_classifier import ClassificationResult, Tier, classify


# ── Helpers ──────────────────────────────────────────────────────────────────


def _intent(action: str = "fs.delete", target: str = "/etc/hosts", **overrides) -> dict:
    base = dict(
        intent_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        action=action,
        target=target,
        params={},
        reason="user_requested",
        risk_level="critical",
    )
    return {**base, **overrides}


def _audit_fields(outcome: Outcome) -> AuditFields:
    return AuditFields(
        session_id="s1",
        turn_index=0,
        intent_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        action="fs.delete",
        target="/etc/hosts",
        tier=3,
        reason="integration_test",
        risk_level="critical",
        outcome=outcome,
        duration_ms=0.0,
        backend="local",
        model="test-model",
        tokens_in=0,
        tokens_out=0,
        cost_estimate_usd=0.0,
    )


# ── Test 1: all DESTRUCTIVE_TOOLS require HITL ───────────────────────────────


def test_all_destructive_tools_require_hitl():
    """Every action in DESTRUCTIVE_TOOLS → classify() → requires_hitl=True → HitlPrompt constructible."""
    for tool in DESTRUCTIVE_TOOLS:
        intent = _intent(action=tool)
        result = classify(intent)
        assert result.requires_hitl, f"{tool} must require HITL"
        HitlPrompt(intent, result, backend="local")

    # Critical-path targets also gate HITL regardless of action
    for path in ["/etc/passwd", "/boot/vmlinuz", "/root/.bashrc", "/etc/shadow"]:
        intent = _intent(target=path)
        result = classify(intent)
        assert result.requires_hitl, f"critical path {path!r} must require HITL"


# ── Test 2: Decision.to_outcome() completeness ───────────────────────────────


def test_decision_to_outcome_covers_audit_fields():
    """DENIED/TIMEOUT/NON_TTY each produce a valid Outcome; APPROVED → None."""
    for decision, expected in [
        (Decision.DENIED,   Outcome.HITL_DENIED),
        (Decision.TIMEOUT,  Outcome.HITL_TIMEOUT),
        (Decision.NON_TTY,  Outcome.HITL_NON_TTY),
    ]:
        outcome = decision.to_outcome()
        assert outcome == expected, f"{decision} should map to {expected}"
        # Confirm AuditFields accepts this Outcome without raising
        _audit_fields(outcome)

    assert Decision.APPROVED.to_outcome() is None, "APPROVED should map to None"


# ── Test 3: intent_schema → classify → _render pipeline ──────────────────────


def test_intent_schema_to_classify_to_render():
    """Full pipeline: schema validate → classify → render completes without error."""
    raw = _intent()
    validated = validate(raw)
    cls_result = classify(validated.intent)

    prompt = HitlPrompt(validated.intent, cls_result, backend="test")
    rendered = prompt._render()

    assert validated.intent["action"] in rendered
    assert validated.intent["target"] in rendered


# ── Test 4: ANSI escapes stripped from cow_summary ───────────────────────────


def test_cow_summary_ansi_stripped_in_render():
    """Terminal injection via mcpd output is neutralised before display."""
    malicious = "safe text\x1b[31mRED\x1b[0m\x1b[1;32mGREEN\x1b[0m"
    cls_result = ClassificationResult(tier=Tier.HIGH, reason="test", reversible=False)
    prompt = HitlPrompt(_intent(), cls_result, cow_summary=malicious)
    rendered = prompt._render()

    # The plain text survives; the escape sequences do not
    assert "safe text" in rendered
    assert "RED" in rendered
    assert "GREEN" in rendered
    assert _ANSI_ESCAPE.search(rendered.split("safe text")[1]) is None, (
        "no ANSI escapes should appear after cow_summary injection"
    )


# ── Test 5: NON_TTY decision written to audit log ────────────────────────────


def test_non_tty_decision_written_to_audit_log(tmp_path: Path):
    """Decision.NON_TTY → to_outcome() → AuditLog → parsed JSONL is correct."""
    log_path = tmp_path / "audit.jsonl"
    outcome = Decision.NON_TTY.to_outcome()
    assert outcome == Outcome.HITL_NON_TTY

    with AuditLog(path=log_path, fsync_each_write=False) as log:
        log.write_fields(_audit_fields(outcome))

    entry = json.loads(log_path.read_text().strip())
    assert entry["outcome"] == "hitl_non_tty"
    assert entry["action"] == "fs.delete"


# ── Test 6: HitlConfig.lockout_seconds flows to presenter ────────────────────


def test_lockout_seconds_from_hitl_config():
    """HitlConfig.lockout_seconds passed to HitlPrompt reaches lockout()."""
    lockout_calls: list[int] = []

    class SpyPresenter(HitlPresenter):
        def show_prompt(self, data: HitlDisplayData) -> None:
            pass

        def lockout(self, seconds: int) -> None:
            lockout_calls.append(seconds)

        def read_decision(self, timeout_seconds: int) -> Decision:
            return Decision.DENIED

    cfg = HitlConfig(lockout_seconds=7, timeout_seconds=30)
    spy = SpyPresenter()
    cls_result = ClassificationResult(tier=Tier.HIGH, reason="test", reversible=False)
    HitlPrompt(
        _intent(),
        cls_result,
        lockout_seconds=cfg.lockout_seconds,
        timeout_seconds=cfg.timeout_seconds,
        presenter=spy,
    ).ask()

    assert lockout_calls == [7], (
        f"lockout() should be called with 7 from HitlConfig, got {lockout_calls}"
    )
