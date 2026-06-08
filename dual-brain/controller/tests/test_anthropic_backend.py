"""Tests for ``backends.anthropic_backend.AnthropicBackend``.

Covers tests 18-30 from the M2.6/M2.7 plan: happy path with native JSON
mode, output_config shape, schema sanitization, no-tools, retry, refusal
behavior, sampling/max_tokens pass-through, SDK exception sanitization,
cost arithmetic.

All tests mock ``anthropic.Anthropic`` — no live API calls.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller.backends import (
    BrainProviderError,
    BrainResponse,
    BrainSchemaError,
    BrainSecurityError,
    SecretRef,
)
from controller.backends.anthropic_backend import AnthropicBackend


# ── Helpers ──────────────────────────────────────────────────────────────────


_FINGERPRINT = "sk-ant-DUMMY-anthropic-fingerprint-Q9X1234567890"


def _intent_schema() -> dict:
    path = Path(__file__).parent.parent / "schemas" / "intent.json"
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _valid_intent() -> dict:
    return {
        "intent_id": str(uuid.uuid4()),
        "action": "system.status",
        "target": "",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }


def _collect_keys(node: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            keys.add(k)
            keys.update(_collect_keys(v))
    elif isinstance(node, list):
        for item in node:
            keys.update(_collect_keys(item))
    return keys


class _Cfg:
    """Minimal BackendConfig-shaped object."""

    def __init__(
        self,
        *,
        model: str = "claude-haiku-4-5",
        max_tokens: int = 1024,
        timeout_seconds: int = 30,
        api_key_env: str = "ICEBREAKER_TEST_ANTH_KEY",
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.api_key = SecretRef(api_key_env)


def _text_response(text: str, *, tokens_in: int = 10, tokens_out: int = 20,
                   stop_reason: str = "end_turn") -> SimpleNamespace:
    """Build a fake anthropic Message response with one text content block."""
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out),
        stop_reason=stop_reason,
    )


class _FakeMessages:
    """Stand-in for ``anthropic.Anthropic().messages``."""

    def __init__(self) -> None:
        self.captured: list[dict] = []
        self.queued_responses: list[object] = []
        self.queued_exceptions: list[BaseException | None] = []

    def create(self, **kwargs):
        self.captured.append(kwargs)
        # If an exception is queued for THIS attempt, raise it.
        if self.queued_exceptions:
            exc = self.queued_exceptions.pop(0)
            if exc is not None:
                raise exc
        if not self.queued_responses:
            raise AssertionError("no queued response — test setup error")
        return self.queued_responses.pop(0)


class _FakeAnthropic:
    """Stand-in for ``anthropic.Anthropic`` itself."""

    last_instance: "_FakeAnthropic | None" = None

    def __init__(self, **init_kwargs):
        self.init_kwargs = init_kwargs
        self.messages = _FakeMessages()
        _FakeAnthropic.last_instance = self


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Install a fake ``anthropic.Anthropic`` class and return the fake module."""
    monkeypatch.setenv("ICEBREAKER_TEST_ANTH_KEY", _FINGERPRINT)
    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropic)
    _FakeAnthropic.last_instance = None
    yield _FakeAnthropic


# ── 18: happy path ──────────────────────────────────────────────────────────


def test_anthropic_complete_happy_path(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    intent = _valid_intent()
    fake_anthropic.last_instance.messages.queued_responses.append(
        _text_response(json.dumps(intent))
    )
    resp = backend.complete("sys", "user", _intent_schema())
    assert isinstance(resp, BrainResponse)
    assert resp.attempts == 1
    assert resp.backend == "anthropic"
    assert resp.content_json == intent
    assert resp.tokens_in == 10
    assert resp.tokens_out == 20


# ── 19: output_config shape ─────────────────────────────────────────────────


def test_anthropic_uses_output_config_format(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    fake_anthropic.last_instance.messages.queued_responses.append(
        _text_response(json.dumps(_valid_intent()))
    )
    backend.complete("sys", "user", _intent_schema())
    kwargs = fake_anthropic.last_instance.messages.captured[0]
    assert "output_config" in kwargs
    fmt = kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert "schema" in fmt
    assert isinstance(fmt["schema"], dict)


# ── 20: maxLength stripped from wire schema ─────────────────────────────────


def test_anthropic_wire_schema_has_no_max_length(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    fake_anthropic.last_instance.messages.queued_responses.append(
        _text_response(json.dumps(_valid_intent()))
    )
    backend.complete("sys", "user", _intent_schema())
    wire_schema = fake_anthropic.last_instance.messages.captured[0][
        "output_config"
    ]["format"]["schema"]
    keys = _collect_keys(wire_schema)
    forbidden = {
        "maxLength",
        "minLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "uniqueItems",
        "oneOf",
    }
    leaked = keys & forbidden
    assert not leaked, f"forbidden keys leaked into Anthropic wire schema: {leaked}"


# ── 21: oneOf converted to anyOf in wire schema ─────────────────────────────


def test_anthropic_oneof_converted_to_anyof_in_wire_schema(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    fake_anthropic.last_instance.messages.queued_responses.append(
        _text_response(json.dumps(_valid_intent()))
    )
    backend.complete("sys", "user", _intent_schema())
    wire_schema = fake_anthropic.last_instance.messages.captured[0][
        "output_config"
    ]["format"]["schema"]
    keys = _collect_keys(wire_schema)
    assert "oneOf" not in keys
    # intent.json's params field uses oneOf, so anyOf must appear
    assert "anyOf" in keys


# ── 22: no tools / tool_choice in kwargs ────────────────────────────────────


def test_anthropic_no_tools_key_in_kwargs(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    fake_anthropic.last_instance.messages.queued_responses.append(
        _text_response(json.dumps(_valid_intent()))
    )
    backend.complete("sys", "user", _intent_schema())
    kwargs = fake_anthropic.last_instance.messages.captured[0]
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs


# ── 23: local validator catches what Anthropic misses ──────────────────────


def test_anthropic_local_validator_catches_what_anthropic_misses(fake_anthropic):
    """The wire schema lacks ``maxLength``; if Anthropic emits a string
    longer than our ORIGINAL schema's 1024 limit, our local validator
    must reject it. Tests the safety floor."""
    backend = AnthropicBackend(_Cfg())
    over_long = _valid_intent()
    over_long["params"] = {"text": "x" * 2000}  # > maxLength=1024
    valid = _valid_intent()
    # First two attempts emit the over-long payload; third valid.
    queue = fake_anthropic.last_instance.messages.queued_responses
    queue.append(_text_response(json.dumps(over_long)))
    queue.append(_text_response(json.dumps(over_long)))
    queue.append(_text_response(json.dumps(valid)))
    resp = backend.complete("sys", "user", _intent_schema(), max_retries=3)
    assert resp.attempts == 3
    assert resp.content_json == valid


# ── 24: retry on schema failure ─────────────────────────────────────────────


def test_anthropic_retry_on_schema_failure(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    bad = {"intent_id": "not-a-uuid"}  # missing required fields too
    good = _valid_intent()
    queue = fake_anthropic.last_instance.messages.queued_responses
    queue.append(_text_response(json.dumps(bad)))
    queue.append(_text_response(json.dumps(good)))
    resp = backend.complete("sys", "user", _intent_schema(), max_retries=2)
    assert resp.attempts == 2


# ── 25: empty content raises BrainProviderError ─────────────────────────────


def test_anthropic_empty_content_raises_provider_error(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    empty_resp = SimpleNamespace(
        content=[],
        usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        stop_reason="end_turn",
    )
    fake_anthropic.last_instance.messages.queued_responses.append(empty_resp)
    with pytest.raises(BrainProviderError, match="no content blocks"):
        backend.complete("sys", "user", _intent_schema())


def test_anthropic_non_text_first_block_raises_provider_error(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    weird_block = SimpleNamespace(type="image", text=None)
    weird = SimpleNamespace(
        content=[weird_block],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        stop_reason="end_turn",
    )
    fake_anthropic.last_instance.messages.queued_responses.append(weird)
    with pytest.raises(BrainProviderError, match="'image'"):
        backend.complete("sys", "user", _intent_schema())


# ── 26: refusal falls through retry loop ────────────────────────────────────


def test_anthropic_refusal_falls_through_retry_loop(fake_anthropic):
    """When Anthropic refuses, response.content[0].text is prose, not JSON.
    `_extract_json_strict` (D19) fails; retry loop kicks in; exhausts to
    `BrainSchemaError` or `BrainTruncationError`."""
    backend = AnthropicBackend(_Cfg())
    refusal_text = (
        "I cannot help with that request as it violates safety guidelines."
    )
    queue = fake_anthropic.last_instance.messages.queued_responses
    for _ in range(3):
        queue.append(_text_response(refusal_text, stop_reason="refusal"))
    with pytest.raises(BrainSchemaError):
        backend.complete("sys", "user", _intent_schema(), max_retries=3)


# ── 27: sampling params reach SDK ───────────────────────────────────────────


def test_anthropic_sampling_params_reach_sdk(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    fake_anthropic.last_instance.messages.queued_responses.append(
        _text_response(json.dumps(_valid_intent()))
    )
    backend.complete("sys", "user", _intent_schema())
    kwargs = fake_anthropic.last_instance.messages.captured[0]
    # Attempt 1 sampling decay = {temperature: 0.4, top_p: 0.9}
    assert kwargs["temperature"] == pytest.approx(0.4)
    assert kwargs["top_p"] == pytest.approx(0.9)


def test_anthropic_sampling_decay_across_retries(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    bad = {"intent_id": "bad"}
    queue = fake_anthropic.last_instance.messages.queued_responses
    queue.append(_text_response(json.dumps(bad)))
    queue.append(_text_response(json.dumps(_valid_intent())))
    backend.complete("sys", "user", _intent_schema(), max_retries=2)
    captures = fake_anthropic.last_instance.messages.captured
    temps = [c["temperature"] for c in captures]
    # Strictly non-increasing
    assert temps[1] <= temps[0]


# ── 28: max_tokens from config ──────────────────────────────────────────────


def test_anthropic_max_tokens_from_config(fake_anthropic):
    backend = AnthropicBackend(_Cfg(max_tokens=2048))
    fake_anthropic.last_instance.messages.queued_responses.append(
        _text_response(json.dumps(_valid_intent()))
    )
    backend.complete("sys", "user", _intent_schema())
    kwargs = fake_anthropic.last_instance.messages.captured[0]
    assert kwargs["max_tokens"] == 2048


def test_anthropic_system_prompt_reaches_sdk(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    fake_anthropic.last_instance.messages.queued_responses.append(
        _text_response(json.dumps(_valid_intent()))
    )
    backend.complete("you are a tool router", "check disk", _intent_schema())
    kwargs = fake_anthropic.last_instance.messages.captured[0]
    assert kwargs["system"] == "you are a tool router"
    assert kwargs["messages"][0]["content"] == "check disk"


# ── 29: SDK exception sanitized ─────────────────────────────────────────────


def test_anthropic_sdk_exception_sanitized(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    sdk_exc = RuntimeError(
        f"401 Unauthorized: Authorization: Bearer {_FINGERPRINT}"
    )
    fake_anthropic.last_instance.messages.queued_responses.append(None)
    fake_anthropic.last_instance.messages.queued_exceptions.append(sdk_exc)
    with pytest.raises(BrainProviderError) as exc_info:
        backend.complete("sys", "user", _intent_schema())
    msg = str(exc_info.value)
    assert _FINGERPRINT not in msg
    assert "[REDACTED]" in msg


# ── 30: cost estimate ──────────────────────────────────────────────────────


def test_anthropic_cost_estimate(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    fake_anthropic.last_instance.messages.queued_responses.append(
        _text_response(
            json.dumps(_valid_intent()), tokens_in=1_000_000, tokens_out=500_000
        )
    )
    resp = backend.complete("sys", "user", _intent_schema())
    # 1.0 * 1M/1M=$1.00 input + 5.0 * 500K/1M=$2.50 output = $3.50
    assert resp.cost_usd == pytest.approx(3.50)


# ── Extra: auditor passes by default (no tools key) ────────────────────────


def test_anthropic_auditor_call_count_increments_per_attempt(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    bad = {"intent_id": "bad"}
    queue = fake_anthropic.last_instance.messages.queued_responses
    queue.append(_text_response(json.dumps(bad)))
    queue.append(_text_response(json.dumps(_valid_intent())))
    backend.complete("sys", "user", _intent_schema(), max_retries=2)
    assert backend._auditor.call_count == 2


def test_anthropic_init_uses_secret_ref(fake_anthropic):
    backend = AnthropicBackend(_Cfg())
    init_kwargs = fake_anthropic.last_instance.init_kwargs
    # The api_key was revealed and passed; verify the SDK got the value but
    # the backend never stores it as an attribute.
    assert init_kwargs["api_key"] == _FINGERPRINT
    assert isinstance(backend._api_key_ref, SecretRef)
