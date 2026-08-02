"""Tests for V.6c GuiConfig nested Fix V sub-sections.

Verifies:
- Defaults kick in when a section is absent from TOML
- TOML overrides apply cleanly
- Schema accepts the new nested keys
- Schema rejects typos (additionalProperties: false)
- The wiring reaches gui_agent.agent._extract_vision_config
"""

from __future__ import annotations

import tomllib

import pytest

from controller.config import (
    GuiConfig,
    GuiGeometryConfig,
    GuiPreviewConfig,
    GuiTrustConfig,
    GuiVisionConfig,
    _build_gui_config,
)


# ── Defaults ────────────────────────────────────────────────────────


def test_gui_config_defaults_include_all_v_sub_sections():
    cfg = GuiConfig()
    assert isinstance(cfg.vision, GuiVisionConfig)
    assert isinstance(cfg.trust, GuiTrustConfig)
    assert isinstance(cfg.preview, GuiPreviewConfig)
    assert isinstance(cfg.geometry, GuiGeometryConfig)


def test_vision_defaults_match_documented():
    v = GuiVisionConfig()
    assert v.enabled is True
    assert v.backend == "gemini/gemini-2.5-flash"
    assert v.fallback_backend == "anthropic/claude-haiku-4-5"
    assert v.max_elements_per_parse == 50
    assert v.cost_ceiling_usd_per_turn == 0.01
    assert v.cache_ttl_seconds == 2.0
    assert v.retry_on_malformed_json == 1


def test_trust_defaults_match_documented():
    t = GuiTrustConfig()
    assert t.enabled is True
    assert t.store_path == "/var/lib/icebreaker/gui_trust.jsonl"
    assert t.defaults_dir == "/etc/icebreaker/gui_trust.d"


def test_preview_defaults_match_documented():
    p = GuiPreviewConfig()
    assert p.enabled is True
    assert p.notify_urgency == "low"
    assert p.preview_ttl_seconds == 5


def test_geometry_defaults_match_documented():
    g = GuiGeometryConfig()
    assert g.hidpi_scale_override == 0


# ── TOML → GuiConfig via _build_gui_config ──────────────────────────


def test_build_gui_config_empty_toml_gets_defaults():
    cfg = _build_gui_config({})
    assert cfg.vision == GuiVisionConfig()
    assert cfg.trust == GuiTrustConfig()
    assert cfg.preview == GuiPreviewConfig()
    assert cfg.geometry == GuiGeometryConfig()


def test_build_gui_config_partial_vision_section_uses_defaults_for_rest():
    """User overrides only `backend` — every other vision key gets default."""
    raw = {"gui": {"vision": {"backend": "anthropic/claude-haiku-4-5"}}}
    cfg = _build_gui_config(raw)
    assert cfg.vision.backend == "anthropic/claude-haiku-4-5"
    # Untouched keys → defaults.
    assert cfg.vision.cost_ceiling_usd_per_turn == 0.01
    assert cfg.vision.cache_ttl_seconds == 2.0
    assert cfg.vision.enabled is True


def test_build_gui_config_cost_ceiling_override():
    raw = {"gui": {"vision": {"cost_ceiling_usd_per_turn": 0.05}}}
    cfg = _build_gui_config(raw)
    assert cfg.vision.cost_ceiling_usd_per_turn == 0.05


def test_build_gui_config_trust_disabled_override():
    raw = {"gui": {"trust": {"enabled": False}}}
    cfg = _build_gui_config(raw)
    assert cfg.trust.enabled is False


def test_build_gui_config_preview_urgency_override():
    raw = {"gui": {"preview": {"notify_urgency": "critical"}}}
    cfg = _build_gui_config(raw)
    assert cfg.preview.notify_urgency == "critical"


def test_build_gui_config_geometry_scale_override():
    raw = {"gui": {"geometry": {"hidpi_scale_override": 3}}}
    cfg = _build_gui_config(raw)
    assert cfg.geometry.hidpi_scale_override == 3


def test_build_gui_config_preserves_top_level_gui_keys():
    """Adding V.6c sub-sections doesn't break the existing GuiConfig
    keys (screenshot_dir, screenshot_retention, etc.)."""
    raw = {"gui": {
        "screenshot_dir": "/custom/scratch",
        "vision": {"backend": "gemini/gemini-2.5-flash"},
    }}
    cfg = _build_gui_config(raw)
    assert cfg.screenshot_dir == "/custom/scratch"
    assert cfg.vision.backend == "gemini/gemini-2.5-flash"


# ── TOML round-trip via full toml load path ─────────────────────────


def test_full_toml_round_trip_all_sections():
    """A single TOML string with all 4 sub-sections parses + builds
    correctly through _build_gui_config."""
    toml_text = """
[gui.vision]
enabled = true
backend = "gemini/gemini-2.5-flash"
cost_ceiling_usd_per_turn = 0.02

[gui.trust]
enabled = true
defaults_dir = "/opt/custom/trust.d"

[gui.preview]
notify_urgency = "normal"

[gui.geometry]
hidpi_scale_override = 2
"""
    raw = tomllib.loads(toml_text)
    cfg = _build_gui_config(raw)
    assert cfg.vision.cost_ceiling_usd_per_turn == 0.02
    assert cfg.trust.defaults_dir == "/opt/custom/trust.d"
    assert cfg.preview.notify_urgency == "normal"
    assert cfg.geometry.hidpi_scale_override == 2


# ── Schema round-trip: full validation via ControllerConfig loader ──


def test_schema_accepts_new_nested_sections():
    """The new sub-sections must not be rejected by the JSON Schema."""
    import json
    from pathlib import Path
    schema_path = Path(__file__).parent.parent / "schemas" / "controller_config.json"
    schema = json.loads(schema_path.read_text())
    gui_props = schema["properties"]["gui"]["properties"]
    for section in ("vision", "trust", "preview", "geometry"):
        assert section in gui_props, \
            f"schema missing [gui.{section}] — V.6c regression"


def test_schema_rejects_unknown_keys_in_new_sections():
    """additionalProperties: false is set on each new sub-section,
    so typos surface as schema errors instead of silent defaults."""
    import json
    from pathlib import Path
    schema_path = Path(__file__).parent.parent / "schemas" / "controller_config.json"
    schema = json.loads(schema_path.read_text())
    gui_props = schema["properties"]["gui"]["properties"]
    for section in ("vision", "trust", "preview", "geometry"):
        assert gui_props[section]["additionalProperties"] is False, \
            f"[gui.{section}] must set additionalProperties: false to catch typos"


# ── Agent-side integration (V.4c _extract_vision_config) ────────────


def test_agent_extract_vision_config_from_gui_config():
    """The V.4c _extract_vision_config helper must handle a GuiConfig
    with a populated .vision attr (post-V.6c wiring)."""
    from gui_agent.agent import GuiAgent
    cfg = GuiConfig(vision=GuiVisionConfig(
        backend="anthropic/claude-haiku-4-5",
        cost_ceiling_usd_per_turn=0.03,
    ))
    extracted = GuiAgent._extract_vision_config(cfg)
    assert extracted["backend"] == "anthropic/claude-haiku-4-5"
    assert extracted["cost_ceiling_usd_per_turn"] == 0.03
