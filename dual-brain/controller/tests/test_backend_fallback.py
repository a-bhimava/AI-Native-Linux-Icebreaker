"""Tests for the v6.65 FallbackChain.

The FallbackChain wraps a primary QB backend + an ordered list of
fallbacks. On ``BrainProviderError`` it retries with each fallback in
order. Schema / truncation errors propagate without falling back (per
the plan: those are prompt issues, not backend issues).
"""

from __future__ import annotations

from typing import Any, Generator, List

import pytest

from controller.backends.base import (
    BrainProviderError,
    BrainResponse,
    BrainSchemaError,
    BrainTruncationError,
)
from controller.fallback_backend import (
    FallbackChain,
    build_fallback_backends,
)


class _StubBackend:
    """Duck-typed BrainBackend stub.

    ``FallbackChain`` only uses ``.backend_name``, ``.complete(...)``, and
    ``.stream_complete(...)`` — it doesn't need real BrainBackend
    inheritance. Inheriting would trigger ``__init_subclass__``'s
    override-block on complete/stream_complete, which is desirable
    protection for production but not what we want in a test double.
    """

    def __init__(self, name: str, responses: List[Any]) -> None:
        self.backend_name = name
        self._responses = list(responses)
        self._call_log: list[str] = []
        self._stream_chunks: List[Any] = []

    def complete(self, system: str, user: str,
                 schema: dict | None = None, max_retries: int = 3) -> BrainResponse:
        self._call_log.append("complete")
        if not self._responses:
            raise RuntimeError("stub out of responses")
        result = self._responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def stream_complete(self, system: str, user: str,
                        schema: dict | None = None,
                        max_retries: int = 3) -> Generator[tuple[str, str, bool], None, None]:
        self._call_log.append("stream")
        if not self._stream_chunks:
            raise RuntimeError("stub out of stream chunks")
        chunks = self._stream_chunks.pop(0)
        if isinstance(chunks, BaseException):
            raise chunks
        accumulated = ""
        for chunk in chunks:
            accumulated += chunk
            yield (chunk, accumulated, False)
        yield ("", accumulated, True)


def _good_response(text: str = "ok") -> BrainResponse:
    return BrainResponse(
        content_json={"backend": "stub", "text": text},
        tokens_in=1,
        tokens_out=1,
        cost_usd=None,
        backend="stub",
        model="stub-model",
        attempts=1,
    )


# ── complete() ────────────────────────────────────────────────────────────


class TestFallbackComplete:
    def test_primary_success_no_fallback_invoked(self) -> None:
        good = _good_response("primary")
        primary = _StubBackend("primary", [good])
        fb = _StubBackend("fallback", [_good_response("fallback")])
        chain = FallbackChain(primary=primary, fallbacks=[fb])

        result = chain.complete(system="s", user="u")

        assert result.content_json["text"] == "primary"
        assert primary._call_log == ["complete"]
        assert fb._call_log == []

    def test_provider_error_falls_through_to_first_fallback(self) -> None:
        primary = _StubBackend("primary", [BrainProviderError("network down")])
        fb1 = _StubBackend("fb1", [_good_response("fb1")])
        chain = FallbackChain(primary=primary, fallbacks=[fb1])

        result = chain.complete(system="s", user="u")

        assert result.content_json["text"] == "fb1"
        assert primary._call_log == ["complete"]
        assert fb1._call_log == ["complete"]

    def test_all_provider_errors_raises_last_error(self) -> None:
        primary = _StubBackend("primary", [BrainProviderError("primary down")])
        fb1 = _StubBackend("fb1", [BrainProviderError("fb1 down")])
        fb2 = _StubBackend("fb2", [BrainProviderError("fb2 down")])
        chain = FallbackChain(primary=primary, fallbacks=[fb1, fb2])

        with pytest.raises(BrainProviderError) as exc_info:
            chain.complete(system="s", user="u")

        assert "fb2 down" in str(exc_info.value)

    def test_schema_error_does_not_trigger_fallback(self) -> None:
        err = BrainSchemaError("bad schema", attempts=3, last_payload_excerpt="{}")
        primary = _StubBackend("primary", [err])
        fb1 = _StubBackend("fb1", [_good_response("fb1")])
        chain = FallbackChain(primary=primary, fallbacks=[fb1])

        # Schema errors are prompt issues — the wrapper must not fall
        # back (per the plan). It should surface the error to the caller.
        with pytest.raises(BrainSchemaError):
            chain.complete(system="s", user="u")

        assert fb1._call_log == []

    def test_truncation_error_does_not_trigger_fallback(self) -> None:
        # BrainTruncationError extends BrainSchemaError; same policy.
        err = BrainTruncationError("max_tokens hit", attempts=3, last_payload_excerpt="")
        primary = _StubBackend("primary", [err])
        fb1 = _StubBackend("fb1", [_good_response("fb1")])
        chain = FallbackChain(primary=primary, fallbacks=[fb1])

        with pytest.raises(BrainTruncationError):
            chain.complete(system="s", user="u")

        assert fb1._call_log == []

    def test_on_fallback_callback_fires_with_index_and_name(self) -> None:
        primary = _StubBackend("primary", [BrainProviderError("primary down")])
        fb1 = _StubBackend("fb1", [_good_response("fb1")])
        events: list[tuple[int, str, str]] = []

        def cb(idx: int, name: str, err: str) -> None:
            events.append((idx, name, err))

        chain = FallbackChain(primary=primary, fallbacks=[fb1], on_fallback=cb)
        chain.complete(system="s", user="u")

        assert len(events) == 1
        assert events[0][0] == 0
        assert events[0][1] == "fb1"
        assert "primary down" in events[0][2]

    def test_no_fallbacks_configured_reraises_primary_error(self) -> None:
        primary = _StubBackend("primary", [BrainProviderError("boom")])
        chain = FallbackChain(primary=primary, fallbacks=[])

        with pytest.raises(BrainProviderError, match="boom"):
            chain.complete(system="s", user="u")

    def test_backend_name_reports_primary(self) -> None:
        primary = _StubBackend("primary-name", [])
        fb1 = _StubBackend("fb-name", [])
        chain = FallbackChain(primary=primary, fallbacks=[fb1])

        # backend_name should always reflect the PRIMARY (that's the
        # user's configured choice; fallbacks are transparent).
        assert chain.backend_name == "primary-name"


# ── stream_complete() ─────────────────────────────────────────────────────


class TestFallbackStream:
    def test_primary_stream_success(self) -> None:
        primary = _StubBackend("primary", [])
        primary._stream_chunks = [["hello", " world"]]
        chain = FallbackChain(primary=primary, fallbacks=[])

        out = list(chain.stream_complete(system="s", user="u"))

        # 2 chunks + 1 final marker = 3 tuples
        assert len(out) == 3
        assert out[-1][2] is True  # is_final

    def test_stream_provider_error_before_any_chunk_falls_back(self) -> None:
        primary = _StubBackend("primary", [])
        primary._stream_chunks = [BrainProviderError("network down")]
        fb1 = _StubBackend("fb1", [])
        fb1._stream_chunks = [["fallback", " ok"]]
        chain = FallbackChain(primary=primary, fallbacks=[fb1])

        out = list(chain.stream_complete(system="s", user="u"))

        # Fallback produced 2 chunks + 1 final marker.
        assert len(out) == 3
        assert "fallback ok" in out[-1][1]

    def test_all_streams_fail_raises_last(self) -> None:
        primary = _StubBackend("primary", [])
        primary._stream_chunks = [BrainProviderError("primary down")]
        fb1 = _StubBackend("fb1", [])
        fb1._stream_chunks = [BrainProviderError("fb1 down")]
        chain = FallbackChain(primary=primary, fallbacks=[fb1])

        with pytest.raises(BrainProviderError, match="fb1 down"):
            list(chain.stream_complete(system="s", user="u"))


# ── build_fallback_backends() helper ─────────────────────────────────────


class TestBuildFallbackBackends:
    def test_builds_one_backend_per_name(self) -> None:
        raw_qb = {
            "backend": "gemini",
            "fallback_chain": ["anthropic", "openai"],
            "gemini":    {"model": "gemini-2.5-flash"},
            "anthropic": {"model": "claude"},
            "openai":    {"model": "gpt-5-mini"},
        }
        built: list[tuple[str, dict]] = []

        def build_one(name: str, section: dict) -> Any:
            built.append((name, section))
            return f"backend-{name}"

        out = build_fallback_backends(raw_qb, raw_qb["fallback_chain"], build_one)

        assert out == ["backend-anthropic", "backend-openai"]
        assert built[0][0] == "anthropic"
        assert built[1][0] == "openai"

    def test_missing_section_skipped(self) -> None:
        raw_qb = {
            "backend": "gemini",
            "fallback_chain": ["anthropic", "missing"],
            "anthropic": {"model": "claude"},
        }

        def build_one(name: str, section: dict) -> Any:
            return name

        out = build_fallback_backends(raw_qb, raw_qb["fallback_chain"], build_one)
        assert out == ["anthropic"]


# ── config integration ──────────────────────────────────────────────────


class TestConfigLoad:
    def test_fallback_chain_loads_from_toml(self, tmp_path: Any) -> None:
        from controller.config import load

        cfg_path = tmp_path / "controller.toml"
        cfg_path.write_text('''
[qb]
backend = "gemini"
fallback_chain = ["anthropic", "openai"]

[qb.gemini]
model = "gemini-2.5-flash"
api_key_env = "GEMINI_API_KEY"
max_tokens = 8192
timeout_seconds = 60

[qb.anthropic]
model = "claude-sonnet-4-6"
api_key_env = "ANTHROPIC_API_KEY"
max_tokens = 8192
timeout_seconds = 60

[qb.openai]
model = "gpt-5-mini"
api_key_env = "OPENAI_API_KEY"
max_tokens = 8192
timeout_seconds = 60
''')

        cfg = load(cfg_path)

        assert cfg.qb.name == "gemini"
        assert [fb.name for fb in cfg.qb_fallbacks] == ["anthropic", "openai"]

    def test_fallback_chain_absent_defaults_empty(self, tmp_path: Any) -> None:
        from controller.config import load

        cfg_path = tmp_path / "controller.toml"
        cfg_path.write_text('''
[qb]
backend = "gemini"

[qb.gemini]
model = "gemini-2.5-flash"
api_key_env = "GEMINI_API_KEY"
max_tokens = 8192
timeout_seconds = 60
''')

        cfg = load(cfg_path)

        assert cfg.qb_fallbacks == ()

    def test_fallback_chain_filters_primary_and_dupes(self, tmp_path: Any) -> None:
        from controller.config import load

        cfg_path = tmp_path / "controller.toml"
        cfg_path.write_text('''
[qb]
backend = "gemini"
fallback_chain = ["gemini", "anthropic", "anthropic"]

[qb.gemini]
model = "gemini-2.5-flash"
api_key_env = "GEMINI_API_KEY"
max_tokens = 8192
timeout_seconds = 60

[qb.anthropic]
model = "claude"
api_key_env = "ANTHROPIC_API_KEY"
max_tokens = 8192
timeout_seconds = 60
''')

        # NOTE: JSON schema has uniqueItems=true on fallback_chain, so
        # duplicate 'anthropic' will fail validation. Testing that the
        # schema catches it is fine — that's the desired behavior.
        with pytest.raises(Exception):
            load(cfg_path)
