"""Tests for gui.widgets — sanitization and widget logic (no GTK display needed)."""

from __future__ import annotations

import pytest

from gui.widgets import _sanitize


class TestSanitize:
    """BP-3: ANSI and control character stripping."""

    def test_plain_text_unchanged(self) -> None:
        assert _sanitize("hello world") == "hello world"

    def test_strips_ansi_color(self) -> None:
        assert _sanitize("\x1b[31mred\x1b[0m") == "red"

    def test_strips_ansi_cursor(self) -> None:
        assert _sanitize("\x1b[2Jclear") == "clear"

    def test_strips_osc_title(self) -> None:
        assert _sanitize("\x1b]0;evil title\x07safe") == "safe"

    def test_strips_c0_controls(self) -> None:
        assert _sanitize("a\x01b\x02c") == "abc"

    def test_strips_c1_controls(self) -> None:
        assert _sanitize("a\x80b\x9fc") == "abc"

    def test_preserves_newlines(self) -> None:
        assert _sanitize("line1\nline2") == "line1\nline2"

    def test_strips_carriage_return(self) -> None:
        assert _sanitize("fake\rprompt") == "fakeprompt"

    def test_preserves_tab(self) -> None:
        assert _sanitize("col1\tcol2") == "col1\tcol2"

    def test_complex_injection(self) -> None:
        injected = "\x1b[2J\x1b[H\r\n\x1b[32mApproved!\x1b[0m\n\x01\x02"
        result = _sanitize(injected)
        assert "\x1b" not in result
        assert "\x01" not in result
        assert "\r" not in result
        assert "Approved!" in result

    def test_empty_string(self) -> None:
        assert _sanitize("") == ""

    def test_unicode_preserved(self) -> None:
        assert _sanitize("hello 🌍") == "hello 🌍"
