"""Tests for gui_agent.geometry — xrandr parsing + HiDPI translation."""

from __future__ import annotations

from unittest.mock import patch

from gui_agent.geometry import Monitor, MonitorLayout, _parse_xrandr


def test_parse_xrandr_single_monitor():
    output = """Screen 0: minimum 8 x 8, current 1920 x 1080, maximum 32767 x 32767
eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 344mm x 194mm
   1920x1080     60.00*+  59.93    50.00
   1680x1050     59.95
HDMI-1 disconnected (normal left inverted right x axis y axis)"""
    monitors = _parse_xrandr(output)
    assert len(monitors) == 1
    m = monitors[0]
    assert m.name == "eDP-1"
    assert m.origin_x == 0 and m.origin_y == 0
    assert m.width == 1920 and m.height == 1080
    # Physical == logical → scale 1.0
    assert m.scale == 1.0


def test_parse_xrandr_hidpi_2x():
    """Physical mode 3840x2160 on a logical 1920x1080 → scale 2.0."""
    output = """eDP-1 connected primary 1920x1080+0+0 344mm x 194mm
   3840x2160     60.00*+
   1920x1080     59.93"""
    monitors = _parse_xrandr(output)
    assert len(monitors) == 1
    assert monitors[0].scale == 2.0


def test_parse_xrandr_scale_override():
    output = """eDP-1 connected primary 1920x1080+0+0 344mm x 194mm
   1920x1080     60.00*+"""
    monitors = _parse_xrandr(output, scale_override=3)
    assert monitors[0].scale == 3.0


def test_parse_xrandr_multi_monitor():
    output = """eDP-1 connected primary 1920x1080+0+0 344mm x 194mm
   1920x1080     60.00*+
HDMI-1 connected 2560x1440+1920+0 597mm x 336mm
   2560x1440     59.95*+"""
    monitors = _parse_xrandr(output)
    assert len(monitors) == 2
    assert monitors[0].name == "eDP-1"
    assert monitors[1].name == "HDMI-1"
    assert monitors[1].origin_x == 1920


def test_parse_xrandr_empty_returns_empty():
    assert _parse_xrandr("") == []


def test_monitor_layout_find_monitor():
    a = Monitor("a", 0, 0, 100, 100, 1.0)
    b = Monitor("b", 100, 0, 200, 100, 1.0)
    layout = MonitorLayout(monitors=(a, b))
    assert layout.find_monitor(50, 50).name == "a"
    assert layout.find_monitor(150, 50).name == "b"
    # Off-screen: falls back to primary.
    assert layout.find_monitor(-1, -1).name == "a"


def test_monitor_layout_translation_roundtrip():
    m = Monitor("test", 100, 200, 800, 600, 2.0)
    layout = MonitorLayout(monitors=(m,))
    # logical (100, 200) is at monitor origin; physical should also be (100, 200)
    assert layout.logical_to_physical(100, 200) == (100, 200)
    # logical (200, 300) is 100 logical from origin → 200 physical from origin
    assert layout.logical_to_physical(200, 300) == (300, 400)
    # physical_to_logical is the inverse
    px, py = layout.logical_to_physical(200, 300)
    lx, ly = layout.physical_to_logical(px, py)
    assert (lx, ly) == (200, 300)


def test_monitor_layout_detect_falls_back_when_xrandr_missing():
    """No xrandr → fallback single monitor at 1x scale."""
    with patch("gui_agent.geometry.subprocess.check_output",
               side_effect=FileNotFoundError("no xrandr")):
        MonitorLayout.invalidate_cache()
        layout = MonitorLayout.detect()
        assert len(layout.monitors) == 1
        assert layout.monitors[0].name == "fallback"
        assert layout.monitors[0].scale == 1.0


def test_monitor_layout_detect_falls_back_when_xrandr_errors():
    """xrandr present but errors → fallback."""
    import subprocess as sp
    with patch("gui_agent.geometry.subprocess.check_output",
               side_effect=sp.CalledProcessError(1, "xrandr")):
        MonitorLayout.invalidate_cache()
        layout = MonitorLayout.detect()
        assert len(layout.monitors) == 1
        assert layout.monitors[0].name == "fallback"


def test_monitor_layout_cache_hit_avoids_reparse():
    """Detect within TTL returns cached layout without spawning xrandr."""
    from gui_agent.geometry import MonitorLayout as GL

    call_count = {"n": 0}
    def _counted(*args, **kwargs):
        call_count["n"] += 1
        return "eDP-1 connected primary 1920x1080+0+0 344mm x 194mm\n   1920x1080  60.00*+"

    GL.invalidate_cache()
    with patch("gui_agent.geometry.subprocess.check_output", side_effect=_counted):
        GL.detect()
        GL.detect()
        GL.detect()
    assert call_count["n"] == 1, "cache did not deduplicate calls"
