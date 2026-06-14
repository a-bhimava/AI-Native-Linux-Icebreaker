"""Tests for HITL display sanitization — G5.1 gate.

Verifies SF-1 fix: every field in the spoof corpus renders with
zero ANSI escapes, zero C0/C1 control chars, and no \\r\\n.
Legitimate values pass through unchanged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from controller.hitl import (
    HitlDisplayData,
    _confusable_warn,
    _sanitize_display,
)
from controller.risk_classifier import Tier

_CORPUS_PATH = Path(__file__).parent / "corpus" / "hitl_spoof.json"
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f\x80-\x9f]")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _load_corpus():
    with _CORPUS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


# ── _sanitize_display unit tests ─────────────────────────────────────────


class TestSanitizeDisplay:
    def test_strips_ansi_escape(self):
        assert _sanitize_display("\x1b[31mred\x1b[0m") == "red"

    def test_strips_cursor_movement(self):
        assert _sanitize_display("\x1b[A\x1b[2Khidden") == "hidden"

    def test_neutralizes_cr(self):
        result = _sanitize_display("real\rfake")
        assert "\r" not in result
        assert "real fake" == result

    def test_neutralizes_lf(self):
        result = _sanitize_display("line1\nline2")
        assert "\n" not in result
        assert "line1 line2" == result

    def test_neutralizes_crlf(self):
        result = _sanitize_display("line1\r\nline2")
        assert "\r" not in result
        assert "\n" not in result

    def test_strips_c0_control(self):
        result = _sanitize_display("bell\x07here")
        assert "\x07" not in result
        assert "bellhere" == result

    def test_strips_c1_control(self):
        result = _sanitize_display("data\x9cmore")
        assert "\x9c" not in result

    def test_strips_null_byte(self):
        result = _sanitize_display("fs\x00.delete")
        assert "\x00" not in result

    def test_truncates_at_max_len(self):
        long_str = "A" * 300
        result = _sanitize_display(long_str, max_len=256)
        assert len(result) <= 260  # 256 + ellipsis char

    def test_preserves_short_string(self):
        assert _sanitize_display("fs.write") == "fs.write"

    def test_preserves_path(self):
        assert _sanitize_display("/home/user/notes.txt") == "/home/user/notes.txt"

    def test_non_string_converted(self):
        assert _sanitize_display(42) == "42"

    def test_empty_string(self):
        assert _sanitize_display("") == ""

    def test_preserves_tab(self):
        result = _sanitize_display("col1\tcol2")
        assert "col1" in result and "col2" in result


# ── Spoof corpus — no control chars in output ────────────────────────────


class TestSpoofCorpus:
    @pytest.fixture
    def corpus(self):
        return _load_corpus()

    def test_corpus_loads(self, corpus):
        assert len(corpus) >= 30, f"expected ≥30 payloads, got {len(corpus)}"

    @pytest.mark.parametrize(
        "entry",
        _load_corpus(),
        ids=[e["name"] for e in _load_corpus()],
    )
    def test_no_control_chars_in_sanitized(self, entry):
        payload = entry["payload"]
        result = _sanitize_display(payload)
        assert not _CONTROL_CHARS.search(result), (
            f"[{entry['name']}] sanitized output contains control chars: {result!r}"
        )
        assert not _ANSI_ESCAPE.search(result), (
            f"[{entry['name']}] sanitized output contains ANSI escape: {result!r}"
        )
        assert "\r" not in result, (
            f"[{entry['name']}] sanitized output contains \\r"
        )
        assert "\n" not in result, (
            f"[{entry['name']}] sanitized output contains \\n"
        )


# ── HitlDisplayData __post_init__ sanitizes all fields ───────────────────


class TestDisplayDataSanitization:
    def test_action_sanitized(self):
        data = HitlDisplayData(
            action="fs\x1b[31m.delete\x1b[0m",
            target="/tmp", tier=Tier.HIGH, risk_level="high",
            reversible=False, backend="local", reason="test",
            blocked_pattern=None, cow_summary=None,
        )
        assert "\x1b" not in data.action
        assert "fs.delete" in data.action

    def test_target_sanitized(self):
        data = HitlDisplayData(
            action="fs.write",
            target="/etc/shadow\r✓ Approved",
            tier=Tier.HIGH, risk_level="high",
            reversible=False, backend="local", reason="test",
            blocked_pattern=None, cow_summary=None,
        )
        assert "\r" not in data.target

    def test_reason_sanitized(self):
        data = HitlDisplayData(
            action="fs.write", target="/tmp",
            tier=Tier.LOW, risk_level="low",
            reversible=True, backend="local",
            reason="line1\nline2\n\x1b[2Jhidden",
            blocked_pattern=None, cow_summary=None,
        )
        assert "\n" not in data.reason
        assert "\x1b" not in data.reason

    def test_blocked_pattern_sanitized(self):
        data = HitlDisplayData(
            action="fs.write", target="/tmp",
            tier=Tier.HIGH, risk_level="high",
            reversible=False, backend="local", reason="test",
            blocked_pattern="/etc\x1b[A\x1b[2K✓",
            cow_summary=None,
        )
        assert "\x1b" not in data.blocked_pattern

    def test_cow_summary_sanitized(self):
        data = HitlDisplayData(
            action="fs.write", target="/tmp",
            tier=Tier.HIGH, risk_level="high",
            reversible=False, backend="local", reason="test",
            blocked_pattern=None,
            cow_summary="preview\x1b[31m RED \x1b[0m data",
        )
        assert "\x1b" not in data.cow_summary

    def test_backend_sanitized(self):
        data = HitlDisplayData(
            action="fs.write", target="/tmp",
            tier=Tier.LOW, risk_level="low",
            reversible=True,
            backend="local\x1b[31m",
            reason="test", blocked_pattern=None, cow_summary=None,
        )
        assert "\x1b" not in data.backend

    def test_legitimate_data_unchanged(self):
        data = HitlDisplayData(
            action="fs.write",
            target="/home/user/notes.txt",
            tier=Tier.LOW, risk_level="low",
            reversible=True, backend="gemini",
            reason="User requested file write",
            blocked_pattern=None, cow_summary=None,
        )
        assert data.action == "fs.write"
        assert data.target == "/home/user/notes.txt"
        assert data.reason == "User requested file write"
        assert data.backend == "gemini"


# ── Confusable warning ───────────────────────────────────────────────────


class TestConfusableWarn:
    def test_ascii_path_no_warning(self):
        assert _confusable_warn("/etc/passwd") == ""

    def test_non_ascii_path_warns(self):
        result = _confusable_warn("/etc/pаsswd")  # Cyrillic а
        assert "non-ASCII" in result or "look-alike" in result

    def test_empty_no_warning(self):
        assert _confusable_warn("") == ""
