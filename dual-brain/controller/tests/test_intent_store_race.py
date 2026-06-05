"""Tests for IntentStore — focused on the M2.0 revise() race fix (P2-F13).

Before M2.0, IntentStore.revise() called self.get() and self.put() as two
separate lock acquisitions. Between them an entry could expire or be
deleted, leading to silent lost updates or KeyError under contention.

After M2.0, revise() holds self._lock across the entire get → merge →
validate → put critical section, so an entry cannot disappear mid-revise.
"""

import threading
import time
from typing import Optional

import pytest

from controller import intent_store as IS
from controller.intent_store import IntentEntry, IntentStore


def _intent(**overrides) -> dict:
    base = {
        "action": "fs.write",
        "target": "/tmp/test",
        "params": {"content": "hello"},
        "reason": "test",
        "risk_level": "low",
    }
    base.update(overrides)
    return base


# ── Basic round-trip ─────────────────────────────────────────────────────────

def test_put_get_round_trip():
    s = IntentStore()
    rid = s.put(_intent())
    fetched = s.get(rid)
    assert fetched == _intent()


def test_get_unknown_returns_none():
    s = IntentStore()
    assert s.get("not-a-real-uuid") is None


def test_revise_round_trip():
    s = IntentStore()
    rid = s.put(_intent(target="/tmp/a"))
    nrid = s.revise(rid, {"target": "/tmp/b"})
    assert nrid is not None
    assert nrid != rid
    assert s.get(nrid)["target"] == "/tmp/b"
    # original retained until TTL expiry
    assert s.get(rid)["target"] == "/tmp/a"


def test_revise_rejects_unknown_keys():
    s = IntentStore()
    rid = s.put(_intent())
    with pytest.raises(ValueError, match="unknown Intent Object fields"):
        s.revise(rid, {"never_in_schema": "x"})


def test_revise_returns_none_for_unknown_original():
    s = IntentStore()
    assert s.revise("nope", {"target": "/tmp/x"}) is None


# ── Expiry behavior ──────────────────────────────────────────────────────────

def test_expired_entry_returns_none_and_is_evicted(monkeypatch):
    s = IntentStore()
    rid = s.put(_intent())
    # Backdate the entry's creation so it appears expired.
    with s._lock:
        s._store[rid] = IntentEntry(intent=_intent(), created_at=time.time() - (IS.TTL_SECONDS + 1))
    assert s.get(rid) is None
    # The expired entry was evicted by the get() call.
    assert len(s) == 0


def test_revise_returns_none_when_original_expired():
    s = IntentStore()
    rid = s.put(_intent())
    with s._lock:
        s._store[rid] = IntentEntry(intent=_intent(), created_at=time.time() - (IS.TTL_SECONDS + 1))
    assert s.revise(rid, {"target": "/tmp/changed"}) is None


# ── Concurrent revise (P2-F13 — the race the M2.0 fix closes) ────────────────

def test_concurrent_revise_chain_no_lost_updates():
    """8 threads each do 50 revisions on the chain head. Final state must
    contain (1 original + 8*50 revisions) entries with no exceptions, no
    None returns from revise(), and no lost ref_ids."""
    s = IntentStore()
    initial_rid = s.put(_intent(target="initial"))

    N_THREADS = 8
    N_ITERATIONS = 50
    errors: list[Exception] = []
    produced_rids: list[str] = []
    rids_lock = threading.Lock()

    def worker(tid: int):
        try:
            for i in range(N_ITERATIONS):
                # Each thread always revises the original. This is the
                # high-contention pattern the M2.0 widened lock must handle.
                new_rid = s.revise(initial_rid, {"target": f"t{tid}-i{i}"})
                assert new_rid is not None, f"revise returned None at tid={tid} i={i}"
                with rids_lock:
                    produced_rids.append(new_rid)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Concurrent revisions raised: {errors}"
    expected_total = 1 + N_THREADS * N_ITERATIONS
    assert len(s) == expected_total, (
        f"Store size {len(s)} != expected {expected_total} — lost or duplicated entries"
    )
    # All produced ref_ids must resolve to a non-None intent.
    sampled = produced_rids[::25]  # sample to keep runtime tight
    for rid in sampled:
        intent = s.get(rid)
        assert intent is not None
        assert intent["target"].startswith("t")


def test_revise_atomic_against_concurrent_delete():
    """A revise() in progress must not crash when another thread deletes the
    same ref_id. Under the widened lock, revise either succeeds atomically
    or returns None — never partial state and never an exception leak."""
    s = IntentStore()
    rid = s.put(_intent())

    errors: list[Exception] = []
    revise_returned_none = threading.Event()
    revise_returned_ok = threading.Event()

    def reviser():
        try:
            for _ in range(200):
                result: Optional[str] = s.revise(rid, {"target": "/tmp/x"})
                if result is None:
                    revise_returned_none.set()
                else:
                    revise_returned_ok.set()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def deleter():
        try:
            for _ in range(200):
                s.delete(rid)
                # The deleter and reviser race: deleter occasionally wins,
                # which is fine — revise should then return None cleanly.
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=reviser)
    t2 = threading.Thread(target=deleter)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Race produced exceptions: {errors}"
    # At least one branch should have been exercised (sanity for the test itself).
    assert revise_returned_none.is_set() or revise_returned_ok.is_set()


def test_revise_atomic_against_concurrent_expiry(monkeypatch):
    """Provoke the expiry race: tiny TTL, revise() repeatedly. Under the
    widened lock no exception escapes; revise either succeeds against a
    still-live snapshot or returns None on a freshly-expired entry."""
    monkeypatch.setattr(IS, "TTL_SECONDS", 0.001)
    s = IntentStore()

    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(500):
                rid = s.put(_intent())
                # Sometimes the entry expires before revise sees it.
                time.sleep(0.0005)
                result = s.revise(rid, {"target": "/tmp/changed"})
                # Either it survived (string) or it expired (None) — both fine.
                assert result is None or isinstance(result, str)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"Race-vs-expiry produced exceptions: {errors}"
