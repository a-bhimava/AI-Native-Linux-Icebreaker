"""Tests for ``controller.turn_events`` — TurnEvent dataclasses.

Covers:
  1. ProgressEvent construction with all fields
  2. TokenEvent construction (token, accumulated, final)
  3. ResultEvent wraps a TurnResult
  4. ErrorEvent construction with default cancelled_at_step=""
  5. ErrorEvent with explicit cancelled_at_step
  6. TurnEvent union type discrimination (isinstance checks)
  7. Frozen dataclass immutability (AttributeError on assign)
  8. All field access works
  9. ResultEvent.result field access
 10. ProgressEvent elapsed_ms is float
 11. TokenEvent final is bool
 12. ErrorEvent error_type and message are str
 13. Multiple ProgressEvent instances are independent
 14. TokenEvent accumulated tracks state correctly
 15. ErrorEvent cancelled_at_step default is empty string
"""

from __future__ import annotations

import pytest

from controller.audit import Outcome
from controller.main import TurnResult
from controller.turn_events import (
    CotEvent,
    ErrorEvent,
    ProgressEvent,
    ResultEvent,
    TokenEvent,
    TurnEvent,
)


# ── 1: ProgressEvent construction ────────────────────────────────────────────


def test_progress_event_construction():
    ev = ProgressEvent(
        step_name="qb_intent",
        step_label="Generating intent...",
        step_index=0,
        total_steps=14,
        elapsed_ms=12.5,
    )
    assert ev.step_name == "qb_intent"
    assert ev.step_label == "Generating intent..."
    assert ev.step_index == 0
    assert ev.total_steps == 14
    assert ev.elapsed_ms == 12.5


# ── 2: TokenEvent construction ──────────────────────────────────────────────


def test_token_event_construction():
    ev = TokenEvent(token="hello", accumulated="hello", final=False)
    assert ev.token == "hello"
    assert ev.accumulated == "hello"
    assert ev.final is False


def test_token_event_final_flag():
    ev = TokenEvent(token="", accumulated="full text", final=True)
    assert ev.final is True
    assert ev.token == ""
    assert ev.accumulated == "full text"


# ── 3: ResultEvent wraps TurnResult ─────────────────────────────────────────


def test_result_event_wraps_turn_result():
    tr = TurnResult(
        success=True,
        output="System is running.",
        outcome=Outcome.EXECUTED,
        tier=0,
        backend="local",
        duration_ms=100.0,
    )
    ev = ResultEvent(result=tr)
    assert ev.result is tr
    assert ev.result.success is True
    assert ev.result.output == "System is running."
    assert ev.result.outcome == Outcome.EXECUTED


# ── 4-5: ErrorEvent construction ────────────────────────────────────────────


def test_error_event_default_cancelled_at_step():
    ev = ErrorEvent(error_type="RuntimeError", message="something broke")
    assert ev.error_type == "RuntimeError"
    assert ev.message == "something broke"
    assert ev.cancelled_at_step == ""


def test_error_event_explicit_cancelled_at_step():
    ev = ErrorEvent(
        error_type="McpdTimeoutError",
        message="mcpd timed out",
        cancelled_at_step="mcpd_dispatch",
    )
    assert ev.cancelled_at_step == "mcpd_dispatch"


# ── 6: TurnEvent union type discrimination ──────────────────────────────────


def test_turn_event_isinstance_progress():
    ev = ProgressEvent(
        step_name="audit", step_label="Recording...",
        step_index=13, total_steps=14, elapsed_ms=500.0,
    )
    assert isinstance(ev, ProgressEvent)
    assert not isinstance(ev, TokenEvent)
    assert not isinstance(ev, ResultEvent)
    assert not isinstance(ev, ErrorEvent)


def test_turn_event_isinstance_token():
    ev = TokenEvent(token="x", accumulated="x", final=False)
    assert isinstance(ev, TokenEvent)
    assert not isinstance(ev, ProgressEvent)


def test_turn_event_isinstance_result():
    tr = TurnResult(success=False, output="err", outcome=Outcome.BRAIN_ERROR)
    ev = ResultEvent(result=tr)
    assert isinstance(ev, ResultEvent)
    assert not isinstance(ev, ErrorEvent)


def test_turn_event_isinstance_error():
    ev = ErrorEvent(error_type="E", message="m")
    assert isinstance(ev, ErrorEvent)
    assert not isinstance(ev, ResultEvent)


# ── 7: Frozen immutability ──────────────────────────────────────────────────


def test_progress_event_is_frozen():
    ev = ProgressEvent(
        step_name="qb_intent", step_label="Generating...",
        step_index=0, total_steps=14, elapsed_ms=0.0,
    )
    with pytest.raises(AttributeError):
        ev.step_name = "changed"


def test_token_event_is_frozen():
    ev = TokenEvent(token="t", accumulated="t", final=False)
    with pytest.raises(AttributeError):
        ev.final = True


def test_result_event_is_frozen():
    tr = TurnResult(success=True, output="ok", outcome=Outcome.EXECUTED)
    ev = ResultEvent(result=tr)
    with pytest.raises(AttributeError):
        ev.result = None


def test_error_event_is_frozen():
    ev = ErrorEvent(error_type="E", message="m")
    with pytest.raises(AttributeError):
        ev.message = "changed"


# ── 8: All field access works ───────────────────────────────────────────────


def test_progress_event_all_fields_accessible():
    ev = ProgressEvent(
        step_name="schema_validation",
        step_label="Validating schema...",
        step_index=1,
        total_steps=14,
        elapsed_ms=25.3,
    )
    fields = ["step_name", "step_label", "step_index", "total_steps", "elapsed_ms"]
    for f in fields:
        assert hasattr(ev, f), f"Missing field: {f}"


def test_error_event_all_fields_accessible():
    ev = ErrorEvent(
        error_type="ValueError",
        message="bad data",
        cancelled_at_step="tool_validation",
    )
    assert ev.error_type == "ValueError"
    assert ev.message == "bad data"
    assert ev.cancelled_at_step == "tool_validation"


# ── CotEvent construction ──────────────────────────────────────────────────


def test_cot_event_construction():
    ev = CotEvent(
        step_index=0,
        step_name="qb_intent",
        step_state="active",
        heading="Intent Generation",
        body="Parsing natural language",
        data={"action": "system.status"},
        timestamp_ms=42.5,
    )
    assert ev.step_index == 0
    assert ev.step_name == "qb_intent"
    assert ev.step_state == "active"
    assert ev.heading == "Intent Generation"
    assert ev.body == "Parsing natural language"
    assert ev.data == {"action": "system.status"}
    assert ev.timestamp_ms == 42.5


def test_cot_event_valid_states():
    for state in ("pending", "active", "done", "failed"):
        ev = CotEvent(
            step_index=0, step_name="test", step_state=state,
            heading="h", body="", data={}, timestamp_ms=0.0,
        )
        assert ev.step_state == state


def test_cot_event_invalid_state_raises():
    with pytest.raises(ValueError, match="step_state must be one of"):
        CotEvent(
            step_index=0, step_name="test", step_state="invalid",
            heading="h", body="", data={}, timestamp_ms=0.0,
        )


def test_cot_event_is_frozen():
    ev = CotEvent(
        step_index=0, step_name="test", step_state="done",
        heading="h", body="", data={}, timestamp_ms=0.0,
    )
    with pytest.raises(AttributeError):
        ev.step_state = "failed"


def test_cot_event_isinstance():
    ev = CotEvent(
        step_index=0, step_name="test", step_state="done",
        heading="h", body="", data={}, timestamp_ms=0.0,
    )
    assert isinstance(ev, CotEvent)
    assert not isinstance(ev, ProgressEvent)
    assert not isinstance(ev, TokenEvent)
    assert not isinstance(ev, ResultEvent)
    assert not isinstance(ev, ErrorEvent)


def test_cot_event_empty_data():
    ev = CotEvent(
        step_index=5, step_name="intent_store", step_state="done",
        heading="Intent Store", body="Stored",
        data={}, timestamp_ms=100.0,
    )
    assert ev.data == {}


def test_cot_event_rich_data():
    ev = CotEvent(
        step_index=2, step_name="risk_classification", step_state="done",
        heading="Risk Classification",
        body="Tier 1 — read-only",
        data={"tier": 1, "reason": "read-only", "reversible": True},
        timestamp_ms=55.0,
    )
    assert ev.data["tier"] == 1
    assert ev.data["reversible"] is True
