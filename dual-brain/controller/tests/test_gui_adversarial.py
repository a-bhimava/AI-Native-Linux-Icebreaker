"""Adversarial tests for GUI element selector injection (PR #26)."""

from __future__ import annotations

import pytest

from controller.audit import sanitize_gui_field


class TestGuiSelectorInjection:
    @pytest.mark.parametrize("payload", [
        "\x1b[31m\x1b[1m⚠ APPROVED\x1b[0m",
        "Normal\r\x1b[AOverwritten approval",
        "Title\x00With\x00Nulls",
        "\x1b]2;fake window title\x07",
    ])
    def test_ansi_injection_in_window_title(self, payload):
        clean = sanitize_gui_field(payload)
        assert "\x1b" not in clean
        assert "\x00" not in clean
        assert "\r" not in clean

    @pytest.mark.parametrize("payload", [
        "push-button\x1b[31m",
        "text\x00field",
    ])
    def test_ansi_injection_in_element_role(self, payload):
        clean = sanitize_gui_field(payload)
        assert "\x1b" not in clean
        assert "\x00" not in clean

    def test_oversized_element_name_truncated(self):
        name = "B" * 500
        clean = sanitize_gui_field(name)
        assert len(clean) <= 260
        assert clean.endswith("...")
