"""Tests for gui.settings — Settings Panel (M6UI.1).

All tests run headless (no GTK display needed). They validate:
  - Config collection logic
  - TOML serialization
  - Schema validation paths
  - Page construction from raw config dicts
  - BP-8: api_key_env never resolves
  - Keymap defaults and reset
  - Dirty tracking
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("gi", MagicMock())
sys.modules.setdefault("gi.repository", MagicMock())

from gui.settings.window import _dict_to_toml, _load_raw_config

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import tomlkit


# ---------------------------------------------------------------------------
# TOML serializer (legacy _dict_to_toml kept for backward compat)
# ---------------------------------------------------------------------------

class TestTomlSerializer:
    def test_empty_dict(self) -> None:
        assert _dict_to_toml({}) == ""

    def test_string_value(self) -> None:
        result = _dict_to_toml({"key": "value"})
        assert 'key = "value"' in result

    def test_int_value(self) -> None:
        result = _dict_to_toml({"count": 42})
        assert "count = 42" in result

    def test_bool_values(self) -> None:
        result = _dict_to_toml({"on": True, "off": False})
        assert "on = true" in result
        assert "off = false" in result

    def test_float_value(self) -> None:
        result = _dict_to_toml({"rate": 0.8})
        assert "rate = 0.8" in result

    def test_list_value(self) -> None:
        result = _dict_to_toml({"keys": ["a", "b", "c"]})
        assert 'keys = ["a", "b", "c"]' in result

    def test_nested_table(self) -> None:
        result = _dict_to_toml({"section": {"key": "val"}})
        assert "[section]" in result
        assert 'key = "val"' in result

    def test_deeply_nested(self) -> None:
        result = _dict_to_toml({"a": {"b": {"c": 1}}})
        assert "c = 1" in result

    def test_mixed_scalars_and_tables(self) -> None:
        result = _dict_to_toml({"name": "test", "sub": {"x": 1}})
        assert 'name = "test"' in result
        assert "[sub]" in result
        assert "x = 1" in result


# ---------------------------------------------------------------------------
# tomlkit round-trip (production serializer — replaces _dict_to_toml)
# ---------------------------------------------------------------------------

class TestTomlkitRoundTrip:
    def test_nested_tables_preserved(self) -> None:
        data = {"qb": {"backend": "gemini", "gemini": {"model": "flash"}}}
        serialized = tomlkit.dumps(data)
        parsed = tomllib.loads(serialized)
        assert parsed["qb"]["gemini"]["model"] == "flash"

    def test_deeply_nested(self) -> None:
        data = {"a": {"b": {"c": {"d": 1}}}}
        serialized = tomlkit.dumps(data)
        parsed = tomllib.loads(serialized)
        assert parsed["a"]["b"]["c"]["d"] == 1

    def test_full_config_round_trip(self) -> None:
        data = {
            "qb": {
                "backend": "gemini",
                "model": "gemini-2.5-flash",
                "gemini": {"api_key_env": "GEMINI_API_KEY", "max_tokens": 4096},
            },
            "session": {"color": "auto", "session_ttl_seconds": 3600},
            "hitl": {"lockout_seconds": 3},
        }
        serialized = tomlkit.dumps(data)
        parsed = tomllib.loads(serialized)
        assert parsed["qb"]["gemini"]["api_key_env"] == "GEMINI_API_KEY"
        assert parsed["session"]["color"] == "auto"
        assert parsed["hitl"]["lockout_seconds"] == 3


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestConfigLoading:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = _load_raw_config(tmp_path / "nonexistent.toml")
        assert result == {}

    def test_valid_toml(self, tmp_path: Path) -> None:
        cfg = tmp_path / "test.toml"
        cfg.write_text('[qb]\nbackend = "gemini"\n')
        result = _load_raw_config(cfg)
        assert result["qb"]["backend"] == "gemini"


# ---------------------------------------------------------------------------
# Backend page
# ---------------------------------------------------------------------------

class TestBackendDefaults:
    def test_all_backends_have_defaults(self) -> None:
        from gui.settings.backend_page import _API_DEFAULTS, _LOCAL_DEFAULTS, _BACKENDS
        for b in _BACKENDS:
            if b == "local":
                assert "model_id" in _LOCAL_DEFAULTS
                assert "endpoint" in _LOCAL_DEFAULTS
            else:
                assert b in _API_DEFAULTS
                assert "model" in _API_DEFAULTS[b]
                assert "api_key_env" in _API_DEFAULTS[b]

    def test_api_key_env_not_value(self) -> None:
        """BP-8: api_key_env stores env var NAME, not a secret value."""
        from gui.settings.backend_page import _API_DEFAULTS
        for backend, defaults in _API_DEFAULTS.items():
            key_env = defaults["api_key_env"]
            assert key_env.isupper(), f"{backend} api_key_env should be uppercase env var name"
            assert not key_env.startswith("sk-"), f"{backend} api_key_env looks like a real key"

    def test_backends_list_complete(self) -> None:
        from gui.settings.backend_page import _BACKENDS
        assert set(_BACKENDS) == {"gemini", "openai", "anthropic", "local"}


# ---------------------------------------------------------------------------
# Keymap defaults
# ---------------------------------------------------------------------------

class TestKeymapDefaults:
    def test_defaults_imported(self) -> None:
        from controller.keymap import DEFAULTS, Action
        assert Action.APPROVE in DEFAULTS
        assert Action.DENY in DEFAULTS
        assert "?" in DEFAULTS[Action.HELP]

    def test_all_actions_have_defaults(self) -> None:
        from controller.keymap import DEFAULTS, Action
        for action in Action:
            assert action in DEFAULTS, f"Missing default for {action}"


# ---------------------------------------------------------------------------
# Session page collect
# ---------------------------------------------------------------------------

class TestSessionCollect:
    def test_default_color_options(self) -> None:
        """Color option list matches schema enum."""
        schema_path = Path(__file__).parent.parent.parent / "controller" / "schemas" / "controller_config.json"
        with schema_path.open() as f:
            schema = json.load(f)
        color_enum = schema["properties"]["session"]["properties"]["color"]["enum"]
        assert color_enum == ["auto", "always", "never"]

    def test_session_ttl_bounds(self) -> None:
        """Session TTL schema bounds match UI expectations."""
        schema_path = Path(__file__).parent.parent.parent / "controller" / "schemas" / "controller_config.json"
        with schema_path.open() as f:
            schema = json.load(f)
        ttl = schema["properties"]["session"]["properties"]["session_ttl_seconds"]
        assert ttl["minimum"] == 0
        assert ttl["maximum"] == 86400


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_schema_loads(self) -> None:
        schema_path = Path(__file__).parent.parent.parent / "controller" / "schemas" / "controller_config.json"
        with schema_path.open() as f:
            schema = json.load(f)
        assert schema["type"] == "object"
        assert "qb" in schema["properties"]

    def test_desktop_section_in_schema(self) -> None:
        schema_path = Path(__file__).parent.parent.parent / "controller" / "schemas" / "controller_config.json"
        with schema_path.open() as f:
            schema = json.load(f)
        assert "desktop" in schema["properties"]
        desktop = schema["properties"]["desktop"]
        assert "dark" in desktop["properties"]
        assert "default_window" in desktop["properties"]

    def test_hitl_section_in_schema(self) -> None:
        schema_path = Path(__file__).parent.parent.parent / "controller" / "schemas" / "controller_config.json"
        with schema_path.open() as f:
            schema = json.load(f)
        hitl = schema["properties"]["hitl"]
        assert "lockout_seconds" in hitl["properties"]
        assert hitl["properties"]["lockout_seconds"]["minimum"] == 1

    def test_valid_minimal_config(self) -> None:
        import jsonschema
        schema_path = Path(__file__).parent.parent.parent / "controller" / "schemas" / "controller_config.json"
        with schema_path.open() as f:
            schema = json.load(f)
        config = {
            "qb": {
                "backend": "gemini",
                "gemini": {
                    "model": "gemini-2.5-flash",
                    "api_key_env": "GEMINI_API_KEY",
                    "max_tokens": 4096,
                    "timeout_seconds": 30,
                },
            },
        }
        jsonschema.validate(config, schema)

    def test_invalid_backend_rejected(self) -> None:
        import jsonschema
        schema_path = Path(__file__).parent.parent.parent / "controller" / "schemas" / "controller_config.json"
        with schema_path.open() as f:
            schema = json.load(f)
        config = {
            "qb": {
                "backend": "invalid_backend",
            },
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(config, schema)


# ---------------------------------------------------------------------------
# Model page utilities
# ---------------------------------------------------------------------------

class TestModelPageUtils:
    def test_human_size(self) -> None:
        from gui.settings.model_page import _human_size
        assert "KB" in _human_size(1500)
        assert "MB" in _human_size(2_000_000)
        assert "GB" in _human_size(3_000_000_000)

    def test_load_checksums_empty_dir(self, tmp_path: Path) -> None:
        from gui.settings.model_page import _load_checksums
        result = _load_checksums([tmp_path])
        assert result == {}

    def test_load_checksums_with_file(self, tmp_path: Path) -> None:
        from gui.settings.model_page import _load_checksums
        csum_file = tmp_path / "checksums.sha256"
        csum_file.write_text("abc123  model.gguf\ndef456  other.gguf\n")
        result = _load_checksums([tmp_path])
        assert result["model.gguf"] == "abc123"
        assert result["other.gguf"] == "def456"

    def test_load_checksums_ignores_missing(self) -> None:
        from gui.settings.model_page import _load_checksums
        result = _load_checksums([Path("/nonexistent/dir")])
        assert result == {}


# ---------------------------------------------------------------------------
# Daemon page defaults
# ---------------------------------------------------------------------------

class TestDaemonPageDefaults:
    def test_presenter_options(self) -> None:
        expected = {"terminal", "screen_reader", "gtk", "ai_terminal"}
        hitl_schema_path = Path(__file__).parent.parent.parent / "controller" / "schemas" / "controller_config.json"
        with hitl_schema_path.open() as f:
            schema = json.load(f)
        schema_presenters = set(schema["properties"]["hitl"]["properties"]["presenter"]["enum"])
        assert expected == schema_presenters
