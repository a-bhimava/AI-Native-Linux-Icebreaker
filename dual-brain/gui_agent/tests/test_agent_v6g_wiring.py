"""Fix V.6g (2026-08-02) — TrustStore consult + [gui.preview] +
[gui.geometry] wiring tests.

CT-scan P1-1 + P1-2 remediation. Pre-V.6g:
- `gui_agent.trust_store.TrustStore` was dead code — never called
  from agent.py. GT F-105 hard-deny claim was aspirational.
- `[gui.preview] notify_urgency` never reached notify-send argv.
- `[gui.geometry] hidpi_scale_override` never reached
  MonitorLayout.detect().

V.6g wires all three so the config actually takes effect.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest

from gui_agent.agent import GuiAgent, _try_notify_send, _VALID_NOTIFY_URGENCIES
from gui_agent.protocol import (
    GUI_CLICK_AT_COORDS,
    GUI_HOVER,
    GUI_PARSE_SCREEN,
    GUI_PRESS_KEY,
)


# ── _extract_sub_config helper ──────────────────────────────────────


def test_extract_sub_config_namespace():
    """SimpleNamespace with .trust attr → returns dict."""
    ns = SimpleNamespace(trust=SimpleNamespace(enabled=False, store_path="/x"))
    d = GuiAgent._extract_sub_config(ns, "trust")
    assert d == {"enabled": False, "store_path": "/x"}


def test_extract_sub_config_dict():
    d = GuiAgent._extract_sub_config({"preview": {"notify_urgency": "critical"}},
                                     "preview")
    assert d == {"notify_urgency": "critical"}


def test_extract_sub_config_missing_returns_empty():
    assert GuiAgent._extract_sub_config(None, "trust") == {}
    assert GuiAgent._extract_sub_config({}, "trust") == {}
    assert GuiAgent._extract_sub_config(SimpleNamespace(), "trust") == {}


# ── Trust store hard-deny consult ───────────────────────────────────


@pytest.fixture
def agent_with_trust(tmp_path, monkeypatch):
    """GuiAgent with a real TrustStore backed by a tmp path + defaults
    dir containing a gnome-terminal deny_always rule."""
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "01-defaults.jsonl").write_text(
        '{"app":"gnome-terminal","tool":"*","tier":"deny_always","reason":"test"}\n'
    )
    cfg = SimpleNamespace(
        trust=SimpleNamespace(
            enabled=True,
            store_path=str(tmp_path / "trust.jsonl"),
            defaults_dir=str(defaults),
        ),
    )
    agent = GuiAgent(scratch_dir=str(tmp_path), config=cfg)
    return agent


def test_raw_click_at_coords_has_no_window_hint(agent_with_trust):
    """gui.click_at_coords doesn't have a `window` param — the trust
    check falls back to '*' wildcard. Terminal hard-deny is keyed on
    app=gnome-terminal, so raw pixel-coord clicks would NOT be blocked
    even when they land on the terminal window. This is a KNOWN gap
    that documented behavior: only grounded_* tools carry window
    context. Raw actions are trusted-by-window-focus in v6.16."""
    with patch("gui_agent.input_synth.subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0)
        result = agent_with_trust.handle_request(GUI_CLICK_AT_COORDS, {
            "x": 100, "y": 200,
        })
    # No trust_deny fired (raw action, no window hint = "*" app match
    # against gnome-terminal-scoped deny doesn't fire).
    assert result.get("reason") != "trust_deny"


def test_trust_hard_deny_blocks_grounded_action_by_window(
    agent_with_trust, monkeypatch
):
    """grounded_click with window='gnome-terminal' → trust_deny.
    xdotool is never called."""
    from gui_agent.protocol import GUI_GROUNDED_CLICK
    # Params validation requires prompt too.
    with patch("gui_agent.input_synth.subprocess.run") as run_mock:
        result = agent_with_trust.handle_request(GUI_GROUNDED_CLICK, {
            "window": "gnome-terminal",
            "prompt": "the close button",
        })
    assert result.get("reason") == "trust_deny"
    assert not result.get("success", True)
    run_mock.assert_not_called()


def test_trust_read_only_tools_skip_check(agent_with_trust):
    """Read-only tools (ping, parse_screen, hover) don't consult the
    trust store — no risk of them causing harm."""
    with patch.object(agent_with_trust, "_trust_hard_deny_check") as check_mock:
        try:
            agent_with_trust.handle_request(GUI_HOVER,
                                             {"x": 100, "y": 200})
        except Exception:
            # Hover may fail for other reasons (xdotool missing) —
            # what we care about is whether the check was CALLED.
            pass
        check_mock.assert_not_called()


def test_trust_disabled_skips_check(tmp_path):
    """[gui.trust] enabled=false → no store instantiated, no consult."""
    cfg = SimpleNamespace(trust=SimpleNamespace(enabled=False))
    agent = GuiAgent(scratch_dir=str(tmp_path), config=cfg)
    assert agent._get_trust_store() is None


def test_trust_store_error_swallowed(tmp_path, capsys):
    """TrustStore construction failure logs + returns None — never
    crashes the agent."""
    cfg = SimpleNamespace(trust=SimpleNamespace(
        enabled=True,
        # Point to a bad path — but TrustStore is fairly permissive
        # about missing paths, so simulate the error via patch:
        store_path=str(tmp_path / "trust.jsonl"),
        defaults_dir=str(tmp_path / "defaults"),
    ))
    agent = GuiAgent(scratch_dir=str(tmp_path), config=cfg)
    with patch("gui_agent.trust_store.TrustStore",
               side_effect=RuntimeError("boom")):
        store = agent._get_trust_store()
    assert store is None
    err = capsys.readouterr().err
    assert "trust store failure" in err


# ── [gui.preview] notify_urgency wiring ─────────────────────────────


def test_try_notify_send_default_urgency_is_low():
    """Backward compat: existing callers not passing urgency get low."""
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0)
        _try_notify_send("t", "b", "/tmp/x.png")
    argv = run_mock.call_args.args[0]
    assert "--urgency=low" in argv


def test_try_notify_send_urgency_normal():
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0)
        _try_notify_send("t", "b", "/tmp/x.png", urgency="normal")
    argv = run_mock.call_args.args[0]
    assert "--urgency=normal" in argv


def test_try_notify_send_urgency_critical():
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0)
        _try_notify_send("t", "b", "/tmp/x.png", urgency="critical")
    argv = run_mock.call_args.args[0]
    assert "--urgency=critical" in argv


def test_try_notify_send_invalid_urgency_falls_back_to_low():
    """Invalid urgency string → silent fallback to 'low' rather than
    crashing notify-send."""
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0)
        _try_notify_send("t", "b", "/tmp/x.png", urgency="EXTREME")
    argv = run_mock.call_args.args[0]
    assert "--urgency=low" in argv


def test_valid_urgencies_include_all_three():
    assert _VALID_NOTIFY_URGENCIES == {"low", "normal", "critical"}


# ── [gui.geometry] scale_override wiring ────────────────────────────


def test_synth_wrap_passes_scale_override_to_detect(tmp_path, monkeypatch):
    """A GuiAgent constructed with geometry.hidpi_scale_override=3
    must pass scale_override=3 to MonitorLayout.detect() on every
    mouse-tool invocation."""
    cfg = SimpleNamespace(geometry=SimpleNamespace(hidpi_scale_override=3))
    agent = GuiAgent(scratch_dir=str(tmp_path), config=cfg)

    with patch("gui_agent.geometry.MonitorLayout.detect") as detect_mock, \
         patch("gui_agent.input_synth.subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0)
        detect_mock.return_value = MagicMock(logical_to_physical=lambda x, y: (x, y),
                                              find_monitor=lambda x, y: MagicMock())
        agent.handle_request(GUI_CLICK_AT_COORDS, {"x": 100, "y": 200})

    detect_mock.assert_called()
    assert detect_mock.call_args.kwargs.get("scale_override") == 3


def test_synth_wrap_default_scale_override_is_zero(tmp_path):
    """No geometry config → scale_override=0 (auto-detect via xrandr)."""
    agent = GuiAgent(scratch_dir=str(tmp_path))  # no config
    with patch("gui_agent.geometry.MonitorLayout.detect") as detect_mock, \
         patch("gui_agent.input_synth.subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0)
        detect_mock.return_value = MagicMock(logical_to_physical=lambda x, y: (x, y),
                                              find_monitor=lambda x, y: MagicMock())
        agent.handle_request(GUI_CLICK_AT_COORDS, {"x": 100, "y": 200})
    assert detect_mock.call_args.kwargs.get("scale_override") == 0


# ── Landlock path additions (V.6g R-1) ──────────────────────────────


def test_sandbox_source_mentions_usr_local():
    """Regression guard: the V.6g Landlock additions (/usr/local,
    /dev/shm, $HOME/.cache) must be in the source. Grep source."""
    from gui_agent import sandbox as mod
    src = open(mod.__file__).read()
    assert "/usr/local" in src, "V.6g Landlock addition missing"
    assert "/dev/shm" in src, "V.6g /dev/shm RW rule missing"
    assert ".cache" in src, "V.6g $HOME/.cache RW rule missing"


def test_sandbox_dev_shm_is_read_write_not_read_only():
    """Regression: /dev/shm needs READ_WRITE. If a refactor moves it
    back under /dev READ_ONLY, Pillow/numpy shm mmaps EACCES."""
    from gui_agent import sandbox as mod
    src = open(mod.__file__).read()
    # Look for the marker + comment block indicating RW.
    assert "/dev/shm" in src
    # Find the line with /dev/shm and check nearby context.
    for i, line in enumerate(src.splitlines()):
        if "/dev/shm" in line and "_add_if_exists" in line:
            assert "_LANDLOCK_READ_WRITE" in line, \
                f"/dev/shm must be READ_WRITE for Pillow/numpy shm; " \
                f"line reads: {line!r}"
            break
    else:
        pytest.fail("/dev/shm _add_if_exists rule not found in sandbox.py")
