"""v6.8 N.2.a — subclass-behavior tests for the three LiteLLM-backed QB backends.

The heavy lifting (LiteLLM transport, exception mapping, streaming
tally, INV-1 tools-exclusion) lives in tests/test_litellm_shared.py.
This file covers the per-subclass surface:

- Model_id prefix (gemini/anthropic/openai) so LiteLLM routes correctly.
- __slots__ still empty (attribute-injection lockdown).
- _call_provider + _stream_provider delegate to _litellm_shared with
  the right kwargs (auditor passed, api_key resolved from SecretRef,
  response_format set).
- Cost estimation returns non-None float for API backends.
- BrainProviderError raised when envelope.schema is None.
- Constructor: refuses mcpd_client (INV-1 sentinel).
- Backend registry lookup (gemini / anthropic / openai names present).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from controller.backends.base import (
    BrainProviderError,
    BrainSecurityError,
    RequestEnvelope,
)
from controller.backends.registry import _REGISTRY


# ── Fixtures ────────────────────────────────────────────────────────────

def _api_key_ref(value: str = "sk-test-key"):
    """Match the SecretRef .reveal() shape used by _ApiBackend."""
    ref = MagicMock()
    ref.reveal.return_value = value
    return ref


def _config(model: str = "test-model") -> SimpleNamespace:
    return SimpleNamespace(
        name="",  # subclass sets backend_name
        model=model,
        api_key=_api_key_ref(),
        max_tokens=256,
        timeout_seconds=30.0,
    )


def _envelope() -> RequestEnvelope:
    return RequestEnvelope(
        system="sys",
        user="user",
        schema={"type": "object", "properties": {}},
        sampling={"temperature": 0.0, "top_p": 1.0},
        tools_disabled=True,
    )


# ── Registry / import wiring ───────────────────────────────────────────

def test_gemini_registered():
    from controller.backends import gemini_backend  # noqa: F401
    assert "gemini" in _REGISTRY


def test_anthropic_registered():
    from controller.backends import anthropic_backend  # noqa: F401
    assert "anthropic" in _REGISTRY


def test_openai_registered():
    from controller.backends import openai_backend  # noqa: F401
    assert "openai" in _REGISTRY


# ── Model_id prefix ────────────────────────────────────────────────────

def test_gemini_model_id_has_gemini_prefix():
    from controller.backends.gemini_backend import GeminiBackend
    b = GeminiBackend(_config("gemini-2.5-flash"))
    assert b._model_id() == "gemini/gemini-2.5-flash"


def test_anthropic_model_id_has_anthropic_prefix():
    from controller.backends.anthropic_backend import AnthropicBackend
    b = AnthropicBackend(_config("claude-sonnet-4-6"))
    assert b._model_id() == "anthropic/claude-sonnet-4-6"


def test_openai_model_id_has_openai_prefix():
    from controller.backends.openai_backend import OpenAIBackend
    b = OpenAIBackend(_config("gpt-5"))
    assert b._model_id() == "openai/gpt-5"


# ── __slots__ lockdown ─────────────────────────────────────────────────

@pytest.mark.parametrize("cls_path", [
    "controller.backends.gemini_backend.GeminiBackend",
    "controller.backends.anthropic_backend.AnthropicBackend",
    "controller.backends.openai_backend.OpenAIBackend",
])
def test_subclass_slots_prevent_attribute_injection(cls_path):
    """D20: adding an attribute at runtime must raise. The base's
    __init_subclass__ check enforces __slots__ presence; here we
    verify it actually works."""
    mod, name = cls_path.rsplit(".", 1)
    __import__(mod)
    import sys
    cls = getattr(sys.modules[mod], name)
    inst = cls(_config())
    with pytest.raises(AttributeError):
        inst.some_random_attr = "leak"


# ── Delegates to LiteLLM shared helper ─────────────────────────────────

def _mock_call_return():
    return ('{"ok": true}', 10, 5)


def _fake_call_with_auditor(**kwargs):
    """Mocks call_via_litellm — must fire the auditor to satisfy base's
    G3 stage-2 probe (BrainBackend.complete checks auditor.call_count)."""
    kwargs["auditor"].intercept({"model": kwargs.get("model", "")})
    return _mock_call_return()


@pytest.mark.parametrize("cls_path,model_prefix", [
    ("controller.backends.gemini_backend.GeminiBackend", "gemini/"),
    ("controller.backends.anthropic_backend.AnthropicBackend", "anthropic/"),
    ("controller.backends.openai_backend.OpenAIBackend", "openai/"),
])
def test_call_provider_delegates_to_call_via_litellm(cls_path, model_prefix):
    mod, name = cls_path.rsplit(".", 1)
    __import__(mod)
    import sys
    cls = getattr(sys.modules[mod], name)
    inst = cls(_config("mymodel"))

    with patch(f"{mod}.call_via_litellm", side_effect=_fake_call_with_auditor) as mock_call:
        text, tin, tout = inst._call_provider(_envelope())

    assert text == '{"ok": true}'
    assert tin == 10 and tout == 5
    kwargs = mock_call.call_args.kwargs
    assert kwargs["model"].startswith(model_prefix)
    assert kwargs["api_key"] == "sk-test-key"
    assert kwargs["max_tokens"] == 256
    assert kwargs["response_format"] == {"type": "json_object"}
    # INV-1 defense: auditor is passed so it fires against the actual
    # LiteLLM kwargs.
    assert kwargs["auditor"] is inst._auditor


@pytest.mark.parametrize("cls_path", [
    "controller.backends.gemini_backend.GeminiBackend",
    "controller.backends.anthropic_backend.AnthropicBackend",
    "controller.backends.openai_backend.OpenAIBackend",
])
def test_stream_provider_delegates_to_stream_via_litellm(cls_path):
    mod, name = cls_path.rsplit(".", 1)
    __import__(mod)
    import sys
    cls = getattr(sys.modules[mod], name)
    inst = cls(_config())

    def _fake_stream(*args, **kwargs):
        yield ("chunk ", 0, 0)
        yield ("", 3, 2)

    with patch(f"{mod}.stream_via_litellm", side_effect=_fake_stream) as mock_stream:
        result = list(inst._stream_provider(_envelope()))

    assert result == [("chunk ", 0, 0), ("", 3, 2)]
    assert mock_stream.call_args.kwargs["auditor"] is inst._auditor


# ── Schema required contract ───────────────────────────────────────────

@pytest.mark.parametrize("cls_path", [
    "controller.backends.gemini_backend.GeminiBackend",
    "controller.backends.anthropic_backend.AnthropicBackend",
    "controller.backends.openai_backend.OpenAIBackend",
])
def test_call_provider_raises_when_schema_none(cls_path):
    mod, name = cls_path.rsplit(".", 1)
    __import__(mod)
    import sys
    cls = getattr(sys.modules[mod], name)
    inst = cls(_config())

    envelope = RequestEnvelope(
        system="", user="", schema=None,
        sampling={"temperature": 0.0, "top_p": 1.0},
    )
    with pytest.raises(BrainProviderError, match="requires a schema"):
        inst._call_provider(envelope)


# ── Cost estimation ────────────────────────────────────────────────────

@pytest.mark.parametrize("cls_path", [
    "controller.backends.gemini_backend.GeminiBackend",
    "controller.backends.anthropic_backend.AnthropicBackend",
    "controller.backends.openai_backend.OpenAIBackend",
])
def test_estimate_cost_returns_positive(cls_path):
    mod, name = cls_path.rsplit(".", 1)
    __import__(mod)
    import sys
    cls = getattr(sys.modules[mod], name)
    inst = cls(_config())
    cost = inst._estimate_cost(tokens_in=1_000_000, tokens_out=1_000_000)
    assert cost is not None
    assert cost > 0


# ── INV-1: constructor refuses mcpd_client ────────────────────────────

@pytest.mark.parametrize("cls_path", [
    "controller.backends.gemini_backend.GeminiBackend",
    "controller.backends.anthropic_backend.AnthropicBackend",
    "controller.backends.openai_backend.OpenAIBackend",
])
def test_constructor_refuses_mcpd_client(cls_path):
    mod, name = cls_path.rsplit(".", 1)
    __import__(mod)
    import sys
    cls = getattr(sys.modules[mod], name)
    # Defense in depth: either BrainSecurityError (base sentinel guard) OR
    # TypeError (subclass doesn't forward kwarg). Both prevent the injection.
    with pytest.raises((BrainSecurityError, TypeError)):
        cls(_config(), mcpd_client=MagicMock())
