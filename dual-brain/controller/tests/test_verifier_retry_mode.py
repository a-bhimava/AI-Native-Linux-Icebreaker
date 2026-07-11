"""F-49 (2026-07-10): configurable verifier retry mode.

The v6.61 UTM sweep showed legitimate write intents rejected as
"verifier call failed". The right retry strategy is workload-dependent,
so v6.63 exposes it as a runtime knob: `verifier.retry_mode` in
`controller.toml`.

These tests cover the ``_should_retry_verifier`` dispatch logic —
which is small and pure. The main.py integration site (skip_tier_01
short-circuit + CoT event emission) is exercised in the corpus tests.

Mode semantics:
  off                 — never retry
  on_call_failed_only — retry only if reason contains "verifier call failed"
                        (F-44 default behaviour)
  on_any_rejection    — retry once on any verified=false
  skip_tier_01        — retry like on_call_failed_only when Tier 2/3
                        (Tier 0/1 skip is at the caller, not here)
"""

from __future__ import annotations

import pytest

from controller.main import _should_retry_verifier
from controller.verifier import VerifierResult


def _rejected(reason: str) -> VerifierResult:
    return VerifierResult(verified=False, reason=reason)


# ── off ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", [
    "verifier call failed",
    "tool does not match action",
    "target scope broadened",
    "",
    None,  # covers the (reason or "") fallback
])
def test_off_never_retries(reason):
    v = _rejected("" if reason is None else reason)
    assert _should_retry_verifier("off", v) is False


# ── on_call_failed_only (default) ──────────────────────────────────────────

def test_on_call_failed_only_retries_on_call_failed_substring():
    v = _rejected("verifier call failed")
    assert _should_retry_verifier("on_call_failed_only", v) is True


def test_on_call_failed_only_retries_case_insensitive():
    v = _rejected("VERIFIER CALL FAILED (timeout)")
    assert _should_retry_verifier("on_call_failed_only", v) is True


def test_on_call_failed_only_does_not_retry_on_semantic_rejection():
    v = _rejected("params.content differs from intent.content")
    assert _should_retry_verifier("on_call_failed_only", v) is False


def test_on_call_failed_only_treats_empty_reason_as_semantic():
    """Empty reason is not a 'call failed' — respect the rejection."""
    v = _rejected("")
    assert _should_retry_verifier("on_call_failed_only", v) is False


# ── on_any_rejection ───────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", [
    "verifier call failed",
    "tool does not match action",
    "target scope broadened",
    "",
    "anything really",
])
def test_on_any_rejection_always_retries(reason):
    v = _rejected(reason)
    assert _should_retry_verifier("on_any_rejection", v) is True


# ── skip_tier_01 ───────────────────────────────────────────────────────────

def test_skip_tier_01_falls_back_to_call_failed_semantics():
    """Skip is enforced at the caller (main.py checks tier <= 1 before
    invoking the verifier at all). If the verifier IS invoked (Tier 2/3),
    retry should behave conservatively like on_call_failed_only."""
    v_call_fail = _rejected("verifier call failed")
    v_semantic = _rejected("tool does not match action")
    assert _should_retry_verifier("skip_tier_01", v_call_fail) is True
    assert _should_retry_verifier("skip_tier_01", v_semantic) is False


# ── unknown mode defaults to conservative ──────────────────────────────────

def test_unknown_mode_defaults_to_call_failed_only_behaviour():
    """Guard against typo'd config values: fall back to the default rather
    than crashing or retrying blindly."""
    v_call_fail = _rejected("verifier call failed")
    v_semantic = _rejected("tool does not match action")
    assert _should_retry_verifier("banana", v_call_fail) is True
    assert _should_retry_verifier("banana", v_semantic) is False


# ── VerifierConfig plumbing ────────────────────────────────────────────────

def test_verifier_config_default_is_on_call_failed_only():
    from controller.verifier import VerifierConfig
    cfg = VerifierConfig()
    assert cfg.retry_mode == "on_call_failed_only"


def test_config_parser_reads_retry_mode_from_toml_section():
    from controller.config import _build_verifier_config
    cfg = _build_verifier_config({"verifier": {"retry_mode": "on_any_rejection"}})
    assert cfg.retry_mode == "on_any_rejection"


def test_config_parser_falls_back_to_default_when_missing():
    from controller.config import _build_verifier_config
    cfg = _build_verifier_config({})
    assert cfg.retry_mode == "on_call_failed_only"
