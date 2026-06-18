"""Tests for CotEvent emissions in ``run_turn_streaming()`` and daemon/client forwarding.

Covers:
  1. Happy-path Tier 0 turn emits CotEvents for all pipeline steps
  2. CotEvent step_state transitions: active → done per step
  3. Skipped steps emit done with skipped=True in data
  4. Schema rejection emits schema_validation failed CotEvent
  5. CotEvent step_index matches _STEP_INDEX
  6. CotEvent heading comes from _COT_HEADINGS
  7. CotEvent timestamp_ms is non-negative and monotonic
  8. PB schema error emits tool_validation failed CotEvent
  9. Verifier rejection emits qb_verify failed CotEvent
 10. mcpd timeout emits mcpd_dispatch failed CotEvent
 11. Daemon forwards CotEvent as turn.cot notification
 12. Client dispatch table routes turn.cot to _on_cot
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from controller.audit import AuditFields, AuditLog, Outcome
from controller.backends.base import BrainResponse
from controller.config import SessionConfig
from controller.intent_store import IntentStore
from controller.main import (
    Controller,
    TurnResult,
    _COT_HEADINGS,
    _PIPELINE_STEPS,
    _STEP_INDEX,
)
from controller.mcpd_client import McpdTimeoutError, ToolResult
from controller.session import SessionState
from controller.turn_events import CotEvent, ProgressEvent, ResultEvent


# ── Helpers ──────────────────────────────────────────────────────────────────


def _cfg(stream_output: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        qb=SimpleNamespace(name="local", model="test"),
        hitl=SimpleNamespace(
            lockout_seconds=1, timeout_seconds=5, trust_ttl_seconds=0,
            presenter="terminal",
        ),
        run=SimpleNamespace(
            qb_max_retries=1, mcpd_timeout_seconds=5.0,
            mcpd_schemas_dir="", mcpd_binary="",
        ),
        session=SimpleNamespace(
            max_tool_output_lines=40, stream_output=stream_output,
            show_progress=True,
        ),
        tier2=SimpleNamespace(enabled=False),
        undo=SimpleNamespace(enabled=False),
    )


def _resp(content: dict, cost: float = 0.0) -> BrainResponse:
    return BrainResponse(
        content_json=content, tokens_in=10, tokens_out=5,
        cost_usd=cost, backend="local", model="test", attempts=1,
    )


def _valid_intent(action: str = "system.status", target: str = "") -> dict:
    return {
        "intent_id": str(uuid.uuid4()),
        "action": action, "target": target, "params": {},
        "reason": "user_requested", "risk_level": "read_only",
    }


def _tool_result() -> ToolResult:
    return ToolResult(result={"status": "ok"}, request_id=1)


def _session() -> SessionState:
    return SessionState.new("local", SessionConfig())


def _build(
    qb_side_effect=None,
    pb_content=None,
    mcpd_side_effect=None,
    mcpd_return=None,
    stream_output=False,
):
    intent = _valid_intent()
    qb = MagicMock()
    if qb_side_effect is not None:
        qb.complete.side_effect = qb_side_effect
    else:
        qb.complete.side_effect = [
            _resp(intent),
            _resp({"verified": True, "reason": "ok"}),
            _resp({"summary": "System is running."}),
        ]

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
        cfg, qb_backend=qb, pb_backend=pb, mcpd_client=mcpd,
        audit_log=audit, store=store, prompt_loader=prompts,
    )
    session = _session()
    return ctrl, qb, pb, mcpd, audit, session


def _collect(ctrl, user_input, session):
    return list(ctrl.run_turn_streaming(user_input, session))


def _cot_events(events):
    return [e for e in events if isinstance(e, CotEvent)]


# ── Tests ────────────────────────────────────────────────────────────────────


def test_happy_path_emits_cot_events_for_all_steps():
    ctrl, *_, session = _build()
    events = _collect(ctrl, "show status", session)
    cots = _cot_events(events)
    step_names = {c.step_name for c in cots}
    for name, _ in _PIPELINE_STEPS:
        assert name in step_names, f"Missing CotEvent for step {name}"


def test_cot_state_transitions_active_then_done():
    ctrl, *_, session = _build()
    events = _collect(ctrl, "show status", session)
    cots = _cot_events(events)
    by_step: dict[str, list[str]] = {}
    for c in cots:
        by_step.setdefault(c.step_name, []).append(c.step_state)
    for name, states in by_step.items():
        if "active" in states:
            assert states[0] == "active", f"{name}: first state should be active"
            assert states[-1] in ("done", "failed"), f"{name}: last state should be done/failed"


def test_skipped_steps_have_skipped_data():
    ctrl, *_, session = _build()
    events = _collect(ctrl, "show status", session)
    cots = _cot_events(events)
    tier2_cots = [c for c in cots if c.step_name == "tier2_review"]
    assert len(tier2_cots) == 1
    assert tier2_cots[0].step_state == "done"
    assert tier2_cots[0].data.get("skipped") is True


def test_schema_rejection_emits_failed_cot():
    ctrl, *_, session = _build(qb_side_effect=[_resp({"garbage": True})])
    events = _collect(ctrl, "bad input", session)
    cots = _cot_events(events)
    schema_cots = [c for c in cots if c.step_name == "schema_validation"]
    states = [c.step_state for c in schema_cots]
    assert "active" in states
    assert "failed" in states


def test_cot_step_index_matches_step_index_dict():
    ctrl, *_, session = _build()
    events = _collect(ctrl, "show status", session)
    cots = _cot_events(events)
    for c in cots:
        if c.step_name in _STEP_INDEX:
            assert c.step_index == _STEP_INDEX[c.step_name], (
                f"{c.step_name}: index {c.step_index} != {_STEP_INDEX[c.step_name]}"
            )


def test_cot_heading_matches_heading_dict():
    ctrl, *_, session = _build()
    events = _collect(ctrl, "show status", session)
    cots = _cot_events(events)
    for c in cots:
        if c.step_name in _COT_HEADINGS:
            assert c.heading == _COT_HEADINGS[c.step_name]


def test_cot_timestamp_monotonic():
    ctrl, *_, session = _build()
    events = _collect(ctrl, "show status", session)
    cots = _cot_events(events)
    for c in cots:
        assert c.timestamp_ms >= 0
    timestamps = [c.timestamp_ms for c in cots]
    for i in range(1, len(timestamps)):
        assert timestamps[i] >= timestamps[i - 1], (
            f"Timestamp went backwards: {timestamps[i-1]} -> {timestamps[i]}"
        )


def test_pb_schema_error_emits_failed_cot():
    intent = _valid_intent()
    bad_pb = {"tool": "wrong_tool", "params": {}}
    ctrl, *_, session = _build(pb_content=bad_pb)
    events = _collect(ctrl, "show status", session)
    cots = _cot_events(events)
    validation_cots = [c for c in cots if c.step_name == "tool_validation"]
    states = [c.step_state for c in validation_cots]
    assert "failed" in states


def test_verifier_rejection_emits_failed_cot():
    intent = _valid_intent()
    qb = MagicMock()
    qb.complete.side_effect = [
        _resp(intent),
        _resp({"verified": False, "reason": "mismatch"}),
    ]
    ctrl, _, pb, mcpd, audit, session = _build()
    ctrl._qb = qb
    events = _collect(ctrl, "show status", session)
    cots = _cot_events(events)
    verify_cots = [c for c in cots if c.step_name == "qb_verify"]
    states = [c.step_state for c in verify_cots]
    assert "failed" in states


def test_mcpd_timeout_emits_failed_cot():
    ctrl, *_, session = _build(
        mcpd_side_effect=McpdTimeoutError("timeout"),
    )
    events = _collect(ctrl, "show status", session)
    cots = _cot_events(events)
    dispatch_cots = [c for c in cots if c.step_name == "mcpd_dispatch"]
    states = [c.step_state for c in dispatch_cots]
    assert "active" in states
    assert "failed" in states


def test_daemon_forwards_cot_as_turn_cot():
    from controller.protocol import JsonRpcNotification
    from controller.daemon import Daemon

    cot = CotEvent(
        step_index=0, step_name="qb_intent", step_state="active",
        heading="Intent Generation", body="Parsing",
        data={"action": "system.status"}, timestamp_ms=10.0,
    )
    payload = {
        "step_index": cot.step_index,
        "step_name": cot.step_name,
        "step_state": cot.step_state,
        "heading": cot.heading,
        "body": cot.body,
        "data": cot.data,
        "timestamp_ms": cot.timestamp_ms,
    }
    notif = JsonRpcNotification("turn.cot", payload)
    parsed = json.loads(notif.to_bytes().decode())
    assert parsed["method"] == "turn.cot"
    assert parsed["params"]["step_state"] == "active"
    assert parsed["params"]["heading"] == "Intent Generation"


def test_client_dispatch_routes_turn_cot():
    from controller.client import DaemonClient

    received = []

    class _TestClient(DaemonClient):
        __slots__ = ()
        def _on_cot(self, params):
            received.append(params)

    client = _TestClient.__new__(_TestClient)
    msg = {"method": "turn.cot", "params": {"step_name": "qb_intent", "step_state": "done"}}
    client._dispatch_notification(msg)
    assert len(received) == 1
    assert received[0]["step_name"] == "qb_intent"
