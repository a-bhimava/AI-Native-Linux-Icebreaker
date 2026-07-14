"""v6.8 Task #147 — bridge regression tests.

Mocks AgentGraph + HitlPresenter and asserts the bridge:
- Emits the minimum-viable event set (Progress + CoT + Result) per §D7
- Reuses ForwardingPresenter for HITL pause/resume per §D8
- Translates every outcome kind (executed/paused-then-approve/paused-
  then-deny/error) to the right terminal ResultEvent
- Never infinite-loops when the intent lookup fails on pause
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from controller.agent_graph_bridge import (
    _first,
    _lookup_intent,
    _traversed_nodes_from_outcome,
    run_via_agent_graph,
    translate_interrupt_payload_to_display_data,
    translate_outcome_to_events,
)
from controller.audit import Outcome
from controller.hitl import Decision, HitlDisplayData
from controller.turn_events import (
    CotEvent,
    ErrorEvent,
    ProgressEvent,
    ResultEvent,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _executed_outcome(*, tier: int = 0) -> dict:
    return {
        "outcome": "executed",
        "session_id": "s",
        "turn_id": "t",
        "intent_id": "iid",
        "tier": tier,
        "tool_call_hash": "hh",
        "mcpd_result_hash": "rh",
        "error_kind": None,
        "error_reason": None,
    }


def _paused_outcome(*, tier: int = 2) -> dict:
    return {
        "outcome": "paused",
        "session_id": "s",
        "turn_id": "t",
        "intent_id": "iid",
        "tier": tier,
        "tool_call_hash": "",
        "mcpd_result_hash": "",
        "error_kind": None,
        "error_reason": None,
    }


def _error_outcome(kind: str = "planner", reason: str = "boom") -> dict:
    return {
        "outcome": "error",
        "session_id": "s",
        "turn_id": "t",
        "intent_id": "" if kind == "planner" else "iid",
        "tier": -1 if kind == "planner" else 2,
        "tool_call_hash": "",
        "mcpd_result_hash": "",
        "error_kind": kind,
        "error_reason": reason,
    }


def _session_ns(session_id: str = "s") -> SimpleNamespace:
    return SimpleNamespace(session_id=session_id)


# ── translate_interrupt_payload_to_display_data ──────────────────────


def test_translate_interrupt_produces_display_data():
    intent = {
        "action": "package.install", "target": "htop",
        "reason": "user wants htop", "risk_level": "medium",
    }
    outcome = _paused_outcome(tier=2)
    display = translate_interrupt_payload_to_display_data(intent, outcome, "gemini")
    assert isinstance(display, HitlDisplayData)
    assert display.action == "package.install"
    assert display.target == "htop"
    assert display.backend == "gemini"
    assert int(display.tier) == 2


def test_translate_interrupt_falls_back_to_medium_tier_on_bad_int():
    intent = {"action": "a", "target": "t", "reason": "r", "risk_level": "medium"}
    outcome = {**_paused_outcome(), "tier": 99}  # invalid Tier value
    display = translate_interrupt_payload_to_display_data(intent, outcome, "b")
    from controller.risk_classifier import Tier
    assert display.tier == Tier.MEDIUM


# ── _traversed_nodes_from_outcome ────────────────────────────────────


def test_traversed_nodes_tier0_executed():
    seen = _traversed_nodes_from_outcome(_executed_outcome(tier=0))
    assert seen == ["planner", "risk_classifier", "executor", "mcpd_dispatcher", "responder"]


def test_traversed_nodes_tier2_executed_includes_verifier_hitl():
    seen = _traversed_nodes_from_outcome(_executed_outcome(tier=2))
    assert "verifier" in seen
    assert "hitl_gate" in seen
    assert "mcpd_dispatcher" in seen


def test_traversed_nodes_denied_stops_at_hitl():
    denied = {**_paused_outcome(tier=2), "outcome": "denied"}
    seen = _traversed_nodes_from_outcome(denied)
    assert seen == ["planner", "risk_classifier", "verifier", "hitl_gate"]


def test_traversed_nodes_planner_error_stops_at_planner():
    seen = _traversed_nodes_from_outcome(_error_outcome("planner"))
    assert seen == ["planner"]


def test_traversed_nodes_pb_error_stops_at_executor():
    seen = _traversed_nodes_from_outcome(_error_outcome("pb"))
    assert seen[-1] == "executor"


# ── translate_outcome_to_events ──────────────────────────────────────


def test_translate_outcome_emits_progress_cot_and_result_for_executed():
    events = translate_outcome_to_events(_executed_outcome(tier=0), time.monotonic(), "gemini")
    types = [type(e) for e in events]
    assert types.count(ProgressEvent) == 5   # planner, risk, executor, mcpd, responder
    assert types.count(CotEvent) == 5
    assert types.count(ResultEvent) == 1
    assert types.count(ErrorEvent) == 0
    # Terminal ResultEvent has success=True, outcome=EXECUTED.
    result_event = next(e for e in events if isinstance(e, ResultEvent))
    assert result_event.result.success is True
    assert result_event.result.outcome == Outcome.EXECUTED


def test_translate_outcome_emits_error_event_and_failed_cot_on_planner_error():
    events = translate_outcome_to_events(_error_outcome("planner", "gemini rate-limited"),
                                          time.monotonic(), "gemini")
    types = [type(e) for e in events]
    assert types.count(ErrorEvent) == 1
    assert types.count(ResultEvent) == 1
    error = next(e for e in events if isinstance(e, ErrorEvent))
    assert error.error_type == "planner"
    assert "rate" in error.message
    result_event = next(e for e in events if isinstance(e, ResultEvent))
    assert result_event.result.success is False
    assert result_event.result.outcome == Outcome.BRAIN_ERROR
    # The planner CoT should be marked failed.
    planner_cot = next(
        e for e in events if isinstance(e, CotEvent) and e.step_name == "planner"
    )
    assert planner_cot.step_state == "failed"


def test_translate_outcome_denied_maps_to_hitl_denied():
    denied = {**_paused_outcome(tier=2), "outcome": "denied"}
    events = translate_outcome_to_events(denied, time.monotonic(), "gemini")
    result_event = next(e for e in events if isinstance(e, ResultEvent))
    assert result_event.result.outcome == Outcome.HITL_DENIED
    assert result_event.result.success is False


# ── run_via_agent_graph — end-to-end ────────────────────────────────


def _make_agent_mock(outcomes: list[dict], intent: dict | None = None) -> MagicMock:
    """Return a MagicMock AgentGraph whose run/resume yield the given
    outcomes in sequence."""
    ag = MagicMock()
    ag._turn_content = {"iid": intent} if intent else {}

    # Both run and resume yield ONE outcome per call. Keep an index.
    calls = {"n": 0}

    def _one_shot(*args, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        # Yield the outcome; if we've exhausted, yield the last again.
        yield outcomes[min(i, len(outcomes) - 1)]

    ag.run.side_effect = _one_shot
    ag.resume.side_effect = _one_shot
    return ag


def test_run_via_agent_graph_tier0_executed_yields_events():
    ag = _make_agent_mock([_executed_outcome(tier=0)])
    presenter = MagicMock()
    events = list(run_via_agent_graph(ag, presenter, "hi", _session_ns()))
    types = [type(e) for e in events]
    assert ResultEvent in types
    result_event = next(e for e in events if isinstance(e, ResultEvent))
    assert result_event.result.outcome == Outcome.EXECUTED
    # Presenter never invoked on a Tier-0 executed path.
    presenter.show_prompt.assert_not_called()


def test_run_via_agent_graph_tier2_pause_then_approve_calls_resume():
    intent = {"action": "package.install", "target": "htop",
              "reason": "install htop", "risk_level": "medium"}
    ag = _make_agent_mock(
        [_paused_outcome(tier=2), _executed_outcome(tier=2)],
        intent=intent,
    )
    presenter = MagicMock()
    presenter.read_decision.return_value = Decision.APPROVED

    events = list(run_via_agent_graph(
        ag, presenter, "install htop", _session_ns(),
    ))
    presenter.show_prompt.assert_called_once()
    presenter.read_decision.assert_called_once()
    ag.resume.assert_called_once_with("s", "approve")

    result_event = next(e for e in events if isinstance(e, ResultEvent))
    assert result_event.result.outcome == Outcome.EXECUTED


def test_run_via_agent_graph_tier2_pause_then_deny_yields_hitl_denied():
    intent = {"action": "package.install", "target": "htop",
              "reason": "install", "risk_level": "medium"}
    ag = _make_agent_mock(
        [
            _paused_outcome(tier=2),
            {**_paused_outcome(tier=2), "outcome": "denied"},
        ],
        intent=intent,
    )
    presenter = MagicMock()
    presenter.read_decision.return_value = Decision.DENIED

    events = list(run_via_agent_graph(ag, presenter, "install htop", _session_ns()))
    ag.resume.assert_called_once_with("s", "deny")
    result_event = next(e for e in events if isinstance(e, ResultEvent))
    assert result_event.result.outcome == Outcome.HITL_DENIED


def test_run_via_agent_graph_planner_error_short_circuits():
    ag = _make_agent_mock([_error_outcome("planner", "gemini down")])
    presenter = MagicMock()
    events = list(run_via_agent_graph(ag, presenter, "hi", _session_ns()))
    presenter.show_prompt.assert_not_called()
    result_event = next(e for e in events if isinstance(e, ResultEvent))
    assert result_event.result.outcome == Outcome.BRAIN_ERROR


def test_run_via_agent_graph_pause_with_missing_intent_bails_out():
    """If the intent lookup fails after a pause, we treat as internal
    error rather than infinite-looping the resume path."""
    ag = _make_agent_mock([_paused_outcome(tier=2)], intent=None)
    ag._turn_content = {}  # explicitly empty
    presenter = MagicMock()
    events = list(run_via_agent_graph(ag, presenter, "hi", _session_ns()))
    presenter.show_prompt.assert_not_called()
    result_event = next(e for e in events if isinstance(e, ResultEvent))
    assert result_event.result.outcome == Outcome.BRAIN_ERROR
    assert "intent lookup" in result_event.result.output


def test_run_via_agent_graph_presenter_failure_denies():
    """If the presenter raises, we bail with an internal error rather
    than crashing the caller."""
    intent = {"action": "package.install", "target": "htop",
              "reason": "install", "risk_level": "medium"}
    ag = _make_agent_mock([_paused_outcome(tier=2)], intent=intent)
    presenter = MagicMock()
    presenter.show_prompt.side_effect = RuntimeError("presenter is dead")
    events = list(run_via_agent_graph(ag, presenter, "install htop", _session_ns()))
    result_event = next(e for e in events if isinstance(e, ResultEvent))
    assert result_event.result.outcome == Outcome.BRAIN_ERROR
    # Never called resume — presenter failure short-circuits.
    ag.resume.assert_not_called()


# ── _first / _lookup_intent ─────────────────────────────────────────


def test_first_returns_none_when_generator_empty():
    def empty():
        if False:
            yield  # pragma: no cover
    assert _first(empty()) is None


def test_first_returns_first_value():
    def one():
        yield {"x": 1}
        yield {"x": 2}
    assert _first(one()) == {"x": 1}


def test_lookup_intent_returns_none_on_empty_id():
    assert _lookup_intent(MagicMock(_turn_content={"iid": {"a": 1}}), "") is None


def test_lookup_intent_returns_none_on_non_dict():
    assert _lookup_intent(
        MagicMock(_turn_content={"iid": "not-a-dict"}),
        "iid",
    ) is None


def test_lookup_intent_returns_intent_dict():
    assert _lookup_intent(
        MagicMock(_turn_content={"iid": {"action": "fs.list"}}),
        "iid",
    ) == {"action": "fs.list"}
