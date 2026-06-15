"""Tests for ``backends.openai_backend.OpenAIBackend``.

Covers: constructor, _call_provider, _stream_provider, auditor probe,
cost estimation, error sanitization, schema transform for OpenAI quirks
(additionalProperties injection, oneOf preserved).

All tests mock ``openai.OpenAI`` — no live API calls.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# openai SDK may not be installed — inject a mock module so the backend
# can be imported and tested without the real package.
import sys
from unittest.mock import MagicMock

_mock_openai = MagicMock()
sys.modules.setdefault("openai", _mock_openai)

from controller.backends import (
    BrainProviderError,
    BrainResponse,
    BrainSchemaError,
    BrainSecurityError,
    SecretRef,
)
from controller.backends.openai_backend import (
    OpenAIBackend,
    _inject_additional_properties_false,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


_FINGERPRINT = "sk-DUMMY-openai-fingerprint-Q9X1234567890"  # pragma: allowlist secret


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


class _Cfg:
    def __init__(
        self,
        *,
        model: str = "gpt-4.1-mini",
        max_tokens: int = 1024,
        timeout_seconds: int = 30,
        api_key_env: str = "ICEBREAKER_TEST_OPENAI_KEY",
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.api_key = SecretRef(api_key_env)


def _make_response(content_json: dict, prompt_tokens: int = 10, completion_tokens: int = 20):
    choice = SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(content_json)),
        finish_reason="stop",
    )
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[choice], usage=usage)


def _make_stream_chunks(content_json: dict, prompt_tokens: int = 10, completion_tokens: int = 20):
    text = json.dumps(content_json)
    chunks = []
    for i, char in enumerate(text):
        delta = SimpleNamespace(content=char, role=None)
        chunk = SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)
        chunks.append(chunk)
    final_chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=None, role=None))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )
    chunks.append(final_chunk)
    return chunks


def _build_backend(monkeypatch, create_fn=None, stream_fn=None):
    monkeypatch.setenv("ICEBREAKER_TEST_OPENAI_KEY", _FINGERPRINT)

    mock_client = MagicMock()
    if create_fn is not None:
        mock_client.chat.completions.create.side_effect = create_fn
    if stream_fn is not None:
        mock_client.chat.completions.create.side_effect = stream_fn

    _mock_openai.OpenAI = MagicMock(return_value=mock_client)
    backend = OpenAIBackend(_Cfg())

    return backend, mock_client


# ── Constructor tests ────────────────────────────────────────────────────────


class TestConstructor:
    def test_requires_api_key(self):
        from controller.backends import BrainConfigError
        with pytest.raises(BrainConfigError, match="api_key"):
            cfg = SimpleNamespace(
                model="gpt-4.1-mini", max_tokens=1024,
                timeout_seconds=30, api_key=None,
            )
            OpenAIBackend(cfg)

    def test_backend_name(self, monkeypatch):
        backend, _ = _build_backend(monkeypatch)
        assert backend.backend_name == "openai"

    def test_lazy_import(self, monkeypatch):
        monkeypatch.setenv("ICEBREAKER_TEST_OPENAI_KEY", _FINGERPRINT)
        mock_cls = MagicMock(return_value=MagicMock())
        _mock_openai.OpenAI = mock_cls
        backend = OpenAIBackend(_Cfg())
        mock_cls.assert_called_once()
        assert backend._client is not None


# ── _call_provider tests ─────────────────────────────────────────────────────


class TestCallProvider:
    def test_happy_path(self, monkeypatch):
        intent = _valid_intent()
        resp = _make_response(intent)

        def create_fn(**kwargs):
            return resp

        backend, mock_client = _build_backend(monkeypatch, create_fn=create_fn)
        result = backend.complete(
            system="test system",
            user="show system status",
            schema=_intent_schema(),
        )
        assert isinstance(result, BrainResponse)
        assert result.content_json["action"] == "system.status"
        assert result.backend == "openai"

    def test_response_format_shape(self, monkeypatch):
        intent = _valid_intent()
        captured = {}

        def create_fn(**kwargs):
            captured.update(kwargs)
            return _make_response(intent)

        backend, _ = _build_backend(monkeypatch, create_fn=create_fn)
        backend.complete(
            system="test", user="test",
            schema=_intent_schema(),
        )
        rf = captured["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["strict"] is True
        assert rf["json_schema"]["name"] == "intent"

    def test_no_tools_in_payload(self, monkeypatch):
        intent = _valid_intent()
        captured = {}

        def create_fn(**kwargs):
            captured.update(kwargs)
            return _make_response(intent)

        backend, _ = _build_backend(monkeypatch, create_fn=create_fn)
        backend.complete(
            system="test", user="test",
            schema=_intent_schema(),
        )
        assert "tools" not in captured
        assert "functions" not in captured
        assert "tool_choice" not in captured

    def test_empty_content_raises(self, monkeypatch):
        choice = SimpleNamespace(
            message=SimpleNamespace(content=""),
            finish_reason="stop",
        )
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=0)
        resp = SimpleNamespace(choices=[choice], usage=usage)

        def create_fn(**kwargs):
            return resp

        backend, _ = _build_backend(monkeypatch, create_fn=create_fn)
        with pytest.raises(BrainProviderError, match="no content"):
            backend.complete(
                system="test", user="test",
                schema=_intent_schema(),
            )

    def test_sdk_error_sanitized(self, monkeypatch):
        def create_fn(**kwargs):
            raise RuntimeError(f"auth failed with key {_FINGERPRINT}")

        backend, _ = _build_backend(monkeypatch, create_fn=create_fn)
        with pytest.raises(BrainProviderError) as exc_info:
            backend.complete(
                system="test", user="test",
                schema=_intent_schema(),
            )
        assert _FINGERPRINT not in str(exc_info.value)

    def test_auditor_called(self, monkeypatch):
        intent = _valid_intent()

        def create_fn(**kwargs):
            return _make_response(intent)

        backend, _ = _build_backend(monkeypatch, create_fn=create_fn)
        assert backend._auditor.call_count == 0
        backend.complete(
            system="test", user="test",
            schema=_intent_schema(),
        )
        assert backend._auditor.call_count >= 1

    def test_auditor_call_count_increments(self, monkeypatch):
        intent = _valid_intent()

        def create_fn(**kwargs):
            return _make_response(intent)

        backend, _ = _build_backend(monkeypatch, create_fn=create_fn)
        before = backend._auditor.call_count
        backend.complete(
            system="test", user="test",
            schema=_intent_schema(),
        )
        assert backend._auditor.call_count == before + 1


# ── Cost estimation ──────────────────────────────────────────────────────────


class TestCost:
    def test_cost_estimate(self, monkeypatch):
        intent = _valid_intent()

        def create_fn(**kwargs):
            return _make_response(intent, prompt_tokens=1000, completion_tokens=500)

        backend, _ = _build_backend(monkeypatch, create_fn=create_fn)
        result = backend.complete(
            system="test", user="test",
            schema=_intent_schema(),
        )
        assert result.cost_usd is not None
        expected = (1000 / 1_000_000) * 0.40 + (500 / 1_000_000) * 1.60
        assert abs(result.cost_usd - expected) < 1e-9


# ── Schema transform ────────────────────────────────────────────────────────


class TestSchemaTransform:
    def test_oneof_preserved(self, monkeypatch):
        captured = {}
        intent = _valid_intent()

        def create_fn(**kwargs):
            captured.update(kwargs)
            return _make_response(intent)

        backend, _ = _build_backend(monkeypatch, create_fn=create_fn)
        schema = _intent_schema()
        backend.complete(system="test", user="test", schema=schema)
        wire = captured["response_format"]["json_schema"]["schema"]
        assert "anyOf" not in json.dumps(wire)

    def test_additional_properties_injected(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "nested": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                    },
                },
            },
        }
        result = _inject_additional_properties_false(schema)
        assert result["additionalProperties"] is False
        assert result["properties"]["nested"]["additionalProperties"] is False

    def test_existing_additional_properties_preserved(self):
        schema = {
            "type": "object",
            "additionalProperties": True,
            "properties": {"a": {"type": "string"}},
        }
        result = _inject_additional_properties_false(schema)
        assert result["additionalProperties"] is True

    def test_non_object_not_modified(self):
        schema = {"type": "string", "minLength": 1}
        result = _inject_additional_properties_false(schema)
        assert "additionalProperties" not in result

    def test_meta_keywords_stripped(self, monkeypatch):
        captured = {}
        intent = _valid_intent()

        def create_fn(**kwargs):
            captured.update(kwargs)
            return _make_response(intent)

        backend, _ = _build_backend(monkeypatch, create_fn=create_fn)
        schema = _intent_schema()
        schema["$schema"] = "http://json-schema.org/draft-07/schema#"
        schema["title"] = "test"
        backend.complete(system="test", user="test", schema=schema)
        wire = captured["response_format"]["json_schema"]["schema"]
        assert "$schema" not in wire
        assert "title" not in wire


# ── Streaming tests ──────────────────────────────────────────────────────────


class TestStreaming:
    def test_stream_yields_chunks(self, monkeypatch):
        intent = _valid_intent()
        chunks = _make_stream_chunks(intent)

        def create_fn(**kwargs):
            if kwargs.get("stream"):
                return iter(chunks)
            return _make_response(intent)

        backend, mock_client = _build_backend(monkeypatch)
        mock_client.chat.completions.create.side_effect = create_fn

        results = list(backend.stream_complete(
            system="test", user="test",
            schema=_intent_schema(),
        ))
        assert len(results) > 0
        _, accumulated, is_final = results[-1]
        assert is_final

    def test_stream_auditor_called(self, monkeypatch):
        intent = _valid_intent()
        chunks = _make_stream_chunks(intent)

        def create_fn(**kwargs):
            if kwargs.get("stream"):
                return iter(chunks)
            return _make_response(intent)

        backend, mock_client = _build_backend(monkeypatch)
        mock_client.chat.completions.create.side_effect = create_fn

        list(backend.stream_complete(
            system="test", user="test",
            schema=_intent_schema(),
        ))
        assert backend._auditor.call_count >= 1

    def test_stream_sdk_error_raises(self, monkeypatch):
        def create_fn(**kwargs):
            raise RuntimeError("stream failed")

        backend, mock_client = _build_backend(monkeypatch)
        mock_client.chat.completions.create.side_effect = create_fn

        with pytest.raises(BrainProviderError):
            list(backend.stream_complete(
                system="test", user="test",
                schema=_intent_schema(),
            ))


# ── Slots enforcement ────────────────────────────────────────────────────────


class TestSlots:
    def test_has_slots(self):
        assert hasattr(OpenAIBackend, "__slots__")

    def test_no_dict(self, monkeypatch):
        backend, _ = _build_backend(monkeypatch)
        assert not hasattr(backend, "__dict__")
