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
    identical content_json and ``attempts == 1``.

    v6.8 N.2.a: both backends now route through LiteLLM. The parity
    check is now: does the LiteLLM adapter's shape survive intact for
    each provider? Mock at the _litellm_shared level (one boundary,
    one mock, one guaranteed shape)."""
    intent = _valid_intent()
    schema = _intent_schema()

    fake_return = (json.dumps(intent), 5, 15)

    monkeypatch.setenv("ICEBREAKER_X_ANTH", "sk-ant-dummy-x")  # pragma: allowlist secret
    monkeypatch.setenv("ICEBREAKER_X_GEM", "AIzaDUMMY-cross-backend-XXXX1234567")

    from unittest.mock import patch
    from controller.backends.anthropic_backend import AnthropicBackend
    from controller.backends.gemini_backend import GeminiBackend

    def _fake_call(**kwargs):
        # Real _litellm_shared.call_via_litellm fires the auditor. Mock
        # must do the same or the base's G3 stage-2 probe raises.
        kwargs["auditor"].intercept({"model": kwargs.get("model", "")})
        return fake_return

    # ---- Anthropic side ----------------------------------------------------
    a_cfg = _Cfg(env="ICEBREAKER_X_ANTH", model="claude-haiku-4-5")
    a_backend = AnthropicBackend(a_cfg)
    with patch(
        "controller.backends.anthropic_backend.call_via_litellm",
        side_effect=_fake_call,
    ):
        a_resp = a_backend.complete("sys", "check disk", schema)

    # ---- Gemini side -------------------------------------------------------
    g_cfg = _Cfg(env="ICEBREAKER_X_GEM", model="gemini-2.0-flash")
    g_backend = GeminiBackend(g_cfg)
    with patch(
        "controller.backends.gemini_backend.call_via_litellm",
        side_effect=_fake_call,
    ):
        g_resp = g_backend.complete("sys", "check disk", schema)

    # ---- Assert structural parity ------------------------------------------
    assert isinstance(a_resp, BrainResponse)
    assert isinstance(g_resp, BrainResponse)
    assert a_resp.content_json == g_resp.content_json
    assert a_resp.attempts == 1
    assert g_resp.attempts == 1
    assert a_resp.backend == "anthropic"
    assert g_resp.backend == "gemini"
