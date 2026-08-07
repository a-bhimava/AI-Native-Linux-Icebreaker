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
import sys
import time
import uuid
from typing import Any, Callable

from .agent_graph_state import GraphState
from .backends.base import BrainError, BrainProviderError
from .mcpd_client import ToolResult

# M7.0.1f (v6.16): destructive ops that need the two-phase COW flow.
# Mirrors main.py::_COW_ELIGIBLE — single source of truth is cow.rs on
# the mcpd side; both controller paths (streaming main.py + AgentGraph
# here) enumerate the same set for the pre-flight gate. Kept in sync
# by the test `test_hitl_cow_summary_present.py::test_cow_eligible_agrees`
# added in the M7.0.1f test slice.
COW_ELIGIBLE: frozenset[str] = frozenset({
    "fs.delete",
    "fs.write",
    "package.install",
    "package.remove",
    "package.upgrade",
})


def _cow_preview_params(intent: dict) -> dict:
    """Build mcpd call params for a COW preview. Mirror of
    main.py::_preview_params — kept a local copy so agent_graph_nodes
    stays importable without pulling main.py's controller-lifecycle
    weight into the graph tests."""
    action = intent.get("action", "")
    if action.startswith("fs."):
        params: dict[str, Any] = {"path": intent.get("target", "")}
        if action == "fs.write":
            params["content"] = intent.get("content", "")
        return params
    if action.startswith("package."):
        return {"package": intent.get("target", "")}
    return {}


def _cow_subject(intent: dict) -> str:
    return intent.get("target", "")
from .plan_executor import (
    normalize_planner_output,
    plan_max_tier,
    resolve_step_markers,
    validate_resolved_intent,
)
from .tier0_fast_path import try_fast_path
from .verifier import should_skip_verifier


# v6.9 Bug F (2026-07-17): permissive schema for the QB planner call.
# QB may emit either a bare Intent (v6.7 backward compat) OR a plan
# wrapper (v6.8 Task #148 multi-step). Gemini's response_schema
# doesn't support anyOf/oneOf, and the strict intent.json requires
# intent_id (server-injected — QB is prompted NOT to emit it). Both
# shapes are legal top-level objects with no required fields; we let
# `normalize_planner_output()` below check the actual shape after the
# backend returns. `additionalProperties: true` keeps Gemini happy on
# fields the schema doesn't declare (e.g. rare "reason" / "risk_level"
# on plan wrappers).
_PLANNER_PERMISSIVE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "plan": {"type": "array"},
        "action": {"type": "string"},
        "target": {"type": "string"},
        "params": {"type": "object"},
        "content": {"type": "string"},
        "pb_hint": {"type": "string"},
        "reason": {"type": "string"},
        "risk_level": {"type": "string"},
    },
    "additionalProperties": True,
}


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
    # v6.9 Scope O Layer 2 Part A: optional ManifestRegistry. If set,
    # mcpd_dispatcher_node checks the registry BEFORE mcpd — matching
    # the wiring in main.py::_try_manifest_dispatch. None → mcpd-only
    # dispatch (backward-compatible with pre-Layer 2 tests).
    manifests: Any = None,
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
        "manifests": manifests,
    }


# ── Helpers ──────────────────────────────────────────────────────────


# ── v6.12 Fix B: visited_nodes helper ─────────────────────────────────
#
# Every node calls _mark("<node_name>", "done"|"failed", update_dict) as
# it returns, so the bridge's translate_outcome_to_events can render the
# CoT panel from the actual traversal instead of inferring it from
# outcome fields (the pre-v6.12 approach couldn't represent short-
# circuits like F-32 schema reject, F-35 unsupported, or HITL deny —
# every failed turn rendered all-green in the CoT panel).
#
# visited_nodes is Annotated[list, add] in GraphState so LangGraph's
# reducer appends across nodes; each _mark call contributes exactly one
# tuple.


def _mark(node: str, node_state: str, update: dict) -> dict:
    """Attach a visited_nodes tuple to a node's return dict.

    node_state must be either "done" (successful traversal) or "failed"
    (this node produced the error_kind and short-circuited). The bridge
    renders "done" nodes in the CoT panel's normal colour and "failed"
    nodes in red. Both cases mean "this node ran"; the distinction is
    whether execution continued past it.
    """
    update["visited_nodes"] = [(node, node_state)]
    return update


def _sha(payload: Any) -> str:
    """SHA-256 hex of the JSON-serialized payload. Used everywhere we
    reference content in state without carrying it."""
    if isinstance(payload, (bytes, bytearray)):
        return hashlib.sha256(bytes(payload)).hexdigest()
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_stdout(result: Any) -> str:
    """v6.9 P0-3 (2026-07-15 CT scan): uniformly extract a tool's stdout
    string regardless of whether the dispatcher returned a
    mcpd_client.ToolResult (from either mcpd or the Scope O manifest
    path) or a legacy SimpleNamespace-shape fallback. Returns "" if no
    stdout can be recovered — never raises. Used by the result-hash step
    in mcpd_dispatcher_node so the hash is shape-agnostic and both
    dispatch branches produce identical keys for identical stdout."""
    inner = getattr(result, "result", None)
    if isinstance(inner, dict):
        v = inner.get("stdout", "")
        if isinstance(v, str):
            return v
    v = getattr(result, "stdout", "")
    if isinstance(v, str):
        return v
    return ""


# ── Nodes ────────────────────────────────────────────────────────────


def planner_node(collab: dict) -> Callable[[GraphState], dict]:
    """QB planner — reads user query, emits either a bare Intent (v6.7
    backward compat) OR a plan wrapper (v6.8 Task #148 multi-step).

    Task #148: normalize both shapes to a canonical plan list; store
    the whole plan by ``plan_id`` in turn_content; write ``intent_id``
    for the FIRST step so downstream nodes (risk_classifier, executor)
    have a starting point.
    """

    def _run(state: GraphState) -> dict:
        try:
            # v6.9 Bug E (2026-07-17): monolithic path uses per-backend
            # prompt names (qb_gemini, qb_anthropic, qb_openai, qb_local);
            # this path was calling `.get("qb")` which doesn't exist —
            # broke every AgentGraph invocation the moment Bug C flipped
            # the flag on. Read backend from cfg (one QB backend per
            # daemon) to stay in lock-step with main.py:533.
            _backend = getattr(getattr(collab["cfg"], "qb", None), "backend", "gemini")
            qb_system = collab["prompts"].get(f"qb_{_backend}")
            # v6.9 Bug F (2026-07-17): use the permissive planner
            # schema (no required fields, both shapes legal). See the
            # module-level _PLANNER_PERMISSIVE_SCHEMA docstring.
            resp = collab["qb"].complete(
                system=qb_system,
                user=state["query"],
                schema=_PLANNER_PERMISSIVE_SCHEMA,
                max_retries=1,
            )
            raw = resp.content_json
        except BrainError as exc:
            return _mark("planner", "failed", {
                "intent_valid": False,
                "error_kind": "planner",
                "error_reason": f"{type(exc).__name__}: {exc}"[:400],
                "completed": True,
            })

        # Task #148: accept bare intent OR plan wrapper.
        try:
            plan = normalize_planner_output(raw)
        except ValueError as exc:
            return _mark("planner", "failed", {
                "intent_valid": False,
                "error_kind": "planner",
                "error_reason": f"malformed planner output: {exc}"[:400],
                "completed": True,
            })

        # v6.11 Fix #1 (F-32 recurrence guard): before storing the plan,
        # short-circuit path-requiring actions with empty/"/" target. The
        # monolithic path enforces this at main.py:623-643; Task #173
        # flipped agent_graph.enabled=True without porting the guard, so
        # empty target flowed through PB into mcpd which rejected with a
        # cryptic "Invalid params: \"\" is shorter than 1 character at
        # /path" JSON Schema error. Reuse main.py's _PATH_REQUIRING_ACTIONS
        # (no forking — drift protection).
        from .main import _PATH_REQUIRING_ACTIONS
        for step_idx, step in enumerate(plan):
            step_action = str(step.get("action", "") or "")
            step_target = str(step.get("target", "") or "")
            if step_action in _PATH_REQUIRING_ACTIONS and step_target in ("", "/"):
                # v6.12 Fix F (F-86 2026-07-21): populate state["output"]
                # so the bridge's TokenEvent path streams the friendly
                # rejection to the terminal, matching the F-35 shape.
                # Pre-v6.12 this branch set error_reason only and the
                # user saw the bridge's `[error] F-32: ...` rendering.
                _msg = (
                    f"F-32: step {step_idx+1}/{len(plan)} ({step_action}) "
                    f"has ambiguous target {step_target!r}. Query too "
                    f"ambiguous to route safely — try naming a specific "
                    f"path (e.g. '/home/icebreaker/Downloads')."
                )[:400]
                return _mark("planner", "failed", {
                    "intent_valid": False,
                    "error_kind": "schema",
                    "error_reason": _msg,
                    "output": _msg,
                    "completed": True,
                })

        # v6.11 Fix #2 (F-35 short-circuit): if QB emitted the
        # `system.unsupported` landing pad, do NOT proceed to verifier /
        # executor / mcpd_dispatcher. main.py's _emit_unsupported yields
        # a friendly card and returns; the AgentGraph equivalent is to
        # mark completed=True + populate output so responder_node has
        # something to render and downstream nodes short-circuit via
        # after_planner routing. Without this, the intent flowed to
        # verifier which saw empty tool_call.tool and produced the
        # confusing error "tool_call.tool is missing, cannot match
        # intent.action 'system.unsupported'" (qb_verifier.txt:22 rubric).
        if plan[0].get("action") == "system.unsupported":
            unsupported_params = plan[0].get("params", {}) or {}
            requested = str(unsupported_params.get("requested_intent", "") or "")
            suggestion = str(unsupported_params.get("suggestion", "") or "")
            msg_lines = [
                "This kind of request isn't supported yet.",
            ]
            if requested:
                msg_lines.append(f"You asked: {requested}")
            if suggestion:
                msg_lines.append(f"Suggestion: {suggestion}")
            # v6.12: system.unsupported is a controlled short-circuit, not
            # a failure. Mark the planner "done" (it did its job — routed
            # to unsupported) so the CoT panel doesn't flash red for a
            # totally normal outcome.
            return _mark("planner", "done", {
                "intent_valid": False,
                "error_kind": "unsupported",
                "error_reason": "F-35: system.unsupported short-circuit",
                "output": "\n".join(msg_lines),
                "completed": True,
            })

        # v6.11 Fix #6 (F-43/F-47b applied to ALL plan steps): the
        # monolithic path calls _normalize_server_owned_fields on the
        # intent to overwrite QB-emitted placeholder intent_id /
        # schema_version / timestamp with authoritative server values.
        # Task #148 introduced multi-step plans; Task #173 flipped
        # agent_graph.enabled=True without porting the helper — so step
        # 2+ carried placeholder intent_id ('DO_NOT_EMIT') and failed
        # validation. Apply to every step (idempotent — safe on step 0
        # too, giving F-43/F-47b coverage for the AgentGraph path).
        from .main import _normalize_server_owned_fields
        for step in plan:
            _normalize_server_owned_fields(step)

        # Store the whole plan under a fresh plan_id. Also store step 0
        # under an intent_id so risk_classifier's per-step lookup (which
        # uses intent_id) finds it. Subsequent steps get looked up via
        # the plan_id + step_index keys the executor builds.
        import uuid as _uuid
        plan_id = str(_uuid.uuid4())
        collab["turn_content"][plan_id] = plan

        intent_id = collab["intent_store"].put(plan[0])
        collab["turn_content"][intent_id] = plan[0]

        return _mark("planner", "done", {
            "plan_id": plan_id,
            "step_index": 0,
            "total_steps": len(plan),
            "intent_id": intent_id,
            "intent_valid": True,
        })

    return _run


def risk_classifier_node(collab: dict) -> Callable[[GraphState], dict]:
    """Classify the plan's tier (0/1/2/3).

    Task #148 (D13): for multi-step plans, use max(step.tier) so HITL
    fires ONCE for the whole plan at the highest-risk step. Single-step
    plans behave identically to Task #146 (single intent classification).
    """

    def _run(state: GraphState) -> dict:
        plan_id = state.get("plan_id", "")
        plan = collab["turn_content"].get(plan_id) if plan_id else None

        if plan and isinstance(plan, list) and len(plan) > 1:
            # Multi-step plan: max-tier wins.
            tier = plan_max_tier(plan, collab["risk_classify"])
            return _mark("risk_classifier", "done", {"tier": int(tier)})

        # Backward-compat single-step path — classify the intent directly.
        intent = collab["turn_content"].get(state.get("intent_id", ""))
        if intent is None:
            return _mark("risk_classifier", "failed", {
                "error_kind": "internal",
                "error_reason": "risk classifier: intent lookup miss",
                "completed": True,
            })
        result = collab["risk_classify"](intent)
        return _mark("risk_classifier", "done", {"tier": int(getattr(result, "tier", -1))})

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
            return _mark("verifier", "done", {"tool_call_valid": True})

        intent = collab["turn_content"].get(state.get("intent_id", ""))
        # v6.12 Fix E (F-85 2026-07-21): read the ACTUAL tool_call the
        # executor produced. Pre-v6.12 shape ran verifier BEFORE executor
        # and passed tool_call={} because there was nothing to pass —
        # but qb_verifier.txt rubric #1 requires `tool_call.tool ==
        # intent.action` verbatim, so empty tool_call failed every check.
        # Now the graph runs executor → verifier for tier>=2 (see
        # after_executor at ~line 1030 for the edge move), so
        # state["tool_call_hash"] is set and turn_content has the real
        # tool_call keyed by hash.
        tool_call = collab["turn_content"].get(state.get("tool_call_hash", ""), {})
        if not isinstance(tool_call, dict):
            tool_call = {}
        #
        # v6.11 Fix #4 (F-49-recur): monolithic path (main.py) retries
        # once when `_should_retry_verifier(retry_mode, vresult)` returns
        # True — this handles "verifier call failed" (network flake /
        # LLM timeout / single-shot rejection) per config's retry_mode.
        # Task #173 flipped agent_graph.enabled=True without porting the
        # retry, so legitimate writes intermittently rejected on Gemini
        # flake. Mirror the monolithic behaviour: single retry when the
        # helper says so.
        from .main import _should_retry_verifier
        retry_mode = str(getattr(vcfg, "retry_mode", "on_call_failed_only") or "on_call_failed_only")

        def _verify_once():
            return collab["verifier"].verify(
                intent=intent,
                tool_call=tool_call,
                qb=collab["qb"],
                verifier_system=collab["prompts"].get("qb_verifier"),
            )

        try:
            vresult = _verify_once()
        except Exception as exc:  # noqa: BLE001
            return _mark("verifier", "failed", {
                "tool_call_valid": False,
                "error_kind": "verifier",
                "error_reason": f"{type(exc).__name__}: {exc}"[:400],
                "completed": True,
            })
        # F-49-recur: single retry (max 1) if the retry_mode + result agree.
        if (not getattr(vresult, "verified", False)
                and _should_retry_verifier(retry_mode, vresult)):
            try:
                vresult = _verify_once()
            except Exception as exc:  # noqa: BLE001
                return _mark("verifier", "failed", {
                    "tool_call_valid": False,
                    "error_kind": "verifier",
                    "error_reason": f"{type(exc).__name__}: {exc}"[:400],
                    "completed": True,
                })
        if not getattr(vresult, "verified", False):
            return _mark("verifier", "failed", {
                "tool_call_valid": False,
                "error_kind": "verifier",
                "error_reason": (getattr(vresult, "reason", "") or "verifier rejected")[:400],
                "completed": True,
            })
        return _mark("verifier", "done", {"tool_call_valid": True})

    return _run


def cow_preview_node(collab: dict) -> Callable[[GraphState], dict]:
    """M7.0.1f (v6.16): pre-flight the destructive op so hitl_gate's
    modal renders the diff at first ask (whitepaper §8.2). No interrupt
    here — this node is a side-effectful preview (mcpd.call) that runs
    exactly once per turn. LangGraph does NOT re-run this node when
    hitl_gate's interrupt resumes (only the interrupted node re-runs),
    so the intent_id we stash here survives cleanly across the pause.

    Non-COW ops pass through as no-ops. Preview failures degrade to
    visible-warn — hitl_gate still fires, just without the diff.
    """

    def _run(state: GraphState) -> dict:
        intent = collab["turn_content"].get(state.get("intent_id", ""))
        if not intent or intent.get("action") not in COW_ELIGIBLE:
            # No-op for non-COW ops. cow_preview_ok=True because the node
            # ran to completion — dispatcher's COW-eligible guard doesn't
            # apply here anyway (checked by action inclusion).
            return _mark("cow_preview", "done", {
                "cow_pending_intent_id": None,
                "cow_diff_json": None,
                "cow_preview_ok": True,
            })
        try:
            preview_result: ToolResult = collab["mcpd"].call(
                intent["action"], _cow_preview_params(intent),
            )
        except Exception as exc:  # noqa: BLE001
            # Visible-warn — hitl_gate fires without diff. cow_preview_ok
            # stays False so the dispatcher REFUSES a raw mcpd.call for
            # this COW-eligible action (would silently return a fresh
            # ticket without executing — the exact failure the M7.0.1h
            # Defect #1 fix targets).
            return _mark("cow_preview", "failed", {
                "cow_pending_intent_id": None,
                "cow_diff_json": None,
                "cow_preview_ok": False,
                "error_kind": "mcpd",
                "error_reason": f"cow preview error: {type(exc).__name__}: {exc}"[:400],
            })
        # Defensive: some tests use SimpleNamespace fakes that lack the
        # ToolResult COW helpers. getattr() with False default treats
        # those returns as "no gate needed" — same shape as an
        # actually-executed op on the fs.write $HOME fast path.
        if getattr(preview_result, "requires_cow_approval", False):
            return _mark("cow_preview", "done", {
                "cow_pending_intent_id": getattr(preview_result, "cow_intent_id", None),
                "cow_diff_json": getattr(preview_result, "dry_run_diff", None),
                "cow_preview_ok": True,
            })
        # mcpd didn't gate (e.g. fs.write inside $HOME) — no diff, no
        # commit needed later; dispatcher will fall through to raw
        # mcpd.call as usual. cow_preview_ok=True signals "preview ran
        # cleanly, no gate needed" (distinct from the exception branch).
        return _mark("cow_preview", "done", {
            "cow_pending_intent_id": None,
            "cow_diff_json": None,
            "cow_preview_ok": True,
        })

    return _run


def hitl_gate_node(collab: dict) -> Callable[[GraphState], dict]:
    """HITL — pauses via LangGraph interrupt() until user approves/denies.

    Pure by construction: reads state + collab, calls interrupt() with a
    payload, returns the decision. Interrupt re-runs the node on resume
    — safe because we do no other side effects here.

    M7.0.1f: when state carries `cow_diff_json` (populated by
    `cow_preview_node`), the payload includes it so the client's bridge
    can format the diff into the modal's cow_summary field. Non-COW ops
    or non-COW-eligible flows carry the historical payload unchanged.
    """

    from langgraph.types import interrupt  # local import — see §13.5 pattern

    def _run(state: GraphState) -> dict:
        intent = collab["turn_content"].get(state.get("intent_id", ""))
        payload: dict[str, Any] = {
            "type": "hitl_prompt",
            "turn_id": state.get("turn_id", ""),
            "intent_id": state.get("intent_id", ""),
            "intent_action": (intent or {}).get("action", ""),
            "tier": int(state.get("tier", -1)),
            "summary": (intent or {}).get("reason", ""),
        }
        # M7.0.1f: enrich payload with the COW diff when cow_preview_node
        # stashed one. Bridge picks this up in
        # translate_interrupt_payload_to_display_data and formats via
        # cow_summary.format_diff into cow_summary before the modal renders.
        cow_diff = state.get("cow_diff_json")
        if cow_diff:
            payload["cow_diff_json"] = cow_diff
            payload["cow_intent_id"] = state.get("cow_pending_intent_id", "")
        # interrupt() raises to pause; on resume the return value is the
        # user's decision passed via Command(resume="approve"|"deny").
        decision = interrupt(payload)
        # v6.12 Fix B: deny/timeout marks hitl_gate "failed" so the CoT
        # panel renders it red (matches the fact that execution
        # short-circuits at the gate — after_hitl returns END on deny).
        # Approve marks it "done" so the panel shows the gate ran and
        # execution continued into executor.
        node_state = "done" if decision == "approve" else "failed"
        return _mark("hitl_gate", node_state, {
            "hitl_decision": decision,
            "hitl_required": True,
        })

    return _run


def executor_node(collab: dict) -> Callable[[GraphState], dict]:
    """Turn the CURRENT plan step's Intent into a validated mcp_tool_call.

    Task #148: look up the current step by (plan_id, step_index); resolve
    ``$STEP_<N>_STDOUT`` markers using prior step results; re-validate
    against intent.json (marker-substitution must complete before dispatch);
    then Tier-0 fast path OR PB grammar-decode.
    """

    def _run(state: GraphState) -> dict:
        # Task #148: look up the current step from the plan.
        plan_id = state.get("plan_id", "")
        step_index = int(state.get("step_index", 0))
        total_steps = int(state.get("total_steps", 1))
        plan = collab["turn_content"].get(plan_id) if plan_id else None

        if plan and isinstance(plan, list) and step_index < len(plan):
            raw_step = plan[step_index]
            # Gather prior step results (indices 0..step_index-1).
            prior_results: dict[int, str] = {}
            for i in range(step_index):
                key = f"{plan_id}:{i}:result"
                cached = collab["turn_content"].get(key)
                if cached is not None:
                    prior_results[i] = _extract_stdout_for_marker(cached)
            # Substitute markers → resolved intent.
            intent = resolve_step_markers(raw_step, prior_results)

            # Post-substitution strict validation.
            validation_err = validate_resolved_intent(
                intent, collab["intent_schema"],
            )
            if validation_err is not None:
                return _mark("executor", "failed", {
                    "tool_call_valid": False,
                    "error_kind": "planner",
                    "error_reason": (
                        f"step {step_index + 1}/{total_steps} "
                        f"failed validation: {validation_err}"
                    )[:400],
                    "completed": True,
                })
        else:
            # Backward-compat single-intent path (Task #146 behavior).
            intent = collab["turn_content"].get(state.get("intent_id", ""))
            if intent is None:
                return _mark("executor", "failed", {
                    "tool_call_valid": False,
                    "error_kind": "internal",
                    "error_reason": "executor: intent lookup miss",
                    "completed": True,
                })

        tier = int(state.get("tier", -1))
        rcfg = getattr(collab["cfg"], "run", None)
        fast_enabled = getattr(rcfg, "tier0_fast_path", True)

        tool_call = None
        if fast_enabled:
            tool_call = try_fast_path(intent, tier)

        if tool_call is None:
            # v6.16 M7.0.2f: slow path via PbRetryLoop. AgentGraph runs
            # the verifier as a SEPARATE downstream node (verifier_node
            # at agent_graph_nodes.py:387), so we skip verification
            # inside the loop to avoid double-vote. QB-consult is also
            # skipped here — AgentGraph doesn't have the coaching
            # channel wired; adding it risks INV-1 leakage without a
            # design pass. Deferred to v6.17. Retry + cost-ceiling +
            # logging discipline still apply.
            from .pb_retry import PbRetryLoop, PbCallResult, PbRetryConfig
            import dataclasses as _dc
            try:
                session_state = collab["session_store"].get(state["session_id"])
            except (AttributeError, KeyError):
                return _mark("executor", "failed", {
                    "tool_call_valid": False,
                    "error_kind": "internal",
                    "error_reason": "executor: session lookup miss",
                    "completed": True,
                })

            pb_system = collab["prompts"].get("pb")

            def _pb_complete_fn(intent_arg, current_pb_hint):
                pb_user = session_state.build_pb_user_turn(
                    state.get("intent_id", ""),
                    intent_arg["action"],
                    tool_schema={},
                    target=intent_arg.get("target", ""),
                    content=intent_arg.get("content", ""),
                    pb_hint=current_pb_hint,
                )
                resp = collab["pb"].complete(
                    system=pb_system, user=pb_user,
                    schema=None, max_retries=1,
                )
                # No _validate_tool_call in AgentGraph — mcpd_dispatcher
                # validates downstream. Matches pre-M7.0.2 AgentGraph
                # behaviour. getattr fallbacks tolerate test doubles
                # that omit cost/token attributes on SimpleNamespace.
                return PbCallResult(
                    tool_call=resp.content_json,
                    cost_usd=float(getattr(resp, "cost_usd", 0.0) or 0.0),
                    tokens_in=int(getattr(resp, "tokens_in", 0) or 0),
                    tokens_out=int(getattr(resp, "tokens_out", 0) or 0),
                )

            def _verify_fn(_intent_arg, _tool_call_arg):
                # Verifier runs as its own node — synthesize a pass so
                # PbRetryLoop treats attempt 1 as success (no retry
                # unless PB call raises).
                from .verifier import VerifierResult
                return VerifierResult(
                    verified=True,
                    reason="deferred to verifier_node",
                    votes_cast=0, verified_count=0,
                )

            def _qb_repair_fn(*_args):
                return ""  # QB-consult deferred to v6.17

            # Look up PbRetryConfig from either `controller_cfg` (real
            # AgentGraph wiring in agent_graph.py) or `cfg` (some test
            # kits). Falls back to defaults if neither is present.
            _cfg_holder = (
                collab.get("controller_cfg") or collab.get("cfg")
            )
            _pb_retry_cfg_raw = getattr(_cfg_holder, "pb_retry", None)
            if _pb_retry_cfg_raw is not None:
                pb_retry_cfg = PbRetryConfig(**_dc.asdict(_pb_retry_cfg_raw))
            else:
                pb_retry_cfg = PbRetryConfig()

            try:
                _loop = PbRetryLoop(
                    cfg=pb_retry_cfg,
                    pb_complete_fn=_pb_complete_fn,
                    verify_fn=_verify_fn,
                    qb_repair_fn=_qb_repair_fn,
                    logger=None,
                )
                pb_result = _loop.run(intent)
            except BrainError as exc:
                return _mark("executor", "failed", {
                    "tool_call_valid": False,
                    "error_kind": "pb",
                    "error_reason": f"{type(exc).__name__}: {exc}"[:400],
                    "completed": True,
                })

            if not pb_result.success:
                return _mark("executor", "failed", {
                    "tool_call_valid": False,
                    "error_kind": "pb",
                    "error_reason": (
                        f"pb retry exhausted: {pb_result.final_reason}"
                    )[:400],
                    "pb_attempts": len(pb_result.attempts),
                    "completed": True,
                })
            tool_call = pb_result.final_tool_call

        # Stash tool_call by hash so mcpd_dispatcher can retrieve it.
        tc_hash = _sha(tool_call)
        collab["turn_content"][tc_hash] = tool_call

        return _mark("executor", "done", {
            "tool_call_hash": tc_hash,
            "tool_call_valid": True,
        })

    return _run


def _extract_stdout_for_marker(result: Any) -> str:
    """Extract the string a ``$STEP_<N>_STDOUT`` marker should resolve to.

    mcpd_dispatcher stores whatever the dispatcher returned. Post-v6.9-P0-3
    (2026-07-15 CT scan) both dispatch paths (mcpd + manifest) return a
    real ``mcpd_client.ToolResult(result={...})``, so ``.result["stdout"]``
    is the primary shape. Older shapes still tolerated for defensive
    resilience. Falls back to ``str(result)`` at the end so markers get
    SOMETHING to substitute (unlike ``_extract_stdout`` which returns "" —
    markers want a substitution string, hashes want empty for consistency).

    Common shapes handled:
    - ToolResult(result={"stdout": "..."}) — v6.9 primary
    - object.stdout attribute (legacy SimpleNamespace fallback)
    - dict {"stdout": "..."}
    - anything else → str(...) fallback
    """
    inner = getattr(result, "result", None)
    if isinstance(inner, dict) and isinstance(inner.get("stdout"), str):
        return inner["stdout"]
    stdout = getattr(result, "stdout", None)
    if stdout is None and isinstance(result, dict):
        stdout = result.get("stdout")
    if stdout is None:
        stdout = str(result)
    return str(stdout)


# v6.12 Fix I+ (2026-07-22): subprocess isolation for gui.*/rpa.* dispatch.
#
# The initial Fix I (2026-07-21) called `from gui_agent.agent import GuiAgent`
# + `handle_request(...)` directly inside the daemon's turn-worker thread.
# pyatspi (transitively imported) lazy-initializes GLib, which requires a
# running GMainLoop in the caller's thread. The daemon worker thread has
# none → first AT-SPI enumeration (e.g. gui.get_window_list) aborted the
# daemon with SIGTRAP inside libglib-2.0. Ubuntu apport caught it as a
# process crash on UTM Stage E2.
#
# Fix I+ runs the call in a fresh subprocess (fresh interpreter → fresh
# GLib init → mainloop wraps single call safely). Costs ~100-200ms per
# gui.*/rpa.* call. Faster strategies (persistent worker daemon, direct
# D-Bus AT-SPI, daemon-side mainloop thread) documented in
# GROUND_TRUTH.md § 7 F-88/F-89 notes for v6.13+.


def _config_to_dict(cfg: Any) -> dict:
    """Serialize a Gui/RpaConfig dataclass instance to a plain dict so
    the subprocess can rebuild a SimpleNamespace from it. Only public
    (non-underscore, non-callable) fields are serialized. Returns {}
    when cfg is None.

    V.6f (2026-08-02): nested dataclass fields (e.g. GuiConfig.vision =
    GuiVisionConfig(...)) are recursively converted via
    ``dataclasses.asdict()`` so `[gui.vision]` / `[gui.trust]` /
    `[gui.preview]` / `[gui.geometry]` sub-sections reach the agent
    subprocess. Pre-V.6f, nested dataclasses were coerced to `str(v)`
    which produced a repr string the agent couldn't parse — the
    entire `[gui.vision]` etc surface was silently ignored on the OC
    edition. CT-scan P0-3.
    """
    if cfg is None:
        return {}
    # Fast path: if the whole thing is a dataclass instance, asdict()
    # gives us the recursive-nested-dict form directly.
    import dataclasses
    if dataclasses.is_dataclass(cfg) and not isinstance(cfg, type):
        try:
            return dataclasses.asdict(cfg)
        except Exception:  # noqa: BLE001
            pass  # fall through to the field-by-field walk below
    out: dict = {}
    for name in dir(cfg):
        if name.startswith("_"):
            continue
        try:
            v = getattr(cfg, name)
        except Exception:  # noqa: BLE001
            continue
        if callable(v):
            continue
        # Coerce Path / other exotic types to str for JSON round-trip.
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[name] = v
        elif dataclasses.is_dataclass(v):
            # Nested dataclass field — recurse into it so
            # [gui.vision]/[gui.trust]/... reach the child.
            try:
                out[name] = dataclasses.asdict(v)
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(v, (list, tuple)):
            # Preserve simple sequences of primitives.
            try:
                out[name] = [x for x in v if isinstance(x, (str, int, float, bool))]
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                out[name] = str(v)
            except Exception:  # noqa: BLE001
                pass
    return out


def _dispatch_in_subprocess(
    kind: str, tool_call: dict, cfg: Any, timeout_s: float = 10.0,
) -> dict:
    """Run gui.*/rpa.* dispatch in a fresh subprocess via the
    ``controller.gui_worker`` module. Returns a dict with either
    ``{"ok": True, "result": {...}}`` or ``{"ok": False, "error": "..."}``.
    Never raises — every failure path (timeout, non-zero exit, JSON
    parse error, subprocess spawn failure) is caught and reported in
    the envelope.

    Callers wrap the returned payload into a ToolResult so downstream
    graph code (hash, responder) sees the same shape whether the tool
    was mcpd-, manifest-, or subprocess-dispatched.
    """
    import subprocess  # local import — only pulled in when needed

    req = {
        "kind": kind,
        "tool": tool_call.get("tool", ""),
        "params": tool_call.get("params", {}) or {},
        "config": _config_to_dict(cfg),
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "controller.gui_worker"],
            input=json.dumps(req),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"{tool_call.get('tool', kind)}: subprocess timed out "
                     f"after {timeout_s:.0f}s",
        }
    except (FileNotFoundError, PermissionError) as exc:
        return {
            "ok": False,
            "error": f"{tool_call.get('tool', kind)}: cannot spawn worker: "
                     f"{type(exc).__name__}: {exc}",
        }
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"{tool_call.get('tool', kind)}: worker exit "
                     f"{proc.returncode}: {proc.stderr[:400]}",
        }
    try:
        envelope = json.loads(proc.stdout.strip() or "{}")
    except (json.JSONDecodeError, ValueError) as exc:
        return {
            "ok": False,
            "error": f"{tool_call.get('tool', kind)}: worker stdout "
                     f"unparseable ({exc}); stdout={proc.stdout[:200]!r}",
        }
    if not isinstance(envelope, dict):
        return {"ok": False, "error": "worker returned non-dict envelope"}
    return envelope


def mcpd_dispatcher_node(collab: dict) -> Callable[[GraphState], dict]:
    """Send the tool_call to mcpd. Comes AFTER hitl_gate on Tier ≥ 2
    paths so the side effect only fires on approve.

    Task #148: also stores the result at ``{plan_id}:{step_index}:result``
    so subsequent steps can substitute it via markers. Increments
    step_index in the returned state; the after_mcpd edge routes back
    to executor when step_index < total_steps.
    """

    def _run(state: GraphState) -> dict:
        tool_call = collab["turn_content"].get(state.get("tool_call_hash", ""))
        if tool_call is None:
            return _mark("mcpd_dispatcher", "failed", {
                "error_kind": "internal",
                "error_reason": "mcpd: tool_call lookup miss",
                "completed": True,
            })
        try:
            # v6.9 Scope O Layer 2 Part A: manifest-first dispatch.
            # If the tool is registered in the ManifestRegistry, route
            # to the controller-side impl (nav.cd → session_op.set_cwd)
            # and skip mcpd. Falls through to mcpd for everything the
            # registry doesn't know.
            tool_name = tool_call.get("tool", "")
            manifests = collab.get("manifests")
            if manifests is not None and manifests.has(tool_name):
                from .impl_kinds import DispatchContext
                # session_store is a MagicMock in some tests; get() may
                # return None. session_op tolerates a bare namespace.
                sess = None
                try:
                    sess = collab["session_store"].get(
                        state.get("session_id", "")
                    )
                except Exception:  # noqa: BLE001
                    sess = None
                ctx = DispatchContext(session=sess, audit_log=collab["audit"])
                impl_res = manifests.dispatch(
                    tool_name, tool_call.get("params", {}), ctx,
                )
                # v6.9 P0-3 (2026-07-15 CT scan): wrap ImplResult in a
                # real ToolResult so downstream code sees the SAME shape
                # regardless of whether the tool was manifest- or
                # mcpd-served. Previously we returned SimpleNamespace,
                # which meant a future Tier-2 manifest could bypass COW
                # via this branch (SimpleNamespace has no
                # .requires_cow_approval property). Preserves INV-6.
                _payload = {
                    "stdout": impl_res.stdout,
                    "ok": True,
                    "manifest_name": tool_name,      # INV-8 audit provenance
                    "manifest_dispatched": True,     # discriminates manifest vs mcpd
                }
                _payload.update(impl_res.metadata)
                result = ToolResult(result=_payload, request_id=0)
            elif tool_name.startswith("gui.") or tool_name.startswith("rpa."):
                # v6.12 Fix I+ (F-88/F-89 2026-07-22): route to
                # controller.gui_worker subprocess. In-thread call
                # (Fix I initial) crashed the daemon with SIGTRAP
                # inside libglib-2.0 — see _dispatch_in_subprocess
                # docstring above for the full story.
                _kind = "gui" if tool_name.startswith("gui.") else "rpa"
                _cfg_attr = "gui" if _kind == "gui" else "rpa"
                _cfg = getattr(collab.get("cfg"), _cfg_attr, None)
                if _cfg is not None and not getattr(_cfg, "enabled", True):
                    return _mark("mcpd_dispatcher", "failed", {
                        "error_kind": "mcpd",
                        "error_reason": (
                            f"{_kind.upper()} automation is disabled. "
                            f"Enable with [{_cfg_attr}] enabled = true in "
                            "/etc/icebreaker/controller.toml"
                        ),
                        "completed": True,
                    })
                envelope = _dispatch_in_subprocess(_kind, tool_call, _cfg)
                if not envelope.get("ok"):
                    return _mark("mcpd_dispatcher", "failed", {
                        "error_kind": "mcpd",
                        "error_reason": str(envelope.get("error", "unknown"))[:400],
                        "completed": True,
                    })
                sub_result = envelope.get("result") or {}
                if not isinstance(sub_result, dict):
                    sub_result = {"result": sub_result}
                _payload = dict(sub_result)
                # Same stdout selection as the pre-Fix-I+ in-thread
                # path so responder-summarize input is unchanged.
                _stdout = ""
                if isinstance(_payload.get("stdout"), str):
                    _stdout = _payload["stdout"]
                elif isinstance(_payload.get("status"), str):
                    _stdout = f"{tool_name}: {_payload['status']}"
                else:
                    _stdout = json.dumps(_payload)[:2000]
                _payload["stdout"] = _stdout
                _payload.setdefault("ok", True)
                _payload[f"{_kind}_dispatched"] = True
                _payload["subprocess_isolated"] = True   # provenance
                result = ToolResult(result=_payload, request_id=0)
            else:
                # M7.0.1f (v6.16): if cow_preview_node stashed a pending
                # intent_id AND hitl_gate approved, dispatch via cow.commit
                # instead of a raw mcpd.call. This consumes the pre-approved
                # intent from mcpd's IntentStore and executes the real op —
                # the flagship two-phase commit path the whitepaper §5 promises.
                cow_intent_id = state.get("cow_pending_intent_id")
                intent_obj = collab["turn_content"].get(state.get("intent_id", ""))
                if (
                    cow_intent_id
                    and state.get("hitl_decision") == "approve"
                    and intent_obj
                    and intent_obj.get("action") in COW_ELIGIBLE
                ):
                    result = collab["mcpd"].commit_cow(
                        cow_intent_id,
                        intent_obj["action"],
                        _cow_subject(intent_obj),
                    )
                # M7.0.1h (v6.16 post-ct-scan Defect #1): if the intent IS
                # COW-eligible AND cow_preview_ok is False (preview failed
                # at cow_preview_node — errored or never ran), a raw
                # mcpd.call would fire `fs.delete` etc → mcpd returns a
                # fresh ticket → dispatcher would mark it "done" and the
                # real op would silently NEVER RUN while the user believes
                # they approved a delete. Fail loudly instead. Mirrors
                # main.py:1694-1731 second-modal fallback semantics
                # (return an err, don't pretend success).
                #
                # cow_preview_ok=True + no pending_intent_id means preview
                # ran cleanly and mcpd said "no gate needed" (e.g. fs.write
                # inside $HOME fast path) — raw dispatch is safe there.
                elif (
                    intent_obj
                    and intent_obj.get("action") in COW_ELIGIBLE
                    and not state.get("cow_preview_ok", False)
                ):
                    step_index = int(state.get("step_index", 0))
                    total_steps = int(state.get("total_steps", 1))
                    step_label = f"step {step_index + 1}/{total_steps}"
                    return _mark("mcpd_dispatcher", "failed", {
                        "error_kind": "mcpd",
                        "error_reason": (
                            f"{step_label} ({intent_obj.get('action', '')}): "
                            "COW pre-flight failed and no pending intent_id — "
                            "refusing to fire raw mcpd.call for a destructive op "
                            "(would silently return a fresh ticket without executing). "
                            "Retry the turn; if the preview keeps failing check "
                            "mcpd + Landlock/seccomp posture."
                        )[:400],
                        "completed": True,
                    })
                else:
                    # Non-COW-eligible action OR COW-eligible + preview
                    # cleanly said "no gate needed" — dispatch normally.
                    result = collab["mcpd"].call(
                        method=tool_name,
                        params=tool_call.get("params", {}),
                    )
        except Exception as exc:  # noqa: BLE001 — wrap unknown mcpd faults
            step_index = int(state.get("step_index", 0))
            total_steps = int(state.get("total_steps", 1))
            step_label = f"step {step_index + 1}/{total_steps}"
            return _mark("mcpd_dispatcher", "failed", {
                "error_kind": "mcpd",
                "error_reason": f"{step_label} ({tool_call.get('tool', '')}): "
                                f"{type(exc).__name__}: {exc}"[:400],
                "completed": True,
            })
        # v6.9 P0-3 (2026-07-15 CT scan): shape-agnostic hash extraction.
        # Both dispatch paths now return ToolResult (mcpd via .call, manifest
        # via the wrap above), but a legacy SimpleNamespace-shape may still
        # slip through in tests — _extract_stdout handles both.
        result_hash = _sha(_extract_stdout(result) or str(getattr(result, "result", result)))
        collab["turn_content"][result_hash] = result

        # Task #148: also stash by (plan_id, step_index) so subsequent
        # steps' marker resolution can look it up.
        plan_id = state.get("plan_id", "")
        step_index = int(state.get("step_index", 0))
        if plan_id:
            key = f"{plan_id}:{step_index}:result"
            collab["turn_content"][key] = result

        # Advance to the next step. after_mcpd edge decides whether to
        # loop back to executor or route to responder.
        return _mark("mcpd_dispatcher", "done", {
            "mcpd_result_hash": result_hash,
            "step_index": step_index + 1,
        })

    return _run


_SUMMARISE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary"],
    "properties": {"summary": {"type": "string", "maxLength": 2000}},
}


def responder_node(collab: dict) -> Callable[[GraphState], dict]:
    """Terminal node — QB-summarize the last step's tool output.

    v6.10 Track A (2026-07-17): replaced the Task #146 stub with a
    working non-streaming summarizer so ``agent_graph.enabled=True``
    produces non-empty ``TurnResult.output``. Mirrors the monolithic
    ``Controller._qb_summarise`` at ``main.py:2109`` — same prompt,
    same schema, same F-53 exception surface pattern. Full LangGraph
    ``astream_events`` streaming is still Task #151 and deferred.

    Reads:
    - ``state["plan_id"]``, ``state["step_index"]`` — final step index
    - ``collab["turn_content"][f"{plan_id}:{step_index-1}:result"]``
      — the ToolResult stashed by ``mcpd_dispatcher_node``
    - ``state["intent_id"]`` → ``collab["intent_store"].get(iid)``
      for the summarize prompt's action + target context

    Writes:
    - ``state["output"]`` — NL summary string (non-empty on success;
      degraded-but-non-empty on QB failure per F-53 pattern)
    - ``state["completed"]`` = True
    """

    def _run(state: GraphState) -> dict:
        # Locate the last step's result. mcpd_dispatcher_node increments
        # step_index AFTER storing, so the final result key is at
        # (step_index - 1). Plan mode + single-intent both flow this way
        # because a bare intent becomes a 1-step plan (Task #148).
        plan_id = str(state.get("plan_id", "") or "")
        step_index = int(state.get("step_index", 0) or 0)
        final_idx = max(0, step_index - 1)
        result_key = f"{plan_id}:{final_idx}:result"
        tool_result = collab.get("turn_content", {}).get(result_key)

        # Extract a raw output string. ToolResult.result is a dict from
        # mcpd_client; manifest branch (P0-3) also produces ToolResult
        # with `stdout` inside .result. Fall back to str() for shapes
        # we don't recognise so we always populate something.
        raw = ""
        if isinstance(tool_result, ToolResult):
            payload = tool_result.result or {}
            if isinstance(payload, dict):
                raw = str(payload.get("stdout", "")) or json.dumps(payload)[:2000]
            else:
                raw = str(payload)[:2000]
        elif tool_result is not None:
            raw = str(tool_result)[:2000]

        # Pull intent for the summarise prompt's context (action + target).
        intent = {}
        intent_id = str(state.get("intent_id", "") or "")
        intent_store = collab.get("intent_store")
        if intent_id and intent_store is not None:
            got = intent_store.get(intent_id)
            if isinstance(got, dict):
                intent = got

        # Call QB summarize — mirrors main.py::_qb_summarise. F-53
        # pattern: catch any brain failure, surface the exception class
        # + prefix, still populate output so the user sees SOMETHING
        # instead of a silent empty response.
        summarise_system = (
            "Summarise the following tool output in 1-3 plain sentences "
            "for the user. Be concise and factual. Output only JSON: "
            '{"summary": "<text>"}'
        )
        user_msg = json.dumps(
            {
                "tool_output": raw,
                "action": intent.get("action", ""),
                "target": intent.get("target", ""),
            },
            separators=(",", ":"),
        )
        qb = collab.get("qb")
        summary = raw[:200] if raw else ""
        if qb is not None:
            try:
                resp = qb.complete(
                    system=summarise_system,
                    user=user_msg,
                    schema=_SUMMARISE_SCHEMA,
                    max_retries=1,
                )
                got_summary = (resp.content_json or {}).get("summary", "")
                if got_summary:
                    summary = got_summary
            except (BrainError, BrainProviderError, Exception) as exc:  # noqa: BLE001 F-53
                # Prefix so the user sees WHY they're looking at raw
                # bytes instead of a summary. Same shape monolithic
                # main.py uses.
                summary = (
                    f"[summary generation failed: {type(exc).__name__}] "
                    + (raw[:200] if raw else "(no tool output)")
                )

        # If everything else failed and we have literally no raw output
        # (e.g. tool returned an empty dict), synthesise a minimal
        # positive confirmation so TurnResult.output is never empty
        # (which would recreate the Bug C symptom users saw).
        if not summary:
            action = intent.get("action", "action")
            summary = f"{action} completed."

        return _mark("responder", "done", {"completed": True, "output": summary})

    return _run


# ── v6.12 Fix A: audit_writer terminal node ───────────────────────────
#
# Every terminal branch in the graph (responder success, planner
# short-circuit, executor error, mcpd error, verifier reject, hitl deny)
# routes through this node before END so INV-8 is upheld regardless of
# which branch fired. main.py's run_turn_streaming had 12+ audit
# write_fields sites — one per terminal branch — and Task #173's flag
# flip forgot to port them. This node is the AgentGraph equivalent of
# that discipline: one call site, one shape, always fires.
#
# Design:
# - Reads all provenance from state + collab (no new inputs).
# - Maps state.error_kind → Outcome enum via _outcome_from_state.
# - Mirrors AuditFields(...) construction pattern at main.py:711 exactly
#   so audit log rows are indistinguishable regardless of which pipeline
#   produced them.
# - Any exception during write is swallowed with _log_exception —
#   audit failure MUST NOT break turn delivery to the user (BP-10 spirit:
#   never let observability become a DoS amplifier).
# - The node itself marks visited "done" (or "failed" if the write
#   raised) so operators can see audit health in the CoT panel.


def _outcome_from_state(state: GraphState) -> Any:
    """Map the AgentGraph state's terminal fields → Outcome enum.

    Mirrors the Outcome selection main.py::run_turn_streaming makes at
    each of its 12 write_fields call sites: schema-reject → SCHEMA_REJECTED,
    hitl deny/timeout → HITL_DENIED/HITL_TIMEOUT, verifier reject →
    QB_VERIFIER_REJECTED, mcpd fault → TOOL_ERROR, brain fault →
    BRAIN_ERROR, unsupported short-circuit → UNSUPPORTED, otherwise
    EXECUTED. Kept as a plain lookup so a new error_kind falls back to
    BRAIN_ERROR (safe, human-readable) instead of silently misclassifying.
    """
    from .audit import Outcome

    kind = str(state.get("error_kind") or "")
    if not kind:
        return Outcome.EXECUTED

    if kind == "schema":
        return Outcome.SCHEMA_REJECTED
    if kind == "unsupported":
        return Outcome.UNSUPPORTED
    if kind == "verifier":
        return Outcome.QB_VERIFIER_REJECTED
    if kind == "mcpd":
        return Outcome.TOOL_ERROR
    if kind == "hitl":
        # explicit hitl error_kind only fires on non-tty / timeout paths;
        # normal deny comes through hitl_decision instead
        return Outcome.HITL_TIMEOUT
    # planner, pb, internal, validation → generic brain failure bucket
    return Outcome.BRAIN_ERROR


def audit_writer_node(collab: dict) -> Callable[[GraphState], dict]:
    """v6.12 Fix A — terminal node that writes one audit row per turn.

    Runs after every terminal branch (see agent_graph.py edge wiring).
    Failure to write is logged but never re-raised — turn delivery is
    more important than the audit line for that single turn, and repeated
    audit failures surface via the CoT panel (this node visits "failed")
    and via the daemon's structured error log.
    """

    def _run(state: GraphState) -> dict:
        from .audit import AuditFields

        # ── Provenance lookup ──────────────────────────────────────
        # intent is looked up by intent_id; may be absent on pre-planner
        # rejects (BrainError before intent_store.put). Coerce every
        # AuditFields input to the expected type so an unset field never
        # trips make_entry's non-null validators.
        intent = {}
        iid = str(state.get("intent_id", "") or "")
        if iid:
            got = collab.get("turn_content", {}).get(iid)
            if isinstance(got, dict):
                intent = got

        # session_id + turn_index come from SessionStore; on internal
        # errors before the session is even created the store may not
        # know this session — swallow and use safe defaults.
        session_id = str(state.get("session_id", "") or "")
        turn_index = 0
        session_backend = ""
        try:
            sess = collab["session_store"].get(session_id)
            if sess is not None:
                turn_index = int(getattr(sess, "turn_index", 0) or 0)
                session_backend = str(getattr(sess, "backend", "") or "")
        except Exception:  # noqa: BLE001 — audit path must not raise
            pass

        # HITL decision overrides error_kind when the gate itself
        # short-circuited (after_hitl routes to END on deny).
        outcome = _outcome_from_state(state)
        hitl_dec = state.get("hitl_decision")
        if hitl_dec == "deny":
            from .audit import Outcome
            outcome = Outcome.HITL_DENIED

        # Duration from state.t0_monotonic (populated by run() at
        # graph invocation). Zero-safe: if t0 wasn't seeded, we still
        # emit a row with duration_ms=0 rather than skipping the audit.
        t0 = float(state.get("t0_monotonic", 0.0) or 0.0)
        duration_ms = max(0.0, (time.monotonic() - t0) * 1000) if t0 else 0.0

        # Cost / token totals — Task #148 Cost accounting fields are not
        # yet on GraphState (deferred to a follow-up commit). Zero for
        # now so the field is present and the audit row shape stays
        # stable; a later commit fills them in without a schema break.
        cfg = collab.get("cfg")
        qb_model = ""
        try:
            qb_model = str(getattr(getattr(cfg, "qb", None), "model", "") or "")
        except Exception:  # noqa: BLE001
            pass

        fields = AuditFields(
            session_id=session_id,
            turn_index=turn_index,
            intent_id=iid,
            action=str(intent.get("action", "") or ""),
            target=str(intent.get("target", "") or ""),
            tier=int(state.get("tier", -1) or -1),
            reason=str(intent.get("reason", "") or ""),
            risk_level=str(intent.get("risk_level", "") or ""),
            outcome=outcome,
            duration_ms=duration_ms,
            backend=session_backend,
            model=qb_model,
            tokens_in=0,
            tokens_out=0,
            cost_estimate_usd=0.0,
            extra={
                "error_kind": str(state.get("error_kind") or ""),
                "error_reason": str(state.get("error_reason") or "")[:400],
                "plan_id": str(state.get("plan_id") or ""),
                "total_steps": int(state.get("total_steps", 1) or 1),
            },
        )

        try:
            collab["audit"].write_fields(fields)
        except Exception as exc:  # noqa: BLE001 — audit MUST NOT break turn
            try:
                from .main import _log_exception
                from .logger import get_logger
                _log_exception(
                    get_logger("controller.agent_graph.audit_writer"),
                    "audit_writer_node.write_fields",
                    exc,
                )
            except Exception:  # noqa: BLE001 — logging can't cascade either
                pass
            return _mark("audit_writer", "failed", {})

        return _mark("audit_writer", "done", {})

    return _run


# ── Conditional edges ─────────────────────────────────────────────────


def after_planner(state: GraphState) -> str:
    if state.get("error_kind"):
        return "END"
    if not state.get("intent_valid"):
        return "END"
    return "risk_classifier"


def after_risk(state: GraphState) -> str:
    # v6.12 Fix E (F-85 2026-07-21): every tier goes to executor. The
    # pre-v6.12 shape routed tier>=2 directly to verifier before executor
    # ran; that meant verifier_node had NO tool_call to verify (executor
    # is what generates it), so it passed tool_call={} into verify() and
    # the qb_verifier.txt rubric #1 (`tool_call.tool == intent.action`)
    # fired every single time. Every fs.write outside $HOME + every
    # package.install + every service.* op was silently unreachable
    # through the AgentGraph pipeline. This restores v6.7 semantics
    # (verifier runs on the actual tool_call PB produced) by moving the
    # tier branch to after_executor and updating after_hitl to skip
    # executor (already ran) and go straight to mcpd_dispatcher.
    if state.get("error_kind"):
        return "END"
    tier = int(state.get("tier", -1))
    if tier < 0:
        return "END"
    return "executor"


def after_verifier(state: GraphState) -> str:
    if state.get("error_kind"):
        return "END"
    if not state.get("tool_call_valid"):
        return "END"
    return "hitl_gate"


def after_hitl(state: GraphState) -> str:
    # v6.12 Fix E: approve routes to mcpd_dispatcher because executor
    # already ran BEFORE verifier (v6.7 semantics — see after_risk
    # comment). Pre-v6.12 shape was hitl_gate → executor → mcpd_dispatcher
    # because verifier ran before executor; that shape passed tool_call={}
    # into verify() and broke every tier>=2 turn.
    if state.get("hitl_decision") == "approve":
        return "mcpd_dispatcher"
    # deny, timeout, or missing → end without dispatching.
    return "END"


def after_executor(state: GraphState) -> str:
    # v6.12 Fix E: executor now decides whether to route through the
    # verifier + hitl_gate gauntlet (tier>=2) or straight to mcpd_dispatcher
    # (tier<2). Verifier runs AFTER executor so it can see the real
    # tool_call PB produced — the whole point of a verifier.
    if state.get("error_kind"):
        return "END"
    if not state.get("tool_call_valid"):
        return "END"
    tier = int(state.get("tier", -1))
    if tier >= 2:
        return "verifier"
    return "mcpd_dispatcher"


def after_mcpd(state: GraphState) -> str:
    """Task #148: loop back to executor if more steps remain in the plan.

    step_index was incremented by mcpd_dispatcher_node after storing the
    step's result. When step_index reaches total_steps, all steps are
    done → route to responder.
    """
    if state.get("error_kind"):
        return "END"
    step_index = int(state.get("step_index", 0))
    total_steps = int(state.get("total_steps", 1))
    if step_index < total_steps:
        return "executor"   # loop for the next step
    return "responder"
