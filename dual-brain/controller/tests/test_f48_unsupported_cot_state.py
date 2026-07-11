"""Regression tests for F-48.

Failure log entry this file locks in
─────────────────────────────────────

**F-48 (2026-07-10).** V6.6.1 UTM: every ``system.unsupported`` intent
crashed the turn with ``Error: step_state must be one of ['active', 'done',
'failed', 'pending'], got 'unsupported'``. The F-42 fix for ``_cot``
NameError had just landed — but the port introduced a new bug: the fix's
comment said "'unsupported' state → yellow CoT card via styles.tcss", and
that comment got read literally.

Two orthogonal things share the word "unsupported":

* ``step_name="unsupported"`` — CSS class key (Terminal's ``CotCard.on_mount``
  maps ``step_name`` → CSS class ``s-unsupported``, which drives the yellow
  styling). Free-form string, no validation.
* ``step_state=...`` — state-machine value from ``frozenset({"pending",
  "active", "done", "failed"})`` (``turn_events._COT_STEP_STATES``). Passing
  anything else raises ``ValueError`` at ``CotEvent.__post_init__``.

The F-48 fix routes ``_emit_unsupported`` to call
``_make_cot(t0, "unsupported", "done", ...)`` — step_state="done" (state
machine complete), step_name="unsupported" (yellow styling).

Fix shape and what this file guards
────────────────────────────────────

Three layers of regression lock:

1. Direct: ``_make_cot`` accepts every value in ``_COT_STEP_STATES`` and
   ONLY those. Parametrized so a future re-introduction of "unsupported"
   as a state fires immediately.

2. Contract: constructing ``CotEvent`` with ``step_state="unsupported"``
   still raises ``ValueError``. If the state-machine constraint were ever
   relaxed, the F-48 fix would silently over-succeed and the test's
   yellow-styling assumption would drift.

3. Call-site: ``_emit_unsupported`` in ``main.py`` still emits
   ``_make_cot(t0, "unsupported", "done", ...)`` — the exact regression
   the F-48 bug produced was the "done" being "unsupported". Source-level
   grep catches accidental revert.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from controller.main import _make_cot
from controller.turn_events import _COT_STEP_STATES, CotEvent


_MAIN_PY = Path(__file__).parent.parent / "main.py"


# ── Unit tests: _make_cot parametrised over the state frozenset ──────────


@pytest.mark.parametrize("state", sorted(_COT_STEP_STATES))
def test_make_cot_accepts_every_valid_state(state: str) -> None:
    """Every state in the frozenset is a legal ``step_state``. If someone
    tightens the frozenset later without updating call sites, this fires
    on the removed value."""
    ev = _make_cot(t0=0.0, name="unsupported", state=state, body="hi")
    assert isinstance(ev, CotEvent)
    assert ev.step_state == state
    assert ev.step_name == "unsupported"


@pytest.mark.parametrize(
    "invalid_state",
    [
        "unsupported",   # F-48 root case — the exact string that crashed
        "UNSUPPORTED",   # accidental caps
        "success",       # tempting synonym for "done"
        "error",         # tempting synonym for "failed"
        "",              # empty
        "  ",            # whitespace
        None,            # missing
    ],
)
def test_make_cot_rejects_invalid_state(invalid_state) -> None:
    """The exact F-48 failure mode: ``step_state="unsupported"``. Also
    every plausible near-miss. All must raise."""
    with pytest.raises((ValueError, TypeError)):
        _make_cot(t0=0.0, name="unsupported", state=invalid_state, body="hi")


# ── Contract: CotEvent enforces the state constraint at construction ─────


def test_cot_event_rejects_unsupported_step_state_directly() -> None:
    """The underlying dataclass ``__post_init__`` must still raise on the
    exact string that crashed F-48. If the constraint is removed, the
    F-48 fix's premise (step_state ∈ {pending, active, done, failed})
    no longer holds and this test flags it."""
    with pytest.raises(ValueError, match="step_state must be one of"):
        CotEvent(
            step_index=0,
            step_name="unsupported",
            step_state="unsupported",
            heading="Unsupported",
            body="body",
            data={},
            timestamp_ms=0.0,
        )


def test_cot_step_states_frozenset_contains_done_and_not_unsupported() -> None:
    """Sanity: 'done' is the state the F-48 fix uses. 'unsupported' MUST
    NOT be a valid state — reintroducing it is the exact regression."""
    assert "done" in _COT_STEP_STATES
    assert "unsupported" not in _COT_STEP_STATES


# ── Yellow-styling preservation: step_name survives untouched ────────────


def test_unsupported_step_name_preserved_for_css_class_lookup() -> None:
    """Terminal maps step_name → CSS class (``s-{step_name}``). If we
    accidentally rename step_name to something safer like "system", the
    yellow-card CSS would break. Lock in the exact string."""
    ev = _make_cot(t0=0.0, name="unsupported", state="done", body="body")
    assert ev.step_name == "unsupported"


# ── Source-level: _emit_unsupported still uses step_state="done" ─────────


def _read_main_source() -> str:
    return _MAIN_PY.read_text(encoding="utf-8")


def test_emit_unsupported_yields_step_state_done() -> None:
    """The exact regression F-48 covers. ``_emit_unsupported`` MUST call
    ``_make_cot(..., "unsupported", "done", ...)`` for the yellow card.
    A revert to ``"unsupported"`` for step_state — the pre-fix behaviour
    — would fail this test."""
    src = _read_main_source()
    start = src.find("def _emit_unsupported(")
    assert start >= 0, "_emit_unsupported method removed?"
    # Method body ends at the next same-indented ``def `` at class level.
    end = src.find("\n    def ", start + 1)
    body = src[start:end if end > 0 else len(src)]

    # The yellow-card emission uses the "unsupported" step_name — F-35 rule.
    assert re.search(
        r'_make_cot\(\s*t0\s*,\s*"unsupported"\s*,\s*"done"',
        body,
    ), (
        "F-48 regression: _emit_unsupported no longer emits "
        '_make_cot(t0, "unsupported", "done", ...) — either the step_name '
        "was renamed (breaks yellow CSS) or step_state reverted to "
        '"unsupported" (crashes CotEvent construction).'
    )


def test_emit_unsupported_never_passes_unsupported_as_step_state() -> None:
    """Belt-and-braces: the literal token sequence
    ``"unsupported", "unsupported"`` inside a ``_make_cot`` call is the
    exact F-48 bug. If someone accidentally re-introduces it, catch it."""
    src = _read_main_source()
    start = src.find("def _emit_unsupported(")
    end = src.find("\n    def ", start + 1)
    body = src[start:end if end > 0 else len(src)]
    assert not re.search(
        r'_make_cot\(\s*t0\s*,\s*"unsupported"\s*,\s*"unsupported"',
        body,
    ), "F-48 regression: _emit_unsupported passes step_state='unsupported'."
