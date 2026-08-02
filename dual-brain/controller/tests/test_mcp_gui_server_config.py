"""Fix V.6f — mcp_gui_server config threading tests.

CT-scan P0-3: pre-V.6f, `mcp_gui_server::_handle_tools_call` hardcoded
`cfg=None` when calling `_dispatch_in_subprocess`. My V.6c
[gui.vision]/[gui.trust]/[gui.preview]/[gui.geometry] TOML sections
were dead on the OC edition — `docs/vision_automation.md`'s "set
enabled=false to disable vision" claim was FALSE.

V.6f fix: `_get_cfg_for_kind()` lazy-loads ControllerConfig on first
tool call and passes cfg.gui / cfg.rpa down. This test file verifies
the loader is called, that failure falls back gracefully to defaults,
and that the config actually reaches the dispatch layer.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from controller import mcp_gui_server as mcps


@pytest.fixture(autouse=True)
def reset_config_cache():
    """Each test starts with a fresh cache so we can assert loader
    behavior deterministically."""
    mcps._CONFIG_CACHE.clear()
    mcps._CONFIG_CACHE.update({"loaded": False, "gui": None, "rpa": None})
    yield
    mcps._CONFIG_CACHE.clear()
    mcps._CONFIG_CACHE.update({"loaded": False, "gui": None, "rpa": None})


# ── Loader is called once, cached thereafter ─────────────────────────


def test_get_cfg_calls_load_once(monkeypatch):
    from controller.config import GuiConfig, RpaConfig
    mock_cfg = MagicMock()
    mock_cfg.gui = GuiConfig()
    mock_cfg.rpa = RpaConfig()
    load_mock = MagicMock(return_value=mock_cfg)

    with patch("controller.config.load", load_mock):
        gui = mcps._get_cfg_for_kind("gui")
        rpa = mcps._get_cfg_for_kind("rpa")
        gui_again = mcps._get_cfg_for_kind("gui")

    assert load_mock.call_count == 1, \
        "config.load() should be called at most once (cached thereafter)"
    assert isinstance(gui, GuiConfig)
    assert isinstance(rpa, RpaConfig)
    assert gui is gui_again


# ── Graceful fallback to defaults ────────────────────────────────────


def test_missing_config_file_falls_back_to_defaults(capsys):
    """When config.load() raises (missing file, bad TOML, schema
    error), the MCP server MUST still boot with dataclass defaults."""
    from controller.config import GuiConfig, RpaConfig
    with patch("controller.config.load",
               side_effect=FileNotFoundError("no config.toml")):
        gui = mcps._get_cfg_for_kind("gui")
        rpa = mcps._get_cfg_for_kind("rpa")

    assert isinstance(gui, GuiConfig)
    assert isinstance(rpa, RpaConfig)
    err = capsys.readouterr().err
    assert "config load failed" in err
    assert "FileNotFoundError" in err


def test_schema_error_falls_back_to_defaults():
    from controller.config import GuiConfig
    with patch("controller.config.load",
               side_effect=ValueError("bad TOML")):
        gui = mcps._get_cfg_for_kind("gui")
    assert isinstance(gui, GuiConfig)


# ── Config reaches _dispatch_in_subprocess with cfg param ───────────


def test_tools_call_passes_gui_cfg_to_dispatch(monkeypatch):
    """Verify _handle_tools_call now passes cfg (not None) into
    _dispatch_in_subprocess for kind='gui'. Pre-V.6f: cfg=None
    hardcoded → all my [gui.vision] wiring dead."""
    from controller.config import GuiConfig
    mock_cfg = MagicMock()
    mock_cfg.gui = GuiConfig()
    mock_cfg.rpa = MagicMock()
    dispatch_mock = MagicMock(return_value={"ok": True, "result": {}})

    with patch("controller.config.load", return_value=mock_cfg), \
         patch("controller.agent_graph_nodes._dispatch_in_subprocess",
               dispatch_mock):
        mcps._handle_tools_call(
            msg_id=1,
            params={"name": "gui.ping", "arguments": {}},
        )

    dispatch_mock.assert_called_once()
    call_kwargs = dispatch_mock.call_args.kwargs
    assert call_kwargs["cfg"] is not None, \
        "V.6f regression: cfg=None still passed — [gui.vision] etc are dead"
    assert isinstance(call_kwargs["cfg"], GuiConfig)


def test_tools_call_passes_rpa_cfg_for_rpa_tool(monkeypatch):
    from controller.config import GuiConfig, RpaConfig
    mock_cfg = MagicMock()
    mock_cfg.gui = GuiConfig()
    mock_cfg.rpa = RpaConfig()
    dispatch_mock = MagicMock(return_value={"ok": True, "result": {}})

    with patch("controller.config.load", return_value=mock_cfg), \
         patch("controller.agent_graph_nodes._dispatch_in_subprocess",
               dispatch_mock):
        mcps._handle_tools_call(
            msg_id=1,
            params={"name": "rpa.ping", "arguments": {}},
        )

    assert isinstance(dispatch_mock.call_args.kwargs["cfg"], RpaConfig)


# ── _config_to_dict now recurses into nested dataclasses ────────────


def test_config_to_dict_serializes_nested_gui_vision():
    """CT-scan P0-3 root cause: pre-V.6f, `_config_to_dict` coerced
    the nested GuiVisionConfig field into `str(v)` (its repr) rather
    than recursing into it. Post-V.6f the nested dict must be present
    with the actual field values."""
    from controller.agent_graph_nodes import _config_to_dict
    from controller.config import GuiConfig, GuiVisionConfig

    cfg = GuiConfig(vision=GuiVisionConfig(
        enabled=False,   # explicit override, key test scenario
        backend="anthropic/claude-haiku-4-5",
        cost_ceiling_usd_per_turn=0.05,
    ))
    d = _config_to_dict(cfg)
    assert "vision" in d
    assert isinstance(d["vision"], dict)
    assert d["vision"]["enabled"] is False
    assert d["vision"]["backend"] == "anthropic/claude-haiku-4-5"
    assert d["vision"]["cost_ceiling_usd_per_turn"] == 0.05


def test_config_to_dict_nested_trust_and_preview_and_geometry():
    from controller.agent_graph_nodes import _config_to_dict
    from controller.config import GuiConfig
    cfg = GuiConfig()  # all defaults
    d = _config_to_dict(cfg)
    for section in ("vision", "trust", "preview", "geometry"):
        assert section in d, f"[gui.{section}] missing from serialized config"
        assert isinstance(d[section], dict)


def test_config_to_dict_none_returns_empty():
    from controller.agent_graph_nodes import _config_to_dict
    assert _config_to_dict(None) == {}


# ── Round-trip: [gui.vision] enabled=false actually disables ────────


def test_enabled_false_reaches_agent_extract_vision_config():
    """End-to-end: TOML `[gui.vision] enabled = false` → GuiVisionConfig
    → _config_to_dict → SimpleNamespace in gui_worker → GuiAgent
    __init__ → _extract_vision_config picks up enabled=False."""
    from controller.agent_graph_nodes import _config_to_dict
    from controller.config import GuiConfig, GuiVisionConfig
    from gui_agent.agent import GuiAgent
    from types import SimpleNamespace

    cfg = GuiConfig(vision=GuiVisionConfig(enabled=False))
    serialized = _config_to_dict(cfg)
    # Rebuild namespace the way gui_worker does.
    ns = SimpleNamespace(**serialized)
    extracted = GuiAgent._extract_vision_config(ns)
    assert extracted.get("enabled") is False, \
        "V.6f regression: enabled=false didn't survive round-trip"
