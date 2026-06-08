"""Cross-backend smoke test (test 42 from M2.6/M2.7 plan).

Foreshadows M2.13's G10 parity gate: given identical valid input,
both API backends produce ``BrainResponse`` objects with identical
``content_json``. M2.13 owns the full 20-payload corpus; this is just
the shape-mechanics check.
"""

from __future__ import annotations

import json
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller.backends import BrainResponse, SecretRef


def _intent_schema() -> dict:
    path = Path(__file__).parent.parent / "schemas" / "intent.json"
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _valid_intent() -> dict:
    return {
        "intent_id": str(uuid.uuid4()),
        "action": "system.disk",
        "target": "",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }


class _Cfg:
    def __init__(self, *, env: str, model: str):
        self.model = model
        self.max_tokens = 1024
        self.timeout_seconds = 30
        self.api_key = SecretRef(env)


def test_anthropic_and_gemini_emit_same_brainresponse_shape(monkeypatch):
    """Same valid intent JSON → both backends return BrainResponse with
    identical content_json and ``attempts == 1``."""
    intent = _valid_intent()
    schema = _intent_schema()

    # ---- Anthropic side ----------------------------------------------------
    monkeypatch.setenv("ICEBREAKER_X_ANTH", "sk-ant-dummy-x")

    class _FakeAnthMessages:
        def create(self, **kwargs):
            block = SimpleNamespace(type="text", text=json.dumps(intent))
            return SimpleNamespace(
                content=[block],
                usage=SimpleNamespace(input_tokens=5, output_tokens=15),
                stop_reason="end_turn",
            )

    class _FakeAnthClient:
        def __init__(self, **kw):
            self.messages = _FakeAnthMessages()

    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthClient)

    from controller.backends.anthropic_backend import AnthropicBackend
    a_cfg = _Cfg(env="ICEBREAKER_X_ANTH", model="claude-haiku-4-5")
    a_backend = AnthropicBackend(a_cfg)
    a_resp = a_backend.complete("sys", "check disk", schema)

    # ---- Gemini side -------------------------------------------------------
    monkeypatch.setenv("ICEBREAKER_X_GEM", "AIzaDUMMY-cross-backend-XXXX1234567")

    class _FakeModel:
        def __init__(self, name, *, system_instruction=""):
            pass

        def generate_content(self, prompt, *, generation_config=None,
                             request_options=None):
            return SimpleNamespace(
                text=json.dumps(intent),
                usage_metadata=SimpleNamespace(
                    prompt_token_count=5, candidates_token_count=15
                ),
            )

    fake_genai = types.ModuleType("google.generativeai")
    fake_genai.configure = lambda *, api_key: None  # type: ignore[attr-defined]
    fake_genai.GenerativeModel = _FakeModel  # type: ignore[attr-defined]
    google_ns = types.ModuleType("google")
    google_ns.generativeai = fake_genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_ns)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_genai)

    from controller.backends.gemini_backend import GeminiBackend
    g_cfg = _Cfg(env="ICEBREAKER_X_GEM", model="gemini-2.0-flash")
    g_backend = GeminiBackend(g_cfg)
    g_resp = g_backend.complete("sys", "check disk", schema)

    # ---- Assert structural parity ------------------------------------------
    assert isinstance(a_resp, BrainResponse)
    assert isinstance(g_resp, BrainResponse)
    assert a_resp.content_json == g_resp.content_json
    assert a_resp.attempts == 1
    assert g_resp.attempts == 1
    assert a_resp.backend == "anthropic"
    assert g_resp.backend == "gemini"
