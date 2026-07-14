"""v6.8 Task #146 Step 2b — AgentGraph.run() / resume() integration.

Wires a real LangGraph StateGraph + checkpointer + all 7 nodes + edges
and asserts end-to-end behavior for the paths that matter:
- Tier 0 fast path completes: planner → risk → executor → mcpd → responder
- Tier 2 pauses at HitlGate, resume(approve) completes, resume(deny) denies
- Planner error → error outcome, no PB / mcpd fired
- Checkpointer round-trip: run pauses, resume from same session_id works

All LLM/mcpd calls are mocked. The one live smoke test lives in
test_agent_graph_live.py behind ICEBREAKER_LIVE_GEMINI_KEY (D5).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from controller.agent_graph import AgentGraph
from controller.backends.base import BrainProviderError
from controller.config import AgentGraphConfig
from controller.session_store import SessionStore


def _cfg(*, tier0_fast_path: bool = True, tier_floor: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        run=SimpleNamespace(tier0_fast_path=tier0_fast_path),
        verifier=SimpleNamespace(
            tier_floor=tier_floor,
            retry_mode="on_call_failed_only",
        ),
    )


def _make_agent(**overrides) -> AgentGraph:
    intent_store = MagicMock()
    intent_store.put.return_value = "intent-uuid-abc"

    session_state = MagicMock()
    session_state.build_pb_user_turn.return_value = "pb-user-turn"
    # SessionStore has __slots__ so we can't monkey-patch .get; use a
    # MagicMock stand-in that answers .get() the way AgentGraph expects.
    session_store = MagicMock()
    session_store.get.return_value = session_state

    qb = MagicMock()
    qb.complete.return_value = SimpleNamespace(
        content_json={"action": "fs.list", "target": "/tmp", "reason": "list stuff"}
    )

    pb = MagicMock()
    pb.complete.return_value = SimpleNamespace(
        content_json={"tool": "fs.list", "params": {"path": "/tmp"}}
    )

    mcpd = MagicMock()
    mcpd.call.return_value = SimpleNamespace(stdout="listed")

    prompts = MagicMock()
    prompts.get.return_value = ""

    audit = MagicMock()
    risk_classify = MagicMock(return_value=SimpleNamespace(tier=0))
    verifier = MagicMock()
    verifier.verify.return_value = SimpleNamespace(verified=True, reason="ok")

    kit = dict(
        cfg=AgentGraphConfig(enabled=True, checkpointer_path=":memory:",
                             checkpointer_retention_days=30, strict_msgpack=True),
        session_store=session_store,
        qb_backend=qb,
        pb_backend=pb,
        mcpd_client=mcpd,
        audit_log=audit,
        risk_classify=risk_classify,
        verifier=verifier,
        prompts=prompts,
        intent_schema={"type": "object"},
        controller_cfg=_cfg(),
        intent_store=intent_store,
    )
    kit.update(overrides)
    return AgentGraph(**kit)


# ── Tier 0 fast path: planner → risk → executor → mcpd → responder ────


def test_tier0_run_completes_via_fast_path():
    ag = _make_agent()
    try:
        results = list(ag.run("show status", "sess-1"))
        assert len(results) == 1
        out = results[0]
        assert out["outcome"] == "executed"
        assert out["intent_id"] == "intent-uuid-abc"
        assert out["tier"] == 0
        assert out["tool_call_hash"]
        assert out["mcpd_result_hash"]
        # Fast path: PB was never called.
        ag._pb.complete.assert_not_called()
        # mcpd fired exactly once.
        ag._mcpd.call.assert_called_once()
    finally:
        ag.close()


# ── Planner error → outcome=error, no downstream calls ────────────────


def test_planner_error_short_circuits_to_end():
    ag = _make_agent()
    ag._qb.complete.side_effect = BrainProviderError("gemini down")
    try:
        results = list(ag.run("hi", "sess-2"))
        out = results[0]
        assert out["outcome"] == "error"
        assert out["error_kind"] == "planner"
        # Neither PB nor mcpd got called.
        ag._pb.complete.assert_not_called()
        ag._mcpd.call.assert_not_called()
    finally:
        ag.close()


# ── Tier 2 hitl_gate interrupt + resume flow ──────────────────────────


def test_tier2_pauses_at_hitl_then_resumes_on_approve():
    ag = _make_agent()
    # Rig for Tier 2.
    ag._risk_classify = MagicMock(return_value=SimpleNamespace(tier=2))
    ag._qb.complete.return_value = SimpleNamespace(
        content_json={"action": "package.install", "target": "htop", "reason": "install"}
    )
    # Rebuild the graph so the new risk_classify closure is bound.
    ag._graph = ag._build_graph()

    try:
        results = list(ag.run("install htop", "sess-3"))
        out = results[0]
        assert out["outcome"] == "paused", f"expected paused, got {out}"
        # PB hasn't fired yet — the flow paused BEFORE executor.
        ag._pb.complete.assert_not_called()
        ag._mcpd.call.assert_not_called()

        # Resume with approve.
        resumed = list(ag.resume("sess-3", "approve"))
        out2 = resumed[0]
        assert out2["outcome"] == "executed"
        # Now PB + mcpd fired (slow path since package.install is not
        # in the tier-0 allowlist).
        ag._pb.complete.assert_called_once()
        ag._mcpd.call.assert_called_once()
    finally:
        ag.close()


def test_tier2_deny_short_circuits_no_dispatch():
    ag = _make_agent()
    ag._risk_classify = MagicMock(return_value=SimpleNamespace(tier=2))
    ag._qb.complete.return_value = SimpleNamespace(
        content_json={"action": "package.install", "target": "htop", "reason": "install"}
    )
    ag._graph = ag._build_graph()
    try:
        first = list(ag.run("install htop", "sess-4"))
        assert first[0]["outcome"] == "paused"

        denied = list(ag.resume("sess-4", "deny"))
        out = denied[0]
        assert out["outcome"] == "denied"
        ag._pb.complete.assert_not_called()
        ag._mcpd.call.assert_not_called()
    finally:
        ag.close()


# ── Checkpointer round-trip: separate resume() call uses the DB ───────


def test_checkpoint_round_trip_across_run_and_resume():
    """Verifies the SqliteSaver actually persists interrupt state — the
    resume() call is a fresh graph.invoke() and only works because the
    checkpoint DB restored the paused state."""
    ag = _make_agent()
    ag._risk_classify = MagicMock(return_value=SimpleNamespace(tier=2))
    ag._qb.complete.return_value = SimpleNamespace(
        content_json={"action": "package.install", "target": "htop", "reason": "install"}
    )
    ag._graph = ag._build_graph()
    try:
        list(ag.run("install htop", "checkpoint-session"))
        # Between run() and resume() nothing else touched the graph.
        # Resume must succeed because the checkpointer persisted state.
        result = list(ag.resume("checkpoint-session", "approve"))
        assert result[0]["outcome"] == "executed"
    finally:
        ag.close()


# ── Turn content is wiped after terminal ─────────────────────────────


def test_turn_content_cleared_after_success():
    ag = _make_agent()
    try:
        list(ag.run("query", "s-x"))
        assert ag._turn_content == {}
    finally:
        ag.close()


def test_turn_content_survives_pause():
    """Between pause and resume the collaborator's turn_content dict
    MUST still hold the intent — the risk_classifier / executor look
    it up on resume."""
    ag = _make_agent()
    ag._risk_classify = MagicMock(return_value=SimpleNamespace(tier=2))
    ag._qb.complete.return_value = SimpleNamespace(
        content_json={"action": "package.install", "target": "htop", "reason": "install"}
    )
    ag._graph = ag._build_graph()
    try:
        list(ag.run("install", "s-p"))
        # Paused — content still available for the resume path.
        assert "intent-uuid-abc" in ag._turn_content
    finally:
        ag.close()
