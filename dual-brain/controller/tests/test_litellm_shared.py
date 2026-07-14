"""v6.8 N.2.a — LiteLLM shared transport regression lock.

Covers:
- Non-streaming success path (call_via_litellm returns text + token counts)
- Streaming success path (chunk sequence + final usage tally)
- Retry semantics:
  * RateLimitError: retried with backoff, then wrapped
  * Timeout: retried once, then wrapped
  * ContextWindowExceededError: raised as BrainTruncationError immediately
  * AuthenticationError: raised as BrainProviderError, no retry
  * ContentPolicyViolationError: raised with "content_policy" marker
  * Unclassified: raised with "unclassified" marker
- No `tools` kwarg is ever built into the LiteLLM call (INV-1 defense)

The tests patch litellm.completion at the module level in _litellm_shared,
so the real network is never touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import litellm

from controller.backends import _litellm_shared
from controller.backends._litellm_shared import call_via_litellm, stream_via_litellm
from controller.backends.base import (
    BrainProviderError,
    BrainTruncationError,
    RequestEnvelope,
)


# ── Fixtures ────────────────────────────────────────────────────────────

def _envelope(system: str = "sys", user: str = "user") -> RequestEnvelope:
    return RequestEnvelope(
        system=system,
        user=user,
        schema=None,
        sampling={"temperature": 0.0, "top_p": 1.0},
        tools_disabled=True,
    )


def _mock_response(text: str = '{"ok": true}', in_tok: int = 10, out_tok: int = 5) -> MagicMock:
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message = MagicMock()
    r.choices[0].message.content = text
    r.usage = MagicMock()
    r.usage.prompt_tokens = in_tok
    r.usage.completion_tokens = out_tok
    return r


def _mock_stream_chunk(text: str | None = None, prompt_tok: int = 0, completion_tok: int = 0):
    c = MagicMock()
    if text is None:
        # Final usage-only chunk.
        c.choices = []
        c.usage = MagicMock()
        c.usage.prompt_tokens = prompt_tok
        c.usage.completion_tokens = completion_tok
    else:
        c.choices = [MagicMock()]
        c.choices[0].delta = MagicMock()
        c.choices[0].delta.content = text
        c.usage = None
    return c


# ── Non-streaming: success ─────────────────────────────────────────────

def test_call_via_litellm_returns_text_and_tokens():
    with patch.object(_litellm_shared.litellm, "completion", return_value=_mock_response()):
        text, tin, tout = call_via_litellm(
            model="gemini/gemini-2.5-flash",
            envelope=_envelope(),
            max_tokens=100,
            timeout_seconds=10.0,
            api_key="test-key",
        )
    assert text == '{"ok": true}'
    assert tin == 10
    assert tout == 5


def test_call_via_litellm_never_sends_top_p():
    """v6.8 2026-07-13 regression lock: Anthropic Claude 4.5+ rejects
    requests that specify BOTH temperature and top_p. Since Icebreaker
    uses temperature-based sampling decay, top_p is redundant. Make
    sure a future refactor doesn't accidentally reintroduce top_p in
    the LiteLLM kwargs — that would break every Anthropic QB turn."""
    with patch.object(_litellm_shared.litellm, "completion") as mock_comp:
        mock_comp.return_value = _mock_response()
        call_via_litellm(
            model="anthropic/claude-haiku-4-5",
            envelope=_envelope(),
            max_tokens=100,
            timeout_seconds=10.0,
            api_key="key",
        )
    kwargs = mock_comp.call_args.kwargs
    assert "top_p" not in kwargs, (
        "top_p in LiteLLM kwargs will break Anthropic Claude 4.5+"
    )
    assert "temperature" in kwargs  # temperature is still fine


def test_call_via_litellm_never_sends_tools_kwarg():
    """INV-1: the `tools` kwarg must never be built into the LiteLLM
    completion call. Assert on kwargs seen by the mock."""
    with patch.object(_litellm_shared.litellm, "completion") as mock_comp:
        mock_comp.return_value = _mock_response()
        call_via_litellm(
            model="anthropic/claude-sonnet-4-6",
            envelope=_envelope(),
            max_tokens=100,
            timeout_seconds=10.0,
            api_key="key",
        )
    kwargs = mock_comp.call_args.kwargs
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs
    assert "functions" not in kwargs
    assert kwargs["stream"] is False


def test_call_via_litellm_forwards_response_format():
    with patch.object(_litellm_shared.litellm, "completion") as mock_comp:
        mock_comp.return_value = _mock_response()
        call_via_litellm(
            model="openai/gpt-5",
            envelope=_envelope(),
            max_tokens=100,
            timeout_seconds=10.0,
            api_key="key",
            response_format={"type": "json_object"},
        )
    assert mock_comp.call_args.kwargs["response_format"] == {"type": "json_object"}


# ── Non-streaming: retries + terminal errors ──────────────────────────

def test_rate_limit_retries_then_wraps():
    """First 2 calls raise RateLimit; the 3rd succeeds."""
    responses = [litellm.RateLimitError("rl", model="x", llm_provider="y"),
                 litellm.RateLimitError("rl", model="x", llm_provider="y"),
                 _mock_response()]
    with patch.object(_litellm_shared.litellm, "completion", side_effect=responses):
        with patch.object(_litellm_shared.time, "sleep"):  # skip real sleeps
            text, _, _ = call_via_litellm(
                model="gemini/x", envelope=_envelope(),
                max_tokens=100, timeout_seconds=10.0, api_key="k",
                retry_count=2,
            )
    assert text == '{"ok": true}'


def test_rate_limit_exhausted_raises_provider_error():
    """After retry_count attempts the RateLimit gets wrapped."""
    err = litellm.RateLimitError("rl", model="x", llm_provider="y")
    with patch.object(_litellm_shared.litellm, "completion", side_effect=[err, err, err]):
        with patch.object(_litellm_shared.time, "sleep"):
            with pytest.raises(BrainProviderError, match="rate_limit_exhausted"):
                call_via_litellm(
                    model="gemini/x", envelope=_envelope(),
                    max_tokens=100, timeout_seconds=10.0, api_key="k",
                    retry_count=2,
                )


def test_context_window_maps_to_truncation_error():
    err = litellm.ContextWindowExceededError("too big", model="x", llm_provider="y")
    with patch.object(_litellm_shared.litellm, "completion", side_effect=err):
        with pytest.raises(BrainTruncationError):
            call_via_litellm(
                model="gemini/x", envelope=_envelope(),
                max_tokens=100, timeout_seconds=10.0, api_key="k",
            )


def test_authentication_error_no_retry_raises_provider_error():
    err = litellm.AuthenticationError("bad key", model="x", llm_provider="y")
    with patch.object(_litellm_shared.litellm, "completion", side_effect=err) as mock_comp:
        with pytest.raises(BrainProviderError):
            call_via_litellm(
                model="gemini/x", envelope=_envelope(),
                max_tokens=100, timeout_seconds=10.0, api_key="k",
                retry_count=2,
            )
    assert mock_comp.call_count == 1


def test_content_policy_gets_marker():
    err = litellm.ContentPolicyViolationError("nope", model="x", llm_provider="y")
    with patch.object(_litellm_shared.litellm, "completion", side_effect=err):
        with pytest.raises(BrainProviderError, match="content_policy"):
            call_via_litellm(
                model="gemini/x", envelope=_envelope(),
                max_tokens=100, timeout_seconds=10.0, api_key="k",
            )


def test_timeout_retries_once():
    err = litellm.Timeout(message="t", model="x", llm_provider="y")
    responses = [err, _mock_response()]
    with patch.object(_litellm_shared.litellm, "completion", side_effect=responses):
        with patch.object(_litellm_shared.time, "sleep"):
            text, _, _ = call_via_litellm(
                model="gemini/x", envelope=_envelope(),
                max_tokens=100, timeout_seconds=10.0, api_key="k",
                retry_count=1,
            )
    assert text == '{"ok": true}'


def test_unclassified_exception_marker():
    """A completely-unknown exception gets 'unclassified' marker so we
    can spot it in audit logs. Belt-and-braces for future LiteLLM
    exception additions."""
    class SomeNewError(Exception):
        pass

    with patch.object(_litellm_shared.litellm, "completion", side_effect=SomeNewError("mystery")):
        with pytest.raises(BrainProviderError, match="unclassified"):
            call_via_litellm(
                model="gemini/x", envelope=_envelope(),
                max_tokens=100, timeout_seconds=10.0, api_key="k",
            )


def test_empty_content_raises_provider_error():
    resp = _mock_response(text="")
    with patch.object(_litellm_shared.litellm, "completion", return_value=resp):
        with pytest.raises(BrainProviderError, match="empty"):
            call_via_litellm(
                model="gemini/x", envelope=_envelope(),
                max_tokens=100, timeout_seconds=10.0, api_key="k",
            )


def test_malformed_response_gets_marker():
    """Response missing .choices or .message.content — wrap with marker."""
    resp = MagicMock()
    resp.choices = []  # empty list
    with patch.object(_litellm_shared.litellm, "completion", return_value=resp):
        with pytest.raises(BrainProviderError, match="malformed_response"):
            call_via_litellm(
                model="gemini/x", envelope=_envelope(),
                max_tokens=100, timeout_seconds=10.0, api_key="k",
            )


# ── Streaming: success ────────────────────────────────────────────────

def test_stream_via_litellm_yields_chunks_then_usage():
    chunks = [
        _mock_stream_chunk("hello "),
        _mock_stream_chunk("world"),
        _mock_stream_chunk(text=None, prompt_tok=8, completion_tok=3),
    ]
    with patch.object(_litellm_shared.litellm, "completion", return_value=iter(chunks)):
        results = list(stream_via_litellm(
            model="anthropic/claude-sonnet-4-6",
            envelope=_envelope(),
            max_tokens=100,
            timeout_seconds=10.0,
            api_key="k",
        ))

    assert results == [("hello ", 0, 0), ("world", 0, 0), ("", 8, 3)]


def test_stream_stream_option_passes_include_usage_true():
    chunks = [_mock_stream_chunk("x"), _mock_stream_chunk(text=None, prompt_tok=1, completion_tok=1)]
    with patch.object(_litellm_shared.litellm, "completion") as mock_comp:
        mock_comp.return_value = iter(chunks)
        list(stream_via_litellm(
            model="openai/gpt-5",
            envelope=_envelope(),
            max_tokens=100, timeout_seconds=10.0, api_key="k",
        ))
    assert mock_comp.call_args.kwargs["stream"] is True
    assert mock_comp.call_args.kwargs["stream_options"] == {"include_usage": True}


def test_stream_produces_no_content_raises_provider_error():
    """Stream returns only a usage chunk — malformed provider response."""
    with patch.object(
        _litellm_shared.litellm, "completion",
        return_value=iter([_mock_stream_chunk(text=None, prompt_tok=1, completion_tok=0)]),
    ):
        with pytest.raises(BrainProviderError, match="no content"):
            list(stream_via_litellm(
                model="gemini/x", envelope=_envelope(),
                max_tokens=100, timeout_seconds=10.0, api_key="k",
            ))


def test_stream_context_window_maps_to_truncation():
    err = litellm.ContextWindowExceededError("too big", model="x", llm_provider="y")
    with patch.object(_litellm_shared.litellm, "completion", side_effect=err):
        with pytest.raises(BrainTruncationError):
            list(stream_via_litellm(
                model="gemini/x", envelope=_envelope(),
                max_tokens=100, timeout_seconds=10.0, api_key="k",
            ))


def test_stream_mid_stream_error_wraps():
    """Chunk iteration blows up midway — wrap with marker."""

    def _generator():
        yield _mock_stream_chunk("first ")
        raise RuntimeError("connection reset")

    with patch.object(_litellm_shared.litellm, "completion", return_value=_generator()):
        with pytest.raises(BrainProviderError, match="mid_stream_error"):
            list(stream_via_litellm(
                model="gemini/x", envelope=_envelope(),
                max_tokens=100, timeout_seconds=10.0, api_key="k",
            ))
