"""Tests for GuiAgent grounded_* orchestrators — V.4d.

Each grounded tool composes 5-7 steps: screenshot → parse → preview →
LLM element-pick → optional trust check → input_synth actuation. These
tests mock every subsystem so the full compose path is exercised
deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from gui_agent.agent import GuiAgent, _by_id
from gui_agent.protocol import (
    GUI_GROUNDED_CLICK,
    GUI_GROUNDED_DRAG,
    GUI_GROUNDED_SCROLL,
    GUI_GROUNDED_TYPE,
)
from gui_agent.screenshots import ScreenshotResult
from gui_agent.vision import ElementBox, ParseResult


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def scratch_dir(tmp_path):
    d = tmp_path / "scratch"
    d.mkdir()
    return d


@pytest.fixture
def screenshot(scratch_dir):
    png_path = scratch_dir / "screenshot.png"
    Image.new("RGB", (800, 600), color=(30, 30, 40)).save(png_path, "PNG")
    return ScreenshotResult(path=png_path, sha256="a" * 64,
                            width=800, height=600, timestamp=1.0)


def _elements() -> list[ElementBox]:
    """Standard 3-element parsed screen for tests."""
    return [
        ElementBox(id=0, box=(20, 20, 100, 40),
                   caption="Channel list", kind="list", confidence=0.9),
        ElementBox(id=1, box=(200, 400, 300, 30),
                   caption="Message input", kind="input", confidence=0.95),
        ElementBox(id=2, box=(520, 400, 80, 30),
                   caption="Send", kind="button", confidence=0.97),
    ]


def _parse_result(elements=None) -> ParseResult:
    return ParseResult(
        elements=tuple(elements or _elements()),
        vlm_latency_ms=450,
        vlm_cost_usd=0.0001,
        backend_used="gemini/gemini-2.5-flash",
        cached=False,
        screenshot_sha256="sha_" + "0" * 60,
    )


def _build_agent(scratch_dir, screenshot, parse=None, capture_error=None,
                 parse_error=None):
    agent = GuiAgent(scratch_dir=scratch_dir)
    agent._screenshots = MagicMock()
    if capture_error:
        agent._screenshots.capture.side_effect = capture_error
    else:
        agent._screenshots.capture.return_value = screenshot

    mock_vision = MagicMock()
    if parse_error:
        mock_vision.parse_screen.side_effect = parse_error
    else:
        mock_vision.parse_screen.return_value = parse or _parse_result()
    mock_vision.turn_cost_usd.return_value = 0.0002
    agent._vision = mock_vision
    return agent


def _mock_litellm_pick(payload: dict):
    """Return a MagicMock replacement for litellm.completion that
    yields the given JSON payload as message content."""
    def _fake(**kwargs):
        return {"choices": [{"message": {"content": json.dumps(payload)}}]}
    return _fake


def _mock_xdotool_ok():
    """subprocess.run mock — returns rc=0."""
    def _fn(argv, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = b""
        proc.stderr = b""
        _fn.calls.append(argv)
        return proc
    _fn.calls = []
    return _fn


@pytest.fixture
def mock_xrandr_1x():
    output = "eDP-1 connected primary 1920x1080+0+0\n   1920x1080  60.00*+"
    from gui_agent.geometry import MonitorLayout
    MonitorLayout.invalidate_cache()
    with patch("gui_agent.geometry.subprocess.check_output",
               return_value=output):
        yield
    MonitorLayout.invalidate_cache()


# ═══ grounded_click ═══════════════════════════════════════════════════


def test_grounded_click_happy_path(scratch_dir, screenshot, mock_xrandr_1x):
    agent = _build_agent(scratch_dir, screenshot)
    xdo = _mock_xdotool_ok()
    with patch("litellm.completion",
               side_effect=_mock_litellm_pick({"element_id": 2, "confidence": 0.94})), \
         patch("gui_agent.input_synth.subprocess.run", side_effect=xdo), \
         patch("gui_agent.agent._try_notify_send", return_value=True):
        result = agent.handle_request(GUI_GROUNDED_CLICK, {
            "window": "Slack", "prompt": "the Send button",
        })

    assert result["success"], f"expected success, got {result}"
    assert result["picked"]["id"] == 2
    assert result["picked"]["caption"] == "Send"
    # Element 2 centroid = (520 + 40, 400 + 15) = (560, 415)
    assert result["physical_coords"] == [560, 415]
    # xdotool click fired
    argv = xdo.calls[0]
    assert "mousemove" in argv and "click" in argv
    # Meta from parse
    assert result["backend_used"] == "gemini/gemini-2.5-flash"
    assert len(result["elements"]) == 3


def test_grounded_click_screenshot_fails_returns_reason(scratch_dir):
    from gui_agent.screenshots import ScreenshotUnavailableError
    agent = _build_agent(scratch_dir, None,
                         capture_error=ScreenshotUnavailableError("nope"))
    result = agent.handle_request(GUI_GROUNDED_CLICK, {
        "window": "x", "prompt": "y",
    })
    # On the early-exit path (before we get to actuate) there's no
    # `success` key — just `error` + `reason`. Absence itself is the
    # signal.
    assert "success" not in result
    assert result["reason"] == "screenshot_unavailable"
    assert "nope" in result["error"]


def test_grounded_click_vision_disabled_returns_reason(scratch_dir, screenshot):
    from gui_agent.vision import VisionDisabled
    agent = _build_agent(scratch_dir, screenshot,
                         parse_error=VisionDisabled("off"))
    result = agent.handle_request(GUI_GROUNDED_CLICK, {
        "window": "x", "prompt": "y",
    })
    assert result["reason"] == "vision_disabled"


def test_grounded_click_llm_pick_provider_error(scratch_dir, screenshot,
                                                 mock_xrandr_1x):
    agent = _build_agent(scratch_dir, screenshot)
    def _raise(**kwargs):
        raise RuntimeError("Gemini 500")
    with patch("litellm.completion", side_effect=_raise), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_CLICK, {
            "window": "x", "prompt": "the Send button",
        })
    assert result["reason"] == "llm_pick_provider_error"


def test_grounded_click_llm_pick_out_of_range(scratch_dir, screenshot,
                                                mock_xrandr_1x):
    agent = _build_agent(scratch_dir, screenshot)
    # Model returns an id that doesn't exist in the parsed list.
    with patch("litellm.completion",
               side_effect=_mock_litellm_pick({"element_id": 99, "confidence": 0.5})), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_CLICK, {
            "window": "x", "prompt": "y",
        })
    assert result["reason"] == "llm_pick_out_of_range"


def test_grounded_click_llm_pick_malformed_retries_then_gives_up(
    scratch_dir, screenshot, mock_xrandr_1x
):
    agent = _build_agent(scratch_dir, screenshot)
    def _garbage(**kwargs):
        return {"choices": [{"message": {"content": "not json { at all"}}]}
    with patch("litellm.completion", side_effect=_garbage), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_CLICK, {
            "window": "x", "prompt": "y",
        })
    assert result["reason"] == "llm_pick_malformed"


def test_grounded_click_llm_pick_malformed_then_recovers(
    scratch_dir, screenshot, mock_xrandr_1x
):
    """First response is garbage; second (lower temp) parses correctly."""
    agent = _build_agent(scratch_dir, screenshot)
    calls = {"n": 0}
    def _flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"choices": [{"message": {"content": "garbage"}}]}
        return {"choices": [{"message":
                {"content": json.dumps({"element_id": 2, "confidence": 0.8})}}]}
    xdo = _mock_xdotool_ok()
    with patch("litellm.completion", side_effect=_flaky), \
         patch("gui_agent.input_synth.subprocess.run", side_effect=xdo), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_CLICK, {
            "window": "x", "prompt": "Send",
        })
    assert result["success"]
    assert calls["n"] == 2
    assert result["picked"]["id"] == 2


def test_grounded_click_markdown_fence_stripped(
    scratch_dir, screenshot, mock_xrandr_1x
):
    """Gemini sometimes wraps JSON in ```json ... ``` — orchestrator strips."""
    agent = _build_agent(scratch_dir, screenshot)
    def _fenced(**kwargs):
        payload = json.dumps({"element_id": 1, "confidence": 0.9})
        return {"choices": [{"message":
                {"content": f"```json\n{payload}\n```"}}]}
    xdo = _mock_xdotool_ok()
    with patch("litellm.completion", side_effect=_fenced), \
         patch("gui_agent.input_synth.subprocess.run", side_effect=xdo), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_CLICK, {
            "window": "x", "prompt": "Message input",
        })
    assert result["success"]
    assert result["picked"]["id"] == 1


def test_grounded_click_xdotool_failure_surfaces_error(
    scratch_dir, screenshot, mock_xrandr_1x
):
    agent = _build_agent(scratch_dir, screenshot)
    def _fail(argv, **kwargs):
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = b""
        proc.stderr = b"X error"
        return proc
    with patch("litellm.completion",
               side_effect=_mock_litellm_pick({"element_id": 2, "confidence": 0.9})), \
         patch("gui_agent.input_synth.subprocess.run", side_effect=_fail), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_CLICK, {
            "window": "x", "prompt": "y",
        })
    assert not result["success"]
    # ActionResult.error from _run_xdotool carries the inner op name
    # ("click") + rc + stderr. The outer orchestrator's op_name
    # ("grounded_click") isn't re-injected — the inner error is
    # already actionable enough for the audit chain.
    assert "xdotool" in result["error"].lower()
    assert "X error" in result["error"]


def test_grounded_click_with_right_button(scratch_dir, screenshot, mock_xrandr_1x):
    agent = _build_agent(scratch_dir, screenshot)
    xdo = _mock_xdotool_ok()
    with patch("litellm.completion",
               side_effect=_mock_litellm_pick({"element_id": 2, "confidence": 0.9})), \
         patch("gui_agent.input_synth.subprocess.run", side_effect=xdo), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_CLICK, {
            "window": "x", "prompt": "Send", "button": "right",
        })
    assert result["success"]
    # Button 3 = right
    assert "3" in xdo.calls[0]


def test_grounded_click_records_pick_confidence(scratch_dir, screenshot,
                                                 mock_xrandr_1x):
    agent = _build_agent(scratch_dir, screenshot)
    xdo = _mock_xdotool_ok()
    with patch("litellm.completion",
               side_effect=_mock_litellm_pick({"element_id": 2, "confidence": 0.42})), \
         patch("gui_agent.input_synth.subprocess.run", side_effect=xdo), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_CLICK, {
            "window": "x", "prompt": "Send",
        })
    assert result["picked"]["vlm_pick_confidence"] == 0.42


# ═══ grounded_type ════════════════════════════════════════════════════


def test_grounded_type_click_then_type(scratch_dir, screenshot, mock_xrandr_1x):
    agent = _build_agent(scratch_dir, screenshot)
    xdo = _mock_xdotool_ok()
    with patch("litellm.completion",
               side_effect=_mock_litellm_pick({"element_id": 1, "confidence": 0.9})), \
         patch("gui_agent.input_synth.subprocess.run", side_effect=xdo), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_TYPE, {
            "window": "Slack", "prompt": "message input",
            "text": "hello world",
        })
    assert result["success"]
    # type_at_coords fires 2 subprocess calls: click + type
    assert len(xdo.calls) == 2
    assert "click" in xdo.calls[0]
    assert "type" in xdo.calls[1]
    assert "hello world" in xdo.calls[1]
    assert result["picked"]["kind"] == "input"


# ═══ grounded_scroll ══════════════════════════════════════════════════


def test_grounded_scroll_picks_element_and_scrolls(scratch_dir, screenshot,
                                                    mock_xrandr_1x):
    agent = _build_agent(scratch_dir, screenshot)
    xdo = _mock_xdotool_ok()
    with patch("litellm.completion",
               side_effect=_mock_litellm_pick({"element_id": 0, "confidence": 0.9})), \
         patch("gui_agent.input_synth.subprocess.run", side_effect=xdo), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_SCROLL, {
            "window": "Firefox", "prompt": "channel list",
            "direction": "down", "amount": 3,
        })
    assert result["success"]
    # Button 5 = scroll down, --repeat 3
    argv = xdo.calls[0]
    assert "5" in argv
    idx = argv.index("--repeat")
    assert argv[idx + 1] == "3"


def test_grounded_scroll_default_amount(scratch_dir, screenshot, mock_xrandr_1x):
    """No amount → default 3."""
    agent = _build_agent(scratch_dir, screenshot)
    xdo = _mock_xdotool_ok()
    with patch("litellm.completion",
               side_effect=_mock_litellm_pick({"element_id": 0, "confidence": 0.9})), \
         patch("gui_agent.input_synth.subprocess.run", side_effect=xdo), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_SCROLL, {
            "window": "x", "prompt": "y", "direction": "up",
        })
    assert result["success"]
    argv = xdo.calls[0]
    idx = argv.index("--repeat")
    assert argv[idx + 1] == "3"


# ═══ grounded_drag ════════════════════════════════════════════════════


def test_grounded_drag_two_endpoint_pick(scratch_dir, screenshot, mock_xrandr_1x):
    agent = _build_agent(scratch_dir, screenshot)
    xdo = _mock_xdotool_ok()
    with patch("litellm.completion",
               side_effect=_mock_litellm_pick({"source_id": 0, "target_id": 2,
                                                "confidence": 0.85})), \
         patch("gui_agent.input_synth.subprocess.run", side_effect=xdo), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_DRAG, {
            "window": "Files",
            "source_prompt": "channel list",
            "target_prompt": "Send button",
        })
    assert result["success"]
    assert result["picked"]["source_id"] == 0
    assert result["picked"]["target_id"] == 2
    argv = xdo.calls[0]
    assert "mousedown" in argv
    assert "mouseup" in argv


def test_grounded_drag_source_out_of_range(scratch_dir, screenshot, mock_xrandr_1x):
    agent = _build_agent(scratch_dir, screenshot)
    with patch("litellm.completion",
               side_effect=_mock_litellm_pick({"source_id": 99, "target_id": 0,
                                                "confidence": 0.5})), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_DRAG, {
            "window": "x",
            "source_prompt": "s", "target_prompt": "t",
        })
    assert result["reason"] == "llm_pick_out_of_range"


def test_grounded_drag_llm_missing_target_key_retries(scratch_dir, screenshot,
                                                       mock_xrandr_1x):
    """Model returns {"source_id": 0} without target_id → retry with
    lower temp → second call has both → succeeds."""
    agent = _build_agent(scratch_dir, screenshot)
    calls = {"n": 0}
    def _flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"choices": [{"message":
                    {"content": json.dumps({"source_id": 0})}}]}
        return {"choices": [{"message": {"content":
                json.dumps({"source_id": 0, "target_id": 2,
                            "confidence": 0.9})}}]}
    xdo = _mock_xdotool_ok()
    with patch("litellm.completion", side_effect=_flaky), \
         patch("gui_agent.input_synth.subprocess.run", side_effect=xdo), \
         patch("gui_agent.agent._try_notify_send", return_value=False):
        result = agent.handle_request(GUI_GROUNDED_DRAG, {
            "window": "x", "source_prompt": "s", "target_prompt": "t",
        })
    assert result["success"]
    assert calls["n"] == 2


# ═══ Helpers ══════════════════════════════════════════════════════════


def test_by_id_finds():
    els = _elements()
    assert _by_id(els, 1).caption == "Message input"
    assert _by_id(els, 99) is None
    assert _by_id([], 0) is None


def test_grounded_click_preview_repaints_with_target(scratch_dir, screenshot,
                                                      mock_xrandr_1x):
    """After LLM pick, the annotated PNG gets re-rendered with the
    target highlighted + a second notify-send fires."""
    agent = _build_agent(scratch_dir, screenshot)
    xdo = _mock_xdotool_ok()
    notify_calls = []

    def _record_notify(**kwargs):
        notify_calls.append(kwargs)
        return True

    with patch("litellm.completion",
               side_effect=_mock_litellm_pick({"element_id": 2, "confidence": 0.9})), \
         patch("gui_agent.input_synth.subprocess.run", side_effect=xdo), \
         patch("gui_agent.agent._try_notify_send",
               side_effect=lambda **kw: (_record_notify(**kw), True)[1]):
        result = agent.handle_request(GUI_GROUNDED_CLICK, {
            "window": "Slack", "prompt": "Send",
        })

    # Preview path should be a real file (annotate wrote it).
    preview = result.get("preview_path", "")
    assert preview and preview.startswith("/"), \
        f"expected real preview path, got {preview!r}"
    assert Path(preview).is_file()
