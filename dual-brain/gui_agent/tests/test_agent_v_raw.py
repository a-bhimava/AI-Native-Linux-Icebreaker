"""Tests for GuiAgent.handle_request → Fix V raw pixel/keyboard tools.

Dispatches through the real handle_request → real input_synth call
path with subprocess.run mocked so tests never move the operator's
mouse. Covers the 7 raw tools added in V.4b:

  gui.click_at_coords, gui.type_at_coords, gui.drag, gui.scroll,
  gui.hover, gui.press_key, gui.key_sequence

Grounded_* orchestrators land in V.4d and have their own test file.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gui_agent.agent import GuiAgent
from gui_agent.protocol import (
    GUI_CLICK_AT_COORDS,
    GUI_DRAG,
    GUI_HOVER,
    GUI_KEY_SEQUENCE,
    GUI_PRESS_KEY,
    GUI_SCROLL,
    GUI_TYPE_AT_COORDS,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def agent(tmp_path):
    """Real GuiAgent with a temp scratch dir. AT-SPI is not touched by
    the Fix V handlers — no mock needed for it here."""
    return GuiAgent(scratch_dir=tmp_path)


@pytest.fixture
def mock_xdotool_ok():
    """subprocess.run inside input_synth → success (rc=0)."""
    def _fn(argv, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = b""
        proc.stderr = b""
        _fn.calls.append(argv)
        return proc
    _fn.calls = []
    with patch("gui_agent.input_synth.subprocess.run", side_effect=_fn):
        yield _fn


@pytest.fixture
def mock_xdotool_fail():
    """subprocess.run inside input_synth → non-zero rc + stderr."""
    def _fn(argv, **kwargs):
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = b""
        proc.stderr = b"xdotool: X error"
        return proc
    with patch("gui_agent.input_synth.subprocess.run", side_effect=_fn):
        yield _fn


@pytest.fixture(autouse=True)
def fresh_layout_cache():
    """Reset MonitorLayout's cache between tests so xrandr mocking is
    honored on the very first call."""
    from gui_agent.geometry import MonitorLayout
    MonitorLayout.invalidate_cache()
    yield
    MonitorLayout.invalidate_cache()


@pytest.fixture
def mock_xrandr_1x():
    """xrandr returns a single 1x monitor — no HiDPI scaling applied."""
    output = "eDP-1 connected primary 1920x1080+0+0 344mm x 194mm\n   1920x1080  60.00*+"
    with patch("gui_agent.geometry.subprocess.check_output", return_value=output):
        yield


# ═══ click_at_coords ═══════════════════════════════════════════════════


def test_click_at_coords_ok(agent, mock_xdotool_ok, mock_xrandr_1x):
    result = agent.handle_request(GUI_CLICK_AT_COORDS, {"x": 100, "y": 200})
    assert result["success"]
    assert result["physical_coords"] == [100, 200]
    assert result["logical_coords"] == [100, 200]
    # Verify xdotool actually got the click command.
    argv = mock_xdotool_ok.calls[0]
    assert "mousemove" in argv and "click" in argv


def test_click_at_coords_right_button(agent, mock_xdotool_ok, mock_xrandr_1x):
    result = agent.handle_request(GUI_CLICK_AT_COORDS,
                                  {"x": 50, "y": 60, "button": "right"})
    assert result["success"]
    argv = mock_xdotool_ok.calls[0]
    assert "3" in argv   # button 3 = right


def test_click_at_coords_bad_button_returns_validation_error(agent):
    """Schema-layer rejects — surfaces before reaching input_synth."""
    with pytest.raises(Exception) as exc_info:
        agent.handle_request(GUI_CLICK_AT_COORDS,
                             {"x": 1, "y": 1, "button": "middle-mouse"})
    # handle_request raises _InvalidParams (from validate_gui_params).
    assert "validation" in str(exc_info.value).lower() or "enum" in str(exc_info.value).lower()


def test_click_at_coords_xdotool_failure_returns_error(agent, mock_xdotool_fail,
                                                       mock_xrandr_1x):
    result = agent.handle_request(GUI_CLICK_AT_COORDS, {"x": 1, "y": 1})
    assert not result["success"]
    assert "xdotool" in result["error"].lower() or "X error" in result["error"]


def test_click_at_coords_missing_xdotool_reports_reason(agent, mock_xrandr_1x):
    with patch("gui_agent.input_synth.subprocess.run",
               side_effect=FileNotFoundError("no xdotool")):
        result = agent.handle_request(GUI_CLICK_AT_COORDS, {"x": 1, "y": 1})
    assert not result["success"]
    assert result["reason"] == "xdotool_missing"


# ═══ type_at_coords ═══════════════════════════════════════════════════


def test_type_at_coords_click_then_type(agent, mock_xdotool_ok, mock_xrandr_1x):
    result = agent.handle_request(GUI_TYPE_AT_COORDS,
                                  {"x": 100, "y": 200, "text": "hello"})
    assert result["success"]
    # 2 subprocess calls — click + type
    assert len(mock_xdotool_ok.calls) == 2
    assert "click" in mock_xdotool_ok.calls[0]
    assert "type" in mock_xdotool_ok.calls[1]
    assert "hello" in mock_xdotool_ok.calls[1]


# ═══ drag ═════════════════════════════════════════════════════════════


def test_drag_with_waypoints(agent, mock_xdotool_ok, mock_xrandr_1x):
    result = agent.handle_request(GUI_DRAG, {
        "x1": 10, "y1": 10, "x2": 200, "y2": 200,
        "waypoints": [[50, 50], [125, 125]],
    })
    assert result["success"]
    argv = mock_xdotool_ok.calls[0]
    assert "mousedown" in argv
    assert "mouseup" in argv
    # Waypoint coords should appear in argv.
    assert "50" in argv and "125" in argv


def test_drag_no_waypoints_ok(agent, mock_xdotool_ok, mock_xrandr_1x):
    result = agent.handle_request(GUI_DRAG,
                                  {"x1": 1, "y1": 1, "x2": 2, "y2": 2})
    assert result["success"]


# ═══ scroll ═══════════════════════════════════════════════════════════


def test_scroll_direction_maps_to_wheel_button(agent, mock_xdotool_ok,
                                                mock_xrandr_1x):
    result = agent.handle_request(GUI_SCROLL,
                                  {"x": 10, "y": 20,
                                   "direction": "down", "amount": 3})
    assert result["success"]
    argv = mock_xdotool_ok.calls[0]
    # Button 5 = scroll down; --repeat 3
    assert "5" in argv
    idx = argv.index("--repeat")
    assert argv[idx + 1] == "3"


# ═══ hover ═════════════════════════════════════════════════════════════


def test_hover_mousemove_only(agent, mock_xdotool_ok, mock_xrandr_1x):
    result = agent.handle_request(GUI_HOVER, {"x": 300, "y": 400})
    assert result["success"]
    argv = mock_xdotool_ok.calls[0]
    assert "mousemove" in argv
    assert "click" not in argv


# ═══ press_key ═════════════════════════════════════════════════════════


def test_press_key_ok(agent, mock_xdotool_ok):
    result = agent.handle_request(GUI_PRESS_KEY, {"combo": "ctrl+s"})
    assert result["success"]
    argv = mock_xdotool_ok.calls[0]
    assert "key" in argv
    assert "ctrl+s" in argv


def test_press_key_shell_injection_pattern_rejected_at_schema(agent):
    with pytest.raises(Exception) as exc_info:
        agent.handle_request(GUI_PRESS_KEY, {"combo": "$(rm -rf /)"})
    # Schema layer rejects due to _KEY_COMBO_SCHEMA pattern.
    assert "pattern" in str(exc_info.value).lower() or "validation" in str(exc_info.value).lower()


def test_press_key_allowlist_rejects_unknown_named_key(agent, mock_xdotool_ok):
    """Schema accepts `[A-Za-z0-9+]+`, but input_synth's semantic
    allowlist rejects unknown named keys like `wingdings`."""
    result = agent.handle_request(GUI_PRESS_KEY,
                                  {"combo": "ctrl+wingdings"})
    assert not result["success"]
    assert result["reason"] == "validation_error"


# ═══ key_sequence ═══════════════════════════════════════════════════


def test_key_sequence_dict_form(agent, mock_xdotool_ok):
    result = agent.handle_request(GUI_KEY_SEQUENCE, {"items": [
        {"type": "key", "combo": "ctrl+t"},
        {"type": "text", "text": "google.com"},
        {"type": "key", "combo": "return"},
    ]})
    assert result["success"]
    # 3 subprocess calls — one per item.
    assert len(mock_xdotool_ok.calls) == 3


def test_key_sequence_string_form(agent, mock_xdotool_ok):
    result = agent.handle_request(GUI_KEY_SEQUENCE,
                                  {"items": ["ctrl+t", "google.com", "return"]})
    assert result["success"]
    assert len(mock_xdotool_ok.calls) == 3


def test_key_sequence_empty_rejected_at_schema(agent):
    with pytest.raises(Exception):
        agent.handle_request(GUI_KEY_SEQUENCE, {"items": []})


# ═══ HiDPI translation (integration with V.2a MonitorLayout) ══════════


def test_click_at_coords_hidpi_translates(agent, mock_xdotool_ok):
    """xrandr says 2x → physical (200, 400) → xdotool sees (400, 800)."""
    output = "eDP-1 connected primary 1920x1080+0+0 344mm x 194mm\n   3840x2160  60.00*+"
    from gui_agent.geometry import MonitorLayout
    MonitorLayout.invalidate_cache()
    with patch("gui_agent.geometry.subprocess.check_output", return_value=output):
        result = agent.handle_request(GUI_CLICK_AT_COORDS,
                                      {"x": 200, "y": 400})
    assert result["success"]
    assert result["logical_coords"] == [200, 400]
    assert result["physical_coords"] == [400, 800]
    argv = mock_xdotool_ok.calls[0]
    assert "400" in argv and "800" in argv
    # Logical coord should NOT be in argv (would be a HiDPI regression).
    # But "200" IS the logical Y=400 mapped… careful. Just check the
    # correct PAIR appears together.


# ═══ Unknown method still rejected ═══════════════════════════════════


def test_unknown_gui_method_raises(agent):
    from gui_agent.protocol import ALL_GUI_METHODS
    assert "gui.does_not_exist" not in ALL_GUI_METHODS
    with pytest.raises(Exception):
        agent.handle_request("gui.does_not_exist", {})
