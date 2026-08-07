"""Regression tests for F-53.

Failure log entry this file locks in
─────────────────────────────────────

**F-53 (2026-07-10).** V6.63 UTM: verifier rejected legitimate writes with
reason ``verifier call failed: BrainTruncationError`` — Gemini was
truncating the verifier response mid-JSON at ``max_tokens=512`` on longer
intent envelopes (F-41 added ``content`` + ``pb_hint`` fields, expanding
verifier prompts).

Two problems compounded:

1. Verifier config shipped with a stale 512 max_tokens default that
   worked for tiny intents but not the richer envelopes.
2. Every ``except Exception:`` site in the daemon and verifier swallowed
   the exception's type and message — surfacing generic strings like
   ``"verifier call failed"`` or ``"internal error"`` that gave the user
   nowhere to look.

Fix: the F-53 pattern was applied to 12 exception sites via
``_log_exception`` (structured audit line) AND every ``except Exception
as exc:`` surfaces ``type(exc).__name__: {exc}`` in the user-visible
reason string.

Fix shape and what this file guards
────────────────────────────────────

The verifier's two entry points (``SingleVerifier.verify`` and
``MajorityVoter._single_vote``) both catch every exception and return a
``VerifierResult`` whose ``reason`` embeds
``type(exc).__name__: {exc}``. This test locks in that shape for:

* ``BrainTruncationError`` — the specific F-53 case
* ``BrainSchemaError`` — the parent class
* ``BrainProviderError`` — a distinct error class (auth/transport)
* Generic ``Exception`` — the catch-all

Plus a source-level check that the pattern
``type(exc).__name__: {exc}"[:400]`` appears at every documented
verifier F-53 site. If someone reverts to a generic string, this test
fires.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from controller.backends.base import (
    BrainProviderError,
    BrainSchemaError,
    BrainTruncationError,
)
from controller.verifier import (
    MajorityVoter,
    SingleVerifier,
    VerifierConfig,
    VerifierResult,
)


_VERIFIER_PY = Path(__file__).parent.parent / "verifier.py"
_MAIN_PY = Path(__file__).parent.parent / "main.py"


# ── Test doubles ──────────────────────────────────────────────────────────


class _RaisingBackend:
    """QB double whose ``complete()`` raises a caller-chosen exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        max_retries: int = 1,
    ) -> Any:
        raise self._exc


_INTENT = {
    "intent_id": "00000000-0000-4000-8000-000000000000",
    "action": "fs.write",
    "target": "/home/icebreaker/notes.txt",
    "params": {"content": "hi"},
    "reason": "user_requested",
    "risk_level": "medium",
    "schema_version": "1.0.0",
    "timestamp": 1720000000.0,
}
_TOOL_CALL = {"tool": "fs.write", "args": {"path": "/tmp/x", "content": "hi"}}


# ── SingleVerifier: exception surfacing in reason string ─────────────────


def _truncation_error() -> BrainTruncationError:
    return BrainTruncationError(
        last_error="response truncated at max_tokens=32",
        attempts=2,
        last_payload_excerpt='{"verified": tru',
    )


def _schema_error() -> BrainSchemaError:
    return BrainSchemaError(
        last_error="response missing required field 'verified'",
        attempts=3,
        last_payload_excerpt='{"reason": "ok"}',
    )


def test_single_verifier_surfaces_brain_truncation_error_name() -> None:
    """The F-53 root case: verifier truncated. The user-visible reason
    must contain ``BrainTruncationError`` so operators know to bump
    ``max_tokens`` — not just ``verifier call failed``."""
    qb = _RaisingBackend(_truncation_error())
    result = SingleVerifier().verify(
        intent=_INTENT, tool_call=_TOOL_CALL,
        qb=qb, verifier_system="You are a verifier.",
    )
    assert isinstance(result, VerifierResult)
    assert result.verified is False
    assert "BrainTruncationError" in result.reason, (
        f"F-53 regression: reason does not name the exception type. "
        f"reason={result.reason!r}"
    )


def test_single_verifier_reason_is_not_generic_string() -> None:
    """Belt-and-braces: the reason MUST contain more than the pre-fix
    generic ``verifier call failed`` — specifically, the exception's
    ``__name__``. If somebody reverts to a generic swallow, this fires."""
    qb = _RaisingBackend(_truncation_error())
    result = SingleVerifier().verify(
        intent=_INTENT, tool_call=_TOOL_CALL,
        qb=qb, verifier_system="You are a verifier.",
    )
    generic = "verifier call failed"
    assert result.reason.strip() != generic, (
        "F-53 regression: reason reverted to bare 'verifier call failed'"
    )
    assert result.reason != "", "F-53 regression: reason is empty"


def test_single_verifier_surfaces_brain_schema_error_name() -> None:
    """Every exception class name that reaches the verifier catch clause
    must land in the reason — proves the pattern is class-agnostic."""
    qb = _RaisingBackend(_schema_error())
    result = SingleVerifier().verify(
        intent=_INTENT, tool_call=_TOOL_CALL,
        qb=qb, verifier_system="You are a verifier.",
    )
    assert "BrainSchemaError" in result.reason


def test_single_verifier_surfaces_brain_provider_error_name() -> None:
    """Auth/transport error class name reaches user."""
    qb = _RaisingBackend(BrainProviderError("HTTP 401 unauthorized"))
    result = SingleVerifier().verify(
        intent=_INTENT, tool_call=_TOOL_CALL,
        qb=qb, verifier_system="You are a verifier.",
    )
    assert "BrainProviderError" in result.reason


def test_single_verifier_surfaces_generic_exception_type_name() -> None:
    """A totally-unexpected exception class still gets its ``__name__``
    surfaced — proves the catch is genuinely ``Exception`` and not a
    narrow ``BrainError`` catch that would silently swallow surprises."""
    qb = _RaisingBackend(RuntimeError("something surprised the verifier"))
    result = SingleVerifier().verify(
        intent=_INTENT, tool_call=_TOOL_CALL,
        qb=qb, verifier_system="You are a verifier.",
    )
    assert "RuntimeError" in result.reason
    assert "something surprised the verifier" in result.reason


def test_single_verifier_reason_truncated_to_400_chars() -> None:
    """Huge exception messages must not blow the audit row. The F-53
    pattern truncates at 400 chars."""
    huge = BrainProviderError("x" * 10_000)
    qb = _RaisingBackend(huge)
    result = SingleVerifier().verify(
        intent=_INTENT, tool_call=_TOOL_CALL,
        qb=qb, verifier_system="You are a verifier.",
    )
    assert len(result.reason) <= 400
    assert "BrainProviderError" in result.reason


def test_single_verifier_result_records_zero_verified_on_exception() -> None:
    """An exception counts as verified=False, verified_count=0, votes_cast=1
    — the audit needs this shape to know a real attempt was made."""
    qb = _RaisingBackend(_truncation_error())
    result = SingleVerifier().verify(
        intent=_INTENT, tool_call=_TOOL_CALL,
        qb=qb, verifier_system="You are a verifier.",
    )
    assert result.verified is False
    assert result.verified_count == 0
    assert result.votes_cast == 1
    assert result.individual_reasons == [result.reason]


# ── MajorityVoter: same surfacing on each vote ────────────────────────────


def test_majority_voter_single_vote_surfaces_exception_type() -> None:
    """The parallel path uses ``_single_vote`` which has its own catch.
    A regression that reverts either catch shows up here."""
    cfg = VerifierConfig(votes=3, require=2, parallel=False)
    voter = MajorityVoter(cfg)
    qb = _RaisingBackend(_truncation_error())
    result = voter.verify(
        intent=_INTENT, tool_call=_TOOL_CALL,
        qb=qb, verifier_system="You are a verifier.",
    )
    # All three votes raise → all three individual reasons carry the name.
    assert result.verified is False
    assert result.verified_count == 0
    assert result.votes_cast == 3
    assert len(result.individual_reasons) == 3
    for reason in result.individual_reasons:
        assert "BrainTruncationError" in reason, (
            f"F-53 regression in MajorityVoter._single_vote: "
            f"reason={reason!r}"
        )


def test_majority_voter_parallel_path_surfaces_exception_type() -> None:
    """Same via the parallel executor path — F-53 must survive the
    ThreadPoolExecutor wrap."""
    cfg = VerifierConfig(votes=3, require=2, parallel=True)
    voter = MajorityVoter(cfg)
    qb = _RaisingBackend(_truncation_error())
    result = voter.verify(
        intent=_INTENT, tool_call=_TOOL_CALL,
        qb=qb, verifier_system="You are a verifier.",
    )
    assert result.verified is False
    for reason in result.individual_reasons:
        assert "BrainTruncationError" in reason


# ── Source-level: F-53 pattern still present at the verifier sites ───────


def _read_source(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_verifier_uses_f53_pattern_at_both_catch_sites() -> None:
    """The exact ``type(exc).__name__: {exc}`` pattern must appear at
    both catch sites in verifier.py. A regression that reverts to a
    generic string is caught here even before any test runs."""
    src = _read_source(_VERIFIER_PY)
    matches = re.findall(
        r'type\(exc\)\.__name__.*?exc',
        src,
    )
    assert len(matches) >= 2, (
        f"F-53 regression: verifier.py has fewer than 2 sites that "
        f"surface exc type name. Found {len(matches)}."
    )
    # Belt-and-braces: the truncation cap must survive.
    assert "[:400]" in src, (
        "F-53 regression: verifier.py no longer caps reason at 400 "
        "chars — a huge exception message will blow the audit row."
    )


def test_verifier_reason_prefix_string_survives() -> None:
    """The specific prefix ``verifier call failed:`` is what
    ``_should_retry_verifier(mode='on_call_failed_only')`` matches on.
    If this prefix disappears from the reason, the F-44 retry-mode
    dispatch silently stops firing — and F-49's on_call_failed_only
    default becomes a no-op."""
    src = _read_source(_VERIFIER_PY)
    assert "verifier call failed" in src, (
        "F-53/F-44 regression: prefix 'verifier call failed' removed "
        "from verifier.py — F-49 on_call_failed_only mode is now dead"
    )


def test_main_pipes_verifier_reason_into_user_output() -> None:
    """F-53's user-visible surface: main.py must plumb the verifier's
    reason string (which contains ``type(exc).__name__``) all the way
    into the ``TurnResult.output`` string. If someone replaces
    ``f"Verifier rejected: {…reason}"`` with a generic
    ``"Verifier rejected"``, the user loses the ``BrainTruncationError``
    hint and F-53's whole point (naming the failure) is undone.

    v6.16 M7.0.2f (2026-08-06): pre-M7.0.2 this fired at two duplicated
    sites (streaming + non-streaming twins) via ``{vresult.reason}``.
    Post-M7.0.2 both twins funnel through ``_map_pb_failure_outcome``
    which uses ``{single.verifier_result.reason}`` — regex updated
    to accept either pattern (single shared helper OR duplicated
    call sites) so the F-53 discipline is enforced regardless of
    how many call sites the streaming/non-streaming paths use."""
    src = _read_source(_MAIN_PY)
    # Accept either legacy ``{vresult.reason}`` (in case someone
    # brings back the inline version) OR the M7.0.2f-shape
    # ``{...verifier_result.reason}`` inside a "Verifier rejected:" string.
    matches = re.findall(
        r'Verifier rejected: \{[^}]*(?:vresult|verifier_result)\.reason\}',
        src,
    )
    assert len(matches) >= 1, (
        f"F-53 regression: main.py has ZERO sites that surface the "
        f"verifier reason to the user. Found {len(matches)}. The verifier "
        f"can produce BrainTruncationError-shaped reasons, but if main.py "
        f"drops the reason, the user still sees only 'Verifier rejected'."
    )


def test_log_exception_helper_present_in_main() -> None:
    """F-53's structured-logging half: ``_log_exception`` must exist so
    every catch site can call it before deciding what to do with the
    exception. If someone deletes the helper, the whole F-53 story
    unravels."""
    src = _read_source(_MAIN_PY)
    assert "def _log_exception(" in src, (
        "F-53 helper _log_exception removed from main.py"
    )
