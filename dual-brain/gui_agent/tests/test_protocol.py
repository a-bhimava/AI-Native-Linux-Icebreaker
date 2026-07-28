"""Tests for gui_agent.protocol — method constants, schema validation."""

from __future__ import annotations

import pytest

from gui_agent.protocol import (
    ALL_GUI_METHODS,
    GUI_READONLY_METHODS,
    GUI_WRITE_METHODS,
    GUI_PING,
    GUI_CLICK,
    validate_gui_params,
)


class TestMethodConstants:
    def test_all_methods_is_union(self):
        assert ALL_GUI_METHODS == GUI_READONLY_METHODS | GUI_WRITE_METHODS

    def test_readonly_and_write_disjoint(self):
        assert GUI_READONLY_METHODS & GUI_WRITE_METHODS == frozenset()

    def test_method_count(self):
        assert len(ALL_GUI_METHODS) == 8


class TestControlCharRejection:
    """F-101.1 (2026-07-28): shell metachars are now ACCEPTED for GUI
    fields (window/role/name/text) because AT-SPI calls go via D-Bus,
    never a shell. Common legitimate strings that were previously
    rejected include opencode session titles like "OC | System uptime
    check" and menu items like "File & Edit". C0 control characters
    (\\x00–\\x1f) remain rejected because they can corrupt terminal
    output or trigger unexpected keystroke behavior.
    """

    @pytest.mark.parametrize("shell_metachar_ok", [
        "ok;rm -rf /",       # semi
        "hello&world",       # amp — "File & Edit" style
        "$(whoami)",         # dollar-parens
        "test|cat",          # pipe — opencode "OC | ..." titles
        "a`cmd`b",           # backtick
        "foo<bar",           # angle
        "bar>baz",           # angle
        "dollar$sign",       # dollar
    ])
    def test_shell_metachar_in_window_now_accepted(self, shell_metachar_ok):
        # No exception — F-101.1 loosening means these validate.
        validate_gui_params(GUI_CLICK, {
            "window": shell_metachar_ok,
            "role": "button",
            "name": "OK",
        })

    @pytest.mark.parametrize("bad_value", [
        "null\x00byte",
        "control\x1fchar",
        "tab\tinjection",     # C0 (\x09)
        "newline\ninject",    # C0 (\x0a)
    ])
    def test_control_char_in_window_still_rejected(self, bad_value):
        with pytest.raises(Exception):
            validate_gui_params(GUI_CLICK, {
                "window": bad_value,
                "role": "button",
                "name": "OK",
            })

    def test_metachar_in_role_now_accepted(self):
        # F-101.1: role names like "menu-item" already contain safe
        # punctuation; any shell metachar in role is still not a shell
        # injection surface (AT-SPI role names are D-Bus tokens).
        validate_gui_params(GUI_CLICK, {
            "window": "Test",
            "role": "button;evil",
            "name": "OK",
        })

    def test_metachar_in_name_now_accepted(self):
        # F-101.1: menu item names like "File & Edit" or
        # "Save changes to $HOME/foo.txt?" must validate.
        validate_gui_params(GUI_CLICK, {
            "window": "Test",
            "role": "button",
            "name": "Save & Close",
        })


class TestHappyPath:
    def test_ping_accepts_empty(self):
        validate_gui_params(GUI_PING, {})

    def test_find_element_valid(self):
        validate_gui_params("gui.find_element", {
            "window": "Firefox",
            "role": "push-button",
            "name": "Reload",
        })

    def test_type_valid(self):
        validate_gui_params("gui.type", {
            "window": "Terminal",
            "role": "text",
            "name": "input",
            "text": "hello world",
        })

    def test_unknown_method_raises(self):
        with pytest.raises(ValueError, match="unknown GUI method"):
            validate_gui_params("gui.nonexistent", {})
