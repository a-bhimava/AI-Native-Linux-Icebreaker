"""v6.8 Task #145 — SessionStore + TurnMemory + history-field regression lock."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from controller.session import SessionState, TurnMemory
from controller.session_store import SessionStore


def _cfg(ttl: int = 0) -> SimpleNamespace:
    """Minimal SessionConfig-shaped namespace for tests."""
    return SimpleNamespace(
        session_ttl_seconds=ttl,
        max_turns=10,
        undo=SimpleNamespace(max_history=0),
    )


# ── Create / register / get / remove ──────────────────────────────────

def test_create_returns_session_and_registers_it():
    store = SessionStore()
    s = store.create("gemini", _cfg())
    assert s.backend == "gemini"
    assert store.get(s.session_id) is s


def test_get_missing_returns_none():
    store = SessionStore()
    assert store.get("no-such-id") is None


def test_remove_returns_removed_session():
    store = SessionStore()
    s = store.create("gemini", _cfg())
    got = store.remove(s.session_id)
    assert got is s
    assert store.get(s.session_id) is None


def test_remove_missing_returns_none():
    store = SessionStore()
    assert store.remove("nope") is None


def test_register_reuses_session_id_for_reconnect():
    store = SessionStore()
    s = store.create("openai", _cfg())
    replacement = SessionState.new("openai", _cfg())
    # Force the replacement to share the session_id.
    replacement.session_id = s.session_id  # type: ignore[misc]
    store.register(replacement)
    assert store.get(s.session_id) is replacement


# ── list_active + len + contains ──────────────────────────────────────

def test_list_active_returns_current_sessions():
    store = SessionStore()
    a = store.create("gemini", _cfg())
    b = store.create("anthropic", _cfg())
    active = store.list_active()
    active_ids = {s.session_id for s in active}
    assert active_ids == {a.session_id, b.session_id}


def test_list_active_is_snapshot_not_view():
    store = SessionStore()
    a = store.create("gemini", _cfg())
    snap = store.list_active()
    store.remove(a.session_id)
    # snap unchanged even though store mutated.
    assert a in snap


def test_len_and_contains():
    store = SessionStore()
    s = store.create("openai", _cfg())
    assert len(store) == 1
    assert s.session_id in store
    assert "random" not in store


# ── prune_expired ─────────────────────────────────────────────────────

def test_prune_expired_removes_expired_sessions():
    store = SessionStore()
    # TTL = 0 means never expire per SessionState.is_expired().
    fresh = store.create("gemini", _cfg(ttl=0))
    # TTL = -1 makes is_expired() return True always (since time delta > TTL).
    old = store.create("openai", _cfg(ttl=1))
    # Force old's _last_activity into the past.
    object.__setattr__(old, "_last_activity", time.monotonic() - 10)

    removed = store.prune_expired()
    assert removed == 1
    assert fresh.session_id in store
    assert old.session_id not in store


def test_prune_expired_returns_zero_when_none_expired():
    store = SessionStore()
    store.create("gemini", _cfg(ttl=0))
    store.create("anthropic", _cfg(ttl=0))
    assert store.prune_expired() == 0


# ── Thread safety ─────────────────────────────────────────────────────

def test_concurrent_creates_do_not_collide():
    store = SessionStore()
    errors: list[Exception] = []

    def _worker():
        try:
            for _ in range(50):
                store.create("gemini", _cfg())
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(store) == 200


# ── TurnMemory dataclass ──────────────────────────────────────────────

def test_turn_memory_is_frozen_and_serializable():
    tm = TurnMemory(
        turn_id="t1",
        session_id="s1",
        query_hash="q1",
        intent_action="fs.list",
        result_hash="r1",
        result_summary="listed 3 entries",
        outcome="executed",
        tier=0,
        timestamp_utc="2026-07-13T00:00:00Z",
    )
    with pytest.raises(Exception):
        tm.turn_id = "mutated"  # type: ignore[misc]

    # dataclasses.asdict works — critical for LangGraph state carryover.
    from dataclasses import asdict
    d = asdict(tm)
    assert d["intent_action"] == "fs.list"
    assert d["tier"] == 0


# ── SessionState.history field ────────────────────────────────────────

def test_session_state_history_defaults_to_empty_list():
    s = SessionState.new("gemini", _cfg())
    assert s.history == []


def test_session_state_history_is_mutable_list():
    s = SessionState.new("gemini", _cfg())
    tm = TurnMemory(
        turn_id="t1", session_id=s.session_id, query_hash="q",
        intent_action="fs.list", result_hash="r",
        result_summary="ok", outcome="executed", tier=0,
        timestamp_utc="2026-07-13T00:00:00Z",
    )
    s.history.append(tm)
    assert s.history == [tm]


# ── SessionState.new backward compat (D2) ─────────────────────────────

def test_session_state_new_still_works_orphan():
    """SessionState.new(backend, cfg) stays as a classmethod for test
    fixtures. The returned session is 'orphan' — never enters the store,
    but exists for tests that directly construct SessionState."""
    s = SessionState.new("local", _cfg())
    assert s.backend == "local"
    # Not registered anywhere yet — a store lookup would miss it.
    store = SessionStore()
    assert store.get(s.session_id) is None
