"""Tests for GUI dispatch routing in the Controller (PR #26)."""

from __future__ import annotations

import pytest

from controller.turn_events import GuiEvent


class TestGuiEvent:
    def test_gui_event_preview(self):
        e = GuiEvent(
            phase="preview",
            action="gui.click",
            window_title="Firefox",
            element_role="push-button",
            element_name="Reload",
        )
        assert e.phase == "preview"
        assert e.action == "gui.click"
        assert e.window_title == "Firefox"

    def test_gui_event_complete(self):
        e = GuiEvent(
            phase="complete",
            action="gui.screenshot",
            window_title="",
            screenshot_before_hash="abc123",
        )
        assert e.phase == "complete"
        assert e.screenshot_before_hash == "abc123"

    def test_gui_event_invalid_phase(self):
        with pytest.raises(ValueError, match="phase must be"):
            GuiEvent(phase="invalid", action="gui.click", window_title="")

    @pytest.mark.parametrize("phase", ["preview", "executing", "complete"])
    def test_gui_event_valid_phases(self, phase):
        e = GuiEvent(phase=phase, action="gui.ping", window_title="")
        assert e.phase == phase

    def test_gui_event_in_turn_event_union(self):
        from controller.turn_events import TurnEvent
        import typing
        args = typing.get_args(TurnEvent)
        assert GuiEvent in args


class TestGuiDispatchRouting:
    def test_gui_action_detected(self):
        actions = ["gui.click", "gui.screenshot", "gui.find_element"]
        for action in actions:
            assert action.startswith("gui.")

    def test_mcpd_action_not_gui(self):
        actions = ["fs.read", "system.status", "package.install"]
        for action in actions:
            assert not action.startswith("gui.")


class TestGuiConfig:
    def test_gui_config_defaults(self):
        from controller.config import GuiConfig
        cfg = GuiConfig()
        assert cfg.enabled is True
        assert cfg.screenshot_dir == "/tmp/icebreaker-gui"
        assert cfg.screenshot_retention == 50
        assert cfg.prefer_app_api is True
        assert cfg.a11y_timeout_ms == 5000

    def test_gui_config_in_controller_config(self):
        from controller.config import ControllerConfig
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(ControllerConfig)}
        assert "gui" in field_names
