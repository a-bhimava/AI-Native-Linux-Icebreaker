"""v6.17 M7.6a-1f — tests for native-dispatch ring in oc_audit_bridge.

Covers the mark_native_dispatch + is_native_dispatch surface added by
M7.6a-1f, plus its integration with OCAuditBridge._ingest_line (skip
lines that match a recent native dispatch) and McpdClient.call (marks
on every dispatch).

The ring exists to deconflict double-audit rows when Controller.run_turn
_from_intent (M7.6a-1c) dispatches an mcpd call: the native Controller
audits at Step 5 + Step 9, AND mcpd writes to /var/log/mcpd/audit.log
which the bridge tails. Without this filter, each submit_intent turn
would produce two INV-8 rows per real tool call — misleading forensics.

No mcpd Rust change — user decision 2026-08-09. Python-only correlation
via (method, target, timestamp-window).
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from controller import oc_audit_bridge as bridge_mod


# ── Fixtures: clean ring between tests ────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_ring():
    """Reset the module-level ring before + after every test so tests
    don't leak state to each other."""
    bridge_mod._reset_native_ring_for_tests()
    yield
    bridge_mod._reset_native_ring_for_tests()


# ── 1. mark + is_native_dispatch basic contract ──────────────────────

def test_mark_native_dispatch_records_entry():
    """mark_native_dispatch adds to the ring; is_native_dispatch finds it."""
    assert bridge_mod._native_ring_size_for_tests() == 0

    bridge_mod.mark_native_dispatch("fs.list", "/tmp")
    assert bridge_mod._native_ring_size_for_tests() == 1

    # Match on same method + target → True, and consumes the entry.
    assert bridge_mod.is_native_dispatch("fs.list", "/tmp") is True
    assert bridge_mod._native_ring_size_for_tests() == 0


def test_is_native_dispatch_returns_false_when_no_match():
    """No entries → False. No consumption."""
    assert bridge_mod.is_native_dispatch("fs.list", "/tmp") is False


def test_is_native_dispatch_method_mismatch_returns_false():
    """Same target, different method → not a match."""
    bridge_mod.mark_native_dispatch("fs.list", "/tmp")
    assert bridge_mod.is_native_dispatch("fs.write", "/tmp") is False
    # Original entry still there — not consumed.
    assert bridge_mod._native_ring_size_for_tests() == 1


def test_is_native_dispatch_target_mismatch_returns_false():
    """Same method, different target → not a match."""
    bridge_mod.mark_native_dispatch("fs.list", "/tmp")
    assert bridge_mod.is_native_dispatch("fs.list", "/etc") is False
    assert bridge_mod._native_ring_size_for_tests() == 1


# ── 2. Consume-on-match semantics ────────────────────────────────────

def test_is_native_dispatch_consumes_matching_entry():
    """One mark → one match → then no more matches for same key."""
    bridge_mod.mark_native_dispatch("fs.list", "/tmp")
    assert bridge_mod.is_native_dispatch("fs.list", "/tmp") is True
    # Second call for same key → miss (consumed)
    assert bridge_mod.is_native_dispatch("fs.list", "/tmp") is False


def test_multiple_marks_same_key_produce_multiple_matches():
    """Two marks for same (method, target) → two independent matches."""
    bridge_mod.mark_native_dispatch("fs.list", "/tmp")
    bridge_mod.mark_native_dispatch("fs.list", "/tmp")
    assert bridge_mod._native_ring_size_for_tests() == 2

    assert bridge_mod.is_native_dispatch("fs.list", "/tmp") is True
    assert bridge_mod._native_ring_size_for_tests() == 1
    assert bridge_mod.is_native_dispatch("fs.list", "/tmp") is True
    assert bridge_mod._native_ring_size_for_tests() == 0
    assert bridge_mod.is_native_dispatch("fs.list", "/tmp") is False


# ── 3. TTL expiry ────────────────────────────────────────────────────

def test_expired_entries_dont_match():
    """Entries older than window_secs → is_native_dispatch returns False.
    Kept in ring (not evicted by is_native_dispatch — sweeper's job)."""
    bridge_mod.mark_native_dispatch("fs.list", "/tmp")
    # Force ts to look old (~1 hour ago)
    with bridge_mod._RING_LOCK:
        bridge_mod._NATIVE_RING[0].ts_monotonic = time.monotonic() - 3600.0

    assert bridge_mod.is_native_dispatch("fs.list", "/tmp", window_secs=30.0) is False


def test_mark_sweeps_expired_entries():
    """mark_native_dispatch evicts entries older than _RING_TTL_SECONDS."""
    bridge_mod.mark_native_dispatch("fs.list", "/tmp")
    with bridge_mod._RING_LOCK:
        bridge_mod._NATIVE_RING[0].ts_monotonic = (
            time.monotonic() - bridge_mod._RING_TTL_SECONDS - 1.0
        )

    # A new mark triggers TTL sweep → old entry removed.
    bridge_mod.mark_native_dispatch("fs.write", "/etc/hostname")
    assert bridge_mod._native_ring_size_for_tests() == 1
    # And the new one is findable
    assert bridge_mod.is_native_dispatch("fs.write", "/etc/hostname") is True


# ── 4. Ring cap ──────────────────────────────────────────────────────

def test_ring_capped_at_500_entries():
    """deque(maxlen=500) → oldest evicted on overflow."""
    for i in range(600):
        bridge_mod.mark_native_dispatch("fs.list", f"/tmp/{i}")

    assert bridge_mod._native_ring_size_for_tests() == 500
    # First 100 marks got evicted
    assert bridge_mod.is_native_dispatch("fs.list", "/tmp/0") is False
    assert bridge_mod.is_native_dispatch("fs.list", "/tmp/99") is False
    # Later ones still present
    assert bridge_mod.is_native_dispatch("fs.list", "/tmp/500") is True


# ── 5. Bridge integration: _ingest_line skips native-dispatched lines ──

def test_bridge_ingest_line_skips_native_dispatched():
    """Simulate: Controller dispatched fs.list /tmp → mcpd emits an
    audit line with the same method + target. Bridge should skip
    writing that line (native audit row was already written)."""
    audit_log = MagicMock()
    from controller.oc_audit_bridge import OCAuditBridge

    bridge = OCAuditBridge(
        audit_log=audit_log,
        mcpd_audit_path="/tmp/fake-mcpd-audit.log",  # not actually opened
    )

    # Mark the native dispatch
    bridge_mod.mark_native_dispatch("fs.list", "/tmp")

    # Feed a matching mcpd audit line
    mcpd_line = json.dumps({
        "method": "fs.list",
        "params_redacted": {"path": "/tmp"},
        "result_class": "ok",
        "latency_us": 5000,
        "timestamp": "2026-08-10T20:00:00Z",
    }).encode("utf-8")

    bridge._ingest_line(mcpd_line)

    # audit_log.write_fields must NOT have been called — skipped as native dup
    audit_log.write_fields.assert_not_called()
    # Stats counter still incremented (we count the line as processed)
    assert bridge._stats.lines_ingested == 1


def test_bridge_ingest_line_writes_non_native_dispatched():
    """Non-native mcpd audit lines (from opencode or other clients) must
    still get bridge-enriched + written. Regression lock that the filter
    doesn't over-suppress."""
    audit_log = MagicMock()
    from controller.oc_audit_bridge import OCAuditBridge

    bridge = OCAuditBridge(
        audit_log=audit_log,
        mcpd_audit_path="/tmp/fake-mcpd-audit.log",
    )
    # No mark → not a native dispatch
    assert bridge_mod._native_ring_size_for_tests() == 0

    mcpd_line = json.dumps({
        "method": "fs.list",
        "params_redacted": {"path": "/tmp"},
        "result_class": "ok",
        "latency_us": 5000,
        "timestamp": "2026-08-10T20:00:00Z",
    }).encode("utf-8")

    bridge._ingest_line(mcpd_line)

    # write_fields called exactly once — bridge enriched + wrote
    audit_log.write_fields.assert_called_once()
    assert bridge._stats.lines_ingested == 1


def test_bridge_ingest_line_only_skips_one_matching_line():
    """Consume-on-match: if Controller dispatched fs.list once and
    mcpd log has TWO identical fs.list lines (e.g. opencode-issued
    duplicate), only the first is skipped as native — the second gets
    written normally."""
    audit_log = MagicMock()
    from controller.oc_audit_bridge import OCAuditBridge

    bridge = OCAuditBridge(
        audit_log=audit_log,
        mcpd_audit_path="/tmp/fake-mcpd-audit.log",
    )

    bridge_mod.mark_native_dispatch("fs.list", "/tmp")

    mcpd_line = json.dumps({
        "method": "fs.list",
        "params_redacted": {"path": "/tmp"},
        "result_class": "ok",
        "latency_us": 5000,
        "timestamp": "2026-08-10T20:00:00Z",
    }).encode("utf-8")

    bridge._ingest_line(mcpd_line)  # skipped (matches mark)
    bridge._ingest_line(mcpd_line)  # written (no mark left)

    audit_log.write_fields.assert_called_once()


# ── 6. McpdClient.call marks natively (integration) ─────────────────

def test_mcpd_client_call_marks_native_dispatch():
    """McpdClient.call should record every dispatch in the ring so the
    bridge can skip corresponding mcpd audit lines. Full McpdClient
    subprocess is heavy to spin up — this test constructs a minimal
    fake proc + verifies the ring gets marked."""
    from controller.mcpd_client import McpdClient

    # Build a McpdClient without actually calling __init__ (which
    # spawns the subprocess). We just need to test the call() method's
    # mark hook.
    client = object.__new__(McpdClient)
    client._closed = False
    # Minimal state so early guards pass:
    class _FakeProc:
        def poll(self):
            return None
        stdout = None
        stderr = None
        stdin = None
    client._proc = _FakeProc()
    client._lock = MagicMock()
    client._lock.__enter__ = lambda self: None
    client._lock.__exit__ = lambda self, *a: None
    client._default_timeout = 30.0

    # We'll fail after the mark (via the lock context manager returning
    # early). What we care about: the mark fired.
    assert bridge_mod._native_ring_size_for_tests() == 0

    try:
        client.call("fs.list", {"path": "/tmp"})
    except Exception:
        pass  # expected — no real mcpd subprocess

    # Mark fired before the lock code path failed
    assert bridge_mod._native_ring_size_for_tests() == 1
    assert bridge_mod.is_native_dispatch("fs.list", "/tmp") is True


def test_mcpd_client_call_mark_hook_never_raises():
    """PF-9-style: even if the mark hook itself fails (import error,
    ring corruption, whatever), it MUST NOT break the mcpd call.
    The try/except in call() wraps the whole hook."""
    from controller.mcpd_client import McpdClient

    client = object.__new__(McpdClient)
    client._closed = False
    class _FakeProc:
        def poll(self): return None
        stdout = None; stderr = None; stdin = None
    client._proc = _FakeProc()
    client._lock = MagicMock()
    client._lock.__enter__ = lambda self: None
    client._lock.__exit__ = lambda self, *a: None
    client._default_timeout = 30.0

    # Patch mark_native_dispatch to raise → verify call() doesn't die
    # from the exception (falls through to real dispatch failure).
    with patch.object(
        bridge_mod, "mark_native_dispatch",
        side_effect=RuntimeError("simulated ring corruption"),
    ):
        try:
            client.call("fs.list", {"path": "/tmp"})
        except Exception as exc:
            # Real mcpd will fail (no subprocess), but NOT with the
            # mark-hook's RuntimeError
            assert "simulated ring corruption" not in str(exc)


# ── 7. INV-8 semantics preserved — bridge write pattern unchanged ─────

def test_bridge_ingest_line_no_ring_pollution_across_test_isolation():
    """Autouse _clean_ring fixture MUST work — regression lock against
    tests that add entries + forget to clean, poisoning downstream tests."""
    assert bridge_mod._native_ring_size_for_tests() == 0
