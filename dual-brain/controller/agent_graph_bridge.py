"""v6.8 Task #147 — bridge between Controller.run_turn_streaming and AgentGraph.

When ``cfg.agent_graph.enabled`` is True, Controller.run_turn_streaming
delegates to ``run_via_agent_graph`` (this module). The bridge:

1. Translates the graph's terminal outcome dict into the ``TurnEvent``
   stream the daemon + terminal expect.
2. Handles the HITL pause/resume dance by looping between
   ``AgentGraph.run/resume`` and the existing ``HitlPresenter`` — no
   wire-protocol change from the client's perspective.
3. Emits a minimum-viable event stream (ProgressEvent + CotEvent + one
   ResultEvent). Token streaming is Task #151's scope; rich HITL
   semantics (EXPLAIN/MODIFY/TRUST) are Task #152's scope.

Design decisions locked (§9 of plan):
- D7 Event scope: minimum-viable (Progress + CoT + Result). TokenEvent
  streaming loss is a known Task #147 regression restored by Task #151.
- D8 HITL bridge: reuse ForwardingPresenter loop. Decision mapping:
  APPROVED -> "approve"; everything else -> "deny".

Files this module reads / writes:
- ``controller/turn_events.py`` — event dataclass shapes it emits.
- ``controller/hitl.py`` — Decision + HitlDisplayData for the presenter.
- ``controller/audit.py`` — Outcome enum for TurnResult.
"""

from __future__ import annotations

import time
from typing import Any, Iterator, Optional

from .audit import Outcome
from .hitl import Decision, HitlDisplayData
from .risk_classifier import Tier


# Named nodes in the AgentGraph pipeline (mirrors agent_graph.py::_build_graph).
# Each is surfaced as a ProgressEvent + CotEvent pair so the companion
# panel stays populated when the flag is on.
_NODE_LABELS: list[tuple[str, str]] = [
    ("planner",         "Planner (QB → Intent)"),
    ("risk_classifier", "Risk Classification"),
    ("verifier",        "Verifier Vote"),
    ("hitl_gate",       "HITL Gate"),
    ("executor",        "Executor (PB → tool_call)"),
    ("mcpd_dispatcher", "MCP Dispatch"),
    ("responder",       "Responder"),
]
_NODE_INDEX = {name: i for i, (name, _) in enumerate(_NODE_LABELS)}
_TOTAL_STEPS = len(_NODE_LABELS)


# ── Outcome → TurnResult translation ─────────────────────────────────


def _outcome_to_result(outcome: dict, duration_ms: float, backend: str) -> Any:
    """Translate an AgentGraph outcome dict → TurnResult.

    Lazy-imported TurnResult so this module can be tested without
    pulling in main.py (which imports langgraph via agent_graph.py).
    """
    from .main import TurnResult

    kind = outcome.get("outcome", "error")
    if kind == "executed":
        return TurnResult(
            success=True,
            output="",  # populated when Task #151 wires token streaming
            outcome=Outcome.EXECUTED,
            tier=int(outcome.get("tier", 0) or 0),
            backend=backend,
            duration_ms=duration_ms,
        )
    if kind == "denied":
        return TurnResult(
            success=False,
            output="Operation denied at HITL.",
            outcome=Outcome.HITL_DENIED,
            tier=int(outcome.get("tier", 0) or 0),
            backend=backend,
            duration_ms=duration_ms,
        )
    # error / paused-but-terminated / unknown → map to the closest
    # Outcome. Paused should never reach here (the caller loops), so
    # we bucket it as an internal error if we ever see it terminal.
    error_kind = outcome.get("error_kind") or ""
    reason = outcome.get("error_reason") or f"agent_graph terminal state: {kind}"
    outcome_enum = {
        "planner":  Outcome.BRAIN_ERROR,
        "pb":       Outcome.PB_SCHEMA_ERROR,
        "verifier": Outcome.QB_VERIFIER_REJECTED,
        "mcpd":     Outcome.TOOL_ERROR,
        "internal": Outcome.BRAIN_ERROR,
    }.get(error_kind, Outcome.BRAIN_ERROR)
    return TurnResult(
        success=False,
        output=reason,
        outcome=outcome_enum,
        tier=int(outcome.get("tier", 0) or 0),
        backend=backend,
        duration_ms=duration_ms,
    )


# ── Interrupt payload → HitlDisplayData ─────────────────────────────


def translate_interrupt_payload_to_display_data(
    intent: dict,
    outcome: dict,
    backend: str,
) -> HitlDisplayData:
    """Convert an AgentGraph interrupt payload + intent into the
    HitlDisplayData shape the existing HitlPresenter expects.

    Kept as a public function so tests can exercise it in isolation.
    """
    action = str(intent.get("action", "") or "")
    target = str(intent.get("target", "") or "")
    reason = str(intent.get("reason", "") or "")
    risk_level = str(intent.get("risk_level", "") or "")
    tier_int = int(outcome.get("tier", 2) or 2)
    try:
        tier = Tier(tier_int)
    except ValueError:
        tier = Tier.MEDIUM

    return HitlDisplayData(
        action=action,
        target=target,
        tier=tier,
        risk_level=risk_level,
        reversible=False,   # populated when Task #148/149 lands richer intent metadata
        backend=backend,
        reason=reason,
        blocked_pattern=None,
        cow_summary=None,   # Task #148+ wires COW dry-run diff
    )


# ── Event emitters ──────────────────────────────────────────────────


def _emit_progress_and_cot(node_name: str, t0: float, state: str = "done") -> list[Any]:
    """Return (ProgressEvent, CotEvent) pair for a single node.

    Both are frozen dataclasses — safe to yield across thread boundaries
    the daemon spans.
    """
    from .turn_events import CotEvent, ProgressEvent

    idx = _NODE_INDEX.get(node_name, 0)
    label = dict(_NODE_LABELS).get(node_name, node_name)
    now = (time.monotonic() - t0) * 1000
    return [
        ProgressEvent(
            step_name=node_name,
            step_label=label,
            step_index=idx,
            total_steps=_TOTAL_STEPS,
            elapsed_ms=now,
        ),
        CotEvent(
            step_index=idx,
            step_name=node_name,
            step_state=state,
            heading=label,
            body="",
            data={},
            timestamp_ms=now,
        ),
    ]


def _traversed_nodes_from_outcome(outcome: dict) -> list[str]:
    """Infer which nodes the graph traversed by looking at the outcome.

    Task #146 doesn't emit per-node events (Task #151 will via
    astream_events). For Task #147 we approximate: every terminal
    outcome traversed planner + risk_classifier at minimum. From there
    we branch by tier/decision/error to reconstruct which downstream
    nodes ran.
    """
    if outcome.get("error_kind") == "planner":
        return ["planner"]

    seen = ["planner", "risk_classifier"]

    if outcome.get("error_kind") == "internal" and not outcome.get("intent_id"):
        return seen

    tier = int(outcome.get("tier", -1) or -1)
    if tier >= 2:
        seen.append("verifier")
        seen.append("hitl_gate")
        if outcome.get("outcome") == "denied":
            return seen

    seen.append("executor")
    if outcome.get("error_kind") == "pb":
        return seen
    seen.append("mcpd_dispatcher")
    if outcome.get("error_kind") == "mcpd":
        return seen
    seen.append("responder")
    return seen


def translate_outcome_to_events(
    outcome: dict,
    t0: float,
    backend: str,
) -> list[Any]:
    """Turn a terminal AgentGraph outcome into the TurnEvent list to
    yield. Emits ProgressEvent+CotEvent per node traversed + one
    terminal ResultEvent, plus an ErrorEvent when error_kind is set.
    """
    from .turn_events import ErrorEvent, ResultEvent

    events: list[Any] = []
    for node in _traversed_nodes_from_outcome(outcome):
        state = "failed" if _node_owned_error(outcome, node) else "done"
        events.extend(_emit_progress_and_cot(node, t0, state=state))

    duration_ms = (time.monotonic() - t0) * 1000

    if outcome.get("error_kind"):
        events.append(ErrorEvent(
            error_type=str(outcome["error_kind"]),
            message=str(outcome.get("error_reason") or ""),
            cancelled_at_step="",
        ))

    result = _outcome_to_result(outcome, duration_ms, backend)
    events.append(ResultEvent(result=result))
    return events


def _node_owned_error(outcome: dict, node: str) -> bool:
    """Which node owned the error, so its CoT event renders 'failed'
    instead of 'done'. Best-effort mapping from error_kind → node."""
    kind = outcome.get("error_kind")
    return bool(kind and _NODE_INDEX.get(node) == _NODE_INDEX.get({
        "planner": "planner",
        "pb":      "executor",
        "verifier": "verifier",
        "mcpd":    "mcpd_dispatcher",
        "internal": "planner",
    }.get(kind or "", "")))


# ── The bridge loop ─────────────────────────────────────────────────


def run_via_agent_graph(
    agent_graph: Any,
    presenter: Any,
    user_input: str,
    session: Any,
    *,
    backend: str = "",
    hitl_timeout_seconds: int = 30,
) -> Iterator[Any]:
    """Drive the graph + HITL bridge and yield TurnEvents.

    Called from Controller.run_turn_streaming when the flag is on.
    Presenter is the same HitlPresenter the monolithic pipeline uses
    (ForwardingPresenter for daemon mode; TerminalPresenter for direct
    stdin). Reusing it means the client sees the same hitl.prompt /
    hitl.respond RPC pair regardless of which mode the daemon is in.
    """
    t0 = time.monotonic()
    session_id = getattr(session, "session_id", "unknown-session")

    outcome_iter = agent_graph.run(user_input, session_id)
    outcome = _first(outcome_iter)

    while outcome and outcome.get("outcome") == "paused":
        intent_id = outcome.get("intent_id", "")
        intent = _lookup_intent(agent_graph, intent_id)
        if intent is None:
            # Should not happen — graph paused with no intent stored.
            # Treat as internal error rather than infinite-loop.
            outcome = {
                **outcome,
                "outcome": "error",
                "error_kind": "internal",
                "error_reason": "hitl paused but intent lookup failed",
            }
            break

        display = translate_interrupt_payload_to_display_data(intent, outcome, backend)
        try:
            presenter.show_prompt(display)
            decision = presenter.read_decision(timeout_seconds=hitl_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 — presenter failure = deny
            outcome = {
                **outcome,
                "outcome": "error",
                "error_kind": "internal",
                "error_reason": f"presenter failed: {type(exc).__name__}",
            }
            break

        graph_decision = "approve" if decision == Decision.APPROVED else "deny"
        outcome = _first(agent_graph.resume(session_id, graph_decision))

    if outcome is None:
        # Terminal without any yield — shouldn't happen but bucket safely.
        outcome = {"outcome": "error", "error_kind": "internal",
                   "error_reason": "agent_graph yielded no outcome",
                   "tier": -1, "session_id": session_id, "intent_id": ""}

    for event in translate_outcome_to_events(outcome, t0, backend):
        yield event


def _first(it: Any) -> Optional[dict]:
    """Consume the first yield from a generator; return None if empty.

    AgentGraph.run/resume are generators that yield exactly one outcome
    dict. Using next(iter(...)) with a fallback keeps the semantics
    explicit and tolerates future changes.
    """
    for x in it:
        return x
    return None


def _lookup_intent(agent_graph: Any, intent_id: str) -> Optional[dict]:
    """Retrieve the paused intent from the graph's per-turn content dict.

    Access via a public attribute rather than a method keeps this small;
    if we grow more callers, promote to AgentGraph.get_intent(intent_id).
    """
    if not intent_id:
        return None
    turn_content = getattr(agent_graph, "_turn_content", {}) or {}
    intent = turn_content.get(intent_id)
    return intent if isinstance(intent, dict) else None
