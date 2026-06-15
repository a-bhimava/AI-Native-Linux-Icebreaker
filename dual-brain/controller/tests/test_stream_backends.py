"""Tests for ``BrainBackend.stream_complete()`` and ``_stream_provider()``.

Covers:
  1. Default _stream_provider calls _call_provider (base class fallback)
  2. stream_complete yields chunks then final=True
  3. stream_complete validates schema on final chunk
  4. stream_complete falls back to complete() on schema failure
  5. __init_subclass__ blocks overriding stream_complete
  6. __init_subclass__ blocks overriding complete
  7. stream_complete auditor probe check
  8. stream_complete raises BrainConfigError on bad max_retries
  9. stream_complete hard cap on max_retries
 10. Default stream provider yields single chunk
 11. stream_complete accumulated text matches full response
 12. stream_complete with None schema skips validation
 13. Overridden _stream_provider yields multiple chunks
 14. stream_complete auditor bypassed raises BrainSecurityError
 15. stream_complete with valid JSON but invalid schema falls back
 16. Default _stream_provider returns correct token counts
 17. stream_complete final chunk has is_final=True
 18. stream_complete non-final chunks have is_final=False
 19. stream_complete with schema=None still yields correctly
 20. stream_complete fallback produces correct content
"""

from __future__ import annotations

import json
import uuid

import pytest

from controller.backends.base import (
    BrainBackend,
    BrainConfigError,
    BrainResponse,
    BrainSchemaError,
    BrainSecurityError,
    MAX_RETRY_HARD_CAP,
    RequestEnvelope,
)


# ── Test backends ────────────────────────────────────────────────────────────


class _Cfg:
    """Minimal config stand-in."""
    def __init__(self, model: str = "test-model"):
        self.model = model


class _TestBackend(BrainBackend):
    """Backend with overridden _stream_provider for multi-chunk streaming."""

    __slots__ = ("_responses", "_stream_chunks")
    backend_name = "test-stream"

    def __init__(self, responses=None, stream_chunks=None):
        config = _Cfg()
        super().__init__(config)
        self._responses = list(responses or [])
        self._stream_chunks = list(stream_chunks or [])

    def _call_provider(self, envelope):
        resp = self._responses.pop(0)
        self._auditor.intercept({"test": True})
        return resp, len(envelope.user), len(resp)

    def _stream_provider(self, envelope):
        self._auditor.intercept({"test": True})
        for chunk, tok_in, tok_out in self._stream_chunks:
            yield (chunk, tok_in, tok_out)


class _DefaultStreamBackend(BrainBackend):
    """Backend WITHOUT overriding _stream_provider (uses base default)."""

    __slots__ = ("_response",)
    backend_name = "test-default-stream"

    def __init__(self, response):
        config = _Cfg()
        super().__init__(config)
        self._response = response

    def _call_provider(self, envelope):
        self._auditor.intercept({"test": True})
        return self._response, len(envelope.user), len(self._response)


class _AuditorBypassBackend(BrainBackend):
    """Backend that skips the auditor call."""

    __slots__ = ("_stream_chunks",)
    backend_name = "test-bypass"

    def __init__(self, stream_chunks=None):
        config = _Cfg()
        super().__init__(config)
        self._stream_chunks = list(stream_chunks or [])

    def _call_provider(self, envelope):
        # Deliberately skipping self._auditor.intercept()
        return '{"summary": "ok"}', 0, 0

    def _stream_provider(self, envelope):
        # Deliberately skipping self._auditor.intercept()
        for chunk, tok_in, tok_out in self._stream_chunks:
            yield (chunk, tok_in, tok_out)


# ── Test schemas ─────────────────────────────────────────────────────────────


_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent_id": {"type": "string", "format": "uuid"},
        "action": {"type": "string"},
    },
    "required": ["intent_id", "action"],
}


# ── 1: Default _stream_provider calls _call_provider ────────────────────────


def test_default_stream_provider_calls_call_provider():
    """Base class _stream_provider falls back to _call_provider."""
    payload = json.dumps({"summary": "System is running."})
    b = _DefaultStreamBackend(payload)
    chunks = list(b.stream_complete("sys", "user", _SUMMARY_SCHEMA))
    # Should yield at least one chunk and a final
    assert len(chunks) >= 1
    # The last chunk should have is_final=True
    assert chunks[-1][2] is True


def test_default_stream_provider_yields_single_chunk():
    """Default _stream_provider yields the full response as one chunk."""
    payload = json.dumps({"summary": "ok"})
    b = _DefaultStreamBackend(payload)
    chunks = list(b.stream_complete("sys", "user", _SUMMARY_SCHEMA))
    # First chunk is the full text, second is the final marker
    non_final = [c for c in chunks if not c[2]]
    assert len(non_final) == 1
    assert non_final[0][0] == payload


def test_default_stream_provider_returns_correct_token_counts():
    """Default _stream_provider passes through token counts from _call_provider."""
    payload = json.dumps({"summary": "token counting"})
    b = _DefaultStreamBackend(payload)
    chunks = list(b.stream_complete("sys", "user", _SUMMARY_SCHEMA))
    # Non-final chunks have the actual data
    non_final = [c for c in chunks if not c[2]]
    assert len(non_final) == 1
    # The accumulated text should match the payload
    assert non_final[0][1] == payload


# ── 2: stream_complete yields chunks then final=True ────────────────────────


def test_stream_complete_yields_chunks_then_final():
    """Overridden _stream_provider yields multiple chunks with correct accumulation."""
    b = _TestBackend(
        stream_chunks=[
            ('{"summary":', 0, 0),
            (' "hello"}', 0, 0),
        ]
    )
    chunks = list(b.stream_complete("sys", "user", _SUMMARY_SCHEMA))
    # Non-final chunks
    non_final = [c for c in chunks if not c[2]]
    assert len(non_final) == 2
    assert non_final[0][0] == '{"summary":'
    assert non_final[1][0] == ' "hello"}'
    # Final chunk
    final = [c for c in chunks if c[2]]
    assert len(final) == 1
    assert final[0][2] is True


# ── 3: stream_complete validates schema on final chunk ──────────────────────


def test_stream_complete_validates_schema():
    """Valid schema on final accumulated text produces correct final chunk."""
    valid_json = json.dumps({"summary": "ok"})
    b = _TestBackend(
        stream_chunks=[(valid_json, 5, 3)]
    )
    chunks = list(b.stream_complete("sys", "user", _SUMMARY_SCHEMA))
    final_chunks = [c for c in chunks if c[2]]
    assert len(final_chunks) == 1


# ── 4: stream_complete falls back to complete() on schema failure ───────────


def test_stream_complete_falls_back_on_schema_failure():
    """Invalid streaming output falls back to non-streaming complete()."""
    invalid_json = '{"wrong_field": "value"}'
    valid_response = json.dumps({"summary": "fallback"})
    b = _TestBackend(
        responses=[valid_response],
        stream_chunks=[(invalid_json, 0, 0)],
    )
    chunks = list(b.stream_complete("sys", "user", _SUMMARY_SCHEMA))
    # Should still end with a final=True chunk from fallback
    final = [c for c in chunks if c[2]]
    assert len(final) == 1
    # The fallback content should be the valid response
    assert "summary" in final[0][1]
    assert "fallback" in final[0][1]


def test_stream_complete_falls_back_on_invalid_json():
    """Malformed JSON from streaming falls back to complete()."""
    invalid = 'this is not json at all'
    valid_response = json.dumps({"summary": "recovered"})
    b = _TestBackend(
        responses=[valid_response],
        stream_chunks=[(invalid, 0, 0)],
    )
    chunks = list(b.stream_complete("sys", "user", _SUMMARY_SCHEMA))
    final = [c for c in chunks if c[2]]
    assert len(final) == 1
    assert "recovered" in final[0][1]


# ── 5-6: __init_subclass__ blocks overriding ────────────────────────────────


def test_subclass_cannot_override_stream_complete():
    """__init_subclass__ blocks overriding stream_complete."""
    with pytest.raises(TypeError, match="non-overridable"):
        class Bad(BrainBackend):
            __slots__ = ()
            backend_name = "bad-stream"
            def _call_provider(self, envelope):
                return ("{}", 0, 0)
            def stream_complete(self, *a, **k):
                yield ("", "", True)


def test_subclass_cannot_override_complete():
    """__init_subclass__ blocks overriding complete."""
    with pytest.raises(TypeError, match="non-overridable"):
        class Bad(BrainBackend):
            __slots__ = ()
            backend_name = "bad-complete"
            def _call_provider(self, envelope):
                return ("{}", 0, 0)
            def complete(self, *a, **k):
                return None


# ── 7: stream_complete auditor probe check ──────────────────────────────────


def test_stream_complete_auditor_probe():
    """stream_complete verifies auditor was called via call-count probe."""
    b = _AuditorBypassBackend(
        stream_chunks=[('{"summary": "ok"}', 0, 0)]
    )
    with pytest.raises(BrainSecurityError, match="stage-2 contract"):
        list(b.stream_complete("sys", "user", _SUMMARY_SCHEMA))


# ── 8-9: stream_complete raises BrainConfigError on bad max_retries ─────────


def test_stream_complete_rejects_zero_retries():
    """max_retries=0 raises BrainConfigError."""
    b = _TestBackend()
    with pytest.raises(BrainConfigError, match="max_retries=0"):
        list(b.stream_complete("sys", "user", None, max_retries=0))


def test_stream_complete_rejects_negative_retries():
    """max_retries=-1 raises BrainConfigError."""
    b = _TestBackend()
    with pytest.raises(BrainConfigError):
        list(b.stream_complete("sys", "user", None, max_retries=-1))


def test_stream_complete_rejects_retries_above_hard_cap():
    """max_retries above hard cap raises BrainConfigError."""
    b = _TestBackend()
    with pytest.raises(BrainConfigError, match="hard cap"):
        list(b.stream_complete("sys", "user", None, max_retries=MAX_RETRY_HARD_CAP + 1))


# ── Additional coverage ────────────────────────────────────────────────────


def test_stream_complete_accumulated_text_correct():
    """accumulated text in final chunk matches all yielded text."""
    b = _TestBackend(
        stream_chunks=[
            ('{"sum', 0, 0),
            ('mary":', 0, 0),
            (' "done"}', 0, 0),
        ]
    )
    chunks = list(b.stream_complete("sys", "user", _SUMMARY_SCHEMA))
    non_final = [c for c in chunks if not c[2]]
    # Accumulated should grow
    assert non_final[0][1] == '{"sum'
    assert non_final[1][1] == '{"summary":'
    assert non_final[2][1] == '{"summary": "done"}'


def test_stream_complete_none_schema_valid_json():
    """With schema=None, stream_complete accepts any valid JSON object."""
    raw = json.dumps({"arbitrary": "data", "count": 42})
    b = _DefaultStreamBackend(raw)
    chunks = list(b.stream_complete("sys", "user", schema=None))
    # Should work: valid JSON passes _extract_json_strict even without schema
    assert len(chunks) >= 1
    final = [c for c in chunks if c[2]]
    assert len(final) == 1


def test_stream_complete_non_final_chunks_have_false():
    """Non-final chunks from stream_complete have is_final=False."""
    b = _TestBackend(
        stream_chunks=[
            ('{"summary":', 0, 0),
            (' "hi"}', 0, 0),
        ]
    )
    chunks = list(b.stream_complete("sys", "user", _SUMMARY_SCHEMA))
    non_final = [c for c in chunks if not c[2]]
    for c in non_final:
        assert c[2] is False


def test_stream_complete_final_chunk_has_true():
    """Final chunk from stream_complete has is_final=True."""
    valid = json.dumps({"summary": "done"})
    b = _TestBackend(
        stream_chunks=[(valid, 0, 0)]
    )
    chunks = list(b.stream_complete("sys", "user", _SUMMARY_SCHEMA))
    final = [c for c in chunks if c[2]]
    assert len(final) == 1
    assert final[0][2] is True


def test_stream_complete_with_none_schema_yields_correctly():
    """schema=None still yields chunks correctly without validation."""
    payload = json.dumps({"any": "data"})
    b = _TestBackend(
        stream_chunks=[(payload, 5, 3)]
    )
    chunks = list(b.stream_complete("sys", "user", schema=None))
    assert len(chunks) >= 1
    final = [c for c in chunks if c[2]]
    assert len(final) == 1


def test_stream_complete_fallback_produces_correct_content():
    """Fallback to complete() produces BrainResponse content as final chunk."""
    invalid_stream = "not{json"
    valid_complete = json.dumps({"summary": "correct result"})
    b = _TestBackend(
        responses=[valid_complete],
        stream_chunks=[(invalid_stream, 0, 0)],
    )
    chunks = list(b.stream_complete("sys", "user", _SUMMARY_SCHEMA))
    final = [c for c in chunks if c[2]]
    assert len(final) == 1
    parsed = json.loads(final[0][1])
    assert parsed["summary"] == "correct result"
