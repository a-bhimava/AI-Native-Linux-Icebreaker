"""Tests for ``controller.undo`` — UndoEntry, UndoHistory, UndoStatus.

Covers:
  1. UndoEntry construction
  2. UndoEntry frozen immutability
  3. UndoStatus enum values
  4. UndoHistory empty state
  5. UndoHistory.record adds entry
  6. UndoHistory max_depth ring buffer eviction
  7. UndoHistory.last_undoable returns most recent undoable
  8. UndoHistory.last_undoable skips non-undoable (Tier 0)
  9. UndoHistory.last_undoable returns None when empty
 10. UndoHistory.get_by_turn found
 11. UndoHistory.get_by_turn not found
 12. UndoHistory.list_entries order
 13. UndoHistory.mark_undone success
 14. UndoHistory.mark_undone on non-undoable returns False
 15. UndoHistory.mark_undone on non-existent returns False
 16. UndoHistory.clear empties history
 17. UndoHistory.__len__
 18. UndoEntry default undo_status is UNAVAILABLE
 19. UndoHistory max_depth property
 20. UndoEntry fields are all accessible
 21. UndoHistory mark_undone updates status to UNDONE
 22. UndoHistory list_entries returns list (not deque)
 23. UndoHistory record multiple entries
 24. UndoHistory ring buffer preserves newest entries
 25. UndoStatus values are lowercase strings
"""

from __future__ import annotations

import time

import pytest

from controller.undo import UndoEntry, UndoHistory, UndoStatus


# ── Helpers ──────────────────────────────────────────────────────────────────


def _entry(
    turn_index: int = 0,
    action: str = "system.status",
    target: str = "",
    tier: int = 0,
    undoable: bool = False,
    undo_status: UndoStatus = UndoStatus.UNAVAILABLE,
) -> UndoEntry:
    return UndoEntry(
        turn_index=turn_index,
        intent_id=f"intent-{turn_index}",
        action=action,
        target=target,
        outcome="executed",
        timestamp=time.monotonic(),
        tier=tier,
        undoable=undoable,
        undo_status=undo_status,
    )


# ── 1: UndoEntry construction ──────────────────────────────────────────────


def test_undo_entry_construction():
    entry = _entry(turn_index=5, action="fs.read", target="/etc/hostname")
    assert entry.turn_index == 5
    assert entry.action == "fs.read"
    assert entry.target == "/etc/hostname"
    assert entry.outcome == "executed"
    assert entry.tier == 0
    assert entry.undoable is False


# ── 2: UndoEntry frozen immutability ────────────────────────────────────────


def test_undo_entry_is_frozen():
    entry = _entry()
    with pytest.raises(AttributeError):
        entry.turn_index = 99
    with pytest.raises(AttributeError):
        entry.action = "modified"
    with pytest.raises(AttributeError):
        entry.undoable = True


# ── 3: UndoStatus enum values ──────────────────────────────────────────────


def test_undo_status_values():
    assert UndoStatus.AVAILABLE == "available"
    assert UndoStatus.UNAVAILABLE == "unavailable"
    assert UndoStatus.UNDONE == "undone"
    assert UndoStatus.EXPIRED == "expired"


def test_undo_status_values_are_lowercase():
    for member in UndoStatus:
        assert member.value == member.value.lower()


# ── 4: UndoHistory empty state ──────────────────────────────────────────────


def test_undo_history_empty():
    history = UndoHistory()
    assert len(history) == 0
    assert history.list_entries() == []
    assert history.last_undoable() is None


# ── 5: UndoHistory.record adds entry ───────────────────────────────────────


def test_undo_history_record():
    history = UndoHistory()
    entry = _entry(turn_index=1)
    history.record(entry)
    assert len(history) == 1
    assert history.list_entries()[0] is entry


def test_undo_history_record_multiple():
    history = UndoHistory()
    e1 = _entry(turn_index=1)
    e2 = _entry(turn_index=2)
    e3 = _entry(turn_index=3)
    history.record(e1)
    history.record(e2)
    history.record(e3)
    assert len(history) == 3


# ── 6: UndoHistory max_depth ring buffer eviction ──────────────────────────


def test_undo_history_max_depth_eviction():
    history = UndoHistory(max_depth=3)
    for i in range(5):
        history.record(_entry(turn_index=i))
    assert len(history) == 3
    entries = history.list_entries()
    # Oldest entries (0, 1) should have been evicted
    turn_indices = [e.turn_index for e in entries]
    assert turn_indices == [2, 3, 4]


def test_undo_history_ring_buffer_preserves_newest():
    history = UndoHistory(max_depth=2)
    history.record(_entry(turn_index=10))
    history.record(_entry(turn_index=20))
    history.record(_entry(turn_index=30))
    entries = history.list_entries()
    assert len(entries) == 2
    assert entries[0].turn_index == 20
    assert entries[1].turn_index == 30


# ── 7: UndoHistory.last_undoable returns most recent undoable ───────────────


def test_last_undoable_returns_most_recent():
    history = UndoHistory()
    history.record(_entry(turn_index=1, undoable=True))
    history.record(_entry(turn_index=2, undoable=True))
    history.record(_entry(turn_index=3, undoable=False))
    result = history.last_undoable()
    assert result is not None
    assert result.turn_index == 2


# ── 8: UndoHistory.last_undoable skips non-undoable ────────────────────────


def test_last_undoable_skips_non_undoable():
    history = UndoHistory()
    history.record(_entry(turn_index=1, undoable=True))
    history.record(_entry(turn_index=2, undoable=False))
    history.record(_entry(turn_index=3, undoable=False))
    result = history.last_undoable()
    assert result is not None
    assert result.turn_index == 1


def test_last_undoable_all_non_undoable():
    history = UndoHistory()
    history.record(_entry(turn_index=1, undoable=False))
    history.record(_entry(turn_index=2, undoable=False))
    assert history.last_undoable() is None


# ── 9: UndoHistory.last_undoable returns None when empty ────────────────────


def test_last_undoable_empty():
    history = UndoHistory()
    assert history.last_undoable() is None


# ── 10: UndoHistory.get_by_turn found ──────────────────────────────────────


def test_get_by_turn_found():
    history = UndoHistory()
    history.record(_entry(turn_index=5, action="fs.read"))
    history.record(_entry(turn_index=10, action="system.status"))
    result = history.get_by_turn(10)
    assert result is not None
    assert result.action == "system.status"
    assert result.turn_index == 10


# ── 11: UndoHistory.get_by_turn not found ──────────────────────────────────


def test_get_by_turn_not_found():
    history = UndoHistory()
    history.record(_entry(turn_index=1))
    assert history.get_by_turn(99) is None


def test_get_by_turn_empty_history():
    history = UndoHistory()
    assert history.get_by_turn(0) is None


# ── 12: UndoHistory.list_entries order ─────────────────────────────────────


def test_list_entries_order():
    history = UndoHistory()
    history.record(_entry(turn_index=1))
    history.record(_entry(turn_index=2))
    history.record(_entry(turn_index=3))
    entries = history.list_entries()
    assert [e.turn_index for e in entries] == [1, 2, 3]


def test_list_entries_returns_list_not_deque():
    history = UndoHistory()
    history.record(_entry(turn_index=1))
    entries = history.list_entries()
    assert isinstance(entries, list)


# ── 13: UndoHistory.mark_undone success ────────────────────────────────────


def test_mark_undone_success():
    history = UndoHistory()
    history.record(_entry(turn_index=1, undoable=True))
    result = history.mark_undone(1)
    assert result is True
    entry = history.get_by_turn(1)
    assert entry.undo_status == UndoStatus.UNDONE


def test_mark_undone_updates_status():
    history = UndoHistory()
    history.record(_entry(turn_index=1, undoable=True, undo_status=UndoStatus.AVAILABLE))
    history.mark_undone(1)
    entry = history.get_by_turn(1)
    assert entry.undo_status == UndoStatus.UNDONE


# ── 14: UndoHistory.mark_undone on non-undoable returns False ──────────────


def test_mark_undone_non_undoable():
    history = UndoHistory()
    history.record(_entry(turn_index=1, undoable=False))
    result = history.mark_undone(1)
    assert result is False
    # Status should not change
    entry = history.get_by_turn(1)
    assert entry.undo_status == UndoStatus.UNAVAILABLE


# ── 15: UndoHistory.mark_undone on non-existent returns False ──────────────


def test_mark_undone_non_existent():
    history = UndoHistory()
    result = history.mark_undone(99)
    assert result is False


def test_mark_undone_wrong_turn_index():
    history = UndoHistory()
    history.record(_entry(turn_index=1, undoable=True))
    result = history.mark_undone(2)
    assert result is False


# ── 16: UndoHistory.clear empties history ──────────────────────────────────


def test_clear_empties_history():
    history = UndoHistory()
    history.record(_entry(turn_index=1))
    history.record(_entry(turn_index=2))
    assert len(history) == 2
    history.clear()
    assert len(history) == 0
    assert history.list_entries() == []


# ── 17: UndoHistory.__len__ ────────────────────────────────────────────────


def test_len():
    history = UndoHistory()
    assert len(history) == 0
    history.record(_entry())
    assert len(history) == 1
    history.record(_entry(turn_index=1))
    assert len(history) == 2


# ── 18: UndoEntry default undo_status is UNAVAILABLE ──────────────────────


def test_undo_entry_default_status():
    entry = UndoEntry(
        turn_index=0,
        intent_id="test",
        action="system.status",
        target="",
        outcome="executed",
        timestamp=0.0,
        tier=0,
        undoable=False,
    )
    assert entry.undo_status == UndoStatus.UNAVAILABLE


# ── 19: UndoHistory max_depth property ─────────────────────────────────────


def test_max_depth_property():
    history = UndoHistory(max_depth=5)
    assert history.max_depth == 5


def test_max_depth_default():
    history = UndoHistory()
    assert history.max_depth == 10


# ── 20: UndoEntry fields all accessible ────────────────────────────────────


def test_undo_entry_all_fields():
    ts = time.monotonic()
    entry = UndoEntry(
        turn_index=7,
        intent_id="uuid-123",
        action="fs.write",
        target="/tmp/test",
        outcome="executed",
        timestamp=ts,
        tier=2,
        undoable=True,
        undo_status=UndoStatus.AVAILABLE,
    )
    assert entry.turn_index == 7
    assert entry.intent_id == "uuid-123"
    assert entry.action == "fs.write"
    assert entry.target == "/tmp/test"
    assert entry.outcome == "executed"
    assert entry.timestamp == ts
    assert entry.tier == 2
    assert entry.undoable is True
    assert entry.undo_status == UndoStatus.AVAILABLE
