"""Tests for ``backends.gemini_backend.GeminiBackend``.

Covers tests 31-41 from the M2.6/M2.7 plan: happy path with native
``response_schema``, generation_config shape, schema sanitization (with
``format`` stripped), no-tools in audit payload, retry, empty-text
provider error, sampling pass-through, cost arithmetic.

Tests inject a fake ``google.generativeai`` module via ``sys.modules``
because the real SDK is not installed in the test environment. The
backend's lazy import (``import google.generativeai as genai`` inside
``__init__``) resolves to the fake.
"""

from __future__ import annotations

import json
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller.backends import (
    BrainProviderError,
    BrainResponse,
    BrainSchemaError,
    SecretRef,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


_FINGERPRINT = "AIzaDUMMY-gemini-fingerprint-Q9X12345678901234567"


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


def _gemini_response(
    text: str, *, tokens_in: int = 10, tokens_out: int = 20
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=tokens_in,
            candidates_token_count=tokens_out,
        ),
    )


class _FakeModel:
    """Stand-in for ``google.generativeai.GenerativeModel``."""

    last_instance: "_FakeModel | None" = None

    def __init__(self, model_name: str, *, system_instruction: str = ""):
        self.model_name = model_name
        self.system_instruction = system_instruction
        self.captured: list[dict] = []
        self.queued_responses: list[object] = []
        self.queued_exceptions: list[BaseException | None] = []
        _FakeModel.last_instance = self

    def generate_content(self, prompt, *, generation_config=None,
                         request_options=None):
        self.captured.append(
            {
                "prompt": prompt,
                "generation_config": generation_config,
                "request_options": request_options,
            }
        )
        if self.queued_exceptions:
            exc = self.queued_exceptions.pop(0)
            if exc is not None:
                raise exc
        if not self.queued_responses:
            raise AssertionError("no queued response — test setup error")
        return self.queued_responses.pop(0)


class _FakeGenAiModule(types.ModuleType):
    """Stand-in for the ``google.generativeai`` package."""

    def __init__(self):
        super().__init__("google.generativeai")
        self.configured_keys: list[str] = []
        self.GenerativeModel = _FakeModel

    def configure(self, *, api_key: str):
        self.configured_keys.append(api_key)


class _Cfg:
    def __init__(
        self,
        *,
        model: str = "gemini-2.0-flash",
        max_tokens: int = 1024,
        timeout_seconds: int = 30,
        api_key_env: str = "ICEBREAKER_TEST_GEM_KEY",
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.api_key = SecretRef(api_key_env)


@pytest.fixture
def fake_genai(monkeypatch):
    """Inject a fake ``google.generativeai`` into ``sys.modules`` and reset
    the module-level _FakeModel.last_instance pointer for each test."""
    monkeypatch.setenv("ICEBREAKER_TEST_GEM_KEY", _FINGERPRINT)
    fake_module = _FakeGenAiModule()
    google_ns = types.ModuleType("google")
    google_ns.generativeai = fake_module
    monkeypatch.setitem(sys.modules, "google", google_ns)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_module)
    _FakeModel.last_instance = None
    # Construct the backend AFTER sys.modules is patched so the lazy
    # import resolves to the fake. Tests construct via the helper.
    yield fake_module


def _make_backend(cfg: _Cfg | None = None):
    from controller.backends.gemini_backend import GeminiBackend
    return GeminiBackend(cfg or _Cfg())


# ── 31: happy path ──────────────────────────────────────────────────────────


def test_gemini_complete_happy_path(fake_genai):
    intent = _valid_intent()
    queue = _ModelQueue([_gemini_response(json.dumps(intent))])
    queue.attach()
    backend = _make_backend()
    resp = backend.complete("sys", "user", _intent_schema())
    assert isinstance(resp, BrainResponse)
    assert resp.attempts == 1
    assert resp.backend == "gemini"
    assert resp.content_json == intent


class _ModelQueue:
    """Helper: pre-queue responses so each _FakeModel constructed during
    a `complete()` call gets the queued response."""

    def __init__(self, responses, exceptions=None):
        self.responses = list(responses)
        self.exceptions = list(exceptions or [])

    def attach(self):
        """Monkey-patch _FakeModel.__init__ so each new instance picks up
        the next queued response / exception."""
        original_init = _FakeModel.__init__
        captured_models: list[_FakeModel] = []
        responses = self.responses
        exceptions = self.exceptions

        def init(model_self, model_name, *, system_instruction=""):
            original_init(model_self, model_name, system_instruction=system_instruction)
            if responses:
                model_self.queued_responses.append(responses.pop(0))
            if exceptions:
                model_self.queued_exceptions.append(exceptions.pop(0))
            captured_models.append(model_self)

        _FakeModel.__init__ = init  # type: ignore[assignment]
        self.models = captured_models


# ── 32: response_schema in generation_config ────────────────────────────────


def test_gemini_uses_response_schema_in_generation_config(fake_genai):
    queue = _ModelQueue([_gemini_response(json.dumps(_valid_intent()))])
    queue.attach()
    backend = _make_backend()
    backend.complete("sys", "user", _intent_schema())
    captured = queue.models[0].captured[0]
    gc = captured["generation_config"]
    assert "response_schema" in gc
    assert isinstance(gc["response_schema"], dict)


# ── 33: response_mime_type ───────────────────────────────────────────────────


def test_gemini_response_mime_type_is_application_json(fake_genai):
    queue = _ModelQueue([_gemini_response(json.dumps(_valid_intent()))])
    queue.attach()
    backend = _make_backend()
    backend.complete("sys", "user", _intent_schema())
    gc = queue.models[0].captured[0]["generation_config"]
    assert gc["response_mime_type"] == "application/json"


# ── 34: maxLength stripped from wire schema ─────────────────────────────────


def test_gemini_wire_schema_has_no_max_length(fake_genai):
    queue = _ModelQueue([_gemini_response(json.dumps(_valid_intent()))])
    queue.attach()
    backend = _make_backend()
    backend.complete("sys", "user", _intent_schema())
    wire_schema = queue.models[0].captured[0]["generation_config"][
        "response_schema"
    ]
    keys = _collect_keys(wire_schema)
    forbidden = {"maxLength", "minLength", "minimum", "maximum", "oneOf"}
    assert not (keys & forbidden)


# ── 35: format stripped from wire schema ────────────────────────────────────


def test_gemini_format_stripped_in_wire_schema(fake_genai):
    queue = _ModelQueue([_gemini_response(json.dumps(_valid_intent()))])
    queue.attach()
    backend = _make_backend()
    backend.complete("sys", "user", _intent_schema())
    wire_schema = queue.models[0].captured[0]["generation_config"][
        "response_schema"
    ]
    assert "format" not in _collect_keys(wire_schema)


# ── 36: no tools in audit payload ───────────────────────────────────────────


def test_gemini_no_tools_key_in_audit_payload(fake_genai):
    queue = _ModelQueue([_gemini_response(json.dumps(_valid_intent()))])
    queue.attach()
    backend = _make_backend()
    backend.complete("sys", "user", _intent_schema())
    # Auditor was called once and never raised — confirmed by the fact
    # complete() returned. Belt-and-suspenders: scan the captured request.
    captured = queue.models[0].captured[0]
    gc = captured["generation_config"]
    assert "tools" not in gc
    assert "tool_config" not in gc
    assert "grounding" not in gc


# ── 37: local validator catches uuid violation Gemini missed ────────────────


def test_gemini_local_validator_catches_what_gemini_misses(fake_genai):
    """Gemini wire schema has ``format`` stripped → no UUID enforcement.
    Our local validator still requires ``intent_id`` to be a UUID."""
    bad = {**_valid_intent(), "intent_id": "not-a-uuid"}
    good = _valid_intent()
    queue = _ModelQueue(
        [
            _gemini_response(json.dumps(bad)),
            _gemini_response(json.dumps(bad)),
            _gemini_response(json.dumps(good)),
        ]
    )
    queue.attach()
    backend = _make_backend()
    resp = backend.complete("sys", "user", _intent_schema(), max_retries=3)
    assert resp.attempts == 3
    assert resp.content_json == good


# ── 38: retry on schema failure ─────────────────────────────────────────────


def test_gemini_retry_on_schema_failure(fake_genai):
    queue = _ModelQueue(
        [
            _gemini_response('{"intent_id": "bad"}'),
            _gemini_response(json.dumps(_valid_intent())),
        ]
    )
    queue.attach()
    backend = _make_backend()
    resp = backend.complete("sys", "user", _intent_schema(), max_retries=2)
    assert resp.attempts == 2


# ── 39: empty text raises provider error ────────────────────────────────────


def test_gemini_empty_text_raises_provider_error(fake_genai):
    queue = _ModelQueue([_gemini_response("")])
    queue.attach()
    backend = _make_backend()
    with pytest.raises(BrainProviderError, match="empty"):
        backend.complete("sys", "user", _intent_schema())


# ── 40: cost estimate ──────────────────────────────────────────────────────


def test_gemini_cost_estimate(fake_genai):
    queue = _ModelQueue(
        [
            _gemini_response(
                json.dumps(_valid_intent()),
                tokens_in=1_000_000,
                tokens_out=500_000,
            )
        ]
    )
    queue.attach()
    backend = _make_backend()
    resp = backend.complete("sys", "user", _intent_schema())
    # 0.075 * 1.0 input + 0.30 * 0.5 output = 0.075 + 0.15 = 0.225
    assert resp.cost_usd == pytest.approx(0.225)


# ── 41: sampling params reach SDK ───────────────────────────────────────────


def test_gemini_sampling_params_reach_sdk(fake_genai):
    queue = _ModelQueue([_gemini_response(json.dumps(_valid_intent()))])
    queue.attach()
    backend = _make_backend()
    backend.complete("sys", "user", _intent_schema())
    gc = queue.models[0].captured[0]["generation_config"]
    # Attempt 1 sampling decay
    assert gc["temperature"] == pytest.approx(0.4)
    assert gc["top_p"] == pytest.approx(0.9)


def test_gemini_max_output_tokens_from_config(fake_genai):
    queue = _ModelQueue([_gemini_response(json.dumps(_valid_intent()))])
    queue.attach()
    backend = _make_backend(_Cfg(max_tokens=2048))
    backend.complete("sys", "user", _intent_schema())
    gc = queue.models[0].captured[0]["generation_config"]
    assert gc["max_output_tokens"] == 2048


# ── Extra: SDK exception sanitized ──────────────────────────────────────────


def test_gemini_sdk_exception_sanitized(fake_genai):
    queue = _ModelQueue(
        [None],
        exceptions=[
            RuntimeError(f"401 Unauthorized: x-goog-api-key: {_FINGERPRINT}")
        ],
    )
    queue.attach()
    backend = _make_backend()
    with pytest.raises(BrainProviderError) as exc_info:
        backend.complete("sys", "user", _intent_schema())
    msg = str(exc_info.value)
    # Note: _FINGERPRINT happens to be an AIza-prefix shape, which the
    # SECRET_PATTERNS regex covers; sanitizer should redact.
    assert _FINGERPRINT not in msg
    assert "[REDACTED]" in msg


def test_gemini_timeout_passed_to_sdk(fake_genai):
    queue = _ModelQueue([_gemini_response(json.dumps(_valid_intent()))])
    queue.attach()
    backend = _make_backend(_Cfg(timeout_seconds=45))
    backend.complete("sys", "user", _intent_schema())
    captured = queue.models[0].captured[0]
    assert captured["request_options"]["timeout"] == 45


def test_gemini_configures_genai_with_revealed_key(fake_genai):
    backend = _make_backend()
    assert fake_genai.configured_keys == [_FINGERPRINT]
    assert isinstance(backend._api_key_ref, SecretRef)
