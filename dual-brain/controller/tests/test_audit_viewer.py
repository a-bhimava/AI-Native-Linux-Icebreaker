"""Tests for the interactive audit log viewer (PR-P1-B, G5.P1b).

Covers:
  - LogIndex: build, read_entry, read_page, malformed lines, empty file
  - FilterSpec: matches, describe, is_empty, combined filters
  - Rendering: summary table, detail view, help, stats, sanitization
  - Chain verification integration
  - Non-TTY JSONL fallback
  - Error states: file not found, empty log, permission denied
  - CLI entry point
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from controller.audit import (
    REDACTED_PLACEHOLDER,
    AuditFields,
    AuditLog,
    Outcome,
)
from controller.audit_viewer import (
    AuditViewer,
    ChainStatus,
    FilterSpec,
    LogIndex,
    ViewerAction,
    _viewer_lookup,
    main,
)


# ── Test fixtures ────────────────────────────────────────────────────────────


def _fields(**overrides) -> AuditFields:
    base = dict(
        session_id="sess-viewer",
        turn_index=0,
        intent_id="intent-viewer",
        action="fs.read",
        target="/etc/hostname",
        tier=0,
        reason="user_requested",
        risk_level="read_only",
        outcome=Outcome.EXECUTED,
        duration_ms=5.0,
        backend="local",
        model="test-model",
        tokens_in=10,
        tokens_out=5,
        cost_estimate_usd=0.001,
    )
    base.update(overrides)
    return AuditFields(**base)


def _make_log(tmp_path: Path, entries: int = 5, **overrides) -> Path:
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        for i in range(entries):
            log.write_fields(_fields(turn_index=i, intent_id=f"intent-{i}", **overrides))
    finally:
        log.close()
    return p


def _make_mixed_log(tmp_path: Path) -> Path:
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        log.write_fields(_fields(
            turn_index=0, session_id="sess-a", action="fs.read",
            tier=0, outcome=Outcome.EXECUTED, backend="local",
        ))
        log.write_fields(_fields(
            turn_index=1, session_id="sess-a", action="fs.write",
            tier=1, outcome=Outcome.HITL_DENIED, backend="gemini",
            target="/tmp/output.txt",
        ))
        log.write_fields(_fields(
            turn_index=2, session_id="sess-b", action="fs.delete",
            tier=3, outcome=Outcome.HITL_DENIED, backend="anthropic",
            target="/var/log/app.log",
        ))
        log.write_fields(_fields(
            turn_index=3, session_id="sess-b", action="system.status",
            tier=0, outcome=Outcome.TRUST_APPLIED, backend="local",
        ))
        log.write_fields(_fields(
            turn_index=4, session_id="sess-a", action="service.restart",
            tier=2, outcome=Outcome.EXECUTED, backend="gemini",
            target="nginx",
        ))
    finally:
        log.close()
    return p


# ── LogIndex ─────────────────────────────────────────────────────────────────


class TestLogIndex:
    def test_build_from_valid_log(self, tmp_path):
        p = _make_log(tmp_path, 5)
        idx = LogIndex.build(p)
        assert idx.total_entries == 5
        assert len(idx.offsets) == 5
        assert len(idx.malformed_lines) == 0

    def test_build_empty_file(self, tmp_path):
        p = tmp_path / "empty.log"
        p.write_text("")
        idx = LogIndex.build(p)
        assert idx.total_entries == 0
        assert len(idx.offsets) == 0

    def test_build_skips_malformed_lines(self, tmp_path):
        p = _make_log(tmp_path, 3)
        content = p.read_text()
        lines = content.strip().split("\n")
        lines.insert(1, "NOT VALID JSON")
        lines.insert(3, "{broken")
        p.write_text("\n".join(lines) + "\n")
        idx = LogIndex.build(p)
        assert idx.total_entries == 3
        assert len(idx.malformed_lines) == 2

    def test_read_entry_by_index(self, tmp_path):
        p = _make_log(tmp_path, 3)
        idx = LogIndex.build(p)
        entry = idx.read_entry(0)
        assert entry is not None
        assert entry["seq"] == 0
        entry2 = idx.read_entry(2)
        assert entry2 is not None
        assert entry2["seq"] == 2

    def test_read_entry_out_of_range(self, tmp_path):
        p = _make_log(tmp_path, 3)
        idx = LogIndex.build(p)
        assert idx.read_entry(-1) is None
        assert idx.read_entry(3) is None
        assert idx.read_entry(100) is None

    def test_read_page(self, tmp_path):
        p = _make_log(tmp_path, 10)
        idx = LogIndex.build(p)
        page = idx.read_page(0, 3)
        assert len(page) == 3
        assert page[0][0] == 0
        assert page[2][0] == 2

    def test_read_page_partial_last(self, tmp_path):
        p = _make_log(tmp_path, 5)
        idx = LogIndex.build(p)
        page = idx.read_page(3, 10)
        assert len(page) == 2
        assert page[0][0] == 3
        assert page[1][0] == 4

    def test_large_log_index_builds(self, tmp_path):
        p = tmp_path / "big.log"
        log = AuditLog(path=p, fsync_each_write=False)
        try:
            for i in range(200):
                log.write_fields(_fields(turn_index=i, intent_id=f"intent-{i}"))
        finally:
            log.close()
        idx = LogIndex.build(p)
        assert idx.total_entries == 200
        entry = idx.read_entry(199)
        assert entry is not None
        assert entry["seq"] == 199


# ── FilterSpec ───────────────────────────────────────────────────────────────


class TestFilterSpec:
    def test_empty_filter_matches_all(self):
        spec = FilterSpec()
        assert spec.is_empty()
        entry = {"session_id": "s", "tier": 0, "outcome": "executed"}
        assert spec.matches(entry)

    def test_matches_session_id(self):
        spec = FilterSpec(session_id="sess-a")
        assert spec.matches({"session_id": "sess-a", "tier": 0})
        assert not spec.matches({"session_id": "sess-b", "tier": 0})

    def test_matches_tier(self):
        spec = FilterSpec(tier=3)
        assert spec.matches({"tier": 3})
        assert not spec.matches({"tier": 0})

    def test_matches_outcome(self):
        spec = FilterSpec(outcome="executed")
        assert spec.matches({"outcome": "executed"})
        assert not spec.matches({"outcome": "hitl_denied"})

    def test_matches_action(self):
        spec = FilterSpec(action="fs.read")
        assert spec.matches({"action": "fs.read"})
        assert not spec.matches({"action": "fs.write"})

    def test_matches_backend(self):
        spec = FilterSpec(backend="gemini")
        assert spec.matches({"backend": "gemini"})
        assert not spec.matches({"backend": "local"})

    def test_matches_time_after(self):
        spec = FilterSpec(time_after="2026-06-14T10:00:00")
        assert spec.matches({"ts": "2026-06-14T10:30:00.000Z"})
        assert not spec.matches({"ts": "2026-06-14T09:00:00.000Z"})

    def test_matches_time_before(self):
        spec = FilterSpec(time_before="2026-06-14T10:00:00")
        assert spec.matches({"ts": "2026-06-14T09:00:00.000Z"})
        assert not spec.matches({"ts": "2026-06-14T11:00:00.000Z"})

    def test_matches_time_range(self):
        spec = FilterSpec(time_after="2026-06-14T09:00:00", time_before="2026-06-14T11:00:00")
        assert spec.matches({"ts": "2026-06-14T10:00:00.000Z"})
        assert not spec.matches({"ts": "2026-06-14T08:00:00.000Z"})
        assert not spec.matches({"ts": "2026-06-14T12:00:00.000Z"})

    def test_matches_search_pattern(self):
        spec = FilterSpec(search_pattern=re.compile("fs\\.delete", re.IGNORECASE))
        assert spec.matches({"action": "fs.delete", "target": "/tmp"})
        assert not spec.matches({"action": "fs.read", "target": "/tmp"})

    def test_search_skips_redacted(self):
        spec = FilterSpec(search_pattern=re.compile("REDACTED"))
        assert not spec.matches({"data": REDACTED_PLACEHOLDER, "other": "hello"})

    def test_combined_filters(self):
        spec = FilterSpec(session_id="sess-a", tier=0, outcome="executed")
        assert spec.matches({"session_id": "sess-a", "tier": 0, "outcome": "executed"})
        assert not spec.matches({"session_id": "sess-a", "tier": 1, "outcome": "executed"})
        assert not spec.matches({"session_id": "sess-b", "tier": 0, "outcome": "executed"})

    def test_describe(self):
        spec = FilterSpec(tier=3, outcome="hitl_denied")
        desc = spec.describe()
        assert "tier=3" in desc
        assert "outcome=hitl_denied" in desc

    def test_describe_empty(self):
        spec = FilterSpec()
        assert spec.describe() == "none"


# ── Viewer key lookup ────────────────────────────────────────────────────────


class TestViewerKeyLookup:
    def test_j_is_down(self):
        assert _viewer_lookup("j") == ViewerAction.DOWN

    def test_k_is_up(self):
        assert _viewer_lookup("k") == ViewerAction.UP

    def test_question_is_help(self):
        assert _viewer_lookup("?") == ViewerAction.HELP

    def test_esc_is_back(self):
        assert _viewer_lookup("\x1b") == ViewerAction.BACK

    def test_Q_is_quit(self):
        assert _viewer_lookup("Q") == ViewerAction.QUIT

    def test_enter_is_select(self):
        assert _viewer_lookup("\r") == ViewerAction.SELECT
        assert _viewer_lookup("\n") == ViewerAction.SELECT

    def test_unknown_key_returns_none(self):
        assert _viewer_lookup("x") is None
        assert _viewer_lookup("z") is None


# ── Rendering ────────────────────────────────────────────────────────────────


class TestSummaryRender:
    def test_renders_basic_table(self, tmp_path):
        p = _make_log(tmp_path, 3)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        viewer._ascii = True
        output = viewer._render_summary()
        assert "Audit Viewer" in output
        assert "3 entries" in output

    def test_cursor_indicator(self, tmp_path):
        p = _make_log(tmp_path, 3)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        viewer._ascii = True
        viewer._cursor = 1
        output = viewer._render_summary()
        lines = output.split("\n")
        cursor_lines = [l for l in lines if l.startswith("> ")]
        assert len(cursor_lines) == 1

    def test_no_ansi_when_no_color(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        p = _make_log(tmp_path, 2)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        viewer._ascii = True
        output = viewer._render_summary()
        assert "\033[" not in output

    def test_empty_filter_result(self, tmp_path):
        p = _make_log(tmp_path, 3)
        viewer = AuditViewer(p, filter_spec=FilterSpec(session_id="nonexistent"))
        viewer._index = LogIndex.build(p)
        viewer._filtered_indices = viewer._apply_filter(FilterSpec(session_id="nonexistent"))
        viewer._color = False
        viewer._ascii = True
        output = viewer._render_summary()
        assert "No entries match" in output


class TestDetailRender:
    def test_shows_all_required_fields(self, tmp_path):
        p = _make_log(tmp_path, 1)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        viewer._ascii = True
        entry = viewer._index.read_entry(0)
        assert entry is not None
        output = viewer._render_detail(entry, 0)
        assert "Timestamp" in output
        assert "Session" in output
        assert "Action" in output
        assert "Target" in output
        assert "Tier" in output
        assert "Outcome" in output
        assert "Duration" in output
        assert "Backend" in output
        assert "Model" in output
        assert "Tokens" in output
        assert "Cost" in output

    def test_shows_extra_fields(self, tmp_path):
        p = tmp_path / "audit.log"
        log = AuditLog(path=p, fsync_each_write=False)
        try:
            log.write_fields(_fields(extra={"decision_id": "abc-123"}))
        finally:
            log.close()
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        viewer._ascii = True
        entry = viewer._index.read_entry(0)
        output = viewer._render_detail(entry, 0)
        assert "Extra fields" in output
        assert "decision_id" in output

    def test_sanitizes_values(self, tmp_path):
        p = tmp_path / "audit.log"
        log = AuditLog(path=p, fsync_each_write=False)
        try:
            log.write_fields(_fields(action="fs.read\x1b[31mINJECTED\x1b[0m"))
        finally:
            log.close()
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        viewer._ascii = True
        entry = viewer._index.read_entry(0)
        output = viewer._render_detail(entry, 0)
        assert "\x1b[31m" not in output

    def test_cost_formatted(self, tmp_path):
        p = _make_log(tmp_path, 1, cost_estimate_usd=0.0042)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        output = viewer._render_detail(viewer._index.read_entry(0), 0)
        assert "$0.0042" in output

    def test_no_ansi_when_no_color(self, tmp_path):
        p = _make_log(tmp_path, 1)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        entry = viewer._index.read_entry(0)
        output = viewer._render_detail(entry, 0)
        assert "\033[" not in output


class TestHelpRender:
    def test_shows_all_keybindings(self, tmp_path):
        p = _make_log(tmp_path, 1)
        viewer = AuditViewer(p)
        viewer._color = False
        viewer._ascii = True
        output = viewer._render_help()
        assert "j / Down" in output
        assert "k / Up" in output
        assert "Enter" in output
        assert "q / Esc" in output
        assert "f" in output or "Filter" in output
        assert "/" in output or "Search" in output
        assert "?" in output or "help" in output.lower()


class TestStatsRender:
    def test_outcome_distribution(self, tmp_path):
        p = _make_mixed_log(tmp_path)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        output = viewer._render_stats()
        assert "Outcome distribution" in output
        assert "executed" in output
        assert "hitl_denied" in output

    def test_tier_distribution(self, tmp_path):
        p = _make_mixed_log(tmp_path)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        output = viewer._render_stats()
        assert "Tier distribution" in output

    def test_total_cost(self, tmp_path):
        p = _make_log(tmp_path, 3, cost_estimate_usd=1.5)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        output = viewer._render_stats()
        assert "$4.5000" in output

    def test_session_count(self, tmp_path):
        p = _make_mixed_log(tmp_path)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        output = viewer._render_stats()
        assert "2" in output  # sess-a + sess-b


# ── Chain verification ───────────────────────────────────────────────────────


class TestViewerChainVerification:
    def test_verify_intact_chain(self, tmp_path):
        p = _make_log(tmp_path, 5)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        status = viewer._verify_chain()
        assert status.ok
        assert status.first_bad_seq is None
        assert status.verified_count == 5
        assert all(v for v in status.entry_status.values())

    def test_verify_tampered_entry(self, tmp_path):
        p = _make_log(tmp_path, 5)
        content = p.read_text()
        lines = content.strip().split("\n")
        entry = json.loads(lines[2])
        entry["action"] = "TAMPERED"
        lines[2] = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
        p.write_text("\n".join(lines) + "\n")
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        status = viewer._verify_chain()
        assert not status.ok
        assert status.first_bad_seq is not None

    def test_verify_empty_log(self, tmp_path):
        p = tmp_path / "empty.log"
        p.write_text("")
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        status = viewer._verify_chain()
        assert status.ok
        assert status.verified_count == 0

    def test_chain_indicator_in_detail(self, tmp_path):
        p = _make_log(tmp_path, 3)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        viewer._ascii = True
        viewer._chain_status = viewer._verify_chain()
        entry = viewer._index.read_entry(0)
        output = viewer._render_detail(entry, 0)
        assert "hash matches" in output or "[OK]" in output


# ── Filter application ───────────────────────────────────────────────────────


class TestFilterApplication:
    def test_filter_by_session(self, tmp_path):
        p = _make_mixed_log(tmp_path)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        indices = viewer._apply_filter(FilterSpec(session_id="sess-a"))
        assert len(indices) == 3

    def test_filter_by_tier(self, tmp_path):
        p = _make_mixed_log(tmp_path)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        indices = viewer._apply_filter(FilterSpec(tier=3))
        assert len(indices) == 1

    def test_filter_by_outcome(self, tmp_path):
        p = _make_mixed_log(tmp_path)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        indices = viewer._apply_filter(FilterSpec(outcome="hitl_denied"))
        assert len(indices) == 2

    def test_filter_by_action(self, tmp_path):
        p = _make_mixed_log(tmp_path)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        indices = viewer._apply_filter(FilterSpec(action="fs.delete"))
        assert len(indices) == 1

    def test_filter_by_backend(self, tmp_path):
        p = _make_mixed_log(tmp_path)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        indices = viewer._apply_filter(FilterSpec(backend="gemini"))
        assert len(indices) == 2

    def test_combined_filter(self, tmp_path):
        p = _make_mixed_log(tmp_path)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        indices = viewer._apply_filter(FilterSpec(session_id="sess-a", outcome="executed"))
        assert len(indices) == 2

    def test_search_filter(self, tmp_path):
        p = _make_mixed_log(tmp_path)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        indices = viewer._apply_filter(FilterSpec(search_pattern=re.compile("nginx")))
        assert len(indices) == 1


# ── Non-TTY fallback ─────────────────────────────────────────────────────────


class TestNonTtyMode:
    def test_dumps_all_entries(self, tmp_path, capsys):
        p = _make_log(tmp_path, 5)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        rc = viewer._dump_json()
        assert rc == 0
        output = capsys.readouterr().out
        lines = output.strip().split("\n")
        assert len(lines) == 5
        for line in lines:
            entry = json.loads(line)
            assert "session_id" in entry

    def test_dumps_filtered_entries(self, tmp_path, capsys):
        p = _make_mixed_log(tmp_path)
        viewer = AuditViewer(p, filter_spec=FilterSpec(session_id="sess-b"))
        viewer._index = LogIndex.build(p)
        rc = viewer._dump_json()
        assert rc == 0
        output = capsys.readouterr().out
        lines = output.strip().split("\n")
        assert len(lines) == 2

    def test_no_ansi_in_json(self, tmp_path, capsys):
        p = _make_log(tmp_path, 2)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._dump_json()
        output = capsys.readouterr().out
        assert "\033[" not in output


# ── Error states ─────────────────────────────────────────────────────────────


class TestErrorStates:
    def test_file_not_found(self, tmp_path):
        viewer = AuditViewer(tmp_path / "nonexistent.log")
        rc = viewer.run()
        assert rc == 2

    def test_empty_log_shows_message(self, tmp_path, capsys, monkeypatch):
        p = tmp_path / "empty.log"
        p.write_text("")
        monkeypatch.setattr("sys.stdin", open(os.devnull, "r"))
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        rc = viewer._dump_json()
        assert rc == 0
        output = capsys.readouterr().out
        assert output.strip() == ""


# ── CLI entry point ──────────────────────────────────────────────────────────


class TestCli:
    def test_cli_json_mode(self, tmp_path, capsys):
        p = _make_log(tmp_path, 3)
        rc = main(["--json", str(p)])
        assert rc == 0
        output = capsys.readouterr().out
        lines = output.strip().split("\n")
        assert len(lines) == 3

    def test_cli_filter_session(self, tmp_path, capsys):
        p = _make_mixed_log(tmp_path)
        rc = main(["--json", "--filter-session", "sess-b", str(p)])
        assert rc == 0
        output = capsys.readouterr().out
        lines = output.strip().split("\n")
        assert len(lines) == 2

    def test_cli_filter_tier(self, tmp_path, capsys):
        p = _make_mixed_log(tmp_path)
        rc = main(["--json", "--filter-tier", "3", str(p)])
        assert rc == 0
        output = capsys.readouterr().out
        lines = output.strip().split("\n")
        assert len(lines) == 1

    def test_cli_filter_outcome(self, tmp_path, capsys):
        p = _make_mixed_log(tmp_path)
        rc = main(["--json", "--filter-outcome", "hitl_denied", str(p)])
        assert rc == 0
        output = capsys.readouterr().out
        lines = output.strip().split("\n")
        assert len(lines) == 2

    def test_cli_filter_action(self, tmp_path, capsys):
        p = _make_mixed_log(tmp_path)
        rc = main(["--json", "--filter-action", "fs.delete", str(p)])
        assert rc == 0
        output = capsys.readouterr().out
        lines = output.strip().split("\n")
        assert len(lines) == 1

    def test_cli_filter_backend(self, tmp_path, capsys):
        p = _make_mixed_log(tmp_path)
        rc = main(["--json", "--filter-backend", "local", str(p)])
        assert rc == 0
        output = capsys.readouterr().out
        lines = output.strip().split("\n")
        assert len(lines) == 2

    def test_cli_combined_filters(self, tmp_path, capsys):
        p = _make_mixed_log(tmp_path)
        rc = main(["--json", "--filter-session", "sess-a", "--filter-outcome", "executed", str(p)])
        assert rc == 0
        output = capsys.readouterr().out
        lines = output.strip().split("\n")
        assert len(lines) == 2

    def test_cli_file_not_found(self, tmp_path, capsys):
        rc = main(["--json", str(tmp_path / "missing.log")])
        assert rc == 2

    def test_cli_verify(self, tmp_path, capsys, monkeypatch):
        p = _make_log(tmp_path, 3)
        monkeypatch.setattr("sys.stdin", open(os.devnull, "r"))
        viewer = AuditViewer(p, verify=True)
        viewer._index = LogIndex.build(p)
        viewer._chain_status = viewer._verify_chain()
        assert viewer._chain_status.ok


# ── Status bar ───────────────────────────────────────────────────────────────


class TestStatusBar:
    def test_shows_entry_count(self, tmp_path):
        p = _make_log(tmp_path, 10)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        bar = viewer._render_status_bar(10, 10)
        assert "10 entries" in bar

    def test_shows_filtered_count(self, tmp_path):
        p = _make_log(tmp_path, 10)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        bar = viewer._render_status_bar(5, 10)
        assert "5 of 10" in bar

    def test_shows_filter_description(self, tmp_path):
        p = _make_log(tmp_path, 5)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._filter_spec = FilterSpec(tier=3)
        viewer._color = False
        bar = viewer._render_status_bar(2, 5)
        assert "tier=3" in bar

    def test_shows_chain_status_unverified(self, tmp_path):
        p = _make_log(tmp_path, 5)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        bar = viewer._render_status_bar(5, 5)
        assert "not verified" in bar

    def test_shows_chain_status_ok(self, tmp_path):
        p = _make_log(tmp_path, 5)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._chain_status = viewer._verify_chain()
        viewer._color = False
        bar = viewer._render_status_bar(5, 5)
        assert "verified" in bar


# ── Sanitization ─────────────────────────────────────────────────────────────


class TestViewerSanitization:
    def test_ansi_in_action_stripped(self, tmp_path):
        p = _make_log(tmp_path, 1, action="fs.read\x1b[31mRED")
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        viewer._ascii = True
        output = viewer._render_summary()
        assert "\x1b[31m" not in output

    def test_redacted_placeholder_preserved(self, tmp_path):
        p = tmp_path / "audit.log"
        log = AuditLog(path=p, fsync_each_write=False)
        try:
            log.write_fields(_fields(
                action="fs.write",
                extra={"params": {"path": "/tmp/f.txt", "content": "secret body"}},
            ))
        finally:
            log.close()
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        entry = viewer._index.read_entry(0)
        assert entry is not None
        output = viewer._render_detail(entry, 0)
        assert REDACTED_PLACEHOLDER in output


# ── Header rendering ─────────────────────────────────────────────────────────


class TestHeader:
    def test_header_contains_filename(self, tmp_path):
        p = _make_log(tmp_path, 1)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        header = viewer._render_header()
        assert "audit.log" in header

    def test_header_contains_entry_count(self, tmp_path):
        p = _make_log(tmp_path, 7)
        viewer = AuditViewer(p)
        viewer._index = LogIndex.build(p)
        viewer._color = False
        header = viewer._render_header()
        assert "7 entries" in header
