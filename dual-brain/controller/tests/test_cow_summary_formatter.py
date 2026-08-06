"""Tests for dual-brain/controller/cow_summary.py::format_diff.

M7.0.1d (v6.16) — asserts the HITL-modal text matches the shape the
whitepaper §8.2 promises + the terraform-plan compact summary line that
users are trained on.

Regression lock for audit rows B-2 (Tier-3 gate carries no diff) and
C-1 (COW stub) at the presentation layer.
"""
from __future__ import annotations

import pytest

from controller.cow_summary import (
    _human_bytes,
    _sanitize,
    format_diff,
)


# ── _human_bytes ─────────────────────────────────────────────────────────────

def test_human_bytes_covers_all_units():
    assert _human_bytes(0) == "0 bytes"
    assert _human_bytes(1) == "1 byte"
    assert _human_bytes(1023) == "1023 bytes"
    assert _human_bytes(1024) == "1.0 KB"
    assert _human_bytes(1024 * 1024) == "1.0 MB"
    assert _human_bytes(3_435_973_836) == "3.2 GB"
    assert _human_bytes(2 * 1024 ** 4) == "2.0 TB"


def test_human_bytes_handles_signed_input():
    """Formatter takes abs value so a negative bytes_delta pretty-prints."""
    assert _human_bytes(-3_435_973_836) == "3.2 GB"


# ── _sanitize ─────────────────────────────────────────────────────────────────

def test_sanitize_strips_ansi():
    # \x1b[31m red \x1b[0m
    dirty = "\x1b[31mHello\x1b[0m"
    assert _sanitize(dirty) == "Hello"


def test_sanitize_strips_c0_c1():
    dirty = "hello\x00world\x08bye"
    assert _sanitize(dirty) == "helloworldbye"


def test_sanitize_normalizes_crlf():
    assert _sanitize("line1\r\nline2\rline3") == "line1\nline2\nline3"


def test_sanitize_preserves_tab_and_newline():
    assert _sanitize("a\tb\nc") == "a\tb\nc"


def test_sanitize_non_string_returns_empty():
    assert _sanitize(None) == ""
    assert _sanitize(42) == ""
    assert _sanitize({"x": 1}) == ""


# ── format_diff — happy path ─────────────────────────────────────────────────

def test_format_diff_fs_delete_matches_whitepaper_shape():
    """The audit's flagship departure (C-1 + B-2): render fs.delete diff."""
    diff = {
        "operation": "fs.delete",
        "bytes_delta": -3_435_973_836,
        "file_count_delta": -847,
        "affected_paths_sample": [
            "/var/cache/apt/archives/foo.deb",
            "/var/cache/apt/archives/bar.deb",
        ],
        "human_summary": "3.2 GB will be freed. 847 files will be deleted.",
        "risk": "LOW",
        "reversible": False,
    }
    text = format_diff(diff)
    assert text is not None
    # Line 1: whitepaper §8.2 phrasing verbatim from mcpd.
    assert "3.2 GB will be freed. 847 files will be deleted." in text
    # Line 2: terraform-plan compact summary.
    assert "Change: 847 to delete, 3.2 GB freed." in text
    # Line 3: Risk + reversibility.
    assert "Risk: LOW — irreversible." in text
    # Affected block with truncation footer (2 shown, 847 total → 845 more).
    assert "Affected (sample):" in text
    assert "  - /var/cache/apt/archives/foo.deb" in text
    assert "  - ... (845 more)" in text


def test_format_diff_fs_write_new_file():
    diff = {
        "operation": "fs.write",
        "bytes_delta": 42,
        "file_count_delta": 1,
        "affected_paths_sample": ["/etc/hosts.test"],
        "human_summary": "42 bytes will be written to /etc/hosts.test (new file).",
        "risk": "MED",
        "reversible": True,
    }
    text = format_diff(diff)
    assert text is not None
    assert "42 bytes" in text
    assert "Change: 1 to add, 42 bytes added." in text
    assert "Risk: MED — reversible." in text


def test_format_diff_fs_write_shrink():
    diff = {
        "operation": "fs.write",
        "bytes_delta": -100,
        "file_count_delta": 0,
        "affected_paths_sample": ["/etc/some-file"],
        "human_summary": "100 bytes will be written (shrink by 900 bytes).",
        "risk": "MED",
        "reversible": True,
    }
    text = format_diff(diff)
    assert text is not None
    assert "Change: 1 to modify, 100 bytes freed." in text


def test_format_diff_package_install():
    diff = {
        "operation": "package.install",
        "bytes_delta": 0,
        "file_count_delta": 4,
        "affected_paths_sample": [],
        "human_summary": "4 packages will be installed (1 will be upgraded).",
        "risk": "MED",
        "reversible": True,
    }
    text = format_diff(diff)
    assert text is not None
    assert "4 packages will be installed" in text
    # bytes_delta = 0 → no ", X added" suffix
    assert "Change: 4 to install." in text
    assert "Risk: MED — reversible." in text
    # No affected block for package.* (sample is empty).
    assert "Affected" not in text


def test_format_diff_package_remove():
    diff = {
        "operation": "package.remove",
        "bytes_delta": 0,
        "file_count_delta": 2,
        "affected_paths_sample": [],
        "human_summary": "2 packages will be removed.",
        "risk": "MED",
        "reversible": False,
    }
    text = format_diff(diff)
    assert text is not None
    assert "Change: 2 to remove." in text
    assert "Risk: MED — irreversible." in text


def test_format_diff_package_upgrade():
    diff = {
        "operation": "package.upgrade",
        "bytes_delta": 0,
        "file_count_delta": 3,
        "affected_paths_sample": [],
        "human_summary": "3 packages will be upgraded.",
        "risk": "HIGH",
        "reversible": False,
    }
    text = format_diff(diff)
    assert text is not None
    assert "Change: 3 to upgrade." in text
    assert "Risk: HIGH — irreversible." in text


# ── format_diff — edge cases ─────────────────────────────────────────────────

def test_format_diff_none_or_non_dict_returns_none():
    assert format_diff(None) is None
    assert format_diff("not a dict") is None  # type: ignore[arg-type]
    assert format_diff(42) is None  # type: ignore[arg-type]


def test_format_diff_empty_diff_still_renders_risk_line():
    # A diff with no bytes/paths but a risk field still renders the risk line.
    diff = {"risk": "LOW", "reversible": True}
    text = format_diff(diff)
    assert text is not None
    assert "Risk: LOW — reversible." in text


def test_format_diff_sanitizes_ansi_in_human_summary():
    """Defense-in-depth per BP-3: model-authored text through mcpd is scrubbed."""
    diff = {
        "operation": "fs.delete",
        "bytes_delta": -100,
        "file_count_delta": -1,
        "affected_paths_sample": [],
        "human_summary": "\x1b[31mDANGER\x1b[0m 100 bytes freed.",
        "risk": "LOW",
        "reversible": False,
    }
    text = format_diff(diff)
    assert text is not None
    assert "\x1b" not in text
    assert "DANGER 100 bytes freed." in text


def test_format_diff_sanitizes_ansi_in_affected_paths():
    diff = {
        "operation": "fs.delete",
        "bytes_delta": -100,
        "file_count_delta": -1,
        "affected_paths_sample": ["\x1b[31m/etc/passwd\x1b[0m"],
        "human_summary": "test",
        "risk": "LOW",
        "reversible": False,
    }
    text = format_diff(diff)
    assert text is not None
    assert "\x1b" not in text
    assert "  - /etc/passwd" in text


def test_format_diff_no_truncation_footer_when_sample_covers_total():
    diff = {
        "operation": "fs.delete",
        "bytes_delta": -3,
        "file_count_delta": -3,
        "affected_paths_sample": ["/a", "/b", "/c"],
        "human_summary": "3 bytes freed.",
        "risk": "LOW",
        "reversible": False,
    }
    text = format_diff(diff)
    assert text is not None
    assert "  - /a" in text
    assert "  - /b" in text
    assert "  - /c" in text
    # Sample size == total (both 3) — no "... (N more)" line.
    assert "more)" not in text


def test_format_diff_missing_risk_field_shows_question_mark():
    diff = {
        "operation": "fs.delete",
        "bytes_delta": -100,
        "file_count_delta": -1,
        "affected_paths_sample": [],
        "human_summary": "test",
        "reversible": False,
    }
    text = format_diff(diff)
    assert text is not None
    assert "Risk: ? — irreversible." in text


def test_format_diff_line_order_is_stable():
    """Whitepaper §8.2 wants: human_summary → compact → risk → affected."""
    diff = {
        "operation": "fs.delete",
        "bytes_delta": -100,
        "file_count_delta": -1,
        "affected_paths_sample": ["/tmp/x"],
        "human_summary": "HUMAN.",
        "risk": "LOW",
        "reversible": False,
    }
    text = format_diff(diff)
    assert text is not None
    lines = text.split("\n")
    assert lines[0] == "HUMAN."
    assert lines[1].startswith("Change:")
    assert lines[2].startswith("Risk:")
    assert lines[3] == "Affected (sample):"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
