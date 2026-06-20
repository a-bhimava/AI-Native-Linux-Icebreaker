"""Session-scoped trust-grant store for the HITL gate (M5.1c).

When a user presses [T]rust during a HITL prompt, a TrustGrant is recorded.
Subsequent matching intents auto-approve without showing the prompt, subject to:

  - Hard floor: Tier >= HIGH is NEVER grantable AND NEVER trusted (R-5.1).
  - TTL: grants expire after ``trust_ttl_seconds`` (default 0 = feature disabled).
  - Session scope: grants live in process memory; REPL restart clears them.
  - Action + target-prefix match: ``fs.write`` → ``/tmp/`` trusts ``/tmp/foo``
    but not ``/etc/foo``.

Thread-safe via a coarse lock.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from .risk_classifier import Tier


@dataclass(frozen=True)
class TrustGrant:
    grant_id: str
    action: str
    target_prefix: str
    max_tier: Tier
    session_id: str
    granted_at: float
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.monotonic() > self.expires_at


class TrustStore:
    """In-memory, session-scoped trust-grant store."""

    def __init__(self) -> None:
        self._grants: dict[str, TrustGrant] = {}
        self._lock = threading.Lock()

    def grant(
        self,
        action: str,
        target_prefix: str,
        max_tier: Tier,
        session_id: str,
        ttl_seconds: float,
    ) -> TrustGrant:
        if action.startswith("rpa.") and action not in {
            "rpa.ping", "rpa.find_by_image", "rpa.list_workflows",
        }:
            raise ValueError(
                f"Cannot grant trust for RPA write operation {action!r} — "
                "RPA workflows require explicit approval each time"
            )
        if max_tier >= Tier.HIGH:
            raise ValueError(
                f"Cannot grant trust for Tier {max_tier.name} operations "
                f"(floor: Tier < HIGH)"
            )
        now = time.monotonic()
        g = TrustGrant(
            grant_id=uuid.uuid4().hex[:12],
            action=action,
            target_prefix=target_prefix,
            max_tier=max_tier,
            session_id=session_id,
            granted_at=now,
            expires_at=now + ttl_seconds,
        )
        with self._lock:
            self._grants[g.grant_id] = g
        return g

    def is_trusted(
        self,
        action: str,
        target: str,
        tier: Tier,
        session_id: str,
    ) -> Optional[TrustGrant]:
        if tier >= Tier.HIGH:
            return None
        with self._lock:
            for g in list(self._grants.values()):
                if g.expired:
                    del self._grants[g.grant_id]
                    continue
                if (
                    g.session_id == session_id
                    and g.action == action
                    and target.startswith(g.target_prefix)
                    and tier <= g.max_tier
                ):
                    return g
        return None

    def revoke(self, grant_id: str) -> bool:
        with self._lock:
            return self._grants.pop(grant_id, None) is not None

    def list_grants(self, session_id: str | None = None) -> list[TrustGrant]:
        with self._lock:
            expired = [gid for gid, g in self._grants.items() if g.expired]
            for gid in expired:
                del self._grants[gid]
            grants = list(self._grants.values())
        if session_id is not None:
            grants = [g for g in grants if g.session_id == session_id]
        return grants

    def clear(self) -> None:
        with self._lock:
            self._grants.clear()
