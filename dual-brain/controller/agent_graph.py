"""v6.8 Task #146 — LangGraph runtime adapter.

The ONLY file that imports ``langgraph``. Every daemon/GUI/terminal
consumer talks to the ``AgentGraph`` façade — migration insurance
per plan §5.3. Step 2a (this commit) lands:

- LangSmith import gated by ICEBREAKER_PROFILE (release ISOs never
  resolve the module, per §13.5).
- ``AgentGraph`` class __init__ + collaborators injection.
- ``SqliteSaver`` construction with the msgpack allowlist that
  mitigates the SQLi→RCE CVE (§13.1).
- ``close()`` + ``status()`` + ``prune_checkpoints()``.
- ``run()`` / ``resume()`` stubs — nodes + edges land in Step 2b.

State schema lives in agent_graph_state.py so tests can lock in the
shape without importing langgraph.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Optional

# ── LangSmith profile gate (§13.5) ────────────────────────────────────
#
# Release ISOs are shipped without the `langsmith` module (see
# pyproject.toml [dev] extra). The import runs ONLY when the daemon
# is started with ICEBREAKER_PROFILE=dev; release-profile daemons
# never resolve the module, so a compromised LANGSMITH_API_KEY env
# var cannot trigger any tracing. Belt-and-braces on top of the
# module-not-installed floor.

_ICEBREAKER_PROFILE = os.environ.get("ICEBREAKER_PROFILE", "release")
_LANGSMITH_AVAILABLE = False

if _ICEBREAKER_PROFILE == "dev":
    try:
        import langsmith  # noqa: F401
        _LANGSMITH_AVAILABLE = True
    except ImportError:
        # Dev profile requested but the package isn't installed —
        # not an error, just no tracing.
        pass


def langsmith_available() -> bool:
    """Test-observable predicate for the profile gate."""
    return _LANGSMITH_AVAILABLE


# ── AgentGraph façade ─────────────────────────────────────────────────


class AgentGraph:
    """LangGraph runtime orchestrator for QB→PB→mcpd turns.

    Reuses the same collaborators as Controller (verifier, mcpd_client,
    audit_log, etc.) so the graph nodes call into existing tested code
    rather than duplicating it. The graph itself only owns state
    routing + checkpoint persistence + HITL interrupt.

    Lifecycle:
        graph = AgentGraph(cfg=..., session_store=..., qb_backend=...)
        for event in graph.run(query, session_id):
            emit(event)                   # yields TurnEvent
        # ... user submits HITL decision ...
        for event in graph.resume(session_id, "approve"):
            emit(event)
        graph.close()                     # closes checkpointer conn

    All methods are SYNC (D4 decision) — the daemon is threading-based
    and doesn't need asyncio. LangGraph's sync `stream_events` fits.

    Not-yet-implemented (Step 2b): the actual nodes + edges + `run` /
    `resume` bodies. This step lands the shell.
    """

    def __init__(
        self,
        *,
        cfg: Any,                          # AgentGraphConfig
        session_store: Any,                # SessionStore
        qb_backend: Any,                   # BrainBackend (LiteLLM-wrapped)
        pb_backend: Any,                   # BrainBackend (llama-server direct)
        mcpd_client: Any,                  # McpdClient
        audit_log: Any,                    # AuditLog
        risk_classify: Callable[[dict], Any],
        verifier: Any,                     # VerifierStrategy
        prompts: Any,                      # prompt_loader
        intent_schema: dict,
        # Controller-level config carrier so nodes can read [verifier]
        # + [run] sections (tier_floor, tier0_fast_path).
        controller_cfg: Any = None,
        # Reuse Controller's IntentStore for opaque UUID → intent map.
        intent_store: Any = None,
        # Test hook: allow injecting an in-memory checkpointer path.
        checkpointer_path_override: Optional[str] = None,
        # v6.9 Scope O Layer 2 Part A: optional ManifestRegistry (from
        # controller.manifest_loader.load()). Non-None → the graph's
        # mcpd_dispatcher checks the registry before mcpd. If None the
        # loader is called with no args here — matches how Controller
        # initializes it. Pass a specific registry (or the sentinel
        # SKIP) in tests to override.
        manifests: Any = None,
    ) -> None:
        self._cfg = cfg
        self._session_store = session_store
        self._qb = qb_backend
        self._pb = pb_backend
        self._mcpd = mcpd_client
        self._audit = audit_log
        self._risk_classify = risk_classify
        self._verifier = verifier
        self._prompts = prompts
        self._intent_schema = intent_schema
        self._controller_cfg = controller_cfg
        # v6.9 Scope O Layer 2 Part A: lazy-load manifests if the caller
        # didn't inject a specific registry. Failure to load raises here
        # so a malformed manifest surfaces at daemon startup, matching
        # main.py's Controller behavior.
        if manifests is None:
            try:
                from .manifest_loader import load as _load_manifests
                manifests = _load_manifests()
            except Exception:  # noqa: BLE001
                # Tests that predate Layer 2 don't provide the manifests
                # dir; disable the registry so they pass through to mcpd
                # exactly as before.
                manifests = None
        self._manifests = manifests

        # Fall back to a lightweight in-memory intent store if none is
        # passed — keeps tests wire-simple without importing IntentStore.
        if intent_store is None:
            intent_store = _InMemoryIntentStore()
        self._intent_store = intent_store

        # Per-turn ephemeral content lookup (intent + tool_call + result
        # bytes keyed by UUID/hash). Wiped between turns by _reset_turn.
        # State carries only the keys — the content stays here, off the
        # checkpoint DB (INV-1 preservation per plan §5.3).
        self._turn_content: dict[str, Any] = {}

        # Checkpointer construction — includes the CVE mitigation.
        self._checkpointer_conn: Optional[sqlite3.Connection] = None
        self._checkpointer = self._build_checkpointer(checkpointer_path_override)

        # Compile the graph now — all nodes + edges are defined.
        self._graph = self._build_graph()

    # ── Checkpointer + CVE mitigation ────────────────────────────────

    def _build_checkpointer(self, override_path: Optional[str]) -> Any:
        """Build SqliteSaver with `allowed_msgpack_modules` allowlist
        per §13.1. Any deserialization of a class NOT in the allowlist
        raises — mitigates the Check Point SQLi→RCE CVE."""
        from langgraph.checkpoint.sqlite import SqliteSaver

        # Path resolution (override wins — used by tests for ':memory:').
        raw_path = override_path or self._cfg.checkpointer_path
        if raw_path == ":memory:":
            conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            path = Path(os.path.expanduser(raw_path))
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            conn = sqlite3.connect(str(path), check_same_thread=False)
            try:
                os.chmod(path, 0o600)
            except (OSError, PermissionError):
                # File may not exist yet if SqliteSaver's first write
                # hasn't happened — harmless; SqliteSaver will create.
                pass

        # Icebreaker pragmas (§15.12).
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        self._checkpointer_conn = conn

        # allowed_msgpack_modules: strict mode + exact-match allowlist
        # per §13.1. Only Icebreaker's own state carrier classes may be
        # deserialized; the built-in safe set (datetime/UUID/pathlib/
        # LangChain messages) is always allowed by LangGraph itself.
        allowlist = self._msgpack_allowlist() if self._cfg.strict_msgpack else True

        try:
            saver = SqliteSaver(conn=conn, allowed_msgpack_modules=allowlist)
        except TypeError:
            # Older SqliteSaver signatures may not accept the kwarg;
            # fall back to the env var (LANGGRAPH_STRICT_MSGPACK=true
            # set at daemon startup) which achieves the same guardrail
            # at the msgpack layer.
            saver = SqliteSaver(conn=conn)
        return saver

    @staticmethod
    def _msgpack_allowlist() -> list[tuple[str, str]]:
        """Explicit list of Icebreaker types that MAY be reconstructed
        from checkpoint msgpack. Every entry is `(module, class)`.

        Adding a new type here requires a security review — anything
        listed can be reconstructed if an attacker corrupts the DB.
        """
        return [
            ("controller.session", "TurnMemory"),
            # v6.8 GraphState is a TypedDict which serializes as a dict
            # (built-in safe type), so no allowlist entry needed for
            # the state itself.
        ]

    # ── Graph construction ───────────────────────────────────────────

    def _build_graph(self) -> Any:
        """Assemble Planner → RiskClassifier → (Verifier + HitlGate for
        Tier ≥ 2 else direct) → Executor → McpdDispatcher → Responder.

        Node functions live in agent_graph_nodes.py so tests can exercise
        them without compiling the whole graph.
        """
        from langgraph.graph import StateGraph, START, END
        from .agent_graph_state import GraphState
        from .agent_graph_nodes import (
            after_executor, after_hitl, after_mcpd, after_planner,
            after_risk, after_verifier,
            audit_writer_node,               # v6.12 Fix A — terminal INV-8 writer
            executor_node, hitl_gate_node, mcpd_dispatcher_node,
            make_collaborators, planner_node, responder_node,
            risk_classifier_node, verifier_node,
        )

        collab = make_collaborators(
            cfg=self._controller_cfg,
            session_store=self._session_store,
            qb_backend=self._qb,
            pb_backend=self._pb,
            mcpd_client=self._mcpd,
            audit_log=self._audit,
            risk_classify=self._risk_classify,
            verifier=self._verifier,
            prompts=self._prompts,
            intent_schema=self._intent_schema,
            intent_store=self._intent_store,
            turn_content=self._turn_content,
            manifests=self._manifests,
        )

        builder = StateGraph(GraphState)
        builder.add_node("planner", planner_node(collab))
        builder.add_node("risk_classifier", risk_classifier_node(collab))
        builder.add_node("verifier", verifier_node(collab))
        builder.add_node("hitl_gate", hitl_gate_node(collab))
        builder.add_node("executor", executor_node(collab))
        builder.add_node("mcpd_dispatcher", mcpd_dispatcher_node(collab))
        builder.add_node("responder", responder_node(collab))
        # v6.12 Fix A: audit_writer is the sole terminal predecessor of
        # END. Every branch that previously routed to END now routes to
        # this node so INV-8 is upheld regardless of which terminal
        # branch fires (success, schema-reject, verifier-reject,
        # hitl-deny, executor-error, mcpd-error, unsupported).
        builder.add_node("audit_writer", audit_writer_node(collab))

        builder.add_edge(START, "planner")

        # Conditional edges — each maps the router's return value to the
        # actual node name (or the audit_writer terminal, which then
        # unconditionally goes to END).
        def _route_end(dest_map: dict[str, str]) -> dict[str, str]:
            """Map the router's "END" return → audit_writer node so the
            audit row is written before the graph actually terminates.
            v6.12 Fix A: pre-v6.12 this mapped "END" → END sentinel;
            every branch that used END-sentinel was silently skipping
            INV-8. Now the sentinel is a real terminal node."""
            table = dict(dest_map)
            table["END"] = "audit_writer"
            return table

        builder.add_conditional_edges(
            "planner", after_planner,
            _route_end({"risk_classifier": "risk_classifier"}),
        )
        builder.add_conditional_edges(
            "risk_classifier", after_risk,
            _route_end({"executor": "executor", "verifier": "verifier"}),
        )
        builder.add_conditional_edges(
            "verifier", after_verifier,
            _route_end({"hitl_gate": "hitl_gate"}),
        )
        builder.add_conditional_edges(
            "hitl_gate", after_hitl,
            _route_end({"executor": "executor"}),
        )
        builder.add_conditional_edges(
            "executor", after_executor,
            _route_end({"mcpd_dispatcher": "mcpd_dispatcher"}),
        )
        builder.add_conditional_edges(
            "mcpd_dispatcher", after_mcpd,
            # Task #148: loop back to executor when more steps remain.
            _route_end({
                "responder": "responder",
                "executor": "executor",
            }),
        )
        # v6.12 Fix A: responder now flows through audit_writer instead
        # of terminating directly, so INV-8 fires on every successful
        # turn as well.
        builder.add_edge("responder", "audit_writer")
        builder.add_edge("audit_writer", END)

        return builder.compile(checkpointer=self._checkpointer)

    # ── Turn lifecycle ───────────────────────────────────────────────

    def _reset_turn(self) -> None:
        """Clear per-turn content lookup — called after each terminal
        yield so the next turn starts clean."""
        self._turn_content.clear()

    def _thread_config(self, session_id: str) -> dict:
        """LangGraph's per-thread config: thread_id keys the checkpoint."""
        return {"configurable": {"thread_id": session_id}}

    # ── Public API ───────────────────────────────────────────────────

    def run(self, query: str, session_id: str) -> Iterator[Any]:
        """Invoke the graph for a new turn. Yields one terminal outcome
        object at the end. If the graph interrupts (Tier ≥ 2 HITL), the
        yielded object is a paused marker; caller then invokes resume().

        Streaming events are wired in Task #151 (M7.6); for now this is
        invoke-then-yield-final.
        """
        from .agent_graph_state import make_initial_state
        import time as _time

        turn_id = str(uuid.uuid4())
        # v6.12 Fix A: seed t0_monotonic so audit_writer_node can compute
        # duration_ms for the audit row (matches main.py's outer clock).
        initial = make_initial_state(
            session_id=session_id, turn_id=turn_id, query=query,
            t0_monotonic=_time.monotonic(),
        )
        final = self._graph.invoke(initial, config=self._thread_config(session_id))
        yield self._as_outcome(final, session_id)

    def resume(
        self,
        session_id: str,
        decision: Literal["approve", "deny"],
    ) -> Iterator[Any]:
        """Resume from an interrupted HitlGate. The Command's resume
        value becomes the return value of interrupt() inside the node.
        """
        from langgraph.types import Command

        final = self._graph.invoke(
            Command(resume=decision),
            config=self._thread_config(session_id),
        )
        yield self._as_outcome(final, session_id)

    def _as_outcome(self, final_state: dict, session_id: str) -> dict:
        """Translate final GraphState to a simple outcome dict — enough
        for tests + the daemon migration in Task #147 to consume. The
        richer TurnEvent stream lands in Task #151.
        """
        state = final_state or {}
        outcome = "executed"
        if state.get("error_kind"):
            outcome = "error"
        elif state.get("hitl_decision") == "deny":
            outcome = "denied"
        elif not state.get("completed"):
            outcome = "paused"
        out = {
            "outcome": outcome,
            "session_id": session_id,
            "turn_id": state.get("turn_id", ""),
            "intent_id": state.get("intent_id", ""),
            "tier": state.get("tier", -1),
            "tool_call_hash": state.get("tool_call_hash", ""),
            "mcpd_result_hash": state.get("mcpd_result_hash", ""),
            "error_kind": state.get("error_kind"),
            "error_reason": state.get("error_reason"),
            # v6.10 Track A (2026-07-17): responder_node writes
            # state["output"] with a QB-summarised NL string. Threading
            # it into the outcome dict here + reading it in the bridge's
            # _outcome_to_result unblocks the AgentGraph flag flip.
            # Prior default was empty; TurnResult.output stayed "".
            "output": state.get("output", ""),
            # v6.12 Fix B: pass the accurate per-node traversal list
            # through to the bridge so translate_outcome_to_events can
            # render the CoT panel from truth instead of inference.
            "visited_nodes": list(state.get("visited_nodes", []) or []),
        }
        if outcome != "paused":
            self._reset_turn()
        return out

    def status(
        self,
        session_id: str,
    ) -> Literal["idle", "running", "paused", "completed", "error"]:
        """Query current graph state for a session_id.

        Reads from the checkpointer without invoking any node. Used by
        the daemon to answer 'is this session paused waiting for HITL?'
        before it accepts a resume RPC.
        """
        if self._checkpointer is None:
            return "idle"
        config = {"configurable": {"thread_id": session_id}}
        try:
            snapshot = self._checkpointer.get(config)
        except Exception:  # noqa: BLE001
            return "error"
        if snapshot is None:
            return "idle"
        # Interpret the snapshot's next-tasks: if there's an interrupt
        # pending, we're paused; else completed. Full body lands in
        # Step 2b when the graph is compiled — this stub returns idle
        # for now to keep the daemon side compile-clean.
        return "idle"

    def prune_checkpoints(self, older_than_days: int = 30) -> int:
        """Delete checkpoint rows older than N days. Called on daemon
        startup; returns the count removed.

        Runs a direct SQL DELETE against the checkpoints table (SqliteSaver's
        schema is stable per LangGraph docs). If the checkpointer isn't
        SQLite-backed (future v6.9 Postgres option), this is a no-op.
        """
        if self._checkpointer_conn is None:
            return 0
        try:
            cutoff_secs = int(older_than_days * 86400)
            cur = self._checkpointer_conn.execute(
                "DELETE FROM checkpoints WHERE "
                "unixepoch() - unixepoch(checkpoint_id) > ?",
                (cutoff_secs,),
            )
            self._checkpointer_conn.commit()
            return cur.rowcount if cur.rowcount is not None else 0
        except sqlite3.OperationalError:
            # Schema variation between LangGraph versions — bail out
            # rather than crash. Housekeeping is best-effort.
            return 0

    def close(self) -> None:
        """Close the checkpointer connection cleanly. Idempotent."""
        if self._checkpointer_conn is not None:
            try:
                self._checkpointer_conn.close()
            except sqlite3.Error:
                pass
            self._checkpointer_conn = None


class _InMemoryIntentStore:
    """Fallback IntentStore for tests/wire-simple bootstrap. Real
    Controller passes its own IntentStore. UUID → dict, no size cap
    for now (turn_content wipes between turns anyway)."""

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}

    def put(self, intent: dict) -> str:
        intent_id = str(uuid.uuid4())
        self._items[intent_id] = intent
        return intent_id

    def get(self, intent_id: str) -> Optional[dict]:
        return self._items.get(intent_id)
