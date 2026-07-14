"""v6.8 Task #146 Step 2b — node function + edge routing regression lock.

Each node is a pure ``(state) -> partial dict`` closure. Tests cover:
- Planner emits valid intent → intent_valid=True, intent_id set
- Planner on BrainProviderError → error_kind=planner, completed=True
- RiskClassifier sets tier from classify()
- Verifier skipped when tier < tier_floor (M7.1)
- Executor: fast path for Tier 0 → tool_call built by tier0_fast_path
- Executor: slow path calls PB; PB failure → error_kind=pb
- McpdDispatcher: mcpd.call fires with the constructed params
- Every conditional edge routes correctly
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from controller.agent_graph_nodes import (
    after_executor,
    after_hitl,
    after_mcpd,
    after_planner,
    after_risk,
    after_verifier,
    executor_node,
    make_collaborators,
    mcpd_dispatcher_node,
    planner_node,
    responder_node,
    risk_classifier_node,
    verifier_node,
)
from controller.agent_graph_state import make_initial_state
from controller.backends.base import BrainProviderError


# ── Helpers ────────────────────────────────────────────────────────────


def _cfg(*, tier0_fast_path: bool = True, tier_floor: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        run=SimpleNamespace(tier0_fast_path=tier0_fast_path),
        verifier=SimpleNamespace(
            tier_floor=tier_floor,
            retry_mode="on_call_failed_only",
        ),
    )


def _kit(**overrides):
    intent_store = MagicMock()
    intent_store.put.return_value = "intent-uuid-abc"
    intent_store.get.return_value = None

    session_state = MagicMock()
    session_state.build_pb_user_turn.return_value = "pb-user-turn"
    session_store = MagicMock()
    session_store.get.return_value = session_state

    qb = MagicMock()
    qb.complete.return_value = SimpleNamespace(
        content_json={"action": "fs.list", "target": "/tmp"}
    )

    pb = MagicMock()
    pb.complete.return_value = SimpleNamespace(
        content_json={"tool": "fs.list", "params": {"path": "/tmp"}}
    )

    mcpd = MagicMock()
    mcpd.call.return_value = SimpleNamespace(stdout="entries: [...]")

    prompts = MagicMock()
    prompts.get.return_value = ""

    audit = MagicMock()
    risk_classify = MagicMock()
    risk_classify.return_value = SimpleNamespace(tier=0)

    verifier = MagicMock()
    verifier.verify.return_value = SimpleNamespace(verified=True, reason="ok")

    turn_content = {}

    kit = dict(
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
    kit.update(overrides)
    return make_collaborators(**kit)


def _state(**over):
    s = make_initial_state(session_id="s1", turn_id="t1", query="hello")
    s.update(over)
    return s


# ── Planner node ──────────────────────────────────────────────────────


def test_planner_success_returns_intent_id_and_valid():
    collab = _kit()
    result = planner_node(collab)(_state())
    assert result["intent_valid"] is True
    assert result["intent_id"] == "intent-uuid-abc"
    assert collab["turn_content"]["intent-uuid-abc"]["action"] == "fs.list"


def test_planner_on_brain_error_marks_completed_error_planner():
    collab = _kit()
    collab["qb"].complete.side_effect = BrainProviderError("gemini down")
    result = planner_node(collab)(_state())
    assert result["intent_valid"] is False
    assert result["error_kind"] == "planner"
    assert result["completed"] is True
    assert "gemini down" in result["error_reason"]


def test_planner_on_non_dict_payload_marks_invalid():
    collab = _kit()
    collab["qb"].complete.return_value = SimpleNamespace(content_json="not-a-dict")
    result = planner_node(collab)(_state())
    assert result["intent_valid"] is False
    assert result["error_kind"] == "planner"


def test_planner_on_missing_action_marks_invalid():
    collab = _kit()
    collab["qb"].complete.return_value = SimpleNamespace(content_json={"target": "/tmp"})
    result = planner_node(collab)(_state())
    assert result["intent_valid"] is False


# ── Risk classifier node ──────────────────────────────────────────────


def test_risk_classifier_reads_tier_from_classify():
    collab = _kit()
    collab["turn_content"]["iid"] = {"action": "fs.list", "target": "/tmp"}
    collab["risk_classify"].return_value = SimpleNamespace(tier=2)
    result = risk_classifier_node(collab)(_state(intent_id="iid"))
    assert result["tier"] == 2


def test_risk_classifier_missing_intent_returns_internal_error():
    collab = _kit()
    result = risk_classifier_node(collab)(_state(intent_id="nope"))
    assert result["error_kind"] == "internal"


# ── Verifier node ─────────────────────────────────────────────────────


def test_verifier_skipped_for_tier_below_floor():
    """M7.1 integration: at tier=1 with tier_floor=2 we short-circuit."""
    collab = _kit()
    result = verifier_node(collab)(_state(tier=1))
    assert result["tool_call_valid"] is True
    # verifier.verify() should NOT have been called.
    collab["verifier"].verify.assert_not_called()


def test_verifier_runs_at_tier_ge_floor_and_passes():
    collab = _kit()
    collab["turn_content"]["iid"] = {"action": "package.install", "target": "htop"}
    result = verifier_node(collab)(_state(intent_id="iid", tier=2))
    assert result["tool_call_valid"] is True
    collab["verifier"].verify.assert_called_once()


def test_verifier_rejection_marks_completed():
    collab = _kit()
    collab["turn_content"]["iid"] = {"action": "package.install", "target": "htop"}
    collab["verifier"].verify.return_value = SimpleNamespace(
        verified=False, reason="looks phishy"
    )
    result = verifier_node(collab)(_state(intent_id="iid", tier=2))
    assert result["tool_call_valid"] is False
    assert result["error_kind"] == "verifier"
    assert "phishy" in result["error_reason"]


# ── Executor node ─────────────────────────────────────────────────────


def test_executor_tier0_fast_path_skips_pb():
    """M7.2 integration: Tier 0 fs.list uses tier0_fast_path.try_fast_path
    and NEVER calls PB."""
    collab = _kit()
    collab["turn_content"]["iid"] = {"action": "fs.list", "target": "/home"}
    result = executor_node(collab)(_state(intent_id="iid", tier=0))
    assert result["tool_call_valid"] is True
    assert result["tool_call_hash"]
    # PB was never called on fast path.
    collab["pb"].complete.assert_not_called()
    # The constructed tool_call is stored under the hash.
    tc = collab["turn_content"][result["tool_call_hash"]]
    assert tc == {"tool": "fs.list", "params": {"path": "/home"}}


def test_executor_slow_path_calls_pb_when_action_not_in_allowlist():
    collab = _kit()
    collab["turn_content"]["iid"] = {"action": "package.install", "target": "htop"}
    result = executor_node(collab)(_state(intent_id="iid", tier=2))
    collab["pb"].complete.assert_called_once()
    assert result["tool_call_valid"] is True


def test_executor_pb_error_marks_completed_pb_error():
    collab = _kit()
    collab["turn_content"]["iid"] = {"action": "package.install", "target": "htop"}
    collab["pb"].complete.side_effect = BrainProviderError("pb offline")
    result = executor_node(collab)(_state(intent_id="iid", tier=2))
    assert result["error_kind"] == "pb"
    assert result["completed"] is True


def test_executor_fast_path_off_forces_pb():
    """Config toggle: even Tier 0 hits PB when tier0_fast_path=False."""
    collab = _kit()
    collab["cfg"] = _cfg(tier0_fast_path=False)
    collab["turn_content"]["iid"] = {"action": "fs.list", "target": "/tmp"}
    executor_node(collab)(_state(intent_id="iid", tier=0))
    collab["pb"].complete.assert_called_once()


# ── McpdDispatcher node ───────────────────────────────────────────────


def test_mcpd_dispatcher_calls_mcpd_with_constructed_params():
    collab = _kit()
    collab["turn_content"]["tch"] = {"tool": "fs.list", "params": {"path": "/tmp"}}
    result = mcpd_dispatcher_node(collab)(_state(tool_call_hash="tch"))
    assert result["mcpd_result_hash"]
    call_kwargs = collab["mcpd"].call.call_args.kwargs
    assert call_kwargs["method"] == "fs.list"
    assert call_kwargs["params"] == {"path": "/tmp"}


def test_mcpd_dispatcher_missing_tool_call_returns_internal_error():
    collab = _kit()
    result = mcpd_dispatcher_node(collab)(_state(tool_call_hash="missing"))
    assert result["error_kind"] == "internal"


def test_mcpd_dispatcher_mcpd_error_marks_completed_mcpd_error():
    collab = _kit()
    collab["turn_content"]["tch"] = {"tool": "fs.list", "params": {"path": "/tmp"}}
    collab["mcpd"].call.side_effect = RuntimeError("mcpd down")
    result = mcpd_dispatcher_node(collab)(_state(tool_call_hash="tch"))
    assert result["error_kind"] == "mcpd"


# ── Responder node ────────────────────────────────────────────────────


def test_responder_marks_completed():
    collab = _kit()
    result = responder_node(collab)(_state())
    assert result["completed"] is True


# ── Edge routing ──────────────────────────────────────────────────────


def test_after_planner_routes_to_risk_on_success():
    assert after_planner(_state(intent_valid=True)) == "risk_classifier"


def test_after_planner_routes_to_end_on_error():
    assert after_planner(_state(intent_valid=False, error_kind="planner")) == "END"


def test_after_planner_routes_to_end_on_invalid():
    assert after_planner(_state(intent_valid=False)) == "END"


@pytest.mark.parametrize("tier,expected", [
    (0, "executor"), (1, "executor"),
    (2, "verifier"), (3, "verifier"),
])
def test_after_risk_routes_by_tier(tier, expected):
    assert after_risk(_state(tier=tier)) == expected


def test_after_risk_ends_on_unclassified():
    assert after_risk(_state(tier=-1)) == "END"


def test_after_verifier_routes_to_hitl_on_pass():
    assert after_verifier(_state(tool_call_valid=True)) == "hitl_gate"


def test_after_verifier_ends_on_reject():
    assert after_verifier(_state(tool_call_valid=False)) == "END"


def test_after_hitl_routes_to_executor_on_approve():
    assert after_hitl(_state(hitl_decision="approve")) == "executor"


def test_after_hitl_ends_on_deny():
    assert after_hitl(_state(hitl_decision="deny")) == "END"


def test_after_hitl_ends_on_none():
    assert after_hitl(_state(hitl_decision=None)) == "END"


def test_after_executor_routes_to_mcpd_on_valid():
    assert after_executor(_state(tool_call_valid=True)) == "mcpd_dispatcher"


def test_after_executor_ends_on_invalid():
    assert after_executor(_state(tool_call_valid=False)) == "END"


def test_after_executor_ends_on_error():
    assert after_executor(_state(tool_call_valid=True, error_kind="pb")) == "END"


def test_after_mcpd_routes_to_responder_on_success():
    # mcpd_dispatcher increments step_index before after_mcpd runs;
    # simulate: single-step plan means step_index=1, total_steps=1 → done.
    assert after_mcpd(_state(step_index=1, total_steps=1)) == "responder"


def test_after_mcpd_loops_to_executor_when_more_steps():
    """Task #148: multi-step plans loop back to executor after each
    step's mcpd dispatch completes."""
    assert after_mcpd(_state(step_index=1, total_steps=3)) == "executor"


def test_after_mcpd_routes_to_responder_when_all_steps_done():
    assert after_mcpd(_state(step_index=3, total_steps=3)) == "responder"


def test_after_mcpd_ends_on_error():
    assert after_mcpd(_state(error_kind="mcpd")) == "END"
