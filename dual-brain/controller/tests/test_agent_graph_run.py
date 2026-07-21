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
    # v6.12 Fix E (F-85): verifier now runs AFTER executor. Executor
    # (and thus PB, since package.install isn't tier-0 fast-path) fires
    # BEFORE the HITL pause. mcpd stays gated behind HITL.
    ag = _make_agent()
    ag._risk_classify = MagicMock(return_value=SimpleNamespace(tier=2))
    ag._qb.complete.return_value = SimpleNamespace(
        content_json={"action": "package.install", "target": "htop", "reason": "install"}
    )
    ag._graph = ag._build_graph()

    try:
        results = list(ag.run("install htop", "sess-3"))
        out = results[0]
        assert out["outcome"] == "paused", f"expected paused, got {out}"
        # v6.12 Fix E: executor ran before HITL so PB fired. mcpd remains
        # gated behind the pause.
        ag._pb.complete.assert_called_once()
        ag._mcpd.call.assert_not_called()

        # Resume with approve.
        resumed = list(ag.resume("sess-3", "approve"))
        out2 = resumed[0]
        assert out2["outcome"] == "executed"
        # PB call count stays at 1 — after_hitl routes to mcpd_dispatcher,
        # not back to executor.
        ag._pb.complete.assert_called_once()
        ag._mcpd.call.assert_called_once()
    finally:
        ag.close()


def test_tier2_deny_short_circuits_no_dispatch():
    # v6.12 Fix E: executor runs before HITL; deny stops before mcpd.
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
        # v6.12 Fix E: PB ran once before the pause; deny prevents mcpd.
        ag._pb.complete.assert_called_once()
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


def test_multi_step_plan_tier0_executes_all_steps():
    """v6.8 Task #148 F-62 compound-intent fix: QB emits a 2-step plan;
    executor loops twice; mcpd fires twice; marker resolution provably
    replaces $STEP_0_STDOUT in step 2's params.

    Both steps use Tier-0 fast-path tools so marker resolution happens
    in-code (no PB mock overriding the tool_call shape). Step 1 =
    network.status (no params); step 2 = fs.list with target that
    references step 1's stdout via marker.
    """
    ag = _make_agent()
    ag._qb.complete.return_value = SimpleNamespace(content_json={
        "plan": [
            {"action": "network.status", "target": "",
             "reason": "user_requested", "risk_level": "low"},
            # Step 2 target references step 0's stdout — after resolution
            # this becomes /tmp/foo which flows into params.path via
            # tier0_fast_path.
            {"action": "fs.list", "target": "/tmp/$STEP_0_STDOUT",
             "reason": "user_requested", "risk_level": "low"},
        ]
    })
    ag._risk_classify = MagicMock(return_value=SimpleNamespace(tier=0))
    ag._mcpd.call.side_effect = [
        SimpleNamespace(stdout="foo"),               # step 0
        SimpleNamespace(stdout="listed /tmp/foo"),   # step 1
    ]
    ag._graph = ag._build_graph()

    try:
        results = list(ag.run("get ip and list the dir", "sess-plan-1"))
        out = results[0]
        assert out["outcome"] == "executed", f"expected executed, got {out}"
        assert ag._mcpd.call.call_count == 2, "mcpd should fire for BOTH steps"

        # Step 2's params.path came from marker substitution: /tmp/foo.
        step2_kwargs = ag._mcpd.call.call_args_list[1].kwargs
        assert step2_kwargs["params"]["path"] == "/tmp/foo", (
            f"marker not resolved; got params={step2_kwargs['params']}"
        )
        assert "$STEP_" not in step2_kwargs["params"]["path"], (
            "marker survived substitution"
        )
    finally:
        ag.close()


def test_multi_step_plan_step_failure_short_circuits():
    """Step 2 fails at mcpd → outcome=error with step-labeled reason."""
    ag = _make_agent()
    ag._qb.complete.return_value = SimpleNamespace(content_json={
        "plan": [
            {"action": "network.status", "target": "",
             "reason": "u", "risk_level": "low"},
            {"action": "fs.write", "target": "/tmp/x.txt",
             "content": "$STEP_0_STDOUT",
             "reason": "u", "risk_level": "low"},
        ]
    })
    ag._risk_classify = MagicMock(return_value=SimpleNamespace(tier=0))
    ag._mcpd.call.side_effect = [
        SimpleNamespace(stdout="10.0.0.1"),
        RuntimeError("permission denied"),
    ]
    ag._graph = ag._build_graph()

    try:
        results = list(ag.run("query", "sess-plan-fail"))
        out = results[0]
        assert out["outcome"] == "error"
        assert out["error_kind"] == "mcpd"
        assert "step 2/2" in out["error_reason"]
        # First step still fired, second step failed.
        assert ag._mcpd.call.call_count == 2
    finally:
        ag.close()


def test_multi_step_plan_bare_intent_backward_compat():
    """A QB that emits a bare intent (v6.7 shape) still works — the
    Planner normalizes it to a 1-step plan and the graph executes
    identically to Task #146."""
    ag = _make_agent()
    # Bare intent (no 'plan' wrapper).
    ag._qb.complete.return_value = SimpleNamespace(content_json={
        "action": "fs.list", "target": "/tmp",
        "reason": "user_requested", "risk_level": "low",
    })
    ag._risk_classify = MagicMock(return_value=SimpleNamespace(tier=0))
    ag._graph = ag._build_graph()

    try:
        results = list(ag.run("list /tmp", "sess-plan-bc"))
        out = results[0]
        assert out["outcome"] == "executed"
        assert ag._mcpd.call.call_count == 1
    finally:
        ag.close()


def test_multi_step_plan_max_tier_wins_for_hitl_gate():
    """Task #148 D13: multi-step plan with a Tier-2 step at position 1
    should trigger HitlGate once at max-tier, not per-step. Verify by
    running and observing the graph pauses before ANY mcpd fires."""
    ag = _make_agent()
    ag._qb.complete.return_value = SimpleNamespace(content_json={
        "plan": [
            {"action": "network.status", "target": "",
             "reason": "u", "risk_level": "low"},
            {"action": "package.install", "target": "htop",
             "reason": "u", "risk_level": "medium"},
        ]
    })
    # First step is Tier 0, second is Tier 2. Max is 2.
    def _rc(step):
        tier = 2 if step.get("action") == "package.install" else 0
        return SimpleNamespace(tier=tier)
    ag._risk_classify = _rc
    ag._graph = ag._build_graph()

    try:
        results = list(ag.run("get IP and install htop", "sess-plan-tier"))
        out = results[0]
        # HitlGate should have paused the graph before any mcpd call.
        assert out["outcome"] == "paused"
        assert ag._mcpd.call.call_count == 0
        assert out["tier"] == 2
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
