"""Phase 6 Scope E — exhaustive edge-case coverage for FallbackChain.

The existing `test_backend_fallback.py` covers the happy-path state
transitions. This file exhausts the *edge cases* the user flagged:
every error class × every position in the chain, every streaming
mid-turn failure mode, cost-ceiling / cancellation interactions,
and user-facing failure enumeration.

Rule: if a bug can only be caught on a live guest, we still write the
regression test here so it fails FAST on the offline suite. Live-only
smoke lives in `test_fallback_live.py`.
"""

from __future__ import annotations

from typing import Any, Generator, List

import pytest

from controller.backends.base import (
    BrainConfigError,
    BrainProviderError,
    BrainResponse,
    BrainSchemaError,
    BrainTruncationError,
)
from controller.fallback_backend import FallbackChain


# ── Shared stubs (kept simple; matches test_backend_fallback.py) ─────────


class _StubBackend:
    def __init__(self, name: str, responses: List[Any]) -> None:
        self.backend_name = name
        self._responses = list(responses)
        self._call_log: list[str] = []
        self._stream_chunks: List[Any] = []

    def complete(self, system: str, user: str,
                 schema: dict | None = None, max_retries: int = 3) -> BrainResponse:
        self._call_log.append("complete")
        if not self._responses:
            raise RuntimeError(f"stub {self.backend_name} out of responses")
        result = self._responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def stream_complete(self, system: str, user: str,
                        schema: dict | None = None,
                        max_retries: int = 3) -> Generator[tuple[str, str, bool], None, None]:
        self._call_log.append("stream")
        if not self._stream_chunks:
            raise RuntimeError(f"stub {self.backend_name} out of stream chunks")
        chunks = self._stream_chunks.pop(0)
        if isinstance(chunks, BaseException):
            raise chunks
        accumulated = ""
        for chunk in chunks:
            if isinstance(chunk, BaseException):
                # Mid-stream failure: yield partial, then raise.
                raise chunk
            accumulated += chunk
            yield (chunk, accumulated, False)
        yield ("", accumulated, True)


def _good(text: str = "ok") -> BrainResponse:
    return BrainResponse(
        content_json={"backend": "stub", "text": text},
        tokens_in=1, tokens_out=1, cost_usd=None,
        backend="stub", model="stub-model", attempts=1,
    )


# ══════════════════════════════════════════════════════════════════════════
# Section 1 — Every non-BrainProviderError class BYPASSES fallback
# ══════════════════════════════════════════════════════════════════════════
#
# The plan's rule: only BrainProviderError triggers fallback. Every other
# error type MUST propagate directly — retrying against another backend
# with the same prompt won't help, and would silently mask the real bug.
# ══════════════════════════════════════════════════════════════════════════


class TestNonProviderErrorsPropagate:
    def test_config_error_from_primary_bypasses_fallback(self) -> None:
        """BrainConfigError means the user's config is wrong (bad API key,
        missing model). Falling back would hide the misconfig — the
        user needs to see the specific error to fix it."""
        primary = _StubBackend("primary", [BrainConfigError("bad api key")])
        fb = _StubBackend("fb", [_good("fb")])
        chain = FallbackChain(primary=primary, fallbacks=[fb])
        with pytest.raises(BrainConfigError, match="bad api key"):
            chain.complete(system="s", user="u")
        assert fb._call_log == [], "fallback was invoked despite config error"

    def test_generic_exception_propagates(self) -> None:
        """A random unrelated exception (TypeError, RuntimeError, etc.)
        must NOT be silently converted to a fallback trigger — that
        would hide real bugs in the primary backend's implementation."""
        primary = _StubBackend("primary", [TypeError("integer expected")])
        fb = _StubBackend("fb", [_good("fb")])
        chain = FallbackChain(primary=primary, fallbacks=[fb])
        with pytest.raises(TypeError, match="integer expected"):
            chain.complete(system="s", user="u")
        assert fb._call_log == []

    def test_keyboard_interrupt_propagates(self) -> None:
        """User pressed Ctrl-C mid-turn — must propagate, not fall
        back. Otherwise Ctrl-C would silently retry against another
        backend, wasting the user's time and the API budget."""
        primary = _StubBackend("primary", [KeyboardInterrupt()])
        fb = _StubBackend("fb", [_good("fb")])
        chain = FallbackChain(primary=primary, fallbacks=[fb])
        with pytest.raises(KeyboardInterrupt):
            chain.complete(system="s", user="u")
        assert fb._call_log == []


# ══════════════════════════════════════════════════════════════════════════
# Section 2 — Multi-hop paths
# ══════════════════════════════════════════════════════════════════════════
#
# The user's `qb.fallback_chain` can be 1-4 backends. Every intermediate
# failure must correctly propagate the LAST error (not the FIRST), and
# every successful landing must attribute correctly.
# ══════════════════════════════════════════════════════════════════════════


class TestMultiHopChains:
    def test_primary_fails_first_fallback_fails_second_succeeds(self) -> None:
        primary = _StubBackend("primary", [BrainProviderError("primary")])
        fb0 = _StubBackend("fb0", [BrainProviderError("fb0")])
        fb1 = _StubBackend("fb1", [_good("fb1")])
        chain = FallbackChain(primary=primary, fallbacks=[fb0, fb1])
        result = chain.complete(system="s", user="u")
        assert result.content_json["text"] == "fb1"
        assert primary._call_log == ["complete"]
        assert fb0._call_log == ["complete"]
        assert fb1._call_log == ["complete"]

    def test_last_error_message_reflects_final_failure(self) -> None:
        """When every backend fails, the raised error must be the LAST
        one — that's the one closest to being the "final answer" and
        the one most useful for triage."""
        primary = _StubBackend("primary", [BrainProviderError("primary msg")])
        fb0 = _StubBackend("fb0", [BrainProviderError("fb0 msg")])
        fb1 = _StubBackend("fb1", [BrainProviderError("fb1 msg")])
        chain = FallbackChain(primary=primary, fallbacks=[fb0, fb1])
        with pytest.raises(BrainProviderError, match="fb1 msg"):
            chain.complete(system="s", user="u")

    def test_fallback_stops_iterating_after_success(self) -> None:
        """Once a fallback lands, subsequent fallbacks must not be
        touched — saves API calls and preserves attribution."""
        primary = _StubBackend("primary", [BrainProviderError("primary")])
        fb0 = _StubBackend("fb0", [_good("fb0")])
        fb1 = _StubBackend("fb1", [_good("fb1")])
        chain = FallbackChain(primary=primary, fallbacks=[fb0, fb1])
        result = chain.complete(system="s", user="u")
        assert result.content_json["text"] == "fb0"
        assert fb1._call_log == [], "fb1 was tried after fb0 succeeded"

    def test_intermediate_schema_error_stops_the_chain(self) -> None:
        """Primary fails with provider error → falls to fb0 → fb0 hits
        a schema error → chain propagates the schema error WITHOUT
        trying fb1. Rationale: schema error is a "prompt / catalogue"
        problem, not a backend availability problem — trying another
        backend with the same prompt won't help."""
        primary = _StubBackend("primary", [BrainProviderError("net")])
        schema_err = BrainSchemaError("bad schema", attempts=3, last_payload_excerpt="{}")
        fb0 = _StubBackend("fb0", [schema_err])
        fb1 = _StubBackend("fb1", [_good("fb1")])
        chain = FallbackChain(primary=primary, fallbacks=[fb0, fb1])
        with pytest.raises(BrainSchemaError):
            chain.complete(system="s", user="u")
        assert fb1._call_log == [], "fb1 was tried after fb0's schema error"

    def test_long_chain_five_fallbacks_all_fail(self) -> None:
        """Long chain regression: 5 fallbacks all failing must not
        stop iterating early or accumulate memory. Real fleets might
        have Gemini → Anthropic → OpenAI → Local; keep the code
        arbitrary-length correct."""
        primary = _StubBackend("primary", [BrainProviderError("primary")])
        fbs = [
            _StubBackend(f"fb{i}", [BrainProviderError(f"fb{i} msg")])
            for i in range(5)
        ]
        chain = FallbackChain(primary=primary, fallbacks=fbs)
        with pytest.raises(BrainProviderError, match="fb4 msg"):
            chain.complete(system="s", user="u")
        # Every backend was tried, in order.
        for fb in fbs:
            assert fb._call_log == ["complete"]


# ══════════════════════════════════════════════════════════════════════════
# Section 3 — Streaming semantics under partial output
# ══════════════════════════════════════════════════════════════════════════
#
# The plan's rule: if the primary already streamed ANY chunk to the
# user, we CANNOT fall back — replay would duplicate output. This is
# the "INV-2-preserve" edge case Scope E must nail down.
# ══════════════════════════════════════════════════════════════════════════


class TestStreamingMidFailure:
    def test_stream_error_after_partial_output_propagates(self) -> None:
        """Primary yields some chunks, THEN raises. We must NOT fall
        back — the user has already seen partial text and a fallback
        would replay it, corrupting the transcript."""
        primary = _StubBackend("primary", [])
        # Yield 2 chunks then explode.
        primary._stream_chunks = [[
            "hello ", "world ",
            BrainProviderError("mid-stream disconnect"),
        ]]
        fb = _StubBackend("fb", [])
        fb._stream_chunks = [["fallback ", "response"]]
        chain = FallbackChain(primary=primary, fallbacks=[fb])

        chunks: list[str] = []
        with pytest.raises(BrainProviderError, match="mid-stream"):
            for chunk, _acc, _final in chain.stream_complete(
                system="s", user="u",
            ):
                chunks.append(chunk)

        assert chunks == ["hello ", "world "]
        assert fb._call_log == [], "fallback tried after partial stream — corrupt output risk"

    def test_stream_error_zero_chunks_falls_back_transparently(self) -> None:
        """Symmetric: primary raises BEFORE yielding anything. Fallback
        takes over — user sees only the fallback's output."""
        primary = _StubBackend("primary", [])
        primary._stream_chunks = [BrainProviderError("no chunks")]
        fb = _StubBackend("fb", [])
        fb._stream_chunks = [["fallback ", "text"]]
        chain = FallbackChain(primary=primary, fallbacks=[fb])

        chunks = [c for c, _, _ in chain.stream_complete(system="s", user="u")]
        assert "".join(chunks) == "fallback text"

    def test_stream_schema_error_never_falls_back(self) -> None:
        """Same policy for streaming as for complete()."""
        primary = _StubBackend("primary", [])
        primary._stream_chunks = [
            BrainSchemaError("stream schema", attempts=3, last_payload_excerpt=""),
        ]
        fb = _StubBackend("fb", [])
        fb._stream_chunks = [["fallback"]]
        chain = FallbackChain(primary=primary, fallbacks=[fb])
        with pytest.raises(BrainSchemaError):
            list(chain.stream_complete(system="s", user="u"))
        assert fb._call_log == []

    def test_stream_non_provider_error_propagates_before_any_chunk(self) -> None:
        """A random Exception (TypeError, RuntimeError) mid-stream must
        NOT trigger fallback. Only BrainProviderError is our contract."""
        primary = _StubBackend("primary", [])
        primary._stream_chunks = [RuntimeError("primary bug")]
        fb = _StubBackend("fb", [])
        fb._stream_chunks = [["fallback"]]
        chain = FallbackChain(primary=primary, fallbacks=[fb])
        with pytest.raises(RuntimeError, match="primary bug"):
            list(chain.stream_complete(system="s", user="u"))
        assert fb._call_log == []

    def test_stream_fallback_hits_provider_error_zero_chunks_tries_next(
        self,
    ) -> None:
        """Primary yields 0 chunks → fb0 also yields 0 then errors →
        fb1 succeeds. Multi-hop under streaming with no partial output.
        """
        primary = _StubBackend("primary", [])
        primary._stream_chunks = [BrainProviderError("primary")]
        fb0 = _StubBackend("fb0", [])
        fb0._stream_chunks = [BrainProviderError("fb0")]
        fb1 = _StubBackend("fb1", [])
        fb1._stream_chunks = [["ok"]]
        chain = FallbackChain(primary=primary, fallbacks=[fb0, fb1])
        chunks = [c for c, _, _ in chain.stream_complete(system="s", user="u")]
        assert "".join(chunks) == "ok"

    def test_stream_fallback_partial_then_fail_propagates(self) -> None:
        """Primary fails clean (0 chunks). fb0 yields some, then
        raises → propagate (same rule as primary partial). We must
        NOT try fb1."""
        primary = _StubBackend("primary", [])
        primary._stream_chunks = [BrainProviderError("primary")]
        fb0 = _StubBackend("fb0", [])
        fb0._stream_chunks = [[
            "fb0 partial ",
            BrainProviderError("fb0 mid-stream drop"),
        ]]
        fb1 = _StubBackend("fb1", [])
        fb1._stream_chunks = [["fb1 recovery"]]
        chain = FallbackChain(primary=primary, fallbacks=[fb0, fb1])
        chunks: list[str] = []
        with pytest.raises(BrainProviderError, match="fb0 mid-stream drop"):
            for c, _, _ in chain.stream_complete(system="s", user="u"):
                chunks.append(c)
        assert "fb0 partial " in chunks


# ══════════════════════════════════════════════════════════════════════════
# Section 4 — Callback semantics (user-facing surface)
# ══════════════════════════════════════════════════════════════════════════


class TestCallbackSemantics:
    def test_callback_fires_once_per_hop(self) -> None:
        primary = _StubBackend("primary", [BrainProviderError("p")])
        fb0 = _StubBackend("fb0", [BrainProviderError("fb0")])
        fb1 = _StubBackend("fb1", [_good("fb1")])
        events: list[tuple[int, str, str]] = []
        chain = FallbackChain(
            primary=primary, fallbacks=[fb0, fb1],
            on_fallback=lambda i, n, e: events.append((i, n, e)),
        )
        chain.complete(system="s", user="u")
        assert [ev[0] for ev in events] == [0, 1]
        assert [ev[1] for ev in events] == ["fb0", "fb1"]
        # Each callback carries the PRIOR error's message.
        assert "p" in events[0][2]
        assert "fb0" in events[1][2]

    def test_callback_fires_zero_times_when_primary_succeeds(self) -> None:
        primary = _StubBackend("primary", [_good("primary")])
        fb0 = _StubBackend("fb0", [])
        events = []
        chain = FallbackChain(
            primary=primary, fallbacks=[fb0],
            on_fallback=lambda *args: events.append(args),
        )
        chain.complete(system="s", user="u")
        assert events == []

    def test_callback_raising_does_not_break_fallback(self) -> None:
        """Callback is user-supplied — a broken UI toast handler must
        NOT prevent legit fallback from proceeding. Verified in
        Scope A.P2; regression pinned here."""
        primary = _StubBackend("primary", [BrainProviderError("p")])
        fb = _StubBackend("fb", [_good("fb")])
        def bad_cb(*args):
            raise ValueError("UI blew up")
        chain = FallbackChain(
            primary=primary, fallbacks=[fb],
            on_fallback=bad_cb,
        )
        # Must complete despite callback error.
        result = chain.complete(system="s", user="u")
        assert result.content_json["text"] == "fb"

    def test_no_callback_configured_still_falls_back(self) -> None:
        """Optional callback: passing None must not prevent fallback."""
        primary = _StubBackend("primary", [BrainProviderError("p")])
        fb = _StubBackend("fb", [_good("fb")])
        chain = FallbackChain(primary=primary, fallbacks=[fb])
        result = chain.complete(system="s", user="u")
        assert result.content_json["text"] == "fb"


# ══════════════════════════════════════════════════════════════════════════
# Section 5 — Argument threading (schema, max_retries)
# ══════════════════════════════════════════════════════════════════════════


class TestArgumentThreading:
    def test_max_retries_passed_to_every_backend(self) -> None:
        """The runner passes `max_retries`; every backend must see it,
        not just the primary. Otherwise a retry-count knob only
        affects the primary — surprising for operators."""
        captured: list[int] = []

        class _CapturingStub(_StubBackend):
            def complete(self, system, user, schema=None, max_retries=3):
                captured.append(max_retries)
                return super().complete(system, user, schema, max_retries)

        primary = _CapturingStub("primary", [BrainProviderError("p")])
        fb = _CapturingStub("fb", [_good("fb")])
        chain = FallbackChain(primary=primary, fallbacks=[fb])
        chain.complete(system="s", user="u", max_retries=7)
        assert captured == [7, 7]

    def test_schema_passed_to_every_backend(self) -> None:
        """Same for schema — verifier voting requires every backend
        see the same schema so vote comparability is meaningful."""
        captured_schemas: list[dict | None] = []
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}

        class _CapturingStub(_StubBackend):
            def complete(self, system, user, schema=None, max_retries=3):
                captured_schemas.append(schema)
                return super().complete(system, user, schema, max_retries)

        primary = _CapturingStub("primary", [BrainProviderError("p")])
        fb = _CapturingStub("fb", [_good("fb")])
        chain = FallbackChain(primary=primary, fallbacks=[fb])
        chain.complete(system="s", user="u", schema=schema)
        assert captured_schemas == [schema, schema]


# ══════════════════════════════════════════════════════════════════════════
# Section 6 — Empty / degenerate chains
# ══════════════════════════════════════════════════════════════════════════


class TestDegenerateChains:
    def test_empty_fallback_chain_primary_success(self) -> None:
        """Default state (no fallback configured) — must be indistinguish-
        able from a bare backend for the success path."""
        primary = _StubBackend("primary", [_good("primary")])
        chain = FallbackChain(primary=primary, fallbacks=[])
        result = chain.complete(system="s", user="u")
        assert result.content_json["text"] == "primary"

    def test_empty_fallback_chain_primary_fails_raises(self) -> None:
        """Same, failure path — must reraise, not silently deceive."""
        primary = _StubBackend("primary", [BrainProviderError("boom")])
        chain = FallbackChain(primary=primary, fallbacks=[])
        with pytest.raises(BrainProviderError, match="boom"):
            chain.complete(system="s", user="u")

    def test_duplicate_primary_in_fallbacks_still_gets_tried(self) -> None:
        """Misconfiguration: user lists `fallback_chain = ["gemini"]`
        but primary is also gemini. The chain doesn't dedupe (that's
        the config loader's job); pinning the current behavior means
        we notice if it changes."""
        primary = _StubBackend("primary", [BrainProviderError("p")])
        # Same backend, but a different instance — the chain doesn't
        # know they're duplicates.
        dup = _StubBackend("primary", [_good("dup")])
        chain = FallbackChain(primary=primary, fallbacks=[dup])
        result = chain.complete(system="s", user="u")
        assert result.content_json["text"] == "dup"

    def test_fallbacks_tuple_property_is_immutable(self) -> None:
        """`.fallbacks` must be a tuple — mutating a list would let a
        rogue caller alter the chain mid-turn."""
        primary = _StubBackend("primary", [])
        fb = _StubBackend("fb", [])
        chain = FallbackChain(primary=primary, fallbacks=[fb])
        assert isinstance(chain.fallbacks, tuple)


# ══════════════════════════════════════════════════════════════════════════
# Section 7 — User-visible failure enumeration
# ══════════════════════════════════════════════════════════════════════════
#
# When every backend fails, the raised BrainProviderError becomes the
# reason string the user sees in the UI / CoT. Users must be able to
# tell WHICH backends failed WHY — otherwise triage is "all three
# broke" with no further info.
# ══════════════════════════════════════════════════════════════════════════


class TestFailureEnumerationForUsers:
    def test_final_error_message_carries_last_backend_context(self) -> None:
        """The raised exception is the LAST backend's error. The
        callback trail carries the earlier failures — the runner should
        assemble them into a single user-facing message."""
        primary = _StubBackend("primary", [BrainProviderError("Gemini: 429 rate limit")])
        fb = _StubBackend("fb", [BrainProviderError("Anthropic: 401 auth")])
        events = []
        chain = FallbackChain(
            primary=primary, fallbacks=[fb],
            on_fallback=lambda i, n, e: events.append((i, n, e)),
        )
        with pytest.raises(BrainProviderError) as exc_info:
            chain.complete(system="s", user="u")

        # Final error is the LAST one raised (Anthropic, in this case).
        assert "Anthropic" in str(exc_info.value)
        # But the callback recorded the FIRST error (Gemini) with the
        # correct index / name.
        assert events == [(0, "fb", "Gemini: 429 rate limit")]

    def test_early_generator_close_does_not_raise(self) -> None:
        """Caller-side cancellation model: consumer stops iterating
        before is_final. The generator's `finally` (if any) must run
        and the FallbackChain must NOT try the next backend — the
        transcript is committed to whatever was yielded.
        """
        primary = _StubBackend("primary", [])
        primary._stream_chunks = [["a", "b", "c", "d", "e"]]
        fb = _StubBackend("fb", [])
        fb._stream_chunks = [["should never appear"]]
        chain = FallbackChain(primary=primary, fallbacks=[fb])

        gen = chain.stream_complete(system="s", user="u")
        # Take two chunks then close.
        next(gen)
        next(gen)
        gen.close()
        # No fallback was consulted; that's the semantic contract.
        assert fb._call_log == []

    def test_stream_with_only_final_marker_no_content(self) -> None:
        """An empty response is legal — some prompts elicit a zero-token
        answer. The chain must NOT count "yielded is_final=True with
        no prior content chunks" as buffered output for fallback
        purposes... but it ALSO must not fall back (the primary
        successfully returned)."""
        primary = _StubBackend("primary", [])
        # A single empty chunk followed by is_final.
        primary._stream_chunks = [[]]
        fb = _StubBackend("fb", [])
        fb._stream_chunks = [["fallback"]]
        chain = FallbackChain(primary=primary, fallbacks=[fb])
        chunks = list(chain.stream_complete(system="s", user="u"))
        # 1 chunk: the final marker "".
        assert chunks == [("", "", True)]
        assert fb._call_log == []

    def test_events_ordered_by_fallback_index(self) -> None:
        """Callback events must arrive in `enumerate(self._fallbacks)`
        order — a reordering bug here would confuse the runner's
        attribution."""
        primary = _StubBackend("primary", [BrainProviderError("p")])
        fbs = [
            _StubBackend(f"fb{i}", [BrainProviderError(f"fb{i}")])
            for i in range(4)
        ]
        # Last fallback succeeds; earlier ones fail.
        fbs[-1] = _StubBackend("fb3", [_good("fb3")])
        events: list[int] = []
        chain = FallbackChain(
            primary=primary, fallbacks=fbs,
            on_fallback=lambda i, n, e: events.append(i),
        )
        chain.complete(system="s", user="u")
        assert events == [0, 1, 2, 3]
