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
        # Test hook: allow injecting an in-memory checkpointer path.
        checkpointer_path_override: Optional[str] = None,
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

        # Checkpointer construction — includes the CVE mitigation.
        self._checkpointer_conn: Optional[sqlite3.Connection] = None
        self._checkpointer = self._build_checkpointer(checkpointer_path_override)

        # Graph compile happens in Step 2b once nodes + edges are defined.
        self._graph = None

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

    # ── Public API (bodies land in Step 2b) ──────────────────────────

    def run(self, query: str, session_id: str) -> Iterator[Any]:
        """Sync generator of TurnEvents for a fresh turn.

        Body implemented in Step 2b — Task #146 substantive commit.
        """
        raise NotImplementedError("AgentGraph.run — pending Step 2b")

    def resume(
        self,
        session_id: str,
        decision: Literal["approve", "deny"],
    ) -> Iterator[Any]:
        """Resume an interrupted turn with the HITL decision.

        Body implemented in Step 2b.
        """
        raise NotImplementedError("AgentGraph.resume — pending Step 2b")

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
