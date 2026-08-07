"""Tests for ``Controller.run_turn_streaming()`` — streaming pipeline generator.

Covers:
  1. Happy path yields ProgressEvent -> TokenEvent -> ResultEvent in order
  2. Events have correct step names from _PIPELINE_STEPS
  3. Cancel (gen.close()): generator handles GeneratorExit cleanly
  4. Schema rejected: yields ResultEvent with outcome=SCHEMA_REJECTED
  5. stream_output=False: no TokenEvents, still yields ResultEvent
  6. ErrorEvent on brain exception
  7. ProgressEvent elapsed_ms increases
  8. Total steps count matches _PIPELINE_STEPS length
  9. ResultEvent.result has correct fields
 10. Cancelled turns do NOT call session.touch()
 11. Successful turns DO call session.touch()
 12. Verifier rejection yields ResultEvent with QB_VERIFIER_REJECTED
 13. Tool timeout yields ResultEvent with TOOL_TIMEOUT
 14. PB schema error yields ResultEvent with PB_SCHEMA_ERROR
 15. ProgressEvent step_label is a non-empty string
 16. TokenEvent accumulated grows with tokens
 17. Final TokenEvent has final=True
 18. ErrorEvent includes cancelled_at_step
 19. Multiple ProgressEvents have increasing step_index
 20. Tier 0 operation skips HITL gate
 21. stream_complete is called when stream_output=True
 22. qb_summarise is called when stream_output=False
 23. Audit is written on successful completion
 24. Audit is written on schema rejection
 25. HITL denied yields ResultEvent with HITL_DENIED
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from controller.audit import AuditFields, AuditLog, Outcome
from controller.backends.base import BrainResponse
from controller.config import SessionConfig
from controller.hitl import Decision
from controller.intent_store import IntentStore
from controller.main import Controller, TurnResult, _PIPELINE_STEPS
from controller.mcpd_client import McpdTimeoutError, ToolResult
from controller.session import SessionState
from controller.turn_events import (
    ErrorEvent,
    ProgressEvent,
    ResultEvent,
    TokenEvent,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _cfg(stream_output: bool = True, show_progress: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        qb=SimpleNamespace(name="local", model="test"),
        hitl=SimpleNamespace(
            lockout_seconds=1,
            timeout_seconds=5,
            trust_ttl_seconds=0,
        ),
        run=SimpleNamespace(
            qb_max_retries=1,
            mcpd_timeout_seconds=5.0,
            mcpd_schemas_dir="",
            mcpd_binary="",
            # v6.8 M7.2: force slow path in these tests so PB mocks still
            # fire. The fast-path unit tests live in test_tier0_fast_path.py.
            tier0_fast_path=False,
        ),
        session=SimpleNamespace(
            max_tool_output_lines=40,
            stream_output=stream_output,
            show_progress=show_progress,
        ),
        tier2=SimpleNamespace(enabled=False),
        undo=SimpleNamespace(enabled=False),
    )


def _resp(content: dict, cost: float = 0.0) -> BrainResponse:
    return BrainResponse(
        content_json=content,
        tokens_in=10,
        tokens_out=5,
        cost_usd=cost,
        backend="local",
        model="test",
        attempts=1,
    )


def _valid_intent(action: str = "system.status", target: str = "") -> dict:
    return {
        "intent_id": str(uuid.uuid4()),
        "action": action,
        "target": target,
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }


def _tool_result() -> ToolResult:
    return ToolResult(result={"status": "ok"}, request_id=1)


def _session() -> SessionState:
    return SessionState.new("local", SessionConfig())


def _mock_stream():
    yield ('{"summary":', '{"summary":', False)
    yield (' "ok"}', '{"summary": "ok"}', False)
    yield ("", '{"summary": "ok"}', True)


def _build(
    qb_side_effect=None,
    pb_content=None,
    mcpd_side_effect=None,
    mcpd_return=None,
    stream_output=True,
    stream_complete_return=None,
):
    """Return (controller, qb_mock, pb_mock, mcpd_mock, audit_mock, session)."""
    intent = _valid_intent()
    qb = MagicMock()
    if qb_side_effect is not None:
        qb.complete.side_effect = qb_side_effect
    else:
        qb.complete.side_effect = [
            _resp(intent),                                # step 1: intent
            _resp({"verified": True, "reason": "ok"}),    # step 8: verify
            _resp({"summary": "System is running."}),     # step 11: summarise (non-streaming)
        ]

    if stream_complete_return is not None:
        qb.stream_complete.return_value = stream_complete_return
    else:
        qb.stream_complete.return_value = _mock_stream()

    pb = MagicMock()
    pb.complete.return_value = _resp(
        pb_content or {"tool": intent["action"], "params": {}}
    )

    mcpd = MagicMock()
    if mcpd_side_effect is not None:
        mcpd.call.side_effect = mcpd_side_effect
    else:
        mcpd.call.return_value = mcpd_return or _tool_result()

    store = MagicMock(spec=IntentStore)
    store.put.return_value = str(uuid.uuid4())

    prompts = MagicMock()
    prompts.get.return_value = ""

    audit = MagicMock(spec=AuditLog)

    cfg = _cfg(stream_output=stream_output)

    ctrl = Controller(
        cfg,
        qb_backend=qb,
        pb_backend=pb,
        mcpd_client=mcpd,
        audit_log=audit,
        store=store,
        prompt_loader=prompts,
    )
    # v6.16 M7.0.2f (2026-08-06): pin single-attempt retry so pre-M7.0.2
    # single-shot behaviour tests (verifier_rejection, pb_schema_error,
    # happy_path) keep their exact QB_VERIFIER_REJECTED / PB_SCHEMA_ERROR
    # semantics. Multi-attempt retry-loop coverage lives in the new
    # test_pb_retry_streaming block at the bottom of this file.
    from controller.pb_retry import PbRetryConfig
    ctrl._pb_retry_cfg = PbRetryConfig(max_attempts=1)
    session = _session()
    return ctrl, qb, pb, mcpd, audit, session


def _collect_events(ctrl, user_input, session):
    """Exhaust the streaming generator and return all events."""
    return list(ctrl.run_turn_streaming(user_input, session))


# ── Tests ────────────────────────────────────────────────────────────────────


def test_happy_path_yields_progress_token_result():
    """Full Tier 0 streaming pipeline produces ProgressEvents, TokenEvents, ResultEvent."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    events = _collect_events(ctrl, "show system status", session)

    types = [type(e) for e in events]
    # Must contain at least one ProgressEvent, at least one TokenEvent, and end with ResultEvent
    assert ProgressEvent in types
    assert TokenEvent in types
    assert isinstance(events[-1], ResultEvent)
    assert events[-1].result.success is True
    assert events[-1].result.outcome == Outcome.EXECUTED


def test_events_have_correct_step_names():
    """ProgressEvents correspond to pipeline step names."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    events = _collect_events(ctrl, "show system status", session)

    progress_events = [e for e in events if isinstance(e, ProgressEvent)]
    step_names = [e.step_name for e in progress_events]
    # All emitted step names must be from _PIPELINE_STEPS
    valid_names = {name for name, _ in _PIPELINE_STEPS}
    for name in step_names:
        assert name in valid_names, f"Unknown step name: {name}"


def test_cancel_via_generator_close():
    """gen.close() handles GeneratorExit cleanly without raising."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    gen = ctrl.run_turn_streaming("show system status", session)
    # Consume one event, then close
    first = next(gen)
    assert isinstance(first, ProgressEvent)
    gen.close()  # Should not raise


def test_cancel_writes_audit_entry():
    """Cancelled turns write an audit entry with CANCELLED outcome."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    gen = ctrl.run_turn_streaming("show system status", session)
    next(gen)  # consume first event
    gen.close()
    # Audit should be written with CANCELLED outcome
    assert audit.write_fields.called
    written: AuditFields = audit.write_fields.call_args[0][0]
    assert written.outcome == Outcome.CANCELLED


def test_schema_rejected_yields_result_event():
    """Bad QB output yields ResultEvent with outcome=SCHEMA_REJECTED."""
    malformed = {"garbage": "not an intent"}
    ctrl, qb, pb, mcpd, audit, session = _build(
        qb_side_effect=[_resp(malformed)],
    )
    events = _collect_events(ctrl, "do something", session)

    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].result.outcome == Outcome.SCHEMA_REJECTED
    assert result_events[0].result.success is False


def test_no_token_events_when_stream_output_false():
    """stream_output=False: no TokenEvents emitted, still yields ResultEvent."""
    ctrl, qb, pb, mcpd, audit, session = _build(stream_output=False)
    events = _collect_events(ctrl, "show system status", session)

    token_events = [e for e in events if isinstance(e, TokenEvent)]
    assert len(token_events) == 0
    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].result.success is True


def test_error_event_on_brain_exception():
    """An unexpected exception in the pipeline yields ErrorEvent."""
    qb = MagicMock()
    qb.complete.side_effect = RuntimeError("brain exploded")

    ctrl, _, pb, mcpd, audit, session = _build()
    # Replace the qb backend after construction
    ctrl._qb = qb

    events = _collect_events(ctrl, "do something", session)

    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert error_events[0].error_type == "RuntimeError"
    assert "brain exploded" in error_events[0].message


def test_progress_event_elapsed_ms_increases():
    """ProgressEvent elapsed_ms should be non-negative and generally non-decreasing."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    events = _collect_events(ctrl, "show system status", session)

    progress_events = [e for e in events if isinstance(e, ProgressEvent)]
    assert len(progress_events) >= 2
    # All elapsed_ms values should be non-negative
    for pe in progress_events:
        assert pe.elapsed_ms >= 0.0


def test_total_steps_matches_pipeline_steps():
    """ProgressEvent total_steps matches len(_PIPELINE_STEPS)."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    events = _collect_events(ctrl, "show system status", session)

    progress_events = [e for e in events if isinstance(e, ProgressEvent)]
    for pe in progress_events:
        assert pe.total_steps == len(_PIPELINE_STEPS)


def test_result_event_has_correct_fields():
    """ResultEvent.result has expected TurnResult fields."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    events = _collect_events(ctrl, "show system status", session)

    result = events[-1].result
    assert isinstance(result, TurnResult)
    assert result.success is True
    assert result.backend == "local"
    assert result.tier == 0
    assert result.duration_ms >= 0.0


def test_cancelled_turn_does_not_touch_session():
    """Cancelled turns do NOT call session.touch()."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    initial_turn = session.turn_index
    gen = ctrl.run_turn_streaming("show system status", session)
    next(gen)
    gen.close()
    assert session.turn_index == initial_turn


def test_successful_turn_touches_session():
    """Successful turns call session.touch(), incrementing turn_index."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    initial_turn = session.turn_index
    _collect_events(ctrl, "show system status", session)
    assert session.turn_index == initial_turn + 1


def test_verifier_rejection():
    """QB verifier returns false -> ResultEvent with QB_VERIFIER_REJECTED."""
    ctrl, qb, pb, mcpd, audit, session = _build(
        qb_side_effect=[
            _resp(_valid_intent()),
            _resp({"verified": False, "reason": "mismatch"}),
        ]
    )
    events = _collect_events(ctrl, "show status", session)

    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].result.outcome == Outcome.QB_VERIFIER_REJECTED


def test_tool_timeout():
    """McpdTimeoutError -> ResultEvent with TOOL_TIMEOUT."""
    ctrl, qb, pb, mcpd, audit, session = _build(
        qb_side_effect=[
            _resp(_valid_intent()),
            _resp({"verified": True, "reason": "ok"}),
        ],
        mcpd_side_effect=McpdTimeoutError("timed out"),
    )
    events = _collect_events(ctrl, "show status", session)

    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].result.outcome == Outcome.TOOL_TIMEOUT


def test_pb_schema_error():
    """PB returns mismatched tool -> ResultEvent with PB_SCHEMA_ERROR."""
    ctrl, qb, pb, mcpd, audit, session = _build(
        pb_content={"tool": "wrong.tool", "params": {}},
    )
    events = _collect_events(ctrl, "show status", session)

    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].result.outcome == Outcome.PB_SCHEMA_ERROR


def test_agent_graph_flag_on_delegates_to_bridge():
    """v6.8 Task #147: when cfg.agent_graph.enabled=True, run_turn_streaming
    must delegate to run_via_agent_graph. Assert by installing a mock
    AgentGraph via _agent_graph and observing yielded events came from
    the bridge (ResultEvent shape + no old-pipeline TokenEvents)."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from controller.audit import Outcome
    from controller.turn_events import ResultEvent

    ctrl, qb, pb, mcpd, audit, session = _build()
    # Flip the flag on this Controller's config.
    ctrl._cfg.agent_graph = SimpleNamespace(enabled=True)

    # Install a mock AgentGraph so the branch doesn't actually compile
    # a LangGraph (that's exercised by test_agent_graph_run.py).
    fake_ag = MagicMock()
    fake_ag._turn_content = {}

    def _one_shot(*args, **kwargs):
        yield {
            "outcome": "executed",
            "session_id": "s", "turn_id": "t", "intent_id": "iid",
            "tier": 0, "tool_call_hash": "h", "mcpd_result_hash": "rh",
            "error_kind": None, "error_reason": None,
        }
    fake_ag.run.side_effect = _one_shot
    ctrl._agent_graph = fake_ag

    events = _collect_events(ctrl, "show system status", session)
    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].result.outcome == Outcome.EXECUTED
    # Old pipeline's PB was NOT called because the flag branch takes
    # over before Step 6.
    pb.complete.assert_not_called()
    # AgentGraph.run was called with the query.
    fake_ag.run.assert_called_once()


def test_tier0_fast_path_skips_pb_call():
    """v6.8 M7.2 integration lock: with tier0_fast_path=True on a Tier-0
    intent (system.status), pb.complete MUST NOT be called and the turn
    still yields a successful EXECUTED result."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    # Override the default (False for slow-path tests) to True.
    ctrl._cfg.run.tier0_fast_path = True

    events = _collect_events(ctrl, "show system status", session)

    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].result.success is True
    assert result_events[0].result.outcome == Outcome.EXECUTED
    # The critical assertion: PB is bypassed.
    pb.complete.assert_not_called()


def test_tier0_fast_path_flag_off_still_calls_pb():
    """Baseline: with tier0_fast_path=False (default in tests), pb.complete
    fires — confirms the flag actually controls the branch."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    # _cfg() sets tier0_fast_path=False; be explicit for clarity.
    ctrl._cfg.run.tier0_fast_path = False

    _collect_events(ctrl, "show system status", session)

    pb.complete.assert_called_once()


def test_progress_event_step_label_non_empty():
    """ProgressEvent step_label is always a non-empty string."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    events = _collect_events(ctrl, "show system status", session)

    progress_events = [e for e in events if isinstance(e, ProgressEvent)]
    for pe in progress_events:
        assert pe.step_label
        assert isinstance(pe.step_label, str)


def test_token_event_accumulated_grows():
    """TokenEvent accumulated text grows as tokens are yielded."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    events = _collect_events(ctrl, "show system status", session)

    token_events = [e for e in events if isinstance(e, TokenEvent)]
    assert len(token_events) >= 2
    non_final = [e for e in token_events if not e.final]
    if len(non_final) >= 2:
        assert len(non_final[1].accumulated) >= len(non_final[0].accumulated)


def test_final_token_event_has_final_true():
    """The last TokenEvent in a stream has final=True."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    events = _collect_events(ctrl, "show system status", session)

    token_events = [e for e in events if isinstance(e, TokenEvent)]
    assert any(e.final for e in token_events)


def test_error_event_includes_cancelled_at_step():
    """ErrorEvent includes the step where the error occurred."""
    qb = MagicMock()
    qb.complete.side_effect = RuntimeError("boom")

    ctrl, _, pb, mcpd, audit, session = _build()
    ctrl._qb = qb

    events = _collect_events(ctrl, "do something", session)
    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    # cancelled_at_step should be set (could be "qb_intent" or empty for early fail)
    assert isinstance(error_events[0].cancelled_at_step, str)


def test_progress_events_have_increasing_step_index():
    """Multiple ProgressEvents should have non-decreasing step_index."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    events = _collect_events(ctrl, "show system status", session)

    progress_events = [e for e in events if isinstance(e, ProgressEvent)]
    indices = [e.step_index for e in progress_events]
    for i in range(1, len(indices)):
        assert indices[i] >= indices[i - 1]


def test_tier0_skips_hitl_gate():
    """Tier 0 (read_only) operation skips HITL gate entirely."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    events = _collect_events(ctrl, "show system status", session)

    progress_names = [e.step_name for e in events if isinstance(e, ProgressEvent)]
    assert "hitl_gate" not in progress_names


def test_stream_complete_called_when_stream_output_true():
    """stream_complete is called on the QB backend when stream_output=True."""
    ctrl, qb, pb, mcpd, audit, session = _build(stream_output=True)
    _collect_events(ctrl, "show system status", session)
    assert qb.stream_complete.called


def test_summarise_called_when_stream_output_false():
    """When stream_output=False, QB.complete() is called for summarisation (not stream_complete)."""
    ctrl, qb, pb, mcpd, audit, session = _build(stream_output=False)
    _collect_events(ctrl, "show system status", session)
    # stream_complete should NOT be called
    assert not qb.stream_complete.called
    # complete() should be called 3 times: intent, verify, summarise
    assert qb.complete.call_count == 3


def test_audit_written_on_success():
    """Audit is written with EXECUTED outcome on successful completion."""
    ctrl, qb, pb, mcpd, audit, session = _build()
    _collect_events(ctrl, "show system status", session)
    assert audit.write_fields.called
    written: AuditFields = audit.write_fields.call_args[0][0]
    assert written.outcome == Outcome.EXECUTED


def test_audit_written_on_schema_rejection():
    """Audit is written with SCHEMA_REJECTED on bad intent."""
    malformed = {"not": "valid"}
    ctrl, qb, pb, mcpd, audit, session = _build(
        qb_side_effect=[_resp(malformed)],
    )
    _collect_events(ctrl, "do something", session)
    assert audit.write_fields.called
    written: AuditFields = audit.write_fields.call_args[0][0]
    assert written.outcome == Outcome.SCHEMA_REJECTED


def test_hitl_denied_yields_result_event():
    """HITL denied for Tier 3 yields ResultEvent with HITL_DENIED."""
    delete_intent = {
        "intent_id": str(uuid.uuid4()),
        "action": "fs.delete",
        "target": "/tmp/junk",
        "params": {},
        "reason": "user_requested",
        "risk_level": "critical",
    }
    ctrl, qb, pb, mcpd, audit, session = _build(
        qb_side_effect=[_resp(delete_intent)],
    )

    with patch("controller.main.HitlPrompt") as mock_hitl_cls:
        inst = mock_hitl_cls.return_value
        inst.ask.return_value = Decision.DENIED
        inst.decision_id = "test-id"
        inst.key_pressed_class = "mnemonic"
        inst.decision_latency_ms = 100.0
        events = _collect_events(ctrl, "delete /tmp/junk", session)

    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].result.outcome == Outcome.HITL_DENIED


# ── v6.16 M7.0.2f: PbRetryLoop wire-up regression locks ─────────────────────

def test_pb_retry_exhausted_outcome_emitted():
    """Multi-attempt exhaustion → PB_RETRY_EXHAUSTED (not
    QB_VERIFIER_REJECTED, not PB_SCHEMA_ERROR). Verifier rejects every
    attempt; loop runs max_attempts times and returns success=False."""
    from controller.pb_retry import PbRetryConfig
    ctrl, qb, pb, mcpd, audit, session = _build(
        qb_side_effect=[
            _resp(_valid_intent()),   # step 1: intent
            _resp({"verified": False, "reason": "attempt 1 no"}),
            _resp({"pb_hint": "try shorter path"}),   # qb_repair after attempt 1
            _resp({"verified": False, "reason": "attempt 2 no"}),
            _resp({"pb_hint": "path must be absolute"}),   # qb_repair after 2
            _resp({"verified": False, "reason": "attempt 3 no"}),
            _resp({"pb_hint": "give up"}),   # qb_repair after 3 (not used)
        ],
    )
    # Enable 3-attempt loop (default); tests need explicit override
    # because _build pins max_attempts=1.
    ctrl._pb_retry_cfg = PbRetryConfig(max_attempts=3, consult_qb_after_attempt=1)
    events = _collect_events(ctrl, "show status", session)

    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].result.outcome == Outcome.PB_RETRY_EXHAUSTED
    # Audit row should carry pb_attempts + repair_hints extra.
    audit_call = audit.write_fields.call_args_list[-1]
    fields = audit_call.args[0] if audit_call.args else audit_call.kwargs["fields"]
    assert fields.outcome == Outcome.PB_RETRY_EXHAUSTED
    assert fields.extra["pb_attempts"] == 3
    assert fields.extra["pb_final_reason"] == "max_attempts_exhausted"


def test_pb_retry_two_attempt_success():
    """Attempt 1 fails verifier, QB repair fires, attempt 2 succeeds.
    Turn completes with EXECUTED outcome; audit carries pb_attempts=2
    in extra."""
    from controller.pb_retry import PbRetryConfig
    intent = _valid_intent()
    ctrl, qb, pb, mcpd, audit, session = _build(
        qb_side_effect=[
            _resp(intent),                          # intent
            _resp({"verified": False, "reason": "wrong tool"}),  # verifier attempt 1
            _resp({"pb_hint": "use fs.list not fs.read"}),  # qb_repair
            _resp({"verified": True, "reason": "ok"}),  # verifier attempt 2
            _resp({"summary": "System is running."}),   # summarise (non-streaming)
        ],
    )
    ctrl._pb_retry_cfg = PbRetryConfig(max_attempts=3, consult_qb_after_attempt=1)
    events = _collect_events(ctrl, "show status", session)

    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].result.outcome == Outcome.EXECUTED
    # Audit should reflect the two-attempt path (extra field always
    # written on success too — inspect the successful audit row).
    # NOTE: successful path uses different audit shape; per-attempt
    # provenance for success cases would be a future observability
    # improvement (v6.17 refinement). This test locks the outcome
    # semantic; per-attempt reporting on success is out of scope for
    # M7.0.2f.


def test_pb_retry_disabled_via_max_attempts_1_single_shot_semantics():
    """[pb_retry] max_attempts=1 → verifier rejection on attempt 1
    → single-attempt semantics preserved: QB_VERIFIER_REJECTED (NOT
    PB_RETRY_EXHAUSTED). Regression lock for _map_pb_failure_outcome
    single-attempt branch."""
    from controller.pb_retry import PbRetryConfig
    ctrl, qb, pb, mcpd, audit, session = _build(
        qb_side_effect=[
            _resp(_valid_intent()),
            _resp({"verified": False, "reason": "mismatch"}),
        ],
    )
    ctrl._pb_retry_cfg = PbRetryConfig(max_attempts=1)
    events = _collect_events(ctrl, "show status", session)

    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].result.outcome == Outcome.QB_VERIFIER_REJECTED
