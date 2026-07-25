"""Tests for controller.oc_audit_bridge — v6.13_OC INV-8 audit enrichment.

Follows the per-fix plan (docs at ~/.claude/plans/…, 2026-07-25):
enrichment purity + bridge lifecycle + hash-chain preservation + R18
"never drop audit data" discipline.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from controller.audit import AuditLog, Outcome
from controller.oc_audit_bridge import (
    OC_SESSION_SENTINEL,
    OCAuditBridge,
    enrich_mcpd_entry,
)


# ── enrichment purity tests ───────────────────────────────────────────


def _mcpd_line(**overrides):
    base = {
        "timestamp": "2026-07-25T05:00:00Z",
        "method": "fs.read",
        "request_id": 1,
        "params_redacted": {"path": "/etc/hostname"},
        "result_class": "ok",
        "latency_us": 1234,
    }
    base.update(overrides)
    return base


def test_enrich_ok_result_maps_to_executed():
    entry = _mcpd_line(result_class="ok")
    fields = enrich_mcpd_entry(entry)
    assert fields.outcome == Outcome.EXECUTED
    assert fields.session_id == OC_SESSION_SENTINEL
    assert fields.backend == "opencode_oc"
    assert fields.action == "fs.read"
    assert fields.duration_ms == pytest.approx(1.234, rel=1e-6)


def test_enrich_err_result_maps_to_tool_error():
    entry = _mcpd_line(result_class="err")
    fields = enrich_mcpd_entry(entry)
    assert fields.outcome == Outcome.TOOL_ERROR


def test_enrich_schema_error_maps_to_schema_rejected():
    entry = _mcpd_line(result_class="schema_error")
    fields = enrich_mcpd_entry(entry)
    assert fields.outcome == Outcome.SCHEMA_REJECTED


def test_enrich_extracts_target_from_path_param():
    entry = _mcpd_line(method="fs.read", params_redacted={"path": "/etc/hostname"})
    fields = enrich_mcpd_entry(entry)
    assert fields.target == "/etc/hostname"


def test_enrich_extracts_target_from_unit_param():
    entry = _mcpd_line(
        method="service.logs", params_redacted={"unit": "nginx.service", "lines": 20},
    )
    fields = enrich_mcpd_entry(entry)
    assert fields.target == "nginx.service"
    assert fields.tier == 0  # service.logs is read-only


def test_enrich_unknown_tool_defaults_tier_zero_and_marks_extra():
    entry = _mcpd_line(method="custom.manifest.tool", params_redacted={})
    fields = enrich_mcpd_entry(entry)
    assert fields.tier == 0
    assert fields.extra is not None
    assert fields.extra.get("tier_source") == "default_unknown_tool"


def test_enrich_missing_fields_fills_sentinels_never_raises():
    # Only method + result_class present — everything else absent.
    entry = {"method": "fs.list", "result_class": "ok"}
    fields = enrich_mcpd_entry(entry)  # must not raise
    assert fields.action == "fs.list"
    assert fields.target == ""  # no params → empty target
    assert fields.duration_ms == 0.0
    assert fields.outcome == Outcome.EXECUTED


def test_enrich_tier_2_tool_gets_medium_risk():
    entry = _mcpd_line(method="package.install", params_redacted={"package": "htop"})
    fields = enrich_mcpd_entry(entry)
    assert fields.tier == 2
    assert fields.risk_level == "medium"
    assert fields.target == "htop"


# ── bridge lifecycle tests ────────────────────────────────────────────


@pytest.fixture
def bridge_tmp(tmp_path):
    """Isolated temp dirs for the mcpd log + INV-8 log per test."""
    mcpd_log = tmp_path / "mcpd" / "audit.log"
    inv8_log = tmp_path / "inv8" / "controller-audit.log"
    return {
        "mcpd_log": mcpd_log,
        "inv8_log": inv8_log,
    }


def _write_mcpd_line(path: Path, **overrides):
    """Append one mcpd audit line to path (creating parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = _mcpd_line(**overrides)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _drain_bridge(bridge, expected_lines: int, timeout: float = 5.0):
    """Poll until the bridge has ingested `expected_lines` (or timeout).
    watchdog is asynchronous — tests can't assume immediate ingestion."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bridge.stats().lines_ingested >= expected_lines:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"bridge ingested only {bridge.stats().lines_ingested}/{expected_lines} "
        f"within {timeout}s"
    )


def test_bridge_ingests_new_lines_from_growing_file(bridge_tmp):
    """Append lines after start(); verify they land in INV-8 with chain intact."""
    audit = AuditLog(bridge_tmp["inv8_log"], fsync_each_write=False)
    bridge = OCAuditBridge(
        audit_log=audit,
        mcpd_audit_path=bridge_tmp["mcpd_log"],
    )
    try:
        bridge.start()
        # Append 3 lines after start.
        _write_mcpd_line(bridge_tmp["mcpd_log"], method="fs.read")
        _write_mcpd_line(bridge_tmp["mcpd_log"], method="fs.list")
        _write_mcpd_line(bridge_tmp["mcpd_log"], method="system.status")
        _drain_bridge(bridge, expected_lines=3)
    finally:
        bridge.stop()
        audit.close()

    # Verify hash chain intact.
    ok, first_bad_seq = AuditLog.verify_chain(bridge_tmp["inv8_log"])
    assert ok, f"chain broken at seq {first_bad_seq}"

    # Verify the entries are our OC-bridged ones.
    lines = bridge_tmp["inv8_log"].read_text().strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        entry = json.loads(line)
        assert entry["session_id"] == OC_SESSION_SENTINEL
        assert entry["backend"] == "opencode_oc"


def test_bridge_survives_malformed_line_and_continues(bridge_tmp):
    """A garbage line should increment lines_dropped_malformed but the
    next valid line still lands."""
    audit = AuditLog(bridge_tmp["inv8_log"], fsync_each_write=False)
    bridge = OCAuditBridge(
        audit_log=audit,
        mcpd_audit_path=bridge_tmp["mcpd_log"],
    )
    try:
        bridge.start()
        # Manually append a malformed line + a valid line.
        bridge_tmp["mcpd_log"].parent.mkdir(parents=True, exist_ok=True)
        with open(bridge_tmp["mcpd_log"], "a") as f:
            f.write("{this is not json\n")
            f.write(json.dumps(_mcpd_line(method="fs.stat")) + "\n")
        _drain_bridge(bridge, expected_lines=1)
    finally:
        bridge.stop()
        audit.close()

    assert bridge.stats().lines_dropped_malformed == 1
    assert bridge.stats().lines_ingested == 1


def test_bridge_survives_log_rotation(bridge_tmp):
    """After the mcpd log is moved out (rotation), the bridge should
    reset offset and pick up the new file on next on_created."""
    audit = AuditLog(bridge_tmp["inv8_log"], fsync_each_write=False)
    bridge = OCAuditBridge(
        audit_log=audit,
        mcpd_audit_path=bridge_tmp["mcpd_log"],
    )
    try:
        bridge.start()
        _write_mcpd_line(bridge_tmp["mcpd_log"], method="fs.read")
        _drain_bridge(bridge, expected_lines=1)

        # Rotate: move the current file out, then write a new one.
        rotated = bridge_tmp["mcpd_log"].with_suffix(".log.1")
        bridge_tmp["mcpd_log"].rename(rotated)
        _write_mcpd_line(bridge_tmp["mcpd_log"], method="fs.list")

        _drain_bridge(bridge, expected_lines=2)
    finally:
        bridge.stop()
        audit.close()

    assert bridge.stats().lines_ingested == 2


def test_bridge_stats_counts_updates_correctly(bridge_tmp):
    """After ingesting N lines the counter reports N. Also sanity-checks
    that stats() returns a live view, not a stale copy."""
    audit = AuditLog(bridge_tmp["inv8_log"], fsync_each_write=False)
    bridge = OCAuditBridge(
        audit_log=audit, mcpd_audit_path=bridge_tmp["mcpd_log"],
    )
    try:
        assert bridge.stats().lines_ingested == 0
        bridge.start()
        for i in range(5):
            _write_mcpd_line(bridge_tmp["mcpd_log"], request_id=i)
        _drain_bridge(bridge, expected_lines=5)
        assert bridge.stats().lines_ingested == 5
        assert bridge.stats().write_errors == 0
    finally:
        bridge.stop()
        audit.close()


def test_bridge_stop_is_idempotent(bridge_tmp):
    """Calling stop() twice must not raise."""
    audit = AuditLog(bridge_tmp["inv8_log"], fsync_each_write=False)
    bridge = OCAuditBridge(
        audit_log=audit, mcpd_audit_path=bridge_tmp["mcpd_log"],
    )
    bridge.start()
    bridge.stop()
    bridge.stop()  # no exception
    audit.close()


def test_bridge_start_is_idempotent(bridge_tmp):
    """Calling start() twice creates only one observer."""
    audit = AuditLog(bridge_tmp["inv8_log"], fsync_each_write=False)
    bridge = OCAuditBridge(
        audit_log=audit, mcpd_audit_path=bridge_tmp["mcpd_log"],
    )
    try:
        bridge.start()
        first_started_at = bridge.stats().started_at
        bridge.start()  # no-op
        assert bridge.stats().started_at == first_started_at
    finally:
        bridge.stop()
        audit.close()
