"""Tests for the pluggable classifier registry (M5.2b).

Covers:
  - "rules" strategy resolves to the existing classify() function.
  - Custom strategy registered and callable.
  - Unknown strategy raises KeyError.
  - Registration is idempotent.
"""

from __future__ import annotations

import pytest

from controller.risk_classifier import (
    ClassificationResult,
    Tier,
    classify,
    get_classifier,
    register_classifier,
)


def test_rules_strategy_resolves():
    fn = get_classifier("rules")
    assert fn is classify


def test_rules_strategy_returns_valid_result():
    fn = get_classifier("rules")
    result = fn({"action": "system.status", "target": "", "params": {}, "reason": "test", "risk_level": "read_only"})
    assert isinstance(result, ClassificationResult)
    assert result.tier == Tier.READ_ONLY


def test_custom_strategy_registered():
    def my_classifier(intent: dict) -> ClassificationResult:
        return ClassificationResult(tier=Tier.HIGH, reason="always high", reversible=False)

    register_classifier("custom_test", my_classifier)
    fn = get_classifier("custom_test")
    assert fn is my_classifier
    result = fn({"action": "fs.read", "target": "/etc/hosts"})
    assert result.tier == Tier.HIGH


def test_unknown_strategy_raises():
    with pytest.raises(KeyError, match="Unknown classifier"):
        get_classifier("nonexistent_strategy")


def test_registration_is_idempotent():
    def my_fn(intent: dict) -> ClassificationResult:
        return ClassificationResult(tier=Tier.LOW, reason="low", reversible=True)

    register_classifier("idempotent_test", my_fn)
    register_classifier("idempotent_test", my_fn)
    fn = get_classifier("idempotent_test")
    assert fn is my_fn


def test_overwrite_replaces_previous():
    def fn_a(intent: dict) -> ClassificationResult:
        return ClassificationResult(tier=Tier.LOW, reason="a", reversible=True)

    def fn_b(intent: dict) -> ClassificationResult:
        return ClassificationResult(tier=Tier.HIGH, reason="b", reversible=False)

    register_classifier("overwrite_test", fn_a)
    register_classifier("overwrite_test", fn_b)
    assert get_classifier("overwrite_test") is fn_b
