"""Tests for src/controller/intent_store.py (Architecture 2 — intent store)."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from controller import intent_store

_SAMPLE_INTENT = {
    "intent_id": "00000000-0000-0000-0000-000000000001",
    "action": "service.restart",
    "target": "nginx",
    "params": {},
    "reason": "user_requested",
    "risk_level": "medium",
}


class TestIntentStore:
    def setup_method(self):
        # Use a fresh store for each test
        self.store = intent_store.IntentStore()

    def test_put_returns_uuid(self):
        ref = self.store.put(_SAMPLE_INTENT)
        import re
        assert re.match(r"[0-9a-f-]{36}", ref)

    def test_get_returns_intent(self):
        ref = self.store.put(_SAMPLE_INTENT)
        result = self.store.get(ref)
        assert result == _SAMPLE_INTENT

    def test_get_unknown_returns_none(self):
        assert self.store.get("00000000-0000-0000-0000-000000000000") is None

    def test_delete(self):
        ref = self.store.put(_SAMPLE_INTENT)
        self.store.delete(ref)
        assert self.store.get(ref) is None

    def test_revise_merges_fields(self):
        ref = self.store.put(_SAMPLE_INTENT)
        new_ref = self.store.revise(ref, {"target": "apache2"})
        revised = self.store.get(new_ref)
        assert revised is not None
        assert revised["target"] == "apache2"
        # Original unchanged
        original = self.store.get(ref)
        assert original["target"] == "nginx"

    def test_revise_unknown_ref_returns_none(self):
        result = self.store.revise("nonexistent-ref", {"target": "foo"})
        assert result is None

    def test_revise_rejects_new_keys(self):
        ref = self.store.put(_SAMPLE_INTENT)
        with pytest.raises(ValueError, match="unknown Intent Object fields"):
            self.store.revise(ref, {"completely_new_key": "value"})

    def test_len(self):
        assert len(self.store) == 0
        self.store.put(_SAMPLE_INTENT)
        assert len(self.store) == 1
        self.store.put(_SAMPLE_INTENT)
        assert len(self.store) == 2

    def test_expiry(self, monkeypatch):
        # Patch TTL to 0 seconds so the entry expires immediately
        monkeypatch.setattr(intent_store, "TTL_SECONDS", 0)
        ref = self.store.put(_SAMPLE_INTENT)
        time.sleep(0.01)
        assert self.store.get(ref) is None

    def test_evict_expired(self, monkeypatch):
        monkeypatch.setattr(intent_store, "TTL_SECONDS", 0)
        self.store.put(_SAMPLE_INTENT)
        self.store.put(_SAMPLE_INTENT)
        time.sleep(0.01)
        evicted = self.store.evict_expired()
        assert evicted == 2
        assert len(self.store) == 0


# ── Module-level convenience functions ───────────────────────────────────────

class TestModuleLevelAPI:
    def test_put_get_delete(self):
        intent_store._store = intent_store.IntentStore()  # fresh store
        ref = intent_store.put(_SAMPLE_INTENT)
        assert intent_store.get(ref) == _SAMPLE_INTENT
        intent_store.delete(ref)
        assert intent_store.get(ref) is None

    def test_revise(self):
        intent_store._store = intent_store.IntentStore()
        ref = intent_store.put(_SAMPLE_INTENT)
        new_ref = intent_store.revise(ref, {"target": "mysql"})
        assert intent_store.get(new_ref)["target"] == "mysql"
