"""Tests for ``backends.base`` — the BrainBackend contract.

Covers:
  - G3 sentinel constructor guard (tests 1-2, D14).
  - First-try happy path + retry semantics (3-6, D2/D4/D5).
  - BrainProviderError no-retry contract (7, D13).
  - max_retries hard cap (8, D4).
  - Exception excerpt bound under fuzz (9).
  - Draft-07 + UUID FormatChecker drift guard against intent_schema.py
    (10, D3 + reuse of the existing validator config).
  - Validation against the real intent.json corpus (11).
  - __init_subclass__ override-blocking (12, D3).
  - Bounded retry context — no prior raw output in the prompt (13, D15).
  - Sampling decay across attempts (14, D16).
  - __slots__ attribute-injection lockdown (15, D20).
  - Strict balanced-brace JSON extraction, no regex fallback (16, D19).
  - Envelope tools_disabled is always True (16a, D17 stage 1).
  - Auditor call-count probe catches subclasses that skip intercept
    (16b, D17 stage 2).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from controller.backends import (
    MAX_RETRY_HARD_CAP,
    BrainBackend,
    BrainConfigError,
    BrainProviderError,
    BrainResponse,
    BrainSchemaError,
    BrainSecurityError,
    BrainTruncationError,
    OutboundPayloadAuditor,
    RequestEnvelope,
    register_backend,
    registered_backends,
)
from controller.backends.registry import _reset_registry_for_tests


# ── Helpers ──────────────────────────────────────────────────────────────────


class _Cfg:
    """Minimal config stand-in for tests."""
    def __init__(self, model: str = "mock-model"):
        self.model = model


def _valid_intent_payload() -> dict:
    return {
        "intent_id": str(uuid.uuid4()),
        "action": "system.status",
        "target": "",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }


def _intent_schema() -> dict:
    p = Path(__file__).parent.parent / "schemas" / "intent.json"
    with p.open("r", encoding="utf-8") as h:
        return json.load(h)


class _MockBackend(BrainBackend):
    """Configurable mock. ``responses`` is consumed in order."""

    __slots__ = ("_responses", "_raise_provider", "_skip_auditor", "_observed")
    backend_name = "mock"

    def __init__(self, config, responses, *, raise_provider=False,
                 skip_auditor=False, observed_envelopes=None):
        super().__init__(config)
        self._responses = list(responses)
        self._raise_provider = raise_provider
        self._skip_auditor = skip_auditor
        self._observed = observed_envelopes if observed_envelopes is not None else []

    def _call_provider(self, envelope):
        self._observed.append(envelope)
        if self._raise_provider:
            raise BrainProviderError("simulated 500 from provider")
        raw = self._responses.pop(0)
        if not self._skip_auditor:
            self._auditor.intercept({"messages": [{"role": "user", "content": envelope.user}]})
        return raw, len(envelope.user), len(raw)


# ── 1-2: G3 constructor sentinel guard ───────────────────────────────────────


def test_g3_guard_rejects_mcpd_client_object():
    class T(BrainBackend):
        __slots__ = ()
        backend_name = "t"
        def _call_provider(self, envelope): return ("{}", 0, 0)
    with pytest.raises(BrainSecurityError, match="G3"):
        T(config=_Cfg(), mcpd_client=object())


def test_g3_guard_rejects_mcpd_client_none():
    class T(BrainBackend):
        __slots__ = ()
        backend_name = "t"
        def _call_provider(self, envelope): return ("{}", 0, 0)
    with pytest.raises(BrainSecurityError, match="G3"):
        T(config=_Cfg(), mcpd_client=None)


# ── 3-6: retry semantics ─────────────────────────────────────────────────────


def test_complete_returns_brainresponse_on_first_try():
    b = _MockBackend(_Cfg(), [json.dumps(_valid_intent_payload())])
    resp = b.complete("sys", "user", _intent_schema())
    assert isinstance(resp, BrainResponse)
    assert resp.attempts == 1
    assert resp.backend == "mock"


def test_complete_retries_on_schema_failure_up_to_max():
    bad = json.dumps({"intent_id": "not-a-uuid"})
    good = json.dumps(_valid_intent_payload())
    observed = []
    b = _MockBackend(_Cfg(), [bad, bad, good], observed_envelopes=observed)
    resp = b.complete("sys", "user", _intent_schema(), max_retries=3)
    assert resp.attempts == 3
    assert len(observed) == 3


def test_complete_raises_brainschemaerror_when_retries_exhausted():
    bad = json.dumps({"intent_id": "not-a-uuid", "action": "x"})
    b = _MockBackend(_Cfg(), [bad, bad, bad])
    with pytest.raises(BrainSchemaError) as exc_info:
        b.complete("sys", "user", _intent_schema(), max_retries=3)
    err = exc_info.value
    assert err.attempts == 3
    assert err.last_error
    assert err.last_payload_excerpt
    assert len(err.last_payload_excerpt) <= 200


def test_complete_raises_braintruncationerror_on_json_decode_exhaustion():
    truncated = '{"intent_id":"' + str(uuid.uuid4())[:10]
    b = _MockBackend(_Cfg(), [truncated, truncated, truncated])
    with pytest.raises(BrainTruncationError):
        b.complete("sys", "user", _intent_schema(), max_retries=3)


# ── 7: BrainProviderError must not be retried ────────────────────────────────


def test_complete_does_not_retry_on_provider_error():
    observed = []
    b = _MockBackend(_Cfg(), [], raise_provider=True, observed_envelopes=observed)
    with pytest.raises(BrainProviderError):
        b.complete("sys", "user", _intent_schema(), max_retries=3)
    assert len(observed) == 1


# ── 8: hard cap ───────────────────────────────────────────────────────────────


def test_max_retries_hard_cap():
    b = _MockBackend(_Cfg(), [])
    with pytest.raises(BrainConfigError, match="hard cap"):
        b.complete("sys", "user", None, max_retries=MAX_RETRY_HARD_CAP + 1)


def test_max_retries_must_be_positive():
    b = _MockBackend(_Cfg(), [])
    with pytest.raises(BrainConfigError):
        b.complete("sys", "user", None, max_retries=0)


# ── 9: excerpt bound under fuzz ──────────────────────────────────────────────


@given(payload=st.text(min_size=0, max_size=5000))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_excerpt_bounded_to_200_chars_under_hypothesis_fuzz(payload):
    b = _MockBackend(_Cfg(), [payload, payload, payload])
    try:
        b.complete("sys", "user", _intent_schema(), max_retries=3)
    except (BrainSchemaError, BrainTruncationError) as e:
        assert len(e.last_payload_excerpt) <= 200


# ── 10: Draft-07 + UUID FormatChecker drift guard ────────────────────────────


def test_schema_validation_uses_draft7_with_uuid_format():
    bad = json.dumps({**_valid_intent_payload(), "intent_id": "not-a-uuid"})
    b = _MockBackend(_Cfg(), [bad, bad, bad])
    with pytest.raises(BrainSchemaError):
        b.complete("sys", "user", _intent_schema(), max_retries=3)


# ── 11: validation against real intent.json corpus ───────────────────────────


def test_complete_validates_against_real_intent_schema():
    valid_payloads = [
        _valid_intent_payload(),
        {**_valid_intent_payload(), "action": "fs.read", "target": "/etc/hostname"},
        {**_valid_intent_payload(), "action": "service.status",
         "params": {"unit": "nginx"}},
    ]
    for p in valid_payloads:
        b = _MockBackend(_Cfg(), [json.dumps(p)])
        resp = b.complete("sys", "user", _intent_schema())
        assert resp.content_json == p


# ── 12: __init_subclass__ blocks override of complete() ──────────────────────


def test_subclass_cannot_override_complete():
    with pytest.raises(TypeError, match="non-overridable"):
        class Bad(BrainBackend):
            __slots__ = ()
            backend_name = "bad"
            def _call_provider(self, envelope): return ("{}", 0, 0)
            def complete(self, *a, **k):
                return None


def test_subclass_without_slots_is_rejected():
    """D20 strict: every BrainBackend subclass MUST declare __slots__."""
    with pytest.raises(TypeError, match="__slots__"):
        class NoSlots(BrainBackend):
            backend_name = "no_slots"
            def _call_provider(self, envelope): return ("{}", 0, 0)


# ── 13: bounded retry context (D15) ──────────────────────────────────────────


def test_retry_context_is_statically_bounded():
    """The user string passed to subsequent attempts must NOT accumulate
    prior raw output. Bounded by the fixed-size correction note (~250 chars)."""
    bad = json.dumps({"intent_id": "not-a-uuid"})
    good = json.dumps(_valid_intent_payload())
    observed = []
    b = _MockBackend(_Cfg(), [bad, bad, good], observed_envelopes=observed)
    b.complete("sys", "user", _intent_schema(), max_retries=3)

    base_len = len("user")
    correction_overhead_cap = 300
    for env in observed[1:]:
        assert len(env.user) <= base_len + correction_overhead_cap
        assert bad not in env.user, "prior raw output leaked into retry prompt"


# ── 14: sampling decay (D16) ─────────────────────────────────────────────────


def test_sampling_decays_monotonically():
    bad = json.dumps({"intent_id": "not-a-uuid"})
    good = json.dumps(_valid_intent_payload())
    observed = []
    b = _MockBackend(_Cfg(), [bad, bad, good], observed_envelopes=observed)
    b.complete("sys", "user", _intent_schema(), max_retries=3)
    temps = [env.sampling["temperature"] for env in observed]
    for i in range(1, len(temps)):
        assert temps[i] <= temps[i - 1], f"temperature increased: {temps}"


# ── 15: __slots__ lockdown (D20) ─────────────────────────────────────────────


def test_slots_prevents_attribute_injection():
    b = _MockBackend(_Cfg(), [json.dumps(_valid_intent_payload())])
    with pytest.raises(AttributeError):
        b._validator = lambda x: True


# ── 16: strict JSON extraction, no regex fallback (D19) ──────────────────────


def test_strict_json_extraction_no_regex_fallback():
    chatty = 'Sure! Here is the intent: { "intent_id": invalid json } and more text.'
    b = _MockBackend(_Cfg(), [chatty, chatty, chatty])
    with pytest.raises((BrainSchemaError, BrainTruncationError)):
        b.complete("sys", "user", _intent_schema(), max_retries=3)


def test_strict_json_extraction_handles_chatty_prefix_around_valid_json():
    payload = _valid_intent_payload()
    chatty = f"Sure, here you go:\n{json.dumps(payload)}\nLet me know if you need more!"
    b = _MockBackend(_Cfg(), [chatty])
    resp = b.complete("sys", "user", _intent_schema())
    assert resp.content_json == payload


# ── 16a: envelope tools_disabled always True (D17 stage 1) ───────────────────


def test_envelope_always_sets_tools_disabled_true():
    observed = []
    b = _MockBackend(_Cfg(), [json.dumps(_valid_intent_payload())],
                     observed_envelopes=observed)
    b.complete("sys", "user", _intent_schema())
    assert all(env.tools_disabled is True for env in observed)
    with pytest.raises(Exception):
        observed[0].tools_disabled = False


# ── 16b: auditor call-count probe (D17 stage 2) ──────────────────────────────


def test_auditor_bypass_subclass_raises():
    b = _MockBackend(_Cfg(), [json.dumps(_valid_intent_payload())],
                     skip_auditor=True)
    with pytest.raises(BrainSecurityError, match="stage-2 contract"):
        b.complete("sys", "user", _intent_schema())


# ── Registry contract ────────────────────────────────────────────────────────


def test_registry_round_trip():
    _reset_registry_for_tests()

    @register_backend("test-rt")
    class _T(BrainBackend):
        __slots__ = ()
        def _call_provider(self, envelope): return ("{}", 0, 0)

    assert "test-rt" in registered_backends()
    cfg = type("C", (), {"qb": type("Q", (), {"name": "test-rt", "model": "m"})()})()
    from controller.backends import make_backend
    inst = make_backend(cfg)
    assert isinstance(inst, _T)
    _reset_registry_for_tests()


def test_registry_rejects_duplicate_name():
    _reset_registry_for_tests()

    @register_backend("dup-test")
    class _A(BrainBackend):
        __slots__ = ()
        def _call_provider(self, envelope): return ("{}", 0, 0)

    with pytest.raises(BrainConfigError, match="already registered"):
        @register_backend("dup-test")
        class _B(BrainBackend):
            __slots__ = ()
            def _call_provider(self, envelope): return ("{}", 0, 0)
    _reset_registry_for_tests()


def test_make_backend_rejects_unknown_name():
    _reset_registry_for_tests()
    from controller.backends import make_backend
    cfg = type("C", (), {"qb": type("Q", (), {"name": "nope", "model": "m"})()})()
    with pytest.raises(BrainConfigError, match="unknown backend"):
        make_backend(cfg)
