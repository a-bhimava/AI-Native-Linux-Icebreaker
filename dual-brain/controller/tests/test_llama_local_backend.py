"""Tests for ``backends.llama_local_backend.LlamaCppLocalBackend``.

Mocked llama-server (no live HTTP). Tests:
* Health probe at construction
* transport="unix" raises NotImplementedError-style BrainConfigError
* Unknown transport rejected
* Grammar attached to request body
* Sampling decay round-trip
* Cost is None
* No `tools` key in outbound payload (auditor passes)
* Truncation falls through retry loop to BrainTruncationError
* SDK exception is sanitized (uniform pipeline)
* Cross-backend shape match (foreshadows M2.13 G10)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller.backends import (
    BrainConfigError,
    BrainProviderError,
    BrainResponse,
    BrainSchemaError,
    BrainTruncationError,
)
from controller.backends.llama_local_backend import LlamaCppLocalBackend


# ─── Helpers ──────────────────────────────────────────────────────────────


_GRAMMAR_PATH = (
    Path(__file__).parent.parent / "grammars" / "qb_intent.gbnf"
)


def _intent_schema() -> dict:
    p = Path(__file__).parent.parent / "schemas" / "intent.json"
    with p.open("r", encoding="utf-8") as h:
        return json.load(h)


def _valid_intent() -> dict:
    return {
        "intent_id": str(uuid.uuid4()),
        "action": "system.status",
        "target": "",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }


class _Cfg:
    """Minimal BackendConfig-shaped object for tests."""

    def __init__(
        self,
        *,
        endpoint: str = "http://127.0.0.1:8081",
        transport: str = "http",
        grammar_path: Path | None = None,
        max_tokens: int = 512,
        timeout_seconds: int = 30,
        model: str = "Test Local",
        model_id: str = "test-local",
    ):
        self.endpoint = endpoint
        self.transport = transport
        self.grammar_path = grammar_path or _GRAMMAR_PATH
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.model_id = model_id
        self.draft_model_id = None
        self.api_key = None


class _FakeResponse:
    """Stand-in for ``requests.Response``."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        body: dict | None = None,
        raise_for_status_exc: BaseException | None = None,
    ):
        self.status_code = status_code
        self._body = body or {}
        self._raise = raise_for_status_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def json(self):
        return self._body


class _FakeSession:
    """Stand-in for ``requests.Session``."""

    def __init__(self) -> None:
        self.get_calls: list[tuple] = []
        self.post_calls: list[tuple] = []
        self.health_response = _FakeResponse(body={"status": "ok"})
        self.post_responses: list[_FakeResponse | BaseException] = []

    def get(self, url, timeout=None, **kw):
        self.get_calls.append((url, timeout, kw))
        return self.health_response

    def post(self, url, json=None, timeout=None, **kw):
        self.post_calls.append((url, json, timeout, kw))
        if not self.post_responses:
            raise AssertionError("no queued POST response — test setup error")
        item = self.post_responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _llama_body(content: str, *, tokens_in: int = 50, tokens_out: int = 80) -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}}
        ],
        "usage": {
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
        },
    }


@pytest.fixture
def fake_requests(monkeypatch):
    """Install a fake ``requests`` module that returns _FakeSession."""
    import sys

    fake_session = _FakeSession()
    fake_pkg = SimpleNamespace(Session=lambda: fake_session)
    monkeypatch.setitem(sys.modules, "requests", fake_pkg)
    yield fake_session


# ─── 1: health probe failure raises at construction ──────────────────────


def test_health_probe_failure_raises_brainconfigerror(monkeypatch):
    import sys

    class _Boom:
        def raise_for_status(self):
            raise RuntimeError("connection refused")
        def json(self): return {}

    class _Session:
        def __init__(self):
            pass
        def get(self, url, timeout=None, **kw):
            raise ConnectionRefusedError("connection refused at " + url)
        def post(self, *a, **kw):
            raise AssertionError("post should not be called")

    fake_requests = SimpleNamespace(Session=_Session)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    with pytest.raises(BrainConfigError, match="health probe failed"):
        LlamaCppLocalBackend(_Cfg())


# ─── 2: transport='unix' raises ──────────────────────────────────────────


def test_transport_unix_raises_brainconfigerror():
    cfg = _Cfg(transport="unix")
    with pytest.raises(BrainConfigError, match="Phase 6"):
        LlamaCppLocalBackend(cfg)


def test_unknown_transport_raises():
    cfg = _Cfg(transport="grpc")
    with pytest.raises(BrainConfigError, match="unknown transport"):
        LlamaCppLocalBackend(cfg)


# ─── 3: config validation ────────────────────────────────────────────────


def test_missing_endpoint_raises():
    cfg = _Cfg()
    cfg.endpoint = None
    with pytest.raises(BrainConfigError, match="endpoint"):
        LlamaCppLocalBackend(cfg)


def test_missing_grammar_path_raises():
    cfg = _Cfg()
    cfg.grammar_path = None
    with pytest.raises(BrainConfigError, match="grammar_path"):
        LlamaCppLocalBackend(cfg)


def test_nonexistent_grammar_file_raises(tmp_path):
    cfg = _Cfg(grammar_path=tmp_path / "nonexistent.gbnf")
    with pytest.raises(BrainConfigError, match="grammar file not found"):
        LlamaCppLocalBackend(cfg)


# ─── 4: happy path ───────────────────────────────────────────────────────


def test_complete_happy_path(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg())
    intent = _valid_intent()
    fake_requests.post_responses.append(
        _FakeResponse(body=_llama_body(json.dumps(intent)))
    )
    resp = backend.complete("sys", "user", _intent_schema())
    assert isinstance(resp, BrainResponse)
    assert resp.attempts == 1
    assert resp.backend == "local"
    assert resp.model == "Test Local"
    assert resp.content_json == intent
    assert resp.tokens_in == 50
    assert resp.tokens_out == 80
    assert resp.cost_usd is None


# ─── 5: grammar attached to request ──────────────────────────────────────


def test_grammar_attached_to_request_body(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg())
    intent = _valid_intent()
    fake_requests.post_responses.append(
        _FakeResponse(body=_llama_body(json.dumps(intent)))
    )
    backend.complete("sys", "user", _intent_schema())
    assert len(fake_requests.post_calls) == 1
    _url, body, _timeout, _kw = fake_requests.post_calls[0]
    assert "grammar" in body
    # Sanity: the grammar should contain identifiable tokens from
    # qb_intent.gbnf (e.g. "risk-level" rule).
    assert "risk-level" in body["grammar"] or "risk_level" in body["grammar"]


# ─── 6: no `tools` key in outbound payload ───────────────────────────────


def test_no_tools_key_in_payload(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg())
    intent = _valid_intent()
    fake_requests.post_responses.append(
        _FakeResponse(body=_llama_body(json.dumps(intent)))
    )
    backend.complete("sys", "user", _intent_schema())
    _url, body, _timeout, _kw = fake_requests.post_calls[0]
    assert "tools" not in body
    assert "tool_choice" not in body


# ─── 7: sampling params reach SDK ────────────────────────────────────────


def test_sampling_params_reach_request(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg())
    intent = _valid_intent()
    fake_requests.post_responses.append(
        _FakeResponse(body=_llama_body(json.dumps(intent)))
    )
    backend.complete("sys", "user", _intent_schema())
    _url, body, _timeout, _kw = fake_requests.post_calls[0]
    # Attempt-1 sampling decay = {temperature: 0.4, top_p: 0.9}
    assert body["temperature"] == pytest.approx(0.4)
    assert body["top_p"] == pytest.approx(0.9)


def test_sampling_decays_across_retries(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg())
    bad = '{"intent_id":"not-a-uuid"}'
    good = json.dumps(_valid_intent())
    fake_requests.post_responses.append(_FakeResponse(body=_llama_body(bad)))
    fake_requests.post_responses.append(_FakeResponse(body=_llama_body(good)))
    backend.complete("sys", "user", _intent_schema(), max_retries=2)
    temps = [c[1]["temperature"] for c in fake_requests.post_calls]
    assert temps[1] <= temps[0]


# ─── 8: retry on schema failure ──────────────────────────────────────────


def test_retry_on_schema_failure(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg())
    bad = json.dumps({"intent_id": "not-a-uuid"})
    good = json.dumps(_valid_intent())
    fake_requests.post_responses.append(_FakeResponse(body=_llama_body(bad)))
    fake_requests.post_responses.append(_FakeResponse(body=_llama_body(good)))
    resp = backend.complete("sys", "user", _intent_schema(), max_retries=2)
    assert resp.attempts == 2


# ─── 9: truncation falls through to BrainTruncationError ────────────────


def test_truncation_raises_after_retries(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg())
    truncated = '{"intent_id":"' + str(uuid.uuid4())[:10]
    for _ in range(3):
        fake_requests.post_responses.append(
            _FakeResponse(body=_llama_body(truncated))
        )
    with pytest.raises(BrainTruncationError):
        backend.complete("sys", "user", _intent_schema(), max_retries=3)


# ─── 10: empty response raises BrainProviderError ────────────────────────


def test_empty_content_raises_provider_error(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg())
    fake_requests.post_responses.append(
        _FakeResponse(body=_llama_body(""))
    )
    with pytest.raises(BrainProviderError, match="empty"):
        backend.complete("sys", "user", _intent_schema())


def test_no_choices_raises_provider_error(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg())
    fake_requests.post_responses.append(
        _FakeResponse(body={"choices": [], "usage": {}})
    )
    with pytest.raises(BrainProviderError, match="no choices"):
        backend.complete("sys", "user", _intent_schema())


# ─── 11: SDK exception is sanitized ──────────────────────────────────────


def test_sdk_exception_sanitized(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg())
    # Even though local has no API key, the sanitize pipeline is uniform.
    fake_requests.post_responses.append(
        RuntimeError("connection broken: Authorization: Bearer "
                     "sk-ant-DUMMY1234567890ABCDEFGH should be redacted")  # pragma: allowlist secret
    )
    with pytest.raises(BrainProviderError) as exc_info:
        backend.complete("sys", "user", _intent_schema())
    msg = str(exc_info.value)
    assert "sk-ant-" not in msg
    assert "[REDACTED]" in msg


# ─── 12: api_key is None for local; cost is None ────────────────────────


def test_local_api_key_is_none_and_cost_is_none(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg())
    intent = _valid_intent()
    fake_requests.post_responses.append(
        _FakeResponse(body=_llama_body(json.dumps(intent)))
    )
    resp = backend.complete("sys", "user", _intent_schema())
    assert resp.cost_usd is None
    # config.api_key stays None for local backend
    assert backend._config.api_key is None


# ─── Auditor probe + max_tokens ─────────────────────────────────────────


def test_auditor_call_count_increments_per_attempt(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg())
    bad = json.dumps({"intent_id": "bad"})
    good = json.dumps(_valid_intent())
    fake_requests.post_responses.append(_FakeResponse(body=_llama_body(bad)))
    fake_requests.post_responses.append(_FakeResponse(body=_llama_body(good)))
    backend.complete("sys", "user", _intent_schema(), max_retries=2)
    assert backend._auditor.call_count == 2


def test_max_tokens_from_config_reaches_request(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg(max_tokens=2048))
    fake_requests.post_responses.append(
        _FakeResponse(body=_llama_body(json.dumps(_valid_intent())))
    )
    backend.complete("sys", "user", _intent_schema())
    _url, body, _timeout, _kw = fake_requests.post_calls[0]
    assert body["max_tokens"] == 2048


def test_endpoint_used_in_post_url(fake_requests):
    backend = LlamaCppLocalBackend(_Cfg(endpoint="http://127.0.0.1:8081"))
    fake_requests.post_responses.append(
        _FakeResponse(body=_llama_body(json.dumps(_valid_intent())))
    )
    backend.complete("sys", "user", _intent_schema())
    url, _body, _timeout, _kw = fake_requests.post_calls[0]
    assert url == "http://127.0.0.1:8081/v1/chat/completions"
