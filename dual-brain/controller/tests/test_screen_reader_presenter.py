"""Tests for ``controller.presenters.screen_reader.ScreenReaderPresenter``.

Verifies: no ANSI escapes in output, no box-drawing characters, labeled
fields on separate lines, lockout without countdown spam, Decision mapping.
"""

from __future__ import annotations

import io
import re
import sys
from unittest.mock import patch

import pytest

from controller.hitl import Decision, HitlDisplayData
from controller.keymap import Keymap
from controller.presenters.screen_reader import ScreenReaderPresenter
from controller.risk_classifier import Tier


# ── Helpers ──────────────────────────────────────────────────────────────────

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_BOX_DRAWING = re.compile(r"[─-╿]")


def _sample_data(**overrides) -> HitlDisplayData:
    defaults = dict(
        action="fs.delete",
        target="/tmp/test.txt",
        tier=Tier.HIGH,
        risk_level="critical",
        reversible=False,
        backend="local",
        reason="user_requested",
        blocked_pattern=None,
        cow_summary=None,
    )
    defaults.update(overrides)
    return HitlDisplayData(**defaults)


# ── Output format tests ─────────────────────────────────────────────────────


class TestOutputFormat:
    def test_no_ansi_escapes(self, capsys):
        p = ScreenReaderPresenter()
        p.show_prompt(_sample_data())
        output = capsys.readouterr().out
        assert not _ANSI_ESCAPE.search(output), f"ANSI found: {output!r}"

    def test_no_box_drawing(self, capsys):
        p = ScreenReaderPresenter()
        p.show_prompt(_sample_data())
        output = capsys.readouterr().out
        assert not _BOX_DRAWING.search(output), f"Box drawing found: {output!r}"

    def test_labeled_fields(self, capsys):
        p = ScreenReaderPresenter()
        p.show_prompt(_sample_data())
        output = capsys.readouterr().out
        assert "Action: fs.delete" in output
        assert "Target: /tmp/test.txt" in output
        assert "Risk: critical" in output
        assert "Reversible: No" in output

    def test_reason_shown(self, capsys):
        p = ScreenReaderPresenter()
        p.show_prompt(_sample_data(reason="cleanup requested"))
        output = capsys.readouterr().out
        assert "Reason: cleanup requested" in output

    def test_backend_shown(self, capsys):
        p = ScreenReaderPresenter()
        p.show_prompt(_sample_data(backend="anthropic"))
        output = capsys.readouterr().out
        assert "Backend: anthropic" in output

    def test_cow_summary_shown(self, capsys):
        p = ScreenReaderPresenter()
        p.show_prompt(_sample_data(cow_summary="would delete 3 files"))
        output = capsys.readouterr().out
        assert "Preview: would delete 3 files" in output

    def test_empty_target_says_none(self, capsys):
        p = ScreenReaderPresenter()
        p.show_prompt(_sample_data(target=""))
        output = capsys.readouterr().out
        assert "Target: none" in output

    def test_no_reason_omits_line(self, capsys):
        p = ScreenReaderPresenter()
        p.show_prompt(_sample_data(reason=""))
        output = capsys.readouterr().out
        assert "Reason:" not in output

    def test_approval_header(self, capsys):
        p = ScreenReaderPresenter()
        p.show_prompt(_sample_data())
        output = capsys.readouterr().out
        assert "requires your approval" in output

    def test_reversible_yes(self, capsys):
        p = ScreenReaderPresenter()
        p.show_prompt(_sample_data(reversible=True))
        output = capsys.readouterr().out
        assert "Reversible: Yes" in output


# ── Lockout tests ────────────────────────────────────────────────────────────


class TestLockout:
    def test_lockout_messages(self, capsys):
        p = ScreenReaderPresenter()
        with patch("time.sleep"):
            p.lockout(3)
        output = capsys.readouterr().out
        assert "Approve available in 3 seconds" in output
        assert "Ready." in output

    def test_lockout_no_countdown_spam(self, capsys):
        p = ScreenReaderPresenter()
        with patch("time.sleep"):
            p.lockout(5)
        output = capsys.readouterr().out
        lines = [l for l in output.strip().split("\n") if l.strip()]
        assert len(lines) == 2


# ── Non-TTY tests ────────────────────────────────────────────────────────────


class TestNonTTY:
    def test_non_tty_returns_non_tty(self):
        p = ScreenReaderPresenter()
        with patch.object(sys.stdin, "isatty", return_value=False):
            result = p.pre_check()
        assert result == Decision.NON_TTY

    def test_tty_returns_none(self):
        p = ScreenReaderPresenter()
        with patch.object(sys.stdin, "isatty", return_value=True):
            result = p.pre_check()
        assert result is None


# ── Keymap integration ───────────────────────────────────────────────────────


class TestKeymapIntegration:
    def test_default_keymap(self):
        p = ScreenReaderPresenter()
        assert p._keymap is not None

    def test_custom_keymap(self):
        km = Keymap()
        p = ScreenReaderPresenter(keymap=km)
        assert p._keymap is km


# ── Last key class ───────────────────────────────────────────────────────────


class TestLastKeyClass:
    def test_initial_empty(self):
        p = ScreenReaderPresenter()
        assert p.last_key_class == ""

    def test_non_tty_sets_class(self):
        p = ScreenReaderPresenter()
        with patch.object(sys.stdin, "isatty", return_value=False):
            p.pre_check()
        assert p.last_key_class == "non_tty"
