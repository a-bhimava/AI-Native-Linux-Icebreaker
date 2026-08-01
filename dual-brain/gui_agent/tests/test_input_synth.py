"""Tests for gui_agent.input_synth — xdotool wrappers.

xdotool is monkey-patched so tests never actually move the user's mouse
or type into the operator's terminal. HiDPI translation with an
injected MonitorLayout is covered here at the API boundary; the xrandr
parser + MonitorLayout unit tests live in test_geometry.py.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gui_agent.geometry import Monitor, MonitorLayout
from gui_agent.input_synth import (
    ActionResult,
    InputSynthUnavailable,
    InputSynthValidationError,
    _classify_sequence_item,
    _validate_key_combo,
    check_available,
    click,
    double_click,
    drag,
    hover,
    key_sequence,
    press_key,
    right_click,
    scroll,
    type_at_coords,
    type_text,
)


# ═══ Fixtures ═════════════════════════════════════════════════════════


def _fake_run_ok(argv, **kwargs):
    """subprocess.run monkeypatch that always succeeds and captures argv."""
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = b""
    proc.stderr = b""
    _fake_run_ok.last_argv = argv
    _fake_run_ok.calls.append(argv)
    return proc


@pytest.fixture(autouse=True)
def reset_call_log():
    _fake_run_ok.calls = []
    _fake_run_ok.last_argv = None
    yield


@pytest.fixture
def mock_xdotool_ok():
    """Patch subprocess.run so xdotool commands succeed without a real
    subprocess."""
    with patch("gui_agent.input_synth.subprocess.run", side_effect=_fake_run_ok) as m:
        yield m


# ═══ Availability ═════════════════════════════════════════════════════


def test_check_available_true_when_xdotool_runs(mock_xdotool_ok):
    assert check_available() is True


def test_check_available_false_when_missing():
    with patch("gui_agent.input_synth.subprocess.run",
               side_effect=FileNotFoundError("nope")):
        assert check_available() is False


# ═══ click / hover / right_click / double_click ═══════════════════════


def test_click_left_at_coords(mock_xdotool_ok):
    r = click(x=100, y=200)
    assert r.success
    assert r.physical_coords == (100, 200)
    assert r.logical_coords == (100, 200)
    argv = _fake_run_ok.last_argv
    assert argv[0].endswith("xdotool")
    assert "mousemove" in argv and "100" in argv and "200" in argv
    assert "click" in argv
    # Left button = 1
    assert "1" in argv


def test_click_right_button(mock_xdotool_ok):
    r = right_click(x=50, y=60)
    assert r.success
    # button 3 = right
    assert "3" in _fake_run_ok.last_argv


def test_double_click_uses_repeat_2(mock_xdotool_ok):
    r = double_click(x=10, y=20)
    assert r.success
    argv = _fake_run_ok.last_argv
    assert "--repeat" in argv
    idx = argv.index("--repeat")
    assert argv[idx + 1] == "2"


def test_hover_no_click(mock_xdotool_ok):
    r = hover(x=300, y=400)
    assert r.success
    argv = _fake_run_ok.last_argv
    assert "mousemove" in argv
    assert "click" not in argv


def test_click_out_of_bounds_rejected():
    with pytest.raises(InputSynthValidationError):
        click(x=100000, y=200)
    with pytest.raises(InputSynthValidationError):
        click(x=100, y=-1)


def test_click_bad_button_rejected():
    with pytest.raises(InputSynthValidationError):
        click(x=1, y=1, button="middle-mouse")  # not in allowlist


def test_click_bad_count_rejected():
    with pytest.raises(InputSynthValidationError):
        click(x=1, y=1, count=5)


# ═══ type_text + type_at_coords ═════════════════════════════════════


def test_type_text_ok(mock_xdotool_ok):
    r = type_text("hello world")
    assert r.success
    argv = _fake_run_ok.last_argv
    assert "type" in argv
    assert "hello world" in argv


def test_type_text_length_capped():
    with pytest.raises(InputSynthValidationError):
        type_text("x" * 5000)


def test_type_text_rejects_control_chars():
    with pytest.raises(InputSynthValidationError):
        type_text("bad\x00null")
    with pytest.raises(InputSynthValidationError):
        type_text("bad\x01ctrl")


def test_type_text_allows_newline_and_tab(mock_xdotool_ok):
    r = type_text("multi\nline\ttext")
    assert r.success


def test_type_at_coords_clicks_then_types(mock_xdotool_ok):
    r = type_at_coords(x=100, y=200, text="hi")
    assert r.success
    assert len(_fake_run_ok.calls) == 2  # click, then type
    assert _fake_run_ok.calls[0][0].endswith("xdotool")
    assert "click" in _fake_run_ok.calls[0]
    assert "type" in _fake_run_ok.calls[1]


def test_type_at_coords_aborts_on_click_failure():
    with patch("gui_agent.input_synth.subprocess.run") as run_mock:
        # Click returns rc=1 → subsequent type should NOT fire.
        proc = MagicMock(returncode=1, stdout=b"", stderr=b"boom")
        run_mock.return_value = proc
        r = type_at_coords(x=1, y=1, text="x")
        assert not r.success
        # Only one call — the failed click.
        assert run_mock.call_count == 1


# ═══ drag ═════════════════════════════════════════════════════════════


def test_drag_with_waypoints(mock_xdotool_ok):
    r = drag(x1=10, y1=10, x2=100, y2=100, waypoints=[(50, 50), (75, 75)])
    assert r.success
    argv = _fake_run_ok.last_argv
    # mousedown then mousemove for each waypoint then final mousemove
    assert "mousedown" in argv
    assert "mouseup" in argv
    # Both waypoint coords land in the argv
    assert "50" in argv and "75" in argv


def test_drag_too_many_waypoints_rejected():
    with pytest.raises(InputSynthValidationError):
        drag(x1=1, y1=1, x2=2, y2=2,
             waypoints=[(i, i) for i in range(20)])


def test_drag_bad_hold_ms_rejected():
    with pytest.raises(InputSynthValidationError):
        drag(x1=1, y1=1, x2=2, y2=2, hold_ms=99999)


# ═══ scroll ═══════════════════════════════════════════════════════════


def test_scroll_direction_maps_to_button(mock_xdotool_ok):
    r = scroll(x=10, y=20, direction="down", amount=3)
    assert r.success
    argv = _fake_run_ok.last_argv
    # Button 5 = scroll down
    assert "5" in argv
    idx = argv.index("--repeat")
    assert argv[idx + 1] == "3"


def test_scroll_horizontal_button(mock_xdotool_ok):
    r = scroll(x=10, y=20, direction="right", amount=1)
    assert r.success
    # Button 7 = scroll right
    assert "7" in _fake_run_ok.last_argv


def test_scroll_bad_direction_rejected():
    with pytest.raises(InputSynthValidationError):
        scroll(x=1, y=1, direction="diagonal", amount=1)


def test_scroll_bad_amount_rejected():
    with pytest.raises(InputSynthValidationError):
        scroll(x=1, y=1, direction="up", amount=500)


# ═══ press_key ═════════════════════════════════════════════════════════


def test_press_key_valid_combos(mock_xdotool_ok):
    for combo in ("ctrl+s", "ctrl+shift+t", "F12", "escape", "return", "alt+F4"):
        _fake_run_ok.calls.clear()
        r = press_key(combo)
        assert r.success, f"{combo} failed unexpectedly"
        argv = _fake_run_ok.last_argv
        assert combo in argv


def test_press_key_rejects_shell_injection():
    for bad in ("$(rm -rf /)", "ctrl+`whoami`", "ctrl+s; ls", "$(:)"):
        with pytest.raises(InputSynthValidationError):
            press_key(bad)


def test_press_key_rejects_disallowed_token():
    with pytest.raises(InputSynthValidationError):
        press_key("ctrl+wingdings")  # wingdings not in allowlist


def test_press_key_rejects_too_many_tokens():
    with pytest.raises(InputSynthValidationError):
        press_key("ctrl+shift+alt+meta+super+cmd+F1")


def test_press_key_rejects_empty():
    with pytest.raises(InputSynthValidationError):
        press_key("")
    with pytest.raises(InputSynthValidationError):
        press_key("ctrl+")  # empty second token


# ═══ key_sequence ═════════════════════════════════════════════════════


def test_key_sequence_alternating_key_and_text(mock_xdotool_ok):
    r = key_sequence([
        {"type": "key", "combo": "ctrl+t"},
        {"type": "text", "text": "google.com"},
        {"type": "key", "combo": "return"},
    ])
    assert r.success
    assert len(_fake_run_ok.calls) == 3


def test_key_sequence_string_form(mock_xdotool_ok):
    # Plain strings: "ctrl+t" is a combo (has +), "google.com" is text,
    # "return" is a named key so key. "hello" would be text.
    r = key_sequence(["ctrl+t", "google.com", "return"])
    assert r.success


def test_key_sequence_too_many_items_rejected():
    with pytest.raises(InputSynthValidationError):
        key_sequence(["a"] * 100)


def test_key_sequence_empty_rejected():
    with pytest.raises(InputSynthValidationError):
        key_sequence([])


def test_classify_sequence_item_string_heuristics():
    assert _classify_sequence_item("ctrl+s", 0) == ("key", "ctrl+s")
    assert _classify_sequence_item("escape", 0) == ("key", "escape")
    assert _classify_sequence_item("hello world", 0) == ("text", "hello world")
    assert _classify_sequence_item({"type": "text", "text": "abc"}, 0) == ("text", "abc")


def test_key_sequence_stops_at_first_failure():
    """If item 2 fails, items 3+ don't fire."""
    call_ct = {"n": 0}

    def _fake(argv, **kwargs):
        call_ct["n"] += 1
        proc = MagicMock()
        # Fail on the third call (0-indexed item 2).
        proc.returncode = 1 if call_ct["n"] == 3 else 0
        proc.stdout = b""
        proc.stderr = b"simulated fail"
        return proc

    with patch("gui_agent.input_synth.subprocess.run", side_effect=_fake):
        r = key_sequence(["ctrl+t", "abc", "ctrl+w", "should_not_run"])
        assert not r.success
        assert call_ct["n"] == 3, "sequence continued past a failure"
        assert r.extra["failed_at"] == 2


# ═══ subprocess timeout / OSError ═════════════════════════════════════


def test_click_timeout_returns_failed_result():
    with patch("gui_agent.input_synth.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd=["xdotool"], timeout=5)):
        r = click(x=1, y=1)
        assert not r.success
        assert "timed out" in r.error


def test_click_missing_xdotool_raises_unavailable():
    with patch("gui_agent.input_synth.subprocess.run",
               side_effect=FileNotFoundError("no xdotool")):
        with pytest.raises(InputSynthUnavailable):
            click(x=1, y=1)


# ═══ HiDPI translation ═════════════════════════════════════════════════


def test_click_translates_hidpi_2x(mock_xdotool_ok):
    """A 2x-scale monitor means a logical (100, 200) becomes physical (200, 400)."""
    layout = MonitorLayout(monitors=(Monitor(
        name="test", origin_x=0, origin_y=0,
        width=1920, height=1080, scale=2.0
    ),))
    r = click(x=100, y=200, layout=layout)
    assert r.success
    assert r.logical_coords == (100, 200)
    assert r.physical_coords == (200, 400)
    argv = _fake_run_ok.last_argv
    assert "200" in argv and "400" in argv
    # Original logical coords should NOT appear (would be a bug).
    assert "100" not in argv


def test_no_layout_passes_coords_unchanged(mock_xdotool_ok):
    r = click(x=100, y=200, layout=None)
    assert r.physical_coords == (100, 200)


# MonitorLayout / xrandr parsing tests live in test_geometry.py.
# This file only exercises the *interaction* between input_synth and
# a caller-provided MonitorLayout (see test_click_translates_hidpi_2x
# above).
