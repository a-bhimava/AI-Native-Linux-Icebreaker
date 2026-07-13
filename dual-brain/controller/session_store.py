"""v6.8 Task #145 — SessionStore.

Extracts session lookup out of daemon.py's connection-list into a
dedicated dict-backed store keyed by session_id. Motivations:

1. LangGraph nodes (Task #146+) call in with only a session_id —
   they need a get(session_id) capability we didn't have.
2. Session lifetime becomes explicit: create → get → remove → prune.
3. Adds a `prune_expired` step for long-running daemons (v6.7 leaks
   sessions across all-time; store prunes on startup).

Thread safety: `threading.RLock` guards every mutation. RLock (not Lock)
because `prune_expired` walks + removes in the same call.

Persistence: **in-memory only for v6.8**. Persistence to SQLite (via
LangGraph's checkpointer or a separate table) is a v6.9 concern; the
adapter interface is designed to accept a persistent backend later
without changing callers.

Backward compat (D2): SessionState.new() classmethod stays. Callers
that construct SessionState directly for tests continue to work; they
just create orphan sessions that never enter the store. Real
production code uses SessionStore.create().
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Optional

from .session import SessionState


class SessionStore:
    """Dict-backed session registry with create/get/remove/list/prune."""

    __slots__ = ("_sessions", "_lock")

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.RLock()

    # ── Mutation ──────────────────────────────────────────────────────

    def create(self, backend: str, cfg: Any) -> SessionState:
        """Create a fresh session and register it.

        session_id is generated internally (uuid4). Callers can also
        pass a pre-existing SessionState via `register` (for
        reconstruction scenarios like resume).
        """
        state = SessionState.new(backend, cfg)
        with self._lock:
            self._sessions[state.session_id] = state
        return state

    def register(self, state: SessionState) -> None:
        """Idempotent insert of an already-constructed session.

        Overwrites an existing entry with the same session_id — that's
        deliberate for reconnect semantics (a client reconnecting with
        a known session_id gets the fresh state, not a stale mirror).
        """
        with self._lock:
            self._sessions[state.session_id] = state

    def remove(self, session_id: str) -> Optional[SessionState]:
        """Remove and return the session. None if not present."""
        with self._lock:
            return self._sessions.pop(session_id, None)

    # ── Query ─────────────────────────────────────────────────────────

    def get(self, session_id: str) -> Optional[SessionState]:
        """O(1) lookup. Returns None if the session_id is not registered.

        The primary reason SessionStore exists — LangGraph nodes need
        this capability and the pre-v6.8 daemon didn't expose it.
        """
        with self._lock:
            return self._sessions.get(session_id)

    def list_active(self) -> list[SessionState]:
        """Snapshot of currently-registered sessions. Safe to iterate
        without holding the lock — the returned list is a copy."""
        with self._lock:
            return list(self._sessions.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def __contains__(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    # ── Housekeeping ──────────────────────────────────────────────────

    def prune_expired(self) -> int:
        """Remove sessions whose is_expired() returns True.

        Returns the number of sessions pruned. Called by the daemon at
        startup + on-demand (a scheduled prune via a timer thread is
        deferred to v6.9).
        """
        removed = 0
        with self._lock:
            expired_ids = [
                sid for sid, s in self._sessions.items() if s.is_expired()
            ]
            for sid in expired_ids:
                del self._sessions[sid]
                removed += 1
        return removed
