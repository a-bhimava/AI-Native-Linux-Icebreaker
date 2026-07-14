"""v6.8 Task #146 Step 2b — LangGraph node functions.

Each node is a pure function of ``(state, collaborators) -> partial state
update dict``. Kept as a separate module so tests exercise them without
compiling the whole graph.

Node contract (research §2 idempotency rules):
- Nodes ABOVE an interrupt() must be pure: any side effect will run
  again on resume. In our graph the ONLY node with interrupt is
  HitlGate, and it's placed BEFORE Executor and McpdDispatcher — so
  Executor and McpdDispatcher run exactly once, after resume.
- Planner + Responder call an LLM: expensive if re-run, but idempotent
  from a correctness standpoint. Tolerable.
- RiskClassifier + Verifier: pure computation, cheap to re-run.

Collaborators are passed as a plain dict rather than a class so nodes
stay function-shaped (matches LangGraph's expected node signature and
keeps testing trivial — pass a dict of mocks).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable

from .agent_graph_state import GraphState
from .backends.base import BrainError, BrainProviderError
from .tier0_fast_path import try_fast_path
from .verifier import should_skip_verifier


# ── Collaborator kit ──────────────────────────────────────────────────


def make_collaborators(
    *,
    cfg: Any,                       # ControllerConfig (has .qb, .verifier, etc.)
    session_store: Any,
    qb_backend: Any,
    pb_backend: Any,
    mcpd_client: Any,
    audit_log: Any,
    risk_classify: Callable[[dict], Any],
    verifier: Any,
    prompts: Any,
    intent_schema: dict,
    intent_store: Any,
    # Turn-content lookup (opaque UUID → intent dict). Kept per-graph;
    # wiped between turns. For Task #146 it's a dict; Task #148 will
    # migrate to session-scoped storage when Plan mode lands.
    turn_content: dict,
) -> dict:
    return {
        "cfg": cfg,
        "session_store": session_store,
        "qb": qb_backend,
        "pb": pb_backend,
        "mcpd": mcpd_client,
        "audit": audit_log,
        "risk_classify": risk_classify,
        "verifier": verifier,
        "prompts": prompts,
        "intent_schema": intent_schema,
        "intent_store": intent_store,
        "turn_content": turn_content,
    }


# ── Helpers ──────────────────────────────────────────────────────────


def _sha(payload: Any) -> str:
    """SHA-256 hex of the JSON-serialized payload. Used everywhere we
    reference content in state without carrying it."""
    if isinstance(payload, (bytes, bytearray)):
        return hashlib.sha256(bytes(payload)).hexdigest()
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Nodes ────────────────────────────────────────────────────────────


def planner_node(collab: dict) -> Callable[[GraphState], dict]:
    """QB planner — reads user query, emits a validated Intent.

    Bound closure returns the actual node function; the closure captures
    the collaborator kit so nodes stay pure (state) -> dict.
    """

    def _run(state: GraphState) -> dict:
        try:
            qb_system = collab["prompts"].get("qb")
            resp = collab["qb"].complete(
                system=qb_system,
                user=state["query"],
                schema=collab["intent_schema"],
                max_retries=1,
            )
            intent = resp.content_json
        except BrainError as exc:
            return {
                "intent_valid": False,
                "error_kind": "planner",
                "error_reason": f"{type(exc).__name__}: {exc}"[:400],
                "completed": True,
            }

        if not isinstance(intent, dict) or "action" not in intent:
            return {
                "intent_valid": False,
                "error_kind": "planner",
                "error_reason": "planner returned non-intent payload",
                "completed": True,
            }

        intent_id = collab["intent_store"].put(intent)
        collab["turn_content"][intent_id] = intent

        return {
            "intent_id": intent_id,
            "intent_valid": True,
        }

    return _run


def risk_classifier_node(collab: dict) -> Callable[[GraphState], dict]:
    """Classify the Intent's tier (0/1/2/3). Pure function of the intent."""

    def _run(state: GraphState) -> dict:
        intent = collab["turn_content"].get(state.get("intent_id", ""))
        if intent is None:
            return {
                "error_kind": "internal",
                "error_reason": "risk classifier: intent lookup miss",
                "completed": True,
            }
        result = collab["risk_classify"](intent)
        # ClassificationResult has .tier (an IntEnum) — coerce to int.
        return {"tier": int(getattr(result, "tier", -1))}

    return _run


def verifier_node(collab: dict) -> Callable[[GraphState], dict]:
    """QB verifier — cross-checks the Intent + tool_call for consistency.

    In v6.8 M7.1, tier_floor gates this: below the floor, we skip the
    vote and mark tool_call_valid=True. HitlGate follows immediately
    for Tier ≥ 2 paths (Verifier is only reached on Tier ≥ 2 branches
    anyway per graph edges).
    """

    def _run(state: GraphState) -> dict:
        vcfg = getattr(collab["cfg"], "verifier", None)
        tier = int(state.get("tier", -1))

        if vcfg is not None and should_skip_verifier(vcfg, tier):
            return {"tool_call_valid": True}

        intent = collab["turn_content"].get(state.get("intent_id", ""))
        # Verifier runs on the intent (v6.7 semantics) — for Task #146's
        # 1-step case we don't have a tool_call yet before Executor. So
        # we pass an empty tool_call: verifier votes on intent-shape only.
        try:
            vresult = collab["verifier"].verify(
                intent=intent,
                tool_call={},
                qb=collab["qb"],
                verifier_system=collab["prompts"].get("qb_verifier"),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "tool_call_valid": False,
                "error_kind": "verifier",
                "error_reason": f"{type(exc).__name__}: {exc}"[:400],
                "completed": True,
            }
        if not getattr(vresult, "verified", False):
            return {
                "tool_call_valid": False,
                "error_kind": "verifier",
                "error_reason": (getattr(vresult, "reason", "") or "verifier rejected")[:400],
                "completed": True,
            }
        return {"tool_call_valid": True}

    return _run


def hitl_gate_node(collab: dict) -> Callable[[GraphState], dict]:
    """HITL — pauses via LangGraph interrupt() until user approves/denies.

    Pure by construction: reads state + collab, calls interrupt() with a
    payload, returns the decision. Interrupt re-runs the node on resume
    — safe because we do no other side effects here.
    """

    from langgraph.types import interrupt  # local import — see §13.5 pattern

    def _run(state: GraphState) -> dict:
        intent = collab["turn_content"].get(state.get("intent_id", ""))
        payload = {
            "type": "hitl_prompt",
            "turn_id": state.get("turn_id", ""),
            "intent_id": state.get("intent_id", ""),
            "intent_action": (intent or {}).get("action", ""),
            "tier": int(state.get("tier", -1)),
            "summary": (intent or {}).get("reason", ""),
        }
        # interrupt() raises to pause; on resume the return value is the
        # user's decision passed via Command(resume="approve"|"deny").
        decision = interrupt(payload)
        return {"hitl_decision": decision, "hitl_required": True}

    return _run


def executor_node(collab: dict) -> Callable[[GraphState], dict]:
    """Turn the Intent into a validated mcp_tool_call.

    Fast path (M7.2): for Tier-0 allowlisted actions, construct the
    tool_call in code without calling PB. Otherwise: PB grammar-decode.
    """

    def _run(state: GraphState) -> dict:
        intent = collab["turn_content"].get(state.get("intent_id", ""))
        if intent is None:
            return {
                "error_kind": "internal",
                "error_reason": "executor: intent lookup miss",
                "completed": True,
            }

        tier = int(state.get("tier", -1))
        rcfg = getattr(collab["cfg"], "run", None)
        fast_enabled = getattr(rcfg, "tier0_fast_path", True)

        tool_call = None
        if fast_enabled:
            tool_call = try_fast_path(intent, tier)

        if tool_call is None:
            # Slow path — call PB via HTTP.
            try:
                pb_user = collab["session_store"].get(state["session_id"]).build_pb_user_turn(
                    state.get("intent_id", ""),
                    intent["action"],
                    tool_schema={},  # Task #146 skeleton — Task #147 wires real schema
                    target=intent.get("target", ""),
                    content=intent.get("content", ""),
                    pb_hint=intent.get("pb_hint", ""),
                )
                pb_system = collab["prompts"].get("pb")
                resp = collab["pb"].complete(
                    system=pb_system, user=pb_user, schema=None, max_retries=1,
                )
                tool_call = resp.content_json
            except BrainError as exc:
                return {
                    "tool_call_valid": False,
                    "error_kind": "pb",
                    "error_reason": f"{type(exc).__name__}: {exc}"[:400],
                    "completed": True,
                }
            except AttributeError:
                # session_store.get() may return None in tests; skip PB.
                return {
                    "tool_call_valid": False,
                    "error_kind": "internal",
                    "error_reason": "executor: session lookup miss",
                    "completed": True,
                }

        # Stash the tool_call by hash so mcpd_dispatcher can retrieve it.
        tc_hash = _sha(tool_call)
        collab["turn_content"][tc_hash] = tool_call

        return {
            "tool_call_hash": tc_hash,
            "tool_call_valid": True,
        }

    return _run


def mcpd_dispatcher_node(collab: dict) -> Callable[[GraphState], dict]:
    """Send the tool_call to mcpd. Comes AFTER hitl_gate on Tier ≥ 2
    paths so the side effect only fires on approve.
    """

    def _run(state: GraphState) -> dict:
        tool_call = collab["turn_content"].get(state.get("tool_call_hash", ""))
        if tool_call is None:
            return {
                "error_kind": "internal",
                "error_reason": "mcpd: tool_call lookup miss",
                "completed": True,
            }
        try:
            result = collab["mcpd"].call(
                method=tool_call.get("tool", ""),
                params=tool_call.get("params", {}),
            )
        except Exception as exc:  # noqa: BLE001 — wrap unknown mcpd faults
            return {
                "error_kind": "mcpd",
                "error_reason": f"{type(exc).__name__}: {exc}"[:400],
                "completed": True,
            }
        result_hash = _sha(getattr(result, "stdout", "") or str(result))
        collab["turn_content"][result_hash] = result
        return {"mcpd_result_hash": result_hash}

    return _run


def responder_node(collab: dict) -> Callable[[GraphState], dict]:
    """Terminal node — marks completed=True.

    Task #146 skeleton: no summarization call yet (Task #151 wires it).
    Just close the state so terminal downstream knows we're done.
    """

    def _run(state: GraphState) -> dict:
        return {"completed": True}

    return _run


# ── Conditional edges ─────────────────────────────────────────────────


def after_planner(state: GraphState) -> str:
    if state.get("error_kind"):
        return "END"
    if not state.get("intent_valid"):
        return "END"
    return "risk_classifier"


def after_risk(state: GraphState) -> str:
    if state.get("error_kind"):
        return "END"
    tier = int(state.get("tier", -1))
    if tier < 0:
        return "END"
    if tier >= 2:
        return "verifier"
    return "executor"


def after_verifier(state: GraphState) -> str:
    if state.get("error_kind"):
        return "END"
    if not state.get("tool_call_valid"):
        return "END"
    return "hitl_gate"


def after_hitl(state: GraphState) -> str:
    if state.get("hitl_decision") == "approve":
        return "executor"
    # deny, timeout, or missing → end without dispatching.
    return "END"


def after_executor(state: GraphState) -> str:
    if state.get("error_kind"):
        return "END"
    if not state.get("tool_call_valid"):
        return "END"
    return "mcpd_dispatcher"


def after_mcpd(state: GraphState) -> str:
    if state.get("error_kind"):
        return "END"
    return "responder"
