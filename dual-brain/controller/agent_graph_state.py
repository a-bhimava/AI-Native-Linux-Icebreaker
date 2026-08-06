"""v6.8 Task #146 — AgentGraph state schema.

Split out from agent_graph.py so tests can import the state shape
without pulling in langgraph. The state is a TypedDict per production
best practice (lightweight, no runtime validation cost, works cleanly
with LangGraph reducers).

Design rules (from research 2026-07-13):
- TypedDict + Annotated reducers, NOT Pydantic or @dataclass.
- Never define a mutable default (`field: list = []`) — shared across
  runs, cross-thread corruption. Every accumulating field uses
  Annotated[list, add]; initialize explicitly in the invoke input.
- Store REFERENCES, not content: intent_id (opaque UUID), hashes,
  tier. Raw QB/PB output stays in SessionStore + audit log; the state
  graph carries pointers only. Keeps checkpoint DB small (kilobytes
  not megabytes) AND prevents PB output from leaking through state
  carryover into replanner turns (INV-1 preservation).
- Reducers must be pure: no API calls, no file writes, no random.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Literal, Optional, TypedDict


# ── Reducers ────────────────────────────────────────────────────────────


def last_write_wins(existing, incoming):
    """Default reducer for scalar fields — every update overwrites."""
    return incoming


# ── State schema ───────────────────────────────────────────────────────


class GraphState(TypedDict, total=False):
    """Per-turn state carried through the LangGraph nodes.

    All fields optional (total=False) because nodes emit partial updates.
    Default construction happens explicitly at run() time; NEVER declare
    a default on the TypedDict itself (shared-default corruption).

    Field sizes: max ~1 KB per checkpoint (all int/str/bool + hashes).
    """

    # ── Routing (drives conditional edges) ──────────────────────────
    session_id: Annotated[str, last_write_wins]
    turn_id: Annotated[str, last_write_wins]
    query: Annotated[str, last_write_wins]        # user's raw input
    intent_valid: Annotated[bool, last_write_wins]
    tier: Annotated[int, last_write_wins]         # 0/1/2/3
    tool_call_valid: Annotated[bool, last_write_wins]

    # ── Content references (INV-1: hashes not content) ─────────────
    intent_id: Annotated[str, last_write_wins]         # opaque UUID
    tool_call_hash: Annotated[str, last_write_wins]    # sha256
    mcpd_result_hash: Annotated[str, last_write_wins]  # sha256

    # ── v6.8 Task #148 — Plan mode ─────────────────────────────────
    # Plan lives in AgentGraph._turn_content[plan_id]; state carries
    # only the reference + progress markers.
    plan_id: Annotated[str, last_write_wins]           # UUID or ""
    step_index: Annotated[int, last_write_wins]        # 0-based, current step
    total_steps: Annotated[int, last_write_wins]       # >= 1 when plan_id set

    # ── HITL surface ────────────────────────────────────────────────
    hitl_required: Annotated[bool, last_write_wins]
    hitl_decision: Annotated[
        Optional[Literal["approve", "deny"]], last_write_wins
    ]

    # ── M7.0.1f (v6.16): COW pre-flight + gated commit ───────────────
    # cow_preview_node stashes these BEFORE hitl_gate so the modal payload
    # carries the diff (whitepaper §8.2 shape). mcpd_dispatcher checks
    # cow_pending_intent_id post-approve → calls commit_cow instead of a
    # raw mcpd.call. Persisted by SqliteSaver across the interrupt.
    cow_pending_intent_id: Annotated[Optional[str], last_write_wins]
    cow_diff_json: Annotated[Optional[dict], last_write_wins]

    # ── Error surface ───────────────────────────────────────────────
    error_kind: Annotated[
        Optional[
            Literal[
                "planner", "pb", "validation",
                "verifier", "mcpd", "internal",
            ]
        ],
        last_write_wins,
    ]
    error_reason: Annotated[Optional[str], last_write_wins]

    # ── Terminal markers ────────────────────────────────────────────
    completed: Annotated[bool, last_write_wins]

    # ── Streaming provenance (small) ────────────────────────────────
    last_event_seq: Annotated[int, last_write_wins]

    # ── v6.12 Fix B — explicit per-node traversal for accurate CoT ──
    # Each node appends (node_name, "done"|"failed") as its first line
    # (success path) or in its exception handler (failed path). The
    # bridge consumes this list in translate_outcome_to_events so the
    # CoT panel renders exactly the nodes that ran, with their real
    # state — no more inference from error_kind which cannot represent
    # schema/unsupported/hitl short-circuits accurately.
    visited_nodes: Annotated[list, add]

    # ── v6.12 Fix A — turn start clock for audit duration_ms ────────
    # Populated by the graph invoker at run() time; audit_writer_node
    # computes (time.monotonic() - t0_monotonic) * 1000 for the audit
    # record so the audit row carries the same duration_ms that used
    # to come from run_turn_streaming's outer clock in main.py.
    t0_monotonic: Annotated[float, last_write_wins]

    # ── v6.12 Fix C — responder-produced NL summary for the user ────
    # Written by responder_node from the QB summarize call, threaded to
    # ``outcome["output"]`` by ``AgentGraph._as_outcome``, and consumed
    # by (a) the bridge's F-74 TokenEvent emission for TUI/GUI streaming
    # and (b) daemon.py's run_turn RPC response for the shell trigger
    # (``ib_run.py``) which prints it directly. Prior to v6.12 this key
    # was written by responder but never declared — LangGraph silently
    # dropped it during state merging with downstream nodes (the
    # audit_writer terminal added in v6.12 Fix A made this visible;
    # before that it happened to survive only because responder was the
    # terminal node). Every "quickly through CoT to (done) with no
    # output" symptom traces to this missing declaration. Declaring it
    # here makes state.output round-trip through all downstream nodes.
    output: Annotated[str, last_write_wins]


# ── Factory: build the default initial state for a run ────────────────


def make_initial_state(
    *,
    session_id: str,
    turn_id: str,
    query: str,
    t0_monotonic: float = 0.0,
) -> GraphState:
    """Explicit factory — never rely on TypedDict defaults.

    Every field starts at a well-known 'unset' value so nodes can
    distinguish 'not yet set' from 'set to falsy'.
    """
    return GraphState(
        session_id=session_id,
        turn_id=turn_id,
        query=query,
        intent_valid=False,
        tier=-1,                    # -1 = unclassified sentinel
        tool_call_valid=False,
        intent_id="",
        tool_call_hash="",
        mcpd_result_hash="",
        # v6.8 Task #148 — Plan mode defaults (1-step plan is the norm).
        plan_id="",
        step_index=0,
        total_steps=1,
        hitl_required=False,
        hitl_decision=None,
        cow_pending_intent_id=None,
        cow_diff_json=None,
        error_kind=None,
        error_reason=None,
        completed=False,
        last_event_seq=0,
        visited_nodes=[],
        t0_monotonic=t0_monotonic,
        output="",
    )
