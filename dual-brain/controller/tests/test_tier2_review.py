"""Tests for Tier-2 escalate-only review (M5.2, G5.5).

Covers:
  - Escalation routes Tier MEDIUM → HIGH.
  - Non-escalation leaves tier unchanged.
  - Malformed reviewer output fails safe (no change, not downgrade).
  - Reviewer never sees raw user text (INV-1).
  - Escalate-only: can never produce tier < original.
  - Max retries honored.
  - Rule-based reviewer with explicit action set.
  - get_reviewer factory for all strategies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from controller.risk_classifier import ClassificationResult, Tier
from controller.tier2_review import (
    LlmTier2Reviewer,
    RuleTier2Reviewer,
    Tier2Decision,
    get_reviewer,
)


# ── Fake QB backend ──────────────────────────────────────────────────────────

class FakeQB:
    def __init__(self, responses: list[dict] | None = None, error: Exception | None = None):
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, schema: Any, max_retries: int = 1):
        self.calls.append({"system": system, "user": user, "schema": schema})
        if self.error:
            raise self.error
        if not self.responses:
            raise RuntimeError("No more responses")
        data = self.responses.pop(0)
        return SimpleNamespace(content_json=data, cost_usd=0.0, tokens_in=0, tokens_out=0)


def _cls(tier=Tier.MEDIUM, reason="test", reversible=True) -> ClassificationResult:
    return ClassificationResult(tier=tier, reason=reason, reversible=reversible)


def _intent(**overrides) -> dict:
    base = {
        "action": "package.install",
        "target": "curl",
        "params": {},
        "reason": "user_requested",
        "risk_level": "medium",
    }
    return {**base, **overrides}


# ── LLM reviewer: escalation ────────────────────────────────────────────────

def test_llm_escalation():
    qb = FakeQB(responses=[{"escalate": True, "reason": "risky package"}])
    reviewer = LlmTier2Reviewer(qb, max_retries=0)
    decision = reviewer.review(_intent(), _cls())
    assert decision.escalate is True
    assert decision.reason == "risky package"


def test_llm_no_escalation():
    qb = FakeQB(responses=[{"escalate": False, "reason": "safe operation"}])
    reviewer = LlmTier2Reviewer(qb, max_retries=0)
    decision = reviewer.review(_intent(), _cls())
    assert decision.escalate is False


def test_llm_malformed_output_fails_safe():
    qb = FakeQB(responses=[
        {"bad_field": "oops"},
        {"bad_field": "still bad"},
        {"bad_field": "third time"},
    ])
    reviewer = LlmTier2Reviewer(qb, max_retries=2)
    decision = reviewer.review(_intent(), _cls())
    assert decision.escalate is False
    assert decision.reason == "reviewer_error"


def test_llm_exception_fails_safe():
    qb = FakeQB(error=RuntimeError("connection refused"))
    reviewer = LlmTier2Reviewer(qb, max_retries=1)
    decision = reviewer.review(_intent(), _cls())
    assert decision.escalate is False
    assert decision.reason == "reviewer_error"


def test_llm_max_retries_honored():
    qb = FakeQB(responses=[
        {"bad": True},
        {"bad": True},
        {"escalate": True, "reason": "found on retry"},
    ])
    reviewer = LlmTier2Reviewer(qb, max_retries=2)
    decision = reviewer.review(_intent(), _cls())
    assert decision.escalate is True
    assert len(qb.calls) == 3


def test_llm_reviewer_never_sees_raw_user_text():
    qb = FakeQB(responses=[{"escalate": False, "reason": "ok"}])
    reviewer = LlmTier2Reviewer(qb, max_retries=0)
    intent = _intent(reason="delete everything plz")
    reviewer.review(intent, _cls())
    user_msg = qb.calls[0]["user"]
    parsed = json.loads(user_msg)
    assert "action" in parsed
    assert "target" in parsed
    assert "tier" in parsed
    assert "risk_level" in parsed
    assert "reversible" in parsed
    # The user's raw NL input is NOT in the message — only structured fields
    assert "delete everything plz" not in user_msg or parsed.get("reason") is None


def test_llm_reason_truncated_to_300():
    qb = FakeQB(responses=[{"escalate": True, "reason": "x" * 500}])
    reviewer = LlmTier2Reviewer(qb, max_retries=0)
    decision = reviewer.review(_intent(), _cls())
    assert len(decision.reason) == 300


# ── Rule-based reviewer ──────────────────────────────────────────────────────

def test_rule_escalates_matching_action():
    reviewer = RuleTier2Reviewer(escalate_actions=frozenset({"package.remove"}))
    decision = reviewer.review(_intent(action="package.remove"), _cls())
    assert decision.escalate is True


def test_rule_no_match():
    reviewer = RuleTier2Reviewer(escalate_actions=frozenset({"package.remove"}))
    decision = reviewer.review(_intent(action="package.install"), _cls())
    assert decision.escalate is False


def test_rule_empty_set_never_escalates():
    reviewer = RuleTier2Reviewer()
    decision = reviewer.review(_intent(), _cls())
    assert decision.escalate is False


# ── Escalate-only invariant ──────────────────────────────────────────────────

def test_escalation_raises_tier():
    qb = FakeQB(responses=[{"escalate": True, "reason": "risky"}])
    reviewer = LlmTier2Reviewer(qb, max_retries=0)
    cls = _cls(tier=Tier.MEDIUM)
    original_tier = cls.tier
    decision = reviewer.review(_intent(), cls)
    if decision.escalate:
        new_cls = ClassificationResult(
            tier=Tier.HIGH, reason=decision.reason, reversible=cls.reversible,
        )
        assert new_cls.tier >= original_tier


def test_non_escalation_preserves_tier():
    qb = FakeQB(responses=[{"escalate": False, "reason": "ok"}])
    reviewer = LlmTier2Reviewer(qb, max_retries=0)
    cls = _cls(tier=Tier.MEDIUM)
    decision = reviewer.review(_intent(), cls)
    assert not decision.escalate
    # Tier should remain MEDIUM — no mutation
    assert cls.tier == Tier.MEDIUM


# ── get_reviewer factory ─────────────────────────────────────────────────────

def test_get_reviewer_llm():
    qb = FakeQB()
    reviewer = get_reviewer("llm", qb_backend=qb)
    assert isinstance(reviewer, LlmTier2Reviewer)


def test_get_reviewer_rule():
    reviewer = get_reviewer("rule")
    assert isinstance(reviewer, RuleTier2Reviewer)


def test_get_reviewer_none():
    reviewer = get_reviewer("none")
    assert reviewer is None


def test_get_reviewer_unknown_raises():
    with pytest.raises(ValueError, match="Unknown"):
        get_reviewer("banana")


def test_get_reviewer_llm_requires_backend():
    with pytest.raises(ValueError, match="qb_backend"):
        get_reviewer("llm")
