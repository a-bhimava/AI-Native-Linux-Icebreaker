"""
intent_store.py — In-memory store that maps opaque UUID references to validated
Intent Objects. This is the ONLY place where the raw Intent Object lives after
it leaves the Quarantined Brain.

Security invariant (INV-1, INV-2):
  - The Privileged Brain receives ONLY the opaque intent_id string, never the
    Intent Object itself.
  - The Intent Object is fetched from this store only by the Controller and the
    QB verifier — never by the Privileged Brain.
  - Entries expire after TTL_SECONDS to bound memory and prevent stale replay.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


TTL_SECONDS = 300  # 5 minutes — long enough for HITL approval


@dataclass
class IntentEntry:
    intent: dict
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > TTL_SECONDS


class IntentStore:
    """Thread-safe store of Intent Objects keyed by opaque UUID.

    Usage:
        store = IntentStore()
        ref_id = store.put(validated_intent_object)
        intent = store.get(ref_id)   # returns None if expired or unknown
        store.delete(ref_id)
    """

    def __init__(self) -> None:
        self._store: dict[str, IntentEntry] = {}
        self._lock = threading.Lock()

    def put(self, intent: dict) -> str:
        """Store a validated Intent Object and return an opaque UUID reference."""
        ref_id = str(uuid.uuid4())
        with self._lock:
            self._store[ref_id] = IntentEntry(intent=intent)
        return ref_id

    def get(self, ref_id: str) -> Optional[dict]:
        """Retrieve an Intent Object by reference ID. Returns None if unknown or expired."""
        with self._lock:
            entry = self._store.get(ref_id)
            if entry is None:
                return None
            if entry.is_expired():
                del self._store[ref_id]
                return None
            return entry.intent

    def delete(self, ref_id: str) -> None:
        with self._lock:
            self._store.pop(ref_id, None)

    def revise(self, original_ref_id: str, correction: dict) -> Optional[str]:
        """Apply a structured correction to an existing intent and store it as a new entry.

        The correction dict may contain a subset of top-level Intent Object fields to
        override (e.g. {"target": "/var/cache/apt"}).  Fields not present in correction
        are copied from the original intent unchanged.

        Returns a new opaque reference ID for the revised intent, or None if the
        original intent is unknown or expired.
        """
        original = self.get(original_ref_id)
        if original is None:
            return None

        revised = {**original, **correction}
        # Correction must not introduce new top-level keys not in the original
        extra_keys = set(revised.keys()) - set(original.keys())
        if extra_keys:
            raise ValueError(
                f"Correction introduced unknown Intent Object fields: {extra_keys}. "
                "Only fields present in the original schema are allowed (INV-2)."
            )

        return self.put(revised)

    def evict_expired(self) -> int:
        """Remove all expired entries. Returns count of entries evicted."""
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._store.items() if v.is_expired()]
            for k in expired:
                del self._store[k]
        return len(expired)

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# Module-level singleton — the Controller imports this directly
_store = IntentStore()


def put(intent: dict) -> str:
    return _store.put(intent)


def get(ref_id: str) -> Optional[dict]:
    return _store.get(ref_id)


def delete(ref_id: str) -> None:
    _store.delete(ref_id)


def revise(original_ref_id: str, correction: dict) -> Optional[str]:
    return _store.revise(original_ref_id, correction)


def evict_expired() -> int:
    return _store.evict_expired()
