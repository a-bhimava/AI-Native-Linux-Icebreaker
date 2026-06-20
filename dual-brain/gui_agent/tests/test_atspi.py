"""Tests for gui_agent.atspi — AT-SPI client with mocked pyatspi."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gui_agent.atspi import (
    AtSpiClient,
    AtSpiUnavailableError,
    ElementDescriptor,
    ElementNotFoundError,
    ElementTooSmallError,
    _sanitize_gui_string,
)


def _make_mock_atspi():
    """Build a mock Atspi module with enough structure for AtSpiClient."""
    atspi = MagicMock()
    atspi.CoordType.SCREEN = "SCREEN"
    atspi.StateType.ENABLED = SimpleNamespace(value_nick="enabled")
    atspi.StateType.VISIBLE = SimpleNamespace(value_nick="visible")
    atspi.StateType.FOCUSABLE = SimpleNamespace(value_nick="focusable")
    atspi.StateType.FOCUSED = SimpleNamespace(value_nick="focused")
    return atspi


def _make_acc(name, role_name, children=None, extents=None, states=None, text=None):
    """Build a mock accessible object."""
    acc = MagicMock()
    acc.get_name.return_value = name
    acc.get_role_name.return_value = role_name
    acc.get_child_count.return_value = len(children or [])
    acc.get_child_at_index.side_effect = lambda i: (children or [])[i]
    acc.get_process_id.return_value = 1234

    ext = SimpleNamespace(x=100, y=200, width=80, height=30)
    if extents:
        ext = SimpleNamespace(**extents)
    acc.get_extents.return_value = ext

    state_set = MagicMock()
    state_set.contains.return_value = True
    acc.get_state_set.return_value = state_set

    text_iface = MagicMock()
    text_iface.get_text.return_value = text or ""
    text_iface.get_character_count.return_value = len(text or "")
    acc.get_text_iface.return_value = text_iface

    return acc


class TestFindElement:
    def test_find_element_by_role_and_name(self):
        atspi_mod = _make_mock_atspi()
        btn = _make_acc("OK", "push-button")
        frame = _make_acc("Test Window", "frame", children=[btn])
        app = _make_acc("test-app", "application", children=[frame])
        desktop = _make_acc("desktop", "desktop", children=[app])
        atspi_mod.get_desktop.return_value = desktop

        with patch.dict("sys.modules", {"gi": MagicMock(), "gi.repository": MagicMock()}):
            client = AtSpiClient.__new__(AtSpiClient)
            client._atspi = atspi_mod
            client._available = True
            client._window_cache = None
            client._window_cache_time = 0.0
            client._window_cache_ttl = 2.0

            result = client.find_element("Test Window", "push-button", "OK")
            assert result.role == "push-button"
            assert result.name == "OK"


class TestElementNotFound:
    def test_wrong_role_reason(self):
        atspi_mod = _make_mock_atspi()
        label = _make_acc("Status", "label")
        frame = _make_acc("Test Window", "frame", children=[label])
        app = _make_acc("test-app", "application", children=[frame])
        desktop = _make_acc("desktop", "desktop", children=[app])
        atspi_mod.get_desktop.return_value = desktop

        with patch.dict("sys.modules", {"gi": MagicMock(), "gi.repository": MagicMock()}):
            client = AtSpiClient.__new__(AtSpiClient)
            client._atspi = atspi_mod
            client._available = True
            client._window_cache = None
            client._window_cache_time = 0.0
            client._window_cache_ttl = 2.0

            with pytest.raises(ElementNotFoundError) as exc_info:
                client.find_element("Test Window", "push-button", "OK")
            assert exc_info.value.reason == "wrong_role"

    def test_window_not_found_reason(self):
        atspi_mod = _make_mock_atspi()
        desktop = _make_acc("desktop", "desktop", children=[])
        atspi_mod.get_desktop.return_value = desktop

        with patch.dict("sys.modules", {"gi": MagicMock(), "gi.repository": MagicMock()}):
            client = AtSpiClient.__new__(AtSpiClient)
            client._atspi = atspi_mod
            client._available = True
            client._window_cache = None
            client._window_cache_time = 0.0
            client._window_cache_ttl = 2.0

            with pytest.raises(ElementNotFoundError) as exc_info:
                client.find_element("Nonexistent", "button", "OK")
            assert exc_info.value.reason == "window_not_found"


class TestNoA11yTree:
    def test_empty_window_reason(self):
        atspi_mod = _make_mock_atspi()
        frame = _make_acc("Test Window", "frame", children=[])
        frame.get_child_count.return_value = 0
        app = _make_acc("test-app", "application", children=[frame])
        desktop = _make_acc("desktop", "desktop", children=[app])
        atspi_mod.get_desktop.return_value = desktop

        with patch.dict("sys.modules", {"gi": MagicMock(), "gi.repository": MagicMock()}):
            client = AtSpiClient.__new__(AtSpiClient)
            client._atspi = atspi_mod
            client._available = True
            client._window_cache = None
            client._window_cache_time = 0.0
            client._window_cache_ttl = 2.0

            with pytest.raises(ElementNotFoundError) as exc_info:
                client.find_element("Test Window", "button", "OK")
            assert exc_info.value.reason == "no_a11y_tree"


class TestTinyElement:
    def test_click_rejects_small_element(self):
        atspi_mod = _make_mock_atspi()

        with patch.dict("sys.modules", {"gi": MagicMock(), "gi.repository": MagicMock()}):
            client = AtSpiClient.__new__(AtSpiClient)
            client._atspi = atspi_mod
            client._available = True
            client._window_cache = None
            client._window_cache_time = 0.0
            client._window_cache_ttl = 2.0

            tiny = ElementDescriptor(
                path="/win/tiny",
                role="push-button",
                name="Tiny",
                position=(0, 0),
                size=(5, 5),
                states=frozenset({"enabled"}),
                text="",
            )
            with pytest.raises(ElementTooSmallError, match="5x5px"):
                client.click(tiny)


class TestWindowCache:
    def test_cache_returns_same_list(self):
        atspi_mod = _make_mock_atspi()
        frame = _make_acc("Test Window", "frame", children=[])
        app = _make_acc("test-app", "application", children=[frame])
        desktop = _make_acc("desktop", "desktop", children=[app])
        atspi_mod.get_desktop.return_value = desktop

        with patch.dict("sys.modules", {"gi": MagicMock(), "gi.repository": MagicMock()}):
            client = AtSpiClient.__new__(AtSpiClient)
            client._atspi = atspi_mod
            client._available = True
            client._window_cache = None
            client._window_cache_time = 0.0
            client._window_cache_ttl = 2.0

            list1 = client.get_window_list()
            list2 = client.get_window_list()
            assert list1 is list2
            assert atspi_mod.get_desktop.call_count == 1


class TestSanitize:
    def test_strips_ansi(self):
        assert _sanitize_gui_string("\x1b[31mred\x1b[0m") == "red"

    def test_strips_control_chars(self):
        assert _sanitize_gui_string("hello\x00world") == "helloworld"

    def test_truncates_long_strings(self):
        long_s = "a" * 300
        result = _sanitize_gui_string(long_s)
        assert len(result) == 259  # 256 + "..."
        assert result.endswith("...")
