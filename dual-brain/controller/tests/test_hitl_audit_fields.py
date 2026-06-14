"""Tests for HITL audit field augmentation — extends G5.4.

Verifies:
  - Every decision writes decision_id (UUID)
  - Every decision writes key_pressed_class
  - Every decision writes latency_ms (monotonic-clock-based)
  - New Outcome enum values exist: TRUST_APPLIED, TRUST_GRANTED, MODIFY_REQUESTED
"""

from __future__ import annotations

from io import StringIO

import pytest

from controller.audit import Outcome
from controller.hitl import (
    Decision,
    HitlPresenter,
    HitlPrompt,
)
from controller.risk_classifier import ClassificationResult, Tier


def _intent(**overrides) -> dict:
    base = dict(
        intent_id="test-uuid",
        action="fs.delete",
        target="/etc/hosts",
        params={},
        reason="user_requested",
        risk_level="critical",
    )
    return {**base, **overrides}


def _cls(**overrides) -> ClassificationResult:
    base = dict(tier=Tier.HIGH, reason="critical path", reversible=False)
    return ClassificationResult(**{**base, **overrides})


class TestAuditFieldsOnPrompt:
    def test_decision_id_set_on_non_tty(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
        monkeypatch.setattr("sys.stdin", StringIO(""))

        prompt = HitlPrompt(_intent(), _cls())
        prompt.ask()

        assert prompt.decision_id
        assert len(prompt.decision_id) == 36
        assert "-" in prompt.decision_id

    def test_key_class_set_on_non_tty(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
        monkeypatch.setattr("sys.stdin", StringIO(""))

        prompt = HitlPrompt(_intent(), _cls())
        prompt.ask()

        assert prompt.key_pressed_class == "non_tty"

    def test_latency_set_on_non_tty(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
        monkeypatch.setattr("sys.stdin", StringIO(""))

        prompt = HitlPrompt(_intent(), _cls())
        prompt.ask()

        assert prompt.decision_latency_ms == 0.0

    def test_key_class_set_on_custom_presenter(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)

        class MockPresenter(HitlPresenter):
            def __init__(self):
                self._last_key_class = "numeric"

            def show_prompt(self, data):
                pass

            def lockout(self, seconds):
                pass

            def read_decision(self, timeout_seconds):
                return Decision.APPROVED

        prompt = HitlPrompt(_intent(), _cls(), presenter=MockPresenter())
        prompt.ask()

        assert prompt.key_pressed_class == "numeric"

    def test_latency_positive_for_decisions(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)

        class MockPresenter(HitlPresenter):
            def __init__(self):
                self._last_key_class = "mnemonic"

            def show_prompt(self, data):
                pass

            def lockout(self, seconds):
                pass

            def read_decision(self, timeout_seconds):
                return Decision.DENIED

        prompt = HitlPrompt(_intent(), _cls(), presenter=MockPresenter())
        prompt.ask()

        assert prompt.decision_latency_ms >= 0.0

    def test_decision_id_is_unique_per_prompt(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
        monkeypatch.setattr("sys.stdin", StringIO(""))

        ids = set()
        for _ in range(10):
            prompt = HitlPrompt(_intent(), _cls())
            ids.add(prompt.decision_id)

        assert len(ids) == 10


class TestNewOutcomes:
    def test_trust_applied_exists(self):
        assert Outcome.TRUST_APPLIED.value == "trust_applied"

    def test_trust_granted_exists(self):
        assert Outcome.TRUST_GRANTED.value == "trust_granted"

    def test_modify_requested_exists(self):
        assert Outcome.MODIFY_REQUESTED.value == "modify_requested"

    def test_modify_decision_maps_to_outcome(self):
        assert Decision.MODIFY.to_outcome() == Outcome.MODIFY_REQUESTED
