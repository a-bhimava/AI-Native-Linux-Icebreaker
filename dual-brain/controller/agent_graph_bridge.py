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
from .cow_summary import format_diff as _cow_format_diff
from .hitl import Decision, HitlDisplayData
from .risk_classifier import Tier


# v6.12 Fix F (F-86 2026-07-21): error_kinds that are semantically NOT
# errors — they're controlled short-circuits with valid user-facing text.
# For these, the bridge streams the friendly output as if success and
# skips the ErrorEvent that would drive daemon.py::make_error → ib_run.py
# '[error]' rendering. Hard errors (planner/pb/mcpd/verifier/internal)
# still get ErrorEvent because the LEFT pane genuinely can't render what
# went wrong without operator intervention.
#   - "unsupported": F-35 catalogue landing pad. state.output has the
#     friendly "This kind of request isn't supported yet" card.
#   - "schema":      F-32 ambiguous-target reject. state.output populated
#     by planner_node in v6.12 Fix F so this behaves symmetric to F-35.
_SOFT_ERROR_KINDS: frozenset[str] = frozenset({"unsupported", "schema"})


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
        # v6.10 Track A (2026-07-17): read the responder-populated
        # output. Previously hardcoded "" — the AgentGraph flag flip
        # blocker (Bug C symptom). Full token streaming remains
        # Task #151's scope (astream_events), but the terminal
        # summarizer now produces a complete NL string.
        return TurnResult(
            success=True,
            output=str(outcome.get("output", "") or ""),
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
    error_kind = outcome.get("error_kind") or ""
    # v6.12 Fix F (F-86): soft outcomes get a success=True TurnResult
    # with their friendly text, but track the specific Outcome enum so
    # audit consumers can still filter by outcome=unsupported /
    # outcome=schema_rejected. From the shell trigger's perspective the
    # user asked a coherent question and got a coherent answer — no red
    # [error] rendering.
    if error_kind in _SOFT_ERROR_KINDS:
        soft_enum = {
            "unsupported": Outcome.UNSUPPORTED,
            "schema":      Outcome.SCHEMA_REJECTED,
        }[error_kind]
        return TurnResult(
            success=True,
            output=str(
                outcome.get("output", "")
                or outcome.get("error_reason", "")
                or ""
            ),
            outcome=soft_enum,
            tier=int(outcome.get("tier", 0) or 0),
            backend=backend,
            duration_ms=duration_ms,
        )
    # error / paused-but-terminated / unknown → map to the closest
    # Outcome. Paused should never reach here (the caller loops), so
    # we bucket it as an internal error if we ever see it terminal.
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

    # M7.0.1f (v6.16): if cow_preview_node stashed a diff, format it
    # into cow_summary so the modal renders "3.2 GB will be freed. 847
    # files will be deleted." at the initial HITL ask (whitepaper §8.2).
    cow_diff = outcome.get("cow_diff_json")
    cow_summary_text: Optional[str] = None
    reversible_flag = False
    if isinstance(cow_diff, dict):
        cow_summary_text = _cow_format_diff(cow_diff)
        # The diff's reversibility flag is a better signal than the
        # historical hardcoded False.
        reversible_flag = bool(cow_diff.get("reversible", False))

    return HitlDisplayData(
        action=action,
        target=target,
        tier=tier,
        risk_level=risk_level,
        reversible=reversible_flag,
        backend=backend,
        reason=reason,
        blocked_pattern=None,
        cow_summary=cow_summary_text,
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


def _visited_from_state(outcome: dict) -> list[tuple[str, str]]:
    """v6.12 Fix B — return the (node_name, state) list the graph accrued.

    Each node calls ``_mark(name, state, update)`` in agent_graph_nodes.py
    which contributes ``visited_nodes=[(name, state)]`` to its return
    dict. LangGraph's ``Annotated[list, add]`` reducer appends across the
    traversal so ``outcome["visited_nodes"]`` at the terminal state is
    the accurate ordered list of nodes that ran, with each node's real
    state (done or failed).

    Falls back to the pre-v6.12 outcome-inference approach if the state
    lacks visited_nodes (e.g. old test fixtures that construct outcomes
    by hand instead of running the graph). The fallback emits a warning
    log so we notice any coverage gap. On the shipped path — always via
    ``AgentGraph.run()`` — visited_nodes is always populated because the
    factory ``make_initial_state`` seeds ``visited_nodes=[]`` and every
    node appends.
    """
    visited = outcome.get("visited_nodes")
    if isinstance(visited, list) and visited:
        return [
            (str(item[0]), str(item[1]))
            for item in visited
            if isinstance(item, (tuple, list)) and len(item) >= 2
        ]

    # Fallback for pre-v6.12 outcome shapes (mostly hand-built test
    # fixtures). Kept minimal so real coverage gaps surface via the log.
    try:
        from .logger import get_logger
        get_logger("controller.agent_graph_bridge").warning(
            "visited_nodes missing from outcome — falling back to "
            "pre-v6.12 inference (accuracy degraded)"
        )
    except Exception:  # noqa: BLE001 — logging must never break emission
        pass
    return _legacy_traversal_inference(outcome)


def _legacy_traversal_inference(outcome: dict) -> list[tuple[str, str]]:
    """Pre-v6.12 approximation kept for outcome dicts missing
    visited_nodes. Returns each node paired with a best-effort
    (done|failed) state. Do NOT extend this — new coverage comes from
    the explicit visited_nodes path, not from more inference cases."""
    kind = str(outcome.get("error_kind") or "")
    owner = {
        "planner": "planner",
        "pb":      "executor",
        "verifier": "verifier",
        "mcpd":    "mcpd_dispatcher",
        "internal": "planner",
    }.get(kind, "")

    def _pair(name: str) -> tuple[str, str]:
        return (name, "failed" if name == owner else "done")

    if kind == "planner":
        return [_pair("planner")]

    seen = [_pair("planner"), _pair("risk_classifier")]

    if kind == "internal" and not outcome.get("intent_id"):
        return seen

    tier = int(outcome.get("tier", -1) or -1)
    if tier >= 2:
        seen.append(_pair("verifier"))
        seen.append(_pair("hitl_gate"))
        if outcome.get("outcome") == "denied":
            return seen

    seen.append(_pair("executor"))
    if kind == "pb":
        return seen
    seen.append(_pair("mcpd_dispatcher"))
    if kind == "mcpd":
        return seen
    seen.append(_pair("responder"))
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
    from .turn_events import ErrorEvent, ResultEvent, TokenEvent

    events: list[Any] = []
    # v6.12 Fix B: consume the explicit visited_nodes list mutated by
    # each node instead of inferring from outcome fields. Inference could
    # not represent short-circuits — schema-reject, system.unsupported,
    # hitl-deny all rendered downstream nodes as "done" green even though
    # they never ran. Now the CoT panel mirrors the actual traversal.
    for node_name, node_state in _visited_from_state(outcome):
        events.extend(_emit_progress_and_cot(node_name, t0, state=node_state))

    duration_ms = (time.monotonic() - t0) * 1000

    # v6.12 Fix F (F-86): soft outcomes carry valid user-facing text in
    # state["output"] — don't emit ErrorEvent for them or the shell trigger
    # renders their friendly card as red [error] via daemon.py::make_error.
    error_kind = outcome.get("error_kind") or ""
    if error_kind and error_kind not in _SOFT_ERROR_KINDS:
        events.append(ErrorEvent(
            error_type=str(error_kind),
            message=str(outcome.get("error_reason") or ""),
            cancelled_at_step="",
        ))

    # v6.11 Fix #3 (F-74): emit a final TokenEvent so the terminal's
    # LEFT-pane RichLog renders the responder's output through the
    # existing streaming render path (terminal/app.py::_on_token_event).
    # The monolithic pipeline emits one TokenEvent per token during
    # qb_summarize; Task #173 flipped agent_graph.enabled=True without
    # a corresponding TokenEvent emission, and the terminal LEFT pane
    # stayed empty on every successful turn even though ResultEvent
    # carried the output correctly. Only emit when we have a non-empty
    # output AND there was no error (error path renders via ErrorEvent).
    outcome_output = str(outcome.get("output", "") or "")
    # v6.12 Fix F: soft outcomes (unsupported, schema) DO stream their
    # friendly text through TokenEvent, same as success turns. Hard error
    # kinds skip TokenEvent (their message is already in ErrorEvent
    # emitted above).
    _hard_error = error_kind and error_kind not in _SOFT_ERROR_KINDS
    if outcome_output and not _hard_error:
        events.append(TokenEvent(
            token=outcome_output,
            accumulated=outcome_output,
            final=True,
        ))

    result = _outcome_to_result(outcome, duration_ms, backend)
    events.append(ResultEvent(result=result))
    return events


# _node_owned_error removed in v6.12 — nodes now self-report their state
# via the visited_nodes list; the bridge no longer infers ownership. Any
# code still referencing this helper is broken and should read
# outcome["visited_nodes"] instead.


def _traversed_nodes_from_outcome(outcome: dict) -> list[str]:
    """v6.12 backward-compat shim for existing tests.

    Returns just the node names (dropping the state) from the current
    _visited_from_state path. New code should use _visited_from_state
    directly to get the (name, state) tuples. This shim exists so
    test_agent_graph_bridge.py's pre-v6.12 assertions keep working
    without needing a mass rewrite; it will be removed once those tests
    migrate to visited_nodes-shape assertions.
    """
    return [name for (name, _) in _visited_from_state(outcome)]


# ── The bridge loop ─────────────────────────────────────────────────


def run_via_agent_graph(
    agent_graph: Any,
    presenter: Any,
    user_input: str,
    session: Any,
    *,
    backend: str = "",
    # v6.12 hotfix (2026-07-23 F-95_OC): default was 30. When
    # main.run_turn_streaming forgot to pass this kwarg, agent_graph mode
    # silently auto-denied every Tier ≥ 2 approval at 30s regardless of
    # the toml [hitl] timeout_seconds. Even with the caller fixed, keep
    # the default at 300 (matches HitlPrompt.TIMEOUT_SECONDS + sudo's
    # timestamp_timeout) so future callers can't accidentally reintroduce
    # a too-short human decision window.
    hitl_timeout_seconds: int = 300,
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

    # v6.9 Bug G (2026-07-17): AgentGraph holds its own SessionStore
    # (main.py:422 creates a fresh one) but daemon sessions live in a
    # separate registry. Executor node looks up session by session_id
    # and gets None → 'session lookup miss' for any tool that needs
    # PB (fs.write, package.install, etc.). Register the current
    # session before invocation. Idempotent + thread-safe: SessionStore
    # holds its own RLock; register() overwrites any stale mirror.
    try:
        agent_graph._session_store.register(session)
    except (AttributeError, TypeError):
        pass

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
