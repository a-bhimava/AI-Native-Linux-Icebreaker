"""Tests for the DesktopConfig dataclass and schema integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from controller.config import DesktopConfig


class TestDesktopConfigDefaults:
    def test_default_start_tray_off(self) -> None:
        cfg = DesktopConfig()
        assert cfg.start_tray is False

    def test_default_chatbot_on_tray(self) -> None:
        cfg = DesktopConfig()
        assert cfg.chatbot_on_tray is True

    def test_default_window_chatbot(self) -> None:
        cfg = DesktopConfig()
        assert cfg.default_window == "chatbot"

    def test_default_dark_true(self) -> None:
        cfg = DesktopConfig()
        assert cfg.dark is True

    def test_frozen(self) -> None:
        cfg = DesktopConfig()
        with pytest.raises(AttributeError):
            cfg.dark = False  # type: ignore[misc]


class TestDesktopSchema:
    """Verify the JSON schema accepts the desktop section."""

    @pytest.fixture
    def schema(self) -> dict:
        schema_path = Path(__file__).resolve().parents[2] / "controller" / "schemas" / "controller_config.json"
        with schema_path.open() as f:
            return json.load(f)

    def test_desktop_in_schema(self, schema: dict) -> None:
        assert "desktop" in schema["properties"]

    def test_desktop_properties(self, schema: dict) -> None:
        desktop = schema["properties"]["desktop"]
        props = desktop["properties"]
        assert "start_tray" in props
        assert "dark" in props
        assert "default_window" in props
        assert "chatbot_on_tray" in props

    def test_default_window_enum(self, schema: dict) -> None:
        desktop = schema["properties"]["desktop"]
        enum = desktop["properties"]["default_window"]["enum"]
        assert "chatbot" in enum
        assert "settings" in enum
        assert "audit" in enum
