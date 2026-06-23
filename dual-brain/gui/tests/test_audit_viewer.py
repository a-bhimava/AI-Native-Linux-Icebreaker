"""Tests for gui.audit — Audit Log Viewer (M6UI.6).

Headless tests covering:
  - Entry parsing from JSONL
  - Filter logic (tier, outcome, search text)
  - Timestamp formatting
  - Hash-chain verification
  - CSV export formatting
  - Display field requirements
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("gi", MagicMock())
sys.modules.setdefault("gi.repository", MagicMock())

from gui.audit.window import (
    OUTCOME_FILTER_OPTIONS,
    REQUIRED_DISPLAY_FIELDS,
    TIER_FILTER_OPTIONS,
    entries_to_csv,
    entry_matches_filter,
    format_timestamp,
    parse_entries,
    verify_chain,
)


def _make_entry(
    action: str = "fs.read",
    target: str = "/etc/hostname",
    tier: int = 0,
    outcome: str = "executed",
    seq: Optional[int] = None,
    prev_hash: str = "GENESIS",
    **extra: object,
) -> dict:
    entry = {
        "ts": "2026-06-23T14:30:45.123Z",
        "session_id": "test-session",
        "turn_index": 0,
        "intent_id": "test-intent",
        "action": action,
        "target": target,
        "tier": tier,
        "reason": "user_requested",
        "risk_level": "read_only",
        "outcome": outcome,
        "duration_ms": 12.5,
        "user": "aditya",
        "backend": "local",
        "model": "phi-4",
        "tokens_in": 10,
        "tokens_out": 5,
        "cost_estimate_usd": 0.0,
    }
    if seq is not None:
        entry["seq"] = seq
        entry["prev_hash"] = prev_hash
    entry.update(extra)
    return entry


def _make_chained_entries(n: int) -> list[dict]:
    entries = []
    prev_hash = "GENESIS"
    for i in range(n):
        entry = _make_entry(
            action=f"fs.read.{i}",
            seq=i,
            prev_hash=prev_hash,
        )
        canonical = json.dumps(
            entry, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        prev_hash = hashlib.sha256(canonical).hexdigest()
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Entry parsing
# ---------------------------------------------------------------------------

class TestParseEntries:
    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "audit.log"
        p.write_text("")
        assert parse_entries(p) == []

    def test_missing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "nonexistent.log"
        assert parse_entries(p) == []

    def test_single_entry(self, tmp_path: Path) -> None:
        entry = _make_entry()
        p = tmp_path / "audit.log"
        p.write_text(json.dumps(entry) + "\n")
        result = parse_entries(p)
        assert len(result) == 1
        assert result[0]["action"] == "fs.read"

    def test_multiple_entries(self, tmp_path: Path) -> None:
        entries = [_make_entry(action=f"fs.read.{i}") for i in range(5)]
        p = tmp_path / "audit.log"
        p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = parse_entries(p)
        assert len(result) == 5

    def test_skips_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "audit.log"
        p.write_text(json.dumps(_make_entry()) + "\nNOT_JSON\n" + json.dumps(_make_entry()))
        result = parse_entries(p)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Filter logic
# ---------------------------------------------------------------------------

class TestEntryFilter:
    def test_no_filter_matches_all(self) -> None:
        entry = _make_entry()
        assert entry_matches_filter(entry, None, None, "") is True

    def test_tier_filter_match(self) -> None:
        entry = _make_entry(tier=2)
        assert entry_matches_filter(entry, 2, None, "") is True

    def test_tier_filter_mismatch(self) -> None:
        entry = _make_entry(tier=0)
        assert entry_matches_filter(entry, 2, None, "") is False

    def test_outcome_filter_match(self) -> None:
        entry = _make_entry(outcome="hitl_denied")
        assert entry_matches_filter(entry, None, "hitl_denied", "") is True

    def test_outcome_filter_mismatch(self) -> None:
        entry = _make_entry(outcome="executed")
        assert entry_matches_filter(entry, None, "hitl_denied", "") is False

    def test_search_by_action(self) -> None:
        entry = _make_entry(action="fs.write")
        assert entry_matches_filter(entry, None, None, "fs.write") is True

    def test_search_by_target(self) -> None:
        entry = _make_entry(target="/etc/hosts")
        assert entry_matches_filter(entry, None, None, "hosts") is True

    def test_search_case_insensitive(self) -> None:
        entry = _make_entry(action="FS.WRITE")
        assert entry_matches_filter(entry, None, None, "fs.write") is True

    def test_search_no_match(self) -> None:
        entry = _make_entry(action="fs.read")
        assert entry_matches_filter(entry, None, None, "pkg.install") is False

    def test_combined_filters(self) -> None:
        entry = _make_entry(tier=3, outcome="hitl_denied", action="fs.write")
        assert entry_matches_filter(entry, 3, "hitl_denied", "fs.write") is True

    def test_combined_tier_mismatch(self) -> None:
        entry = _make_entry(tier=1, outcome="hitl_denied")
        assert entry_matches_filter(entry, 3, "hitl_denied", "") is False


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------

class TestTimestampFormat:
    def test_iso_format(self) -> None:
        assert format_timestamp("2026-06-23T14:30:45.123Z") == "06-23 14:30"

    def test_short_timestamp(self) -> None:
        assert format_timestamp("2026-06") == "2026-06"

    def test_empty_string(self) -> None:
        assert format_timestamp("") == ""


# ---------------------------------------------------------------------------
# Hash-chain verification
# ---------------------------------------------------------------------------

class TestVerifyChain:
    def test_empty_chain(self) -> None:
        ok, bad = verify_chain([])
        assert ok is True
        assert bad is None

    def test_single_valid_entry(self) -> None:
        entries = _make_chained_entries(1)
        ok, bad = verify_chain(entries)
        assert ok is True

    def test_multiple_valid_entries(self) -> None:
        entries = _make_chained_entries(10)
        ok, bad = verify_chain(entries)
        assert ok is True

    def test_broken_prev_hash(self) -> None:
        entries = _make_chained_entries(5)
        entries[3]["prev_hash"] = "tampered"
        ok, bad = verify_chain(entries)
        assert ok is False
        assert bad == 3

    def test_missing_seq(self) -> None:
        entries = _make_chained_entries(3)
        del entries[1]["seq"]
        ok, bad = verify_chain(entries)
        assert ok is False
        assert bad == 1

    def test_wrong_seq_order(self) -> None:
        entries = _make_chained_entries(3)
        entries[1]["seq"] = 5
        ok, bad = verify_chain(entries)
        assert ok is False
        assert bad == 1


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

class TestCSVExport:
    def test_empty_returns_empty(self) -> None:
        assert entries_to_csv([]) == ""

    def test_single_entry_has_header(self) -> None:
        result = entries_to_csv([_make_entry()])
        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert "action" in lines[0]

    def test_multiple_entries(self) -> None:
        result = entries_to_csv([_make_entry(), _make_entry(action="fs.write")])
        lines = result.strip().split("\n")
        assert len(lines) == 3


# ---------------------------------------------------------------------------
# Filter options
# ---------------------------------------------------------------------------

class TestFilterOptions:
    def test_tier_options_has_all(self) -> None:
        assert TIER_FILTER_OPTIONS[0] == "All"

    def test_tier_options_count(self) -> None:
        assert len(TIER_FILTER_OPTIONS) == 5

    def test_outcome_options_has_all(self) -> None:
        assert OUTCOME_FILTER_OPTIONS[0] == "All"

    def test_outcome_includes_executed(self) -> None:
        assert "executed" in OUTCOME_FILTER_OPTIONS

    def test_outcome_includes_denied(self) -> None:
        assert "hitl_denied" in OUTCOME_FILTER_OPTIONS


# ---------------------------------------------------------------------------
# Required display fields
# ---------------------------------------------------------------------------

class TestDisplayFields:
    def test_has_timestamp(self) -> None:
        assert "ts" in REQUIRED_DISPLAY_FIELDS

    def test_has_action(self) -> None:
        assert "action" in REQUIRED_DISPLAY_FIELDS

    def test_has_tier(self) -> None:
        assert "tier" in REQUIRED_DISPLAY_FIELDS

    def test_has_outcome(self) -> None:
        assert "outcome" in REQUIRED_DISPLAY_FIELDS
