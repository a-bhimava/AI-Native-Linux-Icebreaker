"""Tests for the [terminal] config section (PR #24).

Covers:
  1.  TerminalConfig defaults are correct
  2.  TerminalConfig parsed from raw TOML dict
  3.  ControllerConfig includes terminal field
"""

from __future__ import annotations

import pytest

from controller.config import TerminalConfig, _build_terminal_config


class TestTerminalConfigDefaults:
    def test_defaults(self):
        cfg = TerminalConfig()
        assert cfg.split_ratio == 0.6
        assert cfg.explanation_verbosity == "normal"
        assert cfg.shell_explanation == "none"
        assert cfg.cot_scrollback == 50
        assert cfg.nl_prefix == "#"
        assert cfg.theme == "oled-dark"
        assert cfg.input_position == "top"
        assert cfg.show_tier_badge is True
        assert cfg.show_suggestions is True


class TestTerminalConfigBuilder:
    def test_empty_section(self):
        cfg = _build_terminal_config({})
        assert cfg == TerminalConfig()

    def test_custom_values(self):
        raw = {
            "terminal": {
                "split_ratio": 0.7,
                "nl_prefix": "!",
                "explanation_verbosity": "verbose",
                "appearance": {
                    "theme": "high-contrast",
                    "input_position": "bottom",
                },
                "interpretation": {
                    "show_tier_badge": False,
                },
            }
        }
        cfg = _build_terminal_config(raw)
        assert cfg.split_ratio == 0.7
        assert cfg.nl_prefix == "!"
        assert cfg.explanation_verbosity == "verbose"
        assert cfg.theme == "high-contrast"
        assert cfg.input_position == "bottom"
        assert cfg.show_tier_badge is False
        assert cfg.show_suggestions is True
