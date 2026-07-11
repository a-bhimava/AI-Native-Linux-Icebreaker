"""QB fallback chain — primary → fallback[0] → fallback[1] ... on
``BrainProviderError``.

v6.65 foundation for pipeline robustness. When a user configures
``qb.fallback_chain = ["anthropic", "openai"]`` in
``controller.toml``, the controller tries Gemini first (the configured
primary), and on transport / API failure falls through to Anthropic,
then OpenAI, etc.

**Fallback fires only on** ``BrainProviderError``. Schema / truncation /
config errors propagate — those are prompt or budget issues that
retrying the same prompt against a different backend won't fix (per
BP-2, we don't paper over misconfigurations).

``FallbackChain`` is NOT a ``BrainBackend`` subclass — the base class
prevents subclasses from overriding ``complete()`` / ``stream_complete()``.
Instead it duck-types the methods the Runner and Verifier call.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Generator, Iterable, Sequence

from . import debug_log
from .backends.base import (
    BrainBackend,
    BrainProviderError,
    BrainResponse,
)


_fb_log = logging.getLogger(__name__)


class FallbackChain:
    """Composition wrapper — try primary, then each fallback in order.

    Args:
        primary: primary QB backend (always tried first).
        fallbacks: ordered list of backends to try after primary fails.
        on_fallback: optional callback fired each time a fallback backend
            is invoked. Signature: ``(index: int, backend_name: str,
            prior_error: str) -> None``. Used by the Runner to emit a
            CoT event so the user sees what happened.
    """

    __slots__ = ("_primary", "_fallbacks", "_on_fallback")

    def __init__(
        self,
        primary: BrainBackend,
        fallbacks: Sequence[BrainBackend],
        on_fallback: Callable[[int, str, str], None] | None = None,
    ) -> None:
        self._primary = primary
        self._fallbacks = tuple(fallbacks)
        self._on_fallback = on_fallback

    @property
    def backend_name(self) -> str:
        return self._primary.backend_name

    @property
    def primary(self) -> BrainBackend:
        return self._primary

    @property
    def fallbacks(self) -> tuple[BrainBackend, ...]:
        return self._fallbacks

    def complete(
        self,
        system: str,
        user: str,
        schema: dict | None = None,
        max_retries: int = 3,
    ) -> BrainResponse:
        return self._dispatch(
            lambda backend: backend.complete(
                system=system, user=user, schema=schema, max_retries=max_retries,
            )
        )

    def stream_complete(
        self,
        system: str,
        user: str,
        schema: dict | None = None,
        max_retries: int = 3,
    ) -> Generator[tuple[str, str, bool], None, None]:
        # Streaming needs a generator; we consume the primary generator
        # until it errors, and only fall back if nothing was yielded (to
        # preserve INV-2-pluggable — a partially-streamed accepted
        # payload shouldn't be replayed by another backend). Practically:
        # if the primary raises before ANY chunk arrives, the fallback
        # takes over transparently.
        buffered: list[tuple[str, str, bool]] = []
        try:
            gen = self._primary.stream_complete(
                system=system, user=user, schema=schema,
                max_retries=max_retries,
            )
            for chunk, acc, is_final in gen:
                buffered.append((chunk, acc, is_final))
                yield (chunk, acc, is_final)
            return
        except BrainProviderError as exc:
            if buffered:
                # Already streamed some tokens — cannot safely fall back
                # (the user has seen partial output; another backend would
                # replay it). Propagate.
                raise
            last_error: BaseException = exc

        for idx, backend in enumerate(self._fallbacks):
            # Phase 6 Scope D/E debug instrumentation.
            if debug_log.is_enabled():
                debug_log.record(
                    "fallback_fire", "fallback_backend",
                    {
                        "hop_index": idx,
                        "primary": self._primary.backend_name,
                        "backend": backend.backend_name,
                        "prior_error_type": type(last_error).__name__,
                        "prior_error_head": str(last_error)[:200],
                    },
                )
            if self._on_fallback is not None:
                try:
                    self._on_fallback(idx, backend.backend_name, str(last_error))
                except Exception as cb_exc:
                    # F-53 Scope A.P2: user-supplied callback raised.
                    # Swallow so a broken UI toast handler can't stop a
                    # legit fallback attempt from proceeding — but log
                    # so callback authors can debug the "toast never
                    # showed up" case.
                    _fb_log.warning(
                        "fallback_backend.on_fallback callback raised: %s: %s",
                        type(cb_exc).__name__, cb_exc,
                    )
            # Phase 6 Scope E fix: track whether THIS fallback yielded
            # any chunks before failing. Same rule as the primary — if
            # partial output reached the user, we cannot try the next
            # fallback (their transcript already contains fb0's text; a
            # fb1 attempt would append fb1's independent response and
            # corrupt the record). Propagate instead.
            fb_buffered: list[tuple[str, str, bool]] = []
            try:
                gen = backend.stream_complete(
                    system=system, user=user, schema=schema,
                    max_retries=max_retries,
                )
                for chunk, acc, is_final in gen:
                    fb_buffered.append((chunk, acc, is_final))
                    yield (chunk, acc, is_final)
                return
            except BrainProviderError as exc:
                if fb_buffered:
                    # Partial output already reached the user — the
                    # transcript is committed to this fallback. No
                    # further fallback can safely take over.
                    raise
                last_error = exc
                continue
        raise last_error

    # ── Internal dispatch (shared with complete) ────────────────────────

    def _dispatch(self, call: Callable[[BrainBackend], BrainResponse]) -> BrainResponse:
        try:
            return call(self._primary)
        except BrainProviderError as exc:
            last_error: BaseException = exc

        for idx, backend in enumerate(self._fallbacks):
            # Phase 6 Scope D/E debug instrumentation.
            if debug_log.is_enabled():
                debug_log.record(
                    "fallback_fire", "fallback_backend",
                    {
                        "hop_index": idx,
                        "primary": self._primary.backend_name,
                        "backend": backend.backend_name,
                        "prior_error_type": type(last_error).__name__,
                        "prior_error_head": str(last_error)[:200],
                    },
                )
            if self._on_fallback is not None:
                try:
                    self._on_fallback(idx, backend.backend_name, str(last_error))
                except Exception as cb_exc:
                    # F-53 Scope A.P2: user-supplied callback raised.
                    # Swallow so a broken UI toast handler can't stop a
                    # legit fallback attempt from proceeding — but log
                    # so callback authors can debug the "toast never
                    # showed up" case.
                    _fb_log.warning(
                        "fallback_backend.on_fallback callback raised: %s: %s",
                        type(cb_exc).__name__, cb_exc,
                    )
            try:
                return call(backend)
            except BrainProviderError as exc:
                last_error = exc
                continue
        raise last_error


def build_fallback_backends(
    raw_qb: dict,
    fallback_names: Iterable[str],
    build_one: Callable[[str, dict], BrainBackend],
) -> list[BrainBackend]:
    """Instantiate one backend per name in ``fallback_names``.

    Each name must have a corresponding ``[qb.<name>]`` section in
    ``raw_qb``. Skips silently if no such section exists (allows
    ``fallback_chain`` to reference backends the user hasn't fully
    configured yet — invalid entries just get skipped rather than
    breaking startup).

    Args:
        raw_qb: the ``[qb]`` TOML table.
        fallback_names: ordered names to try building.
        build_one: injected constructor. Real code passes a closure over
            ``make_backend`` + ``BackendConfig``; tests pass a mock.

    Returns:
        list of backends in the same order as ``fallback_names``, minus
        any that lacked a ``[qb.<name>]`` config section.
    """
    out: list[BrainBackend] = []
    for name in fallback_names:
        section = raw_qb.get(name)
        if not isinstance(section, dict):
            continue
        out.append(build_one(name, section))
    return out
