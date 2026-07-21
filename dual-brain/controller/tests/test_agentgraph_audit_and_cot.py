"""v6.12 regression tests — INV-8 audit-write + accurate CoT traversal.

Each test in this file was failing on `main` prior to the v6.12 branch
and passes only after commits 1–5 land. See
`.claude/plans/users-aditya-pictures-screenshots-scree-starry-cerf.md`
for the full plan; short recap:

- Fix A (audit_writer_node)  — Task #173's flag flip to AgentGraph
  forgot to port the 12+ AuditFields write sites from main.py. Result:
  /var/log/icebreaker/controller-audit.log stayed 0 bytes across every
  turn on the shipped v6.11 ISO (confirmed by on-guest lsof).

- Fix B (visited_nodes)      — The bridge inferred which nodes ran from
  outcome fields (error_kind, tier, hitl_decision). Inference cannot
  represent short-circuits — schema reject, system.unsupported, and
  hitl deny all rendered downstream nodes as "done" green in the CoT
  panel even though those nodes never ran.

Both fixes reinforce R16 (pipeline migrations must audit every
invariant on the old path) and BP-6 (security decisions from
structured facts, not model inference).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from controller.agent_graph_nodes import (
    audit_writer_node,
    hitl_gate_node,
    make_collaborators,
    planner_node,
    responder_node,
    risk_classifier_node,
)
from controller.agent_graph_state import make_initial_state
from controller.agent_graph_bridge import (
    _visited_from_state,
    translate_outcome_to_events,
)
from controller.audit import AuditFields, Outcome
from controller.backends.base import BrainProviderError


# ── Test kit ──────────────────────────────────────────────────────────


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        run=SimpleNamespace(tier0_fast_path=True),
        verifier=SimpleNamespace(tier_floor=2, retry_mode="on_call_failed_only"),
        qb=SimpleNamespace(model="gemini-2.5-flash", backend="gemini"),
    )


def _kit(*, audit=None):
    intent_store = MagicMock()
    intent_store.put.return_value = "intent-uuid-abc"

    session_state = SimpleNamespace(turn_index=7, backend="gemini")
    session_store = MagicMock()
    session_store.get.return_value = session_state

    qb = MagicMock()
    pb = MagicMock()
    mcpd = MagicMock()
    prompts = MagicMock()
    prompts.get.return_value = ""
    audit = audit if audit is not None else MagicMock()
    risk_classify = MagicMock()
    risk_classify.return_value = SimpleNamespace(tier=0)
    verifier = MagicMock()
    verifier.verify.return_value = SimpleNamespace(verified=True, reason="ok")
    turn_content = {}

    return make_collaborators(
        cfg=_cfg(),
        session_store=session_store,
        qb_backend=qb,
        pb_backend=pb,
        mcpd_client=mcpd,
        audit_log=audit,
        risk_classify=risk_classify,
        verifier=verifier,
        prompts=prompts,
        intent_schema={"type": "object"},
        intent_store=intent_store,
        turn_content=turn_content,
    )


def _state(**over):
    s = make_initial_state(
        session_id="sess-1", turn_id="turn-1", query="hi",
        t0_monotonic=1.0,
    )
    s.update(over)
    return s


# ── Fix A: audit_writer_node writes on every terminal ────────────────


class TestAuditWriteFires:
    def test_audit_write_fires_on_successful_turn(self):
        """A completed turn (error_kind unset) writes ONE AuditFields
        row with outcome=EXECUTED and preserves intent_id + session
        provenance from state.

        Guards against the shipped-v6.11 symptom: audit log 0 bytes
        despite active turns because AgentGraph never called
        write_fields at all.
        """
        audit = MagicMock()
        collab = _kit(audit=audit)
        # Seed turn_content with an intent so lookup succeeds.
        collab["turn_content"]["intent-abc"] = {
            "action": "fs.list", "target": "/tmp",
            "reason": "user_requested", "risk_level": "low",
        }
        state = _state(intent_id="intent-abc", tier=0)

        audit_writer_node(collab)(state)

        assert audit.write_fields.call_count == 1
        fields = audit.write_fields.call_args[0][0]
        assert isinstance(fields, AuditFields)
        assert fields.outcome == Outcome.EXECUTED
        assert fields.intent_id == "intent-abc"
        assert fields.action == "fs.list"
        assert fields.session_id == "sess-1"
        assert fields.turn_index == 7          # threaded via session_store
        assert fields.backend == "gemini"

    def test_audit_write_fires_on_planner_reject(self):
        """F-32 schema reject fires audit with SCHEMA_REJECTED outcome
        and populates extra.error_kind='schema' so operators can filter
        the log for rejects."""
        audit = MagicMock()
        collab = _kit(audit=audit)
        state = _state(
            error_kind="schema",
            error_reason="F-32: target is empty",
            completed=True,
        )

        audit_writer_node(collab)(state)

        assert audit.write_fields.call_count == 1
        fields = audit.write_fields.call_args[0][0]
        assert fields.outcome == Outcome.SCHEMA_REJECTED
        assert fields.extra["error_kind"] == "schema"
        assert "F-32" in fields.extra["error_reason"]

    def test_audit_write_fires_on_hitl_deny(self):
        """User-denied HITL turn fires audit with HITL_DENIED outcome
        even though state.error_kind is unset (deny is a user decision,
        not an error)."""
        audit = MagicMock()
        collab = _kit(audit=audit)
        state = _state(
            tier=2, hitl_decision="deny", hitl_required=True,
        )

        audit_writer_node(collab)(state)

        fields = audit.write_fields.call_args[0][0]
        assert fields.outcome == Outcome.HITL_DENIED

    def test_audit_write_fires_on_unsupported_short_circuit(self):
        """F-35 system.unsupported short-circuit fires audit with
        UNSUPPORTED outcome — matches the F-48 discipline that
        unsupported is a controlled non-error terminal."""
        audit = MagicMock()
        collab = _kit(audit=audit)
        state = _state(
            error_kind="unsupported",
            error_reason="F-35 short-circuit",
            completed=True,
        )

        audit_writer_node(collab)(state)

        fields = audit.write_fields.call_args[0][0]
        assert fields.outcome == Outcome.UNSUPPORTED

    def test_audit_write_exception_does_not_break_turn(self):
        """If write_fields raises (disk full, permission denied), the
        node returns cleanly and marks itself visited 'failed' so the
        CoT surfaces the audit health issue. Turn delivery must not
        break — BP-10 (never let observability become a DoS amplifier)."""
        audit = MagicMock()
        audit.write_fields.side_effect = OSError("disk full")
        collab = _kit(audit=audit)
        state = _state(intent_id="", tier=0)

        # Must not raise.
        result = audit_writer_node(collab)(state)

        # Node self-marks failed so operators see it in the CoT panel.
        assert result.get("visited_nodes") == [("audit_writer", "failed")]


# ── Fix B: visited_nodes is populated and consumed accurately ────────


class TestVisitedNodesAccuracy:
    def test_planner_schema_reject_marks_only_planner_failed(self):
        """F-32 empty-target reject: visited_nodes must show
        [(planner, failed)] and NOT include any downstream node. The
        pre-v6.12 inference approach appended risk_classifier + executor
        + mcpd_dispatcher + responder as "done" green here, misleading
        the operator."""
        collab = _kit()
        # Cause planner to emit an intent that trips the F-32 guard.
        collab["qb"].complete.return_value = SimpleNamespace(
            content_json={"action": "fs.list", "target": ""}
        )

        result = planner_node(collab)(_state())

        assert result["error_kind"] == "schema"
        assert result["visited_nodes"] == [("planner", "failed")]

    def test_hitl_deny_marks_hitl_gate_failed(self):
        """HITL deny short-circuits before executor; visited_nodes must
        end at hitl_gate and mark it failed. Downstream nodes (executor,
        mcpd_dispatcher, responder) MUST NOT appear."""
        collab = _kit()

        # Stub the LangGraph interrupt() so it returns "deny" directly
        # instead of raising to pause. hitl_gate_node imports interrupt
        # locally, so we patch it there.
        import controller.agent_graph_nodes as agnodes  # noqa: F401
        from langgraph import types as _lgtypes
        original = _lgtypes.interrupt
        _lgtypes.interrupt = lambda payload: "deny"
        try:
            result = hitl_gate_node(collab)(_state(tier=2))
        finally:
            _lgtypes.interrupt = original

        assert result["hitl_decision"] == "deny"
        assert result["visited_nodes"] == [("hitl_gate", "failed")]

    def test_responder_success_marks_responder_done(self):
        """Positive-path sanity: responder marks itself done and its
        visited_nodes entry contains the correct state."""
        collab = _kit()
        collab["intent_store"].get.return_value = {
            "action": "fs.list", "target": "/tmp",
        }
        collab["qb"].complete.return_value = SimpleNamespace(
            content_json={"summary": "Directory listing rendered."}
        )

        result = responder_node(collab)(
            _state(intent_id="intent-abc", step_index=1, plan_id="plan-x"),
        )

        assert result["completed"] is True
        assert result["visited_nodes"] == [("responder", "done")]

    def test_bridge_visited_from_state_consumes_explicit_list(self):
        """_visited_from_state returns the exact list from
        outcome.visited_nodes when present — no inference. This locks
        in the contract the bridge relies on for CoT emission."""
        outcome = {
            "visited_nodes": [
                ("planner", "done"),
                ("risk_classifier", "done"),
                ("verifier", "failed"),
            ],
            "error_kind": "verifier",
        }

        result = _visited_from_state(outcome)

        assert result == [
            ("planner", "done"),
            ("risk_classifier", "done"),
            ("verifier", "failed"),
        ]

    def test_bridge_events_render_failed_node_state(self):
        """End-to-end: an outcome with visited_nodes containing a
        "failed" entry produces a CotEvent with step_state="failed" for
        that node. Guards against the pre-v6.12 all-green display."""
        from controller.turn_events import CotEvent

        outcome = {
            "outcome": "error",
            "error_kind": "schema",
            "error_reason": "F-32",
            "visited_nodes": [("planner", "failed")],
            "output": "",
        }

        events = translate_outcome_to_events(outcome, t0=0.0, backend="gemini")

        cot_events = [e for e in events if isinstance(e, CotEvent)]
        assert len(cot_events) == 1
        assert cot_events[0].step_name == "planner"
        assert cot_events[0].step_state == "failed"

    def test_bridge_events_show_only_visited_nodes(self):
        """Downstream nodes that never ran MUST NOT appear as CotEvents.
        Pre-v6.12 the schema-reject path incorrectly emitted
        risk_classifier + executor + mcpd_dispatcher + responder as
        green done events."""
        from controller.turn_events import CotEvent

        outcome = {
            "outcome": "error",
            "error_kind": "schema",
            "error_reason": "F-32",
            "visited_nodes": [("planner", "failed")],
            "output": "",
        }

        events = translate_outcome_to_events(outcome, t0=0.0, backend="gemini")

        cot_names = [e.step_name for e in events if isinstance(e, CotEvent)]
        # The ONLY node that visited is planner. Nothing else.
        assert cot_names == ["planner"]
