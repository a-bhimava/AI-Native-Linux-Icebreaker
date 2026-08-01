"""Tests for GuiAgent.handle_request(gui.parse_screen) — V.4c.

The parse_screen handler composes three subsystems:
  1. ScreenshotManager.capture()      (existing, v6.14)
  2. VisionGrounder.parse_screen()    (V.1)
  3. annotate.render_annotated()      (V.3a)
  4. notify-send subprocess           (helper in agent.py)

Tests exercise every failure branch (screenshot unavailable, VLM
disabled/failed/cost-ceiling, annotate exception, notify-send
absent) so downstream callers can rely on the structured error
`reason` field.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from gui_agent.agent import GuiAgent
from gui_agent.protocol import GUI_PARSE_SCREEN
from gui_agent.vision import (
    ElementBox,
    ParseResult,
    VisionAllBackendsFailed,
    VisionCostCeilingExceeded,
    VisionDisabled,
    VisionMalformedResponse,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def scratch_dir(tmp_path):
    d = tmp_path / "scratch"
    d.mkdir()
    return d


@pytest.fixture
def screenshot_result(scratch_dir):
    """A ScreenshotResult with a real PNG file on disk so annotate can
    open it."""
    from gui_agent.screenshots import ScreenshotResult
    png_path = scratch_dir / "screenshot.png"
    img = Image.new("RGB", (400, 300), color=(30, 30, 40))
    img.save(png_path, "PNG")
    return ScreenshotResult(
        path=png_path,
        sha256="a" * 64,
        width=400, height=300,
        timestamp=1.0,
    )


def _canned_parse_result(elements=None) -> ParseResult:
    els = elements if elements is not None else [
        ElementBox(id=0, box=(20, 20, 100, 40),
                   caption="Sign in", kind="button", confidence=0.95),
        ElementBox(id=1, box=(20, 80, 200, 30),
                   caption="Email", kind="input", confidence=0.9),
    ]
    return ParseResult(
        elements=tuple(els),
        vlm_latency_ms=450,
        vlm_cost_usd=0.0001,
        backend_used="gemini/gemini-2.5-flash",
        cached=False,
        screenshot_sha256="canned_sha_" + "0" * 53,
    )


def _mock_agent(scratch_dir, screenshot_result, parse_result=None,
                capture_error=None, parse_error=None):
    """Build a GuiAgent with the screenshot manager + vision grounder
    fully mocked."""
    agent = GuiAgent(scratch_dir=scratch_dir)
    # Mock the screenshot manager on the agent instance.
    agent._screenshots = MagicMock()
    if capture_error:
        agent._screenshots.capture.side_effect = capture_error
    else:
        agent._screenshots.capture.return_value = screenshot_result
    # Mock the vision grounder.
    mock_vision = MagicMock()
    if parse_error:
        mock_vision.parse_screen.side_effect = parse_error
    else:
        mock_vision.parse_screen.return_value = parse_result or _canned_parse_result()
    mock_vision.turn_cost_usd.return_value = 0.0001
    agent._vision = mock_vision
    return agent


# ═══ Happy path ═══════════════════════════════════════════════════════


def test_parse_screen_returns_elements_and_preview(scratch_dir, screenshot_result):
    """Full happy path — screenshot + parse + annotate + notify-send."""
    agent = _mock_agent(scratch_dir, screenshot_result)
    with patch("gui_agent.agent._try_notify_send", return_value=True):
        result = agent.handle_request(GUI_PARSE_SCREEN, {"window": "Slack"})

    assert "elements" in result
    assert len(result["elements"]) == 2
    assert result["elements"][0]["caption"] == "Sign in"
    assert result["backend_used"] == "gemini/gemini-2.5-flash"
    assert result["cached"] is False
    assert result["vlm_cost_usd"] == pytest.approx(0.0001)
    assert result["vlm_latency_ms"] == 450
    assert result["cost_this_turn_usd"] == pytest.approx(0.0001)
    assert result["notify_sent"] is True
    # Preview path should be a real file (annotate wrote it).
    assert Path(result["preview_path"]).is_file()


def test_parse_screen_prompt_hint_passed_through(scratch_dir, screenshot_result):
    agent = _mock_agent(scratch_dir, screenshot_result)
    with patch("gui_agent.agent._try_notify_send", return_value=True):
        agent.handle_request(GUI_PARSE_SCREEN,
                             {"window": "Firefox", "prompt_hint": "find URL bar"})
    call = agent._vision.parse_screen.call_args
    assert call.kwargs.get("prompt_hint") == "find URL bar"


def test_parse_screen_empty_window_works(scratch_dir, screenshot_result):
    """Empty window param → full-screen capture; still succeeds."""
    agent = _mock_agent(scratch_dir, screenshot_result)
    with patch("gui_agent.agent._try_notify_send", return_value=True):
        result = agent.handle_request(GUI_PARSE_SCREEN, {})
    assert "elements" in result


def test_parse_screen_element_shape_matches_schema(scratch_dir, screenshot_result):
    """Every returned element has id, box, caption, kind, confidence."""
    agent = _mock_agent(scratch_dir, screenshot_result)
    with patch("gui_agent.agent._try_notify_send", return_value=True):
        result = agent.handle_request(GUI_PARSE_SCREEN, {"window": "S"})
    for e in result["elements"]:
        assert set(e.keys()) >= {"id", "box", "caption", "kind", "confidence"}
        assert isinstance(e["id"], int)
        assert len(e["box"]) == 4
        assert e["kind"] in {"button", "input", "link", "icon", "text",
                             "list", "menu", "tab", "checkbox", "radio",
                             "slider", "other"}


# ═══ Failure taxonomy ════════════════════════════════════════════════


def test_screenshot_unavailable_returns_reason(scratch_dir):
    from gui_agent.screenshots import ScreenshotUnavailableError
    agent = _mock_agent(scratch_dir, None,
                        capture_error=ScreenshotUnavailableError("no portal"))
    result = agent.handle_request(GUI_PARSE_SCREEN, {"window": "x"})
    assert "elements" not in result
    assert result["reason"] == "screenshot_unavailable"
    assert "no portal" in result["error"]


def test_vision_disabled_returns_reason(scratch_dir, screenshot_result):
    agent = _mock_agent(scratch_dir, screenshot_result,
                        parse_error=VisionDisabled("off"))
    result = agent.handle_request(GUI_PARSE_SCREEN, {"window": "x"})
    assert result["reason"] == "vision_disabled"


def test_vision_cost_ceiling_returns_reason(scratch_dir, screenshot_result):
    agent = _mock_agent(scratch_dir, screenshot_result,
                        parse_error=VisionCostCeilingExceeded(
                            would_reach_usd=0.05, ceiling_usd=0.01))
    result = agent.handle_request(GUI_PARSE_SCREEN, {"window": "x"})
    assert result["reason"] == "vision_cost_ceiling_exceeded"


def test_vision_malformed_response_returns_reason(scratch_dir, screenshot_result):
    agent = _mock_agent(scratch_dir, screenshot_result,
                        parse_error=VisionMalformedResponse("garbled"))
    result = agent.handle_request(GUI_PARSE_SCREEN, {"window": "x"})
    assert result["reason"] == "vision_malformed_response"


def test_vision_all_backends_failed_returns_reason(scratch_dir, screenshot_result):
    agent = _mock_agent(scratch_dir, screenshot_result,
                        parse_error=VisionAllBackendsFailed("gemini + claude down"))
    result = agent.handle_request(GUI_PARSE_SCREEN, {"window": "x"})
    assert result["reason"] == "vision_all_backends_failed"


def test_vision_unexpected_exception_returns_reason(scratch_dir, screenshot_result):
    agent = _mock_agent(scratch_dir, screenshot_result,
                        parse_error=RuntimeError("something weird"))
    result = agent.handle_request(GUI_PARSE_SCREEN, {"window": "x"})
    assert result["reason"] == "vision_unexpected_error"


def test_annotate_failure_does_not_break_parse(scratch_dir, screenshot_result):
    """annotate is best-effort — a failure there records a synthetic
    preview_path but the parse elements still surface."""
    agent = _mock_agent(scratch_dir, screenshot_result)
    with patch("gui_agent.annotate.render_annotated",
               side_effect=RuntimeError("PIL blew up")):
        with patch("gui_agent.agent._try_notify_send", return_value=False):
            result = agent.handle_request(GUI_PARSE_SCREEN, {"window": "x"})
    assert "elements" in result
    assert len(result["elements"]) == 2
    assert result["preview_path"].startswith("annotate-failed:")
    # notify-send should NOT fire when there's no real preview file.
    assert result["notify_sent"] is False


def test_notify_send_missing_records_false_but_still_returns_elements(
    scratch_dir, screenshot_result
):
    """notify-send binary missing → notify_sent=False but everything
    else works. This is the actual macOS-dev state."""
    agent = _mock_agent(scratch_dir, screenshot_result)
    with patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_PARSE_SCREEN, {"window": "x"})
    assert "elements" in result
    assert result["notify_sent"] is False


# ═══ notify-send helper ══════════════════════════════════════════════


def test_try_notify_send_returns_true_on_rc_zero():
    from gui_agent.agent import _try_notify_send
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0)
        assert _try_notify_send("t", "b", "/tmp/x.png") is True


def test_try_notify_send_returns_false_on_missing_binary():
    from gui_agent.agent import _try_notify_send
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert _try_notify_send("t", "b", "/tmp/x.png") is False


def test_try_notify_send_returns_false_on_timeout():
    import subprocess
    from gui_agent.agent import _try_notify_send
    with patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired("notify-send", 2)):
        assert _try_notify_send("t", "b", "/tmp/x.png") is False


# ═══ Vision config extraction ═════════════════════════════════════════


def test_extract_vision_config_from_namespace():
    ns = SimpleNamespace(vision=SimpleNamespace(
        backend="anthropic/claude-haiku-4-5",
        cost_ceiling_usd_per_turn=0.05,
    ))
    result = GuiAgent._extract_vision_config(ns)
    assert result["backend"] == "anthropic/claude-haiku-4-5"
    assert result["cost_ceiling_usd_per_turn"] == 0.05


def test_extract_vision_config_from_dict():
    cfg = {"vision": {"backend": "gemini/gemini-2.5-flash"}}
    result = GuiAgent._extract_vision_config(cfg)
    assert result["backend"] == "gemini/gemini-2.5-flash"


def test_extract_vision_config_absent_returns_empty():
    assert GuiAgent._extract_vision_config(None) == {}
    assert GuiAgent._extract_vision_config(SimpleNamespace()) == {}
    assert GuiAgent._extract_vision_config({}) == {}


# ═══ Vision grounder is lazy and cached ══════════════════════════════


def test_get_vision_lazy_and_memoized(scratch_dir):
    agent = GuiAgent(scratch_dir=scratch_dir)
    assert agent._vision is None   # not created at __init__
    v1 = agent._get_vision()
    v2 = agent._get_vision()
    assert v1 is v2   # same instance across calls
