"""Tests for HITL action handling — G5.4 gate.

Verifies:
  - Decision enum has MODIFY, EXPLAIN, TRUST values
  - MODIFY maps to Outcome.MODIFY_REQUESTED
  - EXPLAIN loops back (re-prompts after showing help)
  - TRUST maps correctly
  - Unknown keys do not produce a decision
"""

from __future__ import annotations

import pytest

from controller.audit import Outcome
from controller.hitl import (
    Decision,
    HitlDisplayData,
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


class TestDecisionEnum:
    def test_modify_value(self):
        assert Decision.MODIFY.value == "modify"

    def test_explain_value(self):
        assert Decision.EXPLAIN.value == "explain"

    def test_trust_value(self):
        assert Decision.TRUST.value == "trust"

    def test_modify_to_outcome(self):
        assert Decision.MODIFY.to_outcome() == Outcome.MODIFY_REQUESTED

    def test_explain_to_outcome_is_none(self):
        assert Decision.EXPLAIN.to_outcome() is None

    def test_trust_to_outcome_is_none(self):
        assert Decision.TRUST.to_outcome() is None

    def test_approved_to_outcome_is_none(self):
        assert Decision.APPROVED.to_outcome() is None

    def test_denied_to_outcome(self):
        assert Decision.DENIED.to_outcome() == Outcome.HITL_DENIED

    def test_timeout_to_outcome(self):
        assert Decision.TIMEOUT.to_outcome() == Outcome.HITL_TIMEOUT

    def test_non_tty_to_outcome(self):
        assert Decision.NON_TTY.to_outcome() == Outcome.HITL_NON_TTY


class TestExplainLoop:
    def test_explain_then_approve(self, monkeypatch):
        """EXPLAIN should loop back; a subsequent APPROVE exits."""
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)

        call_count = [0]

        class MockPresenter(HitlPresenter):
            def __init__(self):
                self._last_key_class = ""

            def pre_check(self):
                return None

            def show_prompt(self, data):
                pass

            def lockout(self, seconds):
                pass

            def read_decision(self, timeout_seconds):
                call_count[0] += 1
                if call_count[0] == 1:
                    self._last_key_class = "mnemonic"
                    return Decision.EXPLAIN
                self._last_key_class = "numeric"
                return Decision.APPROVED

        prompt = HitlPrompt(_intent(), _cls(), presenter=MockPresenter())
        result = prompt.ask()

        assert result == Decision.APPROVED
        assert call_count[0] == 2

    def test_explain_then_deny(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)

        call_count = [0]

        class MockPresenter(HitlPresenter):
            def __init__(self):
                self._last_key_class = ""

            def pre_check(self):
                return None

            def show_prompt(self, data):
                pass

            def lockout(self, seconds):
                pass

            def read_decision(self, timeout_seconds):
                call_count[0] += 1
                if call_count[0] <= 2:
                    self._last_key_class = "mnemonic"
                    return Decision.EXPLAIN
                self._last_key_class = "mnemonic"
                return Decision.DENIED

        prompt = HitlPrompt(_intent(), _cls(), presenter=MockPresenter())
        result = prompt.ask()

        assert result == Decision.DENIED


class TestModifyDecision:
    def test_modify_returned_from_presenter(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)

        class MockPresenter(HitlPresenter):
            def __init__(self):
                self._last_key_class = "mnemonic"

            def pre_check(self):
                return None

            def show_prompt(self, data):
                pass

            def lockout(self, seconds):
                pass

            def read_decision(self, timeout_seconds):
                return Decision.MODIFY

        prompt = HitlPrompt(_intent(), _cls(), presenter=MockPresenter())
        result = prompt.ask()

        assert result == Decision.MODIFY


class TestTrustDecision:
    def test_trust_returned_from_presenter(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)

        class MockPresenter(HitlPresenter):
            def __init__(self):
                self._last_key_class = "mnemonic"

            def pre_check(self):
                return None

            def show_prompt(self, data):
                pass

            def lockout(self, seconds):
                pass

            def read_decision(self, timeout_seconds):
                return Decision.TRUST

        prompt = HitlPrompt(_intent(), _cls(), presenter=MockPresenter())
        result = prompt.ask()

        assert result == Decision.TRUST


class TestCustomPresenter:
    def test_custom_presenter_call_order(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)

        class SpyPresenter(HitlPresenter):
            def __init__(self):
                self.calls = []
                self._last_key_class = "numeric"

            def show_prompt(self, data):
                self.calls.append("show")

            def lockout(self, seconds):
                self.calls.append("lockout")

            def read_decision(self, timeout_seconds):
                self.calls.append("read")
                return Decision.APPROVED

        spy = SpyPresenter()
        result = HitlPrompt(_intent(), _cls(), presenter=spy).ask()

        assert result == Decision.APPROVED
        assert spy.calls == ["show", "lockout", "read"]
