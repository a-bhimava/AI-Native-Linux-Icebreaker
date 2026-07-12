"""Regression tests for F-56.

Failure log entry this file locks in
─────────────────────────────────────

**F-56 (2026-07-11).** Phase 6 Scope F G24 UTM sweep against
``icebreaker@192.168.64.27``: 2 broad-OS rows produced daemon
JSON-RPC error ``-32603 name 'ResultEvent' is not defined``. Rows
``broad-os.gitlog.001`` and ``broad-os.content.resume.001`` — both
have ``expected_action`` in the shipped catalogue but Gemini
routed them to ``system.unsupported`` on the live guest, hitting
``_emit_unsupported`` at ``main.py:2466-2497``.

Root cause: the ``ResultEvent`` class lives in
``controller.turn_events``, which itself imports ``TurnResult``
from ``controller.main`` — a mutual import that the codebase
resolves by deferring ``.turn_events`` imports to each caller's
local scope (see ``run_turn_streaming`` line ~441). Python method
scope does NOT inherit locals from the caller: even though
``_emit_unsupported`` is invoked from inside ``run_turn_streaming``
(which HAS the local ``ResultEvent`` import), the method cannot
see it. Every method that yields ``ResultEvent`` outside the
streaming closure needs its own local import.

Same shape as F-42 (``_cot`` NameError on ``_emit_unsupported``).
The F-42 fix routed CoT emission through ``_make_cot``; F-56 does
the same for ``ResultEvent`` — but via a local import rather than
a factory, because ``ResultEvent`` has only one construction site
and a factory would be over-engineering.

Fix shape and what this file guards
────────────────────────────────────

Three layers:

1. Direct: ``_emit_unsupported`` executes end-to-end and yields a
   valid ``ResultEvent`` — no ``NameError``.

2. Source-level: the local ``from .turn_events import ResultEvent``
   sits ABOVE the first ``yield`` in ``_emit_unsupported``. If
   someone reorders the method and drops the import before the
   yield, this test fires.

3. Contract: no OTHER method in ``main.py`` yields ``ResultEvent``
   without either being inside ``run_turn_streaming``'s scope OR
   having its own local import. Grep-based check across the whole
   file. Catches the same-shape bug at a hypothetical NEW site.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from controller.main import Controller  # noqa: F401  (import proves module loads)
from controller.turn_events import ResultEvent


_MAIN_PY = Path(__file__).parent.parent / "main.py"


def _read_main_source() -> str:
    return _MAIN_PY.read_text(encoding="utf-8")


# ── Behavioural test: _emit_unsupported runs without NameError ───────────


def _minimal_controller() -> MagicMock:
    """Fake Controller shell with the fields ``_emit_unsupported`` reads."""
    c = MagicMock(spec=Controller)
    c._cfg = MagicMock()
    c._cfg.qb.name = "gemini"
    c._cfg.qb.model = "gemini-2.5-flash"
    # `_unsupported_payload` and `_write_unsupported_audit` are collaborators.
    c._unsupported_payload = MagicMock(
        return_value=(
            "delete everything",
            "That request is outside the shipped tool surface.",
            ["fs.delete", "fs.list"],
        )
    )
    c._write_unsupported_audit = MagicMock()
    return c


def test_emit_unsupported_runs_end_to_end_without_nameerror() -> None:
    """F-56 root case: exercise every yield in ``_emit_unsupported`` and
    assert no NameError raised for ``ResultEvent``. If the local import is
    removed, this fires immediately."""
    from controller import main as _main

    controller_shell = _minimal_controller()
    session = MagicMock()
    session.backend = "gemini"
    session.turn_index = 0
    intent = {
        "action": "system.unsupported",
        "target": "",
        "params": {},
        "reason": "user_requested",
        "risk_level": "low",
    }
    qb_response = MagicMock()

    # Drive the generator method with `Controller._emit_unsupported` bound
    # to our fake shell — this is what run_turn_streaming does live.
    events = list(
        _main.Controller._emit_unsupported(
            controller_shell,
            session, intent, "delete everything", qb_response,
            qb_cost=0.0, qb_tokens_in=10, qb_tokens_out=5, t0=0.0,
        )
    )
    # 2 CoT events + 1 ResultEvent = 3 yielded objects.
    assert len(events) == 3
    # The final event must be a valid ResultEvent — this is the F-56 lock.
    final = events[-1]
    assert isinstance(final, ResultEvent), (
        f"F-56 regression: _emit_unsupported did not yield a ResultEvent "
        f"as its final event. Got {type(final).__name__}."
    )
    assert final.result.outcome.value == "unsupported"
    assert final.result.success is False


# ── Source-level: local import present ABOVE first yield ─────────────────


def test_local_result_event_import_present_in_emit_unsupported() -> None:
    """The local ``from .turn_events import ResultEvent`` must exist in
    ``_emit_unsupported`` and precede the first ``yield ResultEvent`` call.
    If it's removed (or a refactor pushes it below the yield), the guest
    hits NameError again — this catches both."""
    src = _read_main_source()
    method_start = src.find("def _emit_unsupported(")
    assert method_start >= 0, "_emit_unsupported method removed?"
    next_method = src.find("\n    def ", method_start + 1)
    method_body = src[method_start:next_method if next_method > 0 else len(src)]

    import_pos = method_body.find(
        "from .turn_events import ResultEvent"
    )
    yield_pos = method_body.find("yield ResultEvent")
    assert import_pos > 0, (
        "F-56 regression: local `from .turn_events import ResultEvent` "
        "removed from _emit_unsupported"
    )
    assert yield_pos > 0, "yield ResultEvent removed from _emit_unsupported"
    assert import_pos < yield_pos, (
        "F-56 regression: local import moved BELOW the yield — Python "
        "will raise NameError on first execution"
    )


# ── Contract: every yield ResultEvent site has ResultEvent in scope ──────


def test_every_yield_result_event_site_has_import_in_scope() -> None:
    """Every method in main.py that yields ResultEvent must either be
    ``run_turn_streaming`` (which has the module-import at line ~441) or
    have its own local import. Catches the same-shape bug at any hypothetical
    NEW ``yield ResultEvent`` site added in the future."""
    src = _read_main_source()
    method_pattern = re.compile(
        r'^    (?:async +)?def (?P<name>\w+)\(',
        re.MULTILINE,
    )
    method_positions = [
        (m.group("name"), m.start())
        for m in method_pattern.finditer(src)
    ]
    method_positions.append(("__end__", len(src)))

    missing = []
    for i, (name, start) in enumerate(method_positions[:-1]):
        end = method_positions[i + 1][1]
        body = src[start:end]
        if "yield ResultEvent" not in body:
            continue
        # Method yields ResultEvent — verify ResultEvent is importable from
        # some scope visible to it.
        if name == "run_turn_streaming":
            # This method has the local import at its top (line ~441).
            continue
        # Any OTHER method that yields ResultEvent must import it locally.
        if "from .turn_events import" in body and "ResultEvent" in body.split(
            "from .turn_events import", 1)[1].split("\n", 1)[0]:
            continue
        missing.append(name)

    assert not missing, (
        f"F-56 regression: the following methods yield ResultEvent without "
        f"a local import: {missing}. Add "
        f"`from .turn_events import ResultEvent` at the top of each."
    )


def test_turn_events_still_imports_from_main_confirming_circular() -> None:
    """The whole reason ResultEvent is a deferred/local import is because
    turn_events.py imports TurnResult from .main. If someone breaks that
    cycle, the deferred imports become unnecessary — this test flags the
    situation so we can remove them cleanly."""
    turn_events_py = _MAIN_PY.parent / "turn_events.py"
    src = turn_events_py.read_text(encoding="utf-8")
    assert "from .main import TurnResult" in src, (
        "turn_events.py no longer imports from .main — the deferred "
        "import pattern in _emit_unsupported / run_turn_streaming is "
        "no longer necessary. Promote ResultEvent to module-top imports "
        "and delete the local ones."
    )
