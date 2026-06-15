"""UndoHistory — session-scoped undo scaffold.

Builds the Controller-side plumbing so ``/undo`` works when mcpd adds
rollback RPCs. Until then, every undo request returns ``UNAVAILABLE``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class UndoStatus(str, Enum):
    AVAILABLE   = "available"
    UNAVAILABLE = "unavailable"
    UNDONE      = "undone"
    EXPIRED     = "expired"


@dataclass(frozen=True)
class UndoEntry:
    turn_index: int
    intent_id: str
    action: str
    target: str
    outcome: str
    timestamp: float
    tier: int
    undoable: bool
    undo_status: UndoStatus = UndoStatus.UNAVAILABLE


class UndoHistory:
    """Session-scoped undo history ring buffer."""

    def __init__(self, max_depth: int = 10) -> None:
        self._entries: deque[UndoEntry] = deque(maxlen=max_depth)
        self._max_depth = max_depth

    @property
    def max_depth(self) -> int:
        return self._max_depth

    def record(self, entry: UndoEntry) -> None:
        self._entries.append(entry)

    def last_undoable(self) -> Optional[UndoEntry]:
        for entry in reversed(self._entries):
            if entry.undoable:
                return entry
        return None

    def get_by_turn(self, turn_index: int) -> Optional[UndoEntry]:
        for entry in self._entries:
            if entry.turn_index == turn_index:
                return entry
        return None

    def list_entries(self) -> list[UndoEntry]:
        return list(self._entries)

    def mark_undone(self, turn_index: int) -> bool:
        for i, entry in enumerate(self._entries):
            if entry.turn_index == turn_index:
                if not entry.undoable:
                    return False
                updated = UndoEntry(
                    turn_index=entry.turn_index,
                    intent_id=entry.intent_id,
                    action=entry.action,
                    target=entry.target,
                    outcome=entry.outcome,
                    timestamp=entry.timestamp,
                    tier=entry.tier,
                    undoable=entry.undoable,
                    undo_status=UndoStatus.UNDONE,
                )
                self._entries[i] = updated
                return True
        return False

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
