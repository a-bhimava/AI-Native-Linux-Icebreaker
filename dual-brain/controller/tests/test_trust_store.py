"""Tests for TrustStore — session-scoped trust grants (M5.1c, G5.4).

Covers:
  - Grant accepts Tier <= MEDIUM; raises on Tier >= HIGH (hard floor #1).
  - is_trusted returns None on Tier >= HIGH even with a matching grant (hard floor #2).
  - Expiry, session mismatch, action mismatch, target prefix mismatch.
  - Revoke, list, clear lifecycle.
  - Thread safety under concurrent access.
"""

from __future__ import annotations

import threading
import time

import pytest

from controller.risk_classifier import Tier
from controller.trust_store import TrustGrant, TrustStore


# ── Helpers ───────────────────────────────────────────────────────────────────

def _store() -> TrustStore:
    return TrustStore()


# ── Grant hard floor (R-5.1 defense #1) ──────────────────────────────────────

def test_grant_tier_read_only_succeeds():
    s = _store()
    g = s.grant("fs.read", "/tmp/", Tier.READ_ONLY, "sess-1", 60)
    assert isinstance(g, TrustGrant)
    assert g.action == "fs.read"


def test_grant_tier_low_succeeds():
    s = _store()
    g = s.grant("fs.write", "/home/user/", Tier.LOW, "sess-1", 60)
    assert g.max_tier == Tier.LOW


def test_grant_tier_medium_succeeds():
    s = _store()
    g = s.grant("service.restart", "/etc/nginx", Tier.MEDIUM, "sess-1", 60)
    assert g.max_tier == Tier.MEDIUM


def test_grant_tier_high_raises():
    s = _store()
    with pytest.raises(ValueError, match="Tier HIGH"):
        s.grant("fs.delete", "/etc/hosts", Tier.HIGH, "sess-1", 60)


def test_grant_tier_high_value_raises():
    s = _store()
    with pytest.raises(ValueError, match="floor"):
        s.grant("fs.delete", "/boot/", Tier(3), "sess-1", 60)


# ── is_trusted hard floor (R-5.1 defense #2) ─────────────────────────────────

def test_is_trusted_returns_grant_on_match():
    s = _store()
    s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 60)
    result = s.is_trusted("fs.write", "/tmp/foo.txt", Tier.MEDIUM, "sess-1")
    assert result is not None
    assert result.action == "fs.write"


def test_is_trusted_returns_none_on_tier_high():
    s = _store()
    s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 60)
    result = s.is_trusted("fs.write", "/tmp/foo.txt", Tier.HIGH, "sess-1")
    assert result is None


def test_is_trusted_returns_none_on_expired(monkeypatch):
    s = _store()
    s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 1)
    real_monotonic = time.monotonic
    monkeypatch.setattr("time.monotonic", lambda: real_monotonic() + 2)
    monkeypatch.setattr(
        "controller.trust_store.time.monotonic", lambda: real_monotonic() + 2
    )
    result = s.is_trusted("fs.write", "/tmp/foo.txt", Tier.MEDIUM, "sess-1")
    assert result is None


def test_is_trusted_returns_none_on_wrong_session():
    s = _store()
    s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 60)
    result = s.is_trusted("fs.write", "/tmp/foo.txt", Tier.MEDIUM, "sess-2")
    assert result is None


def test_is_trusted_returns_none_on_action_mismatch():
    s = _store()
    s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 60)
    result = s.is_trusted("fs.delete", "/tmp/foo.txt", Tier.MEDIUM, "sess-1")
    assert result is None


def test_is_trusted_returns_none_on_target_prefix_mismatch():
    s = _store()
    s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 60)
    result = s.is_trusted("fs.write", "/etc/foo.txt", Tier.MEDIUM, "sess-1")
    assert result is None


def test_is_trusted_tier_below_max_tier_matches():
    s = _store()
    s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 60)
    result = s.is_trusted("fs.write", "/tmp/foo.txt", Tier.LOW, "sess-1")
    assert result is not None


def test_is_trusted_tier_above_max_tier_no_match():
    s = _store()
    s.grant("fs.write", "/tmp/", Tier.LOW, "sess-1", 60)
    result = s.is_trusted("fs.write", "/tmp/foo.txt", Tier.MEDIUM, "sess-1")
    assert result is None


# ── Revoke ────────────────────────────────────────────────────────────────────

def test_revoke_removes_grant():
    s = _store()
    g = s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 60)
    assert s.revoke(g.grant_id) is True
    result = s.is_trusted("fs.write", "/tmp/foo.txt", Tier.MEDIUM, "sess-1")
    assert result is None


def test_revoke_returns_false_on_missing():
    s = _store()
    assert s.revoke("nonexistent") is False


# ── List ──────────────────────────────────────────────────────────────────────

def test_list_grants_returns_all():
    s = _store()
    s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 60)
    s.grant("fs.read", "/var/", Tier.LOW, "sess-1", 60)
    grants = s.list_grants()
    assert len(grants) == 2


def test_list_grants_filters_by_session():
    s = _store()
    s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 60)
    s.grant("fs.read", "/var/", Tier.LOW, "sess-2", 60)
    grants = s.list_grants(session_id="sess-1")
    assert len(grants) == 1
    assert grants[0].session_id == "sess-1"


def test_list_grants_prunes_expired(monkeypatch):
    s = _store()
    s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 1)
    real_monotonic = time.monotonic
    monkeypatch.setattr(
        "controller.trust_store.time.monotonic", lambda: real_monotonic() + 2
    )
    grants = s.list_grants()
    assert len(grants) == 0


# ── Clear ─────────────────────────────────────────────────────────────────────

def test_clear_removes_all():
    s = _store()
    s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 60)
    s.grant("fs.read", "/var/", Tier.LOW, "sess-1", 60)
    s.clear()
    assert s.list_grants() == []


# ── Grant ID uniqueness ──────────────────────────────────────────────────────

def test_grant_ids_are_unique():
    s = _store()
    ids = set()
    for _ in range(100):
        g = s.grant("fs.write", "/tmp/", Tier.MEDIUM, "sess-1", 60)
        ids.add(g.grant_id)
    assert len(ids) == 100


# ── Thread safety ────────────────────────────────────────────────────────────

def test_thread_safety():
    s = _store()
    n_threads = 8
    per_thread = 50
    barrier = threading.Barrier(n_threads)
    errors: list[str] = []

    def worker(tid: int) -> None:
        try:
            barrier.wait()
            for i in range(per_thread):
                g = s.grant("fs.write", f"/tmp/t{tid}/", Tier.MEDIUM, f"sess-{tid}", 60)
                result = s.is_trusted("fs.write", f"/tmp/t{tid}/f{i}", Tier.MEDIUM, f"sess-{tid}")
                if result is None:
                    errors.append(f"t{tid} i{i}: is_trusted returned None")
                s.revoke(g.grant_id)
        except Exception as exc:
            errors.append(f"t{tid}: {exc}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors, f"Thread safety errors: {errors}"
