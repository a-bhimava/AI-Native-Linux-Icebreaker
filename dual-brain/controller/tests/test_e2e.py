"""End-to-end integration tests for Controller.run_turn().

All collaborators are mocked — no live servers required.

Six scenarios:
  1. test_tier0_happy_path          — full Tier 0 pipeline; TurnResult(success=True)
  2. test_tier3_hitl_deny_skips_dispatch — HITL deny; mcpd never called
  3. test_schema_rejection_audited  — bad QB output → SCHEMA_REJECTED in audit
  4. test_verifier_rejection_audited — QB verifier returns false → QB_VERIFIER_REJECTED
  5. test_pb_receives_only_intent_id — PB user turn has UUID only, no raw text (G4)
  6. test_tool_timeout_audited      — McpdTimeoutError → TOOL_TIMEOUT in audit
"""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from controller.audit import AuditFields, AuditLog, Outcome
from controller.backends.base import BrainResponse
from controller.config import SessionConfig
from controller.hitl import Decision
from controller.intent_store import IntentStore
from controller.main import Controller, TurnResult
from controller.mcpd_client import McpdTimeoutError, ToolResult
from controller.session import SessionState


# ── Helpers ───────────────────────────────────────────────────────────────────

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        qb=SimpleNamespace(name="local", model="test-model"),
        hitl=SimpleNamespace(lockout_seconds=3, timeout_seconds=30),
        run=SimpleNamespace(
            qb_max_retries=1,
            mcpd_timeout_seconds=10.0,
            mcpd_schemas_dir="",
            mcpd_binary="",
        ),
        session=SimpleNamespace(max_tool_output_lines=40),
    )


def _resp(content: dict, cost: float = 0.0) -> BrainResponse:
    return BrainResponse(
        content_json=content,
        tokens_in=10,
        tokens_out=5,
        cost_usd=cost,
        backend="local",
        model="test-model",
        attempts=1,
    )


def _intent(action: str = "system.status", target: str = "") -> dict:
    return {
        "intent_id": str(uuid.uuid4()),
        "action": action,
        "target": target,
        "params": {},
        "reason": "user_requested",
        "risk_level": "low",
    }


def _tool_result(status: str = "ok") -> ToolResult:
    return ToolResult(result={"status": status, "output": "disk: 80%"}, request_id=1)


def _build(
    qb_side_effect: list | None = None,
    pb_content: dict | None = None,
    mcpd_side_effect=None,
    mcpd_return=None,
):
    """Return (controller, qb_mock, pb_mock, mcpd_mock, audit_mock)."""
    qb = MagicMock()
    qb.complete.side_effect = qb_side_effect or [
        _resp(_intent()),                               # step 1: intent
        _resp({"verified": True, "reason": "ok"}),     # step 8: verify
        _resp({"summary": "System is running."}),       # step 11: summarise
    ]

    pb = MagicMock()
    pb.complete.return_value = _resp(
        pb_content or {"tool": "system.status", "params": {}}
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

    ctrl = Controller(
        _cfg(),
        qb_backend=qb,
        pb_backend=pb,
        mcpd_client=mcpd,
        audit_log=audit,
        store=store,
        prompt_loader=prompts,
    )
    return ctrl, qb, pb, mcpd, audit


def _session() -> SessionState:
    return SessionState.new("local", SessionConfig())


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_tier0_happy_path():
    """Tier 0: full QB → validate → classify → PB → mcpd → summarise pipeline."""
    ctrl, qb, pb, mcpd, audit = _build()
    session = _session()

    result = ctrl.run_turn("show system status", session)

    assert result.success is True
    assert result.outcome == Outcome.EXECUTED
    assert result.output == "System is running."
    assert mcpd.call.call_count == 1
    # QB called 3 times: intent, verify, summarise
    assert qb.complete.call_count == 3
    # Exactly one audit row written with EXECUTED outcome
    audit.write_fields.assert_called_once()
    written: AuditFields = audit.write_fields.call_args[0][0]
    assert written.outcome == Outcome.EXECUTED
    assert written.action == "system.status"


def test_tier3_hitl_deny_skips_dispatch():
    """Tier 3 (fs.delete): HITL denied → mcpd never called; HITL_DENIED audited."""
    delete_intent = {
        "intent_id": str(uuid.uuid4()),
        "action": "fs.delete",
        "target": "/tmp/junk",
        "params": {},
        "reason": "user_requested",
        "risk_level": "critical",
    }
    ctrl, qb, pb, mcpd, audit = _build(
        qb_side_effect=[_resp(delete_intent)],
    )
    session = _session()

    with patch("controller.main.HitlPrompt") as mock_hitl_cls:
        mock_hitl_cls.return_value.ask.return_value = Decision.DENIED
        result = ctrl.run_turn("delete /tmp/junk", session)

    assert result.success is False
    assert result.outcome == Outcome.HITL_DENIED
    mcpd.call.assert_not_called()
    pb.complete.assert_not_called()
    written: AuditFields = audit.write_fields.call_args[0][0]
    assert written.outcome == Outcome.HITL_DENIED
    assert written.action == "fs.delete"


def test_schema_rejection_audited():
    """Malformed QB output → SCHEMA_REJECTED in audit; mcpd not called."""
    malformed = {"garbage": "not an intent"}
    ctrl, qb, pb, mcpd, audit = _build(
        qb_side_effect=[_resp(malformed)],
    )
    session = _session()

    result = ctrl.run_turn("do something", session)

    assert result.success is False
    assert result.outcome == Outcome.SCHEMA_REJECTED
    mcpd.call.assert_not_called()
    pb.complete.assert_not_called()
    audit.write_fields.assert_called_once()
    written: AuditFields = audit.write_fields.call_args[0][0]
    assert written.outcome == Outcome.SCHEMA_REJECTED


def test_verifier_rejection_audited():
    """QB verifier returns false → QB_VERIFIER_REJECTED; mcpd never called."""
    ctrl, qb, pb, mcpd, audit = _build(
        qb_side_effect=[
            _resp(_intent()),
            _resp({"verified": False, "reason": "tool mismatch"}),
        ]
    )
    session = _session()

    result = ctrl.run_turn("show system status", session)

    assert result.success is False
    assert result.outcome == Outcome.QB_VERIFIER_REJECTED
    mcpd.call.assert_not_called()
    written: AuditFields = audit.write_fields.call_args[0][0]
    assert written.outcome == Outcome.QB_VERIFIER_REJECTED


def test_pb_receives_only_intent_id():
    """G4: PB user turn must contain only intent_id + tool scaffold.

    Raw user text must never reach the Privileged Brain (INV-2).
    """
    ctrl, qb, pb, mcpd, audit = _build()
    session = _session()

    raw_user_text = "please show me my disk usage right now"
    ctrl.run_turn(raw_user_text, session)

    assert pb.complete.called, "PB was not called"
    pb_call_kwargs = pb.complete.call_args[1]  # keyword args
    pb_user_turn: str = pb_call_kwargs["user"]

    # Must be parseable JSON with only the three allowed keys.
    import json as _json
    pb_data = _json.loads(pb_user_turn)
    assert set(pb_data.keys()) == {"intent_id", "allowed_tool", "tool_schema"}

    # The intent_id must be a valid UUID4.
    assert _UUID4_RE.match(pb_data["intent_id"]), "PB intent_id is not a UUID4"

    # Raw user text must be absent.
    assert raw_user_text not in pb_user_turn
    assert "disk usage" not in pb_user_turn


def test_tool_timeout_audited():
    """McpdTimeoutError → TOOL_TIMEOUT in audit; TurnResult(success=False)."""
    ctrl, qb, pb, mcpd, audit = _build(
        qb_side_effect=[
            _resp(_intent()),
            _resp({"verified": True, "reason": "ok"}),
        ],
        mcpd_side_effect=McpdTimeoutError("mcpd did not respond in 10s"),
    )
    session = _session()

    result = ctrl.run_turn("show system status", session)

    assert result.success is False
    assert result.outcome == Outcome.TOOL_TIMEOUT
    audit.write_fields.assert_called_once()
    written: AuditFields = audit.write_fields.call_args[0][0]
    assert written.outcome == Outcome.TOOL_TIMEOUT
