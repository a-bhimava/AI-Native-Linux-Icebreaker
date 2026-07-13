"""v6.8 M7.1 — verifier.tier_floor regression lock.

The skip logic lives in verifier.should_skip_verifier(). Both main.py call
sites (streaming and non-streaming) route through it, so testing the helper
covers both branches.

Contracts:
- Default tier_floor=2 skips Tier 0 and Tier 1; runs on Tier 2 and 3.
- retry_mode=skip_tier_01 (v6.7 backward compat) still skips tier<=1 even if
  tier_floor is set to 0.
- tier_floor of 0 with default retry_mode never skips.
- tier_floor of 4 always skips.

Cross-references:
- Contract: docs/IMPLEMENTATION_PLAN_v6.8_2026-07-13.md §4.3 M7.1
- Implementation: controller/verifier.py::should_skip_verifier
- Call sites: controller/main.py streaming (~L850) + non-streaming (~L1745)
"""

from __future__ import annotations

import pytest

from controller.verifier import VerifierConfig, should_skip_verifier


# ── Default behaviour: tier_floor=2 ────────────────────────────────────────

@pytest.mark.parametrize("tier", [0, 1])
def test_default_floor_skips_low_tier(tier):
    cfg = VerifierConfig()  # defaults: tier_floor=2, retry_mode=on_call_failed_only
    assert should_skip_verifier(cfg, tier) is True


@pytest.mark.parametrize("tier", [2, 3])
def test_default_floor_runs_high_tier(tier):
    cfg = VerifierConfig()
    assert should_skip_verifier(cfg, tier) is False


# ── Explicit tier_floor values ─────────────────────────────────────────────

def test_floor_zero_never_skips():
    cfg = VerifierConfig(tier_floor=0)
    for tier in (0, 1, 2, 3):
        assert should_skip_verifier(cfg, tier) is False


def test_floor_four_always_skips():
    cfg = VerifierConfig(tier_floor=4)
    for tier in (0, 1, 2, 3):
        assert should_skip_verifier(cfg, tier) is True


def test_floor_three_only_runs_tier_three_and_above():
    cfg = VerifierConfig(tier_floor=3)
    assert should_skip_verifier(cfg, 0) is True
    assert should_skip_verifier(cfg, 1) is True
    assert should_skip_verifier(cfg, 2) is True
    assert should_skip_verifier(cfg, 3) is False


# ── Backward compat: retry_mode=skip_tier_01 (v6.7 config) ─────────────────

def test_skip_tier_01_still_skips_low_tier_with_low_floor():
    """A v6.7 config that set retry_mode=skip_tier_01 must keep skipping
    Tier 0/1 intents even after v6.8 lands. tier_floor=0 alone would NOT
    skip; the retry_mode gate rescues the legacy semantic."""
    cfg = VerifierConfig(tier_floor=0, retry_mode="skip_tier_01")
    assert should_skip_verifier(cfg, 0) is True
    assert should_skip_verifier(cfg, 1) is True
    assert should_skip_verifier(cfg, 2) is False  # skip_tier_01 doesn't cover Tier 2+
    assert should_skip_verifier(cfg, 3) is False


def test_new_floor_supersedes_legacy_mode():
    """When both knobs are set, either can trigger skip — union semantics."""
    cfg = VerifierConfig(tier_floor=3, retry_mode="skip_tier_01")
    assert should_skip_verifier(cfg, 0) is True    # skip_tier_01
    assert should_skip_verifier(cfg, 1) is True    # skip_tier_01
    assert should_skip_verifier(cfg, 2) is True    # tier_floor=3
    assert should_skip_verifier(cfg, 3) is False


# ── VerifierConfig plumbing ────────────────────────────────────────────────

def test_verifier_config_default_tier_floor_is_2():
    cfg = VerifierConfig()
    assert cfg.tier_floor == 2


def test_verifier_config_tier_floor_field_is_settable():
    cfg = VerifierConfig(tier_floor=0)
    assert cfg.tier_floor == 0
