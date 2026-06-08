"""Tests for ``backends._api_common._ApiBackend``.

Covers tests 13-17 from the M2.6/M2.7 plan: API-key requirement,
cost-rate formula, SDK-exception sanitization at the boundary, and
``SecretRef`` containment.
"""

from __future__ import annotations

import pytest

from controller.backends import (
    BrainConfigError,
    BrainProviderError,
    SecretRef,
)
from controller.backends._api_common import _ApiBackend


# ── Helpers ──────────────────────────────────────────────────────────────────


class _NoApiKeyConfig:
    """Config-like object with no api_key attribute set."""

    def __init__(self, model: str = "x"):
        self.model = model
        self.api_key = None


class _WithApiKeyConfig:
    """Config-like object carrying a SecretRef."""

    def __init__(self, env_var: str = "ICEBREAKER_TEST_API_KEY"):
        self.model = "mock-model"
        self.api_key = SecretRef(env_var)


class _MockApi(_ApiBackend):
    """Minimal concrete subclass for testing the mixin."""

    __slots__ = ()
    backend_name = "mock-api"
    INPUT_COST_PER_1M_USD = 2.0
    OUTPUT_COST_PER_1M_USD = 10.0

    def _call_provider(self, envelope):
        self._auditor.intercept({"messages": [{"role": "user", "content": "x"}]})
        return ('{"intent_id": "x"}', 1, 1)


# ── 13: api_key required ─────────────────────────────────────────────────────


def test_api_backend_requires_api_key():
    with pytest.raises(BrainConfigError, match="requires api_key"):
        _MockApi(_NoApiKeyConfig())


def test_api_backend_accepts_secret_ref():
    backend = _MockApi(_WithApiKeyConfig())
    assert backend is not None


# ── 14: cost estimate ────────────────────────────────────────────────────────


def test_cost_estimate_uses_subclass_rates():
    backend = _MockApi(_WithApiKeyConfig())
    # 1_000_000 input × $2/1M + 500_000 output × $10/1M = $2 + $5 = $7
    assert backend._estimate_cost(1_000_000, 500_000) == pytest.approx(7.0)


def test_cost_estimate_handles_zero_tokens():
    backend = _MockApi(_WithApiKeyConfig())
    assert backend._estimate_cost(0, 0) == 0.0


# ── 15: SDK exception sanitization ──────────────────────────────────────────


_FINGERPRINT = "sk-ant-DUMMY-fingerprint-XYZ-1234567890ABCDEFGHIJ"


def test_wrap_sdk_call_sanitizes_provider_exception():
    backend = _MockApi(_WithApiKeyConfig())

    def fake_sdk():
        raise RuntimeError(f"401 Unauthorized: Bearer {_FINGERPRINT}")

    with pytest.raises(BrainProviderError) as exc_info:
        backend._wrap_sdk_call(fake_sdk)
    msg = str(exc_info.value)
    assert _FINGERPRINT not in msg
    assert "[REDACTED]" in msg


def test_wrap_sdk_call_passes_through_args_and_kwargs():
    backend = _MockApi(_WithApiKeyConfig())

    def echo(*args, **kwargs):
        return (args, kwargs)

    result = backend._wrap_sdk_call(echo, 1, 2, x=3)
    assert result == ((1, 2), {"x": 3})


# ── 16: from None semantics ──────────────────────────────────────────────────


def test_wrap_sdk_call_strips_chained_context():
    """``from None`` strips ``__context__`` so the original (key-bearing)
    exception is not re-exposed via Python's chained-traceback machinery."""
    backend = _MockApi(_WithApiKeyConfig())

    class _FakeSDKError(RuntimeError):
        pass

    def fake_sdk():
        raise _FakeSDKError(f"Bearer {_FINGERPRINT}")

    try:
        backend._wrap_sdk_call(fake_sdk)
    except BrainProviderError as outer:
        # __context__ is the original SDK exception object. Even though
        # Python keeps __context__ set internally, `from None` sets
        # __suppress_context__ which prevents it appearing in the
        # rendered traceback.
        assert outer.__suppress_context__ is True
        # Cause must be None — we did `from None`.
        assert outer.__cause__ is None


# ── 17: SecretRef containment ────────────────────────────────────────────────


def test_api_backend_api_key_ref_is_secret_ref_not_str():
    backend = _MockApi(_WithApiKeyConfig())
    assert isinstance(backend._api_key_ref, SecretRef)
    assert not isinstance(backend._api_key_ref, str)


def test_api_backend_api_key_value_not_in_repr(monkeypatch):
    """The raw key value must not leak into any default str/repr of the
    backend, even when the env var is set."""
    monkeypatch.setenv("ICEBREAKER_TEST_API_KEY", _FINGERPRINT)
    backend = _MockApi(_WithApiKeyConfig())
    rendered = repr(backend._api_key_ref)
    assert _FINGERPRINT not in rendered
    assert "********" in rendered
