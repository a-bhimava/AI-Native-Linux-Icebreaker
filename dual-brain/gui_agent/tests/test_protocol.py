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


class TestMetacharRejection:
    @pytest.mark.parametrize("bad_value", [
        "ok;rm -rf /",
        "hello&world",
        "$(whoami)",
        "test|cat",
        "a`cmd`b",
        "foo<bar",
        "bar>baz",
        "dollar$sign",
        "null\x00byte",
        "control\x1fchar",
    ])
    def test_metachar_in_window_rejected(self, bad_value):
        with pytest.raises(Exception):
            validate_gui_params(GUI_CLICK, {
                "window": bad_value,
                "role": "button",
                "name": "OK",
            })

    def test_metachar_in_role_rejected(self):
        with pytest.raises(Exception):
            validate_gui_params(GUI_CLICK, {
                "window": "Test",
                "role": "button;evil",
                "name": "OK",
            })

    def test_metachar_in_name_rejected(self):
        with pytest.raises(Exception):
            validate_gui_params(GUI_CLICK, {
                "window": "Test",
                "role": "button",
                "name": "OK$(pwd)",
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
