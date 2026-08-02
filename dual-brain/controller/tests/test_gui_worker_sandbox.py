"""Fix V.6f (2026-08-02) — gui_worker sandbox application tests.

CT-scan P0-2: pre-V.6f, `controller/gui_worker.py::_dispatch` imported
GuiAgent/RpaBridge and called handle_request() with NO sandbox call.
The OC edition dispatch chain (opencode → mcp_gui_server →
_dispatch_in_subprocess → gui_worker → GuiAgent) ran unsandboxed —
INV-5 bypassed on the primary shipping edition.

V.6f fix: `_apply_sandbox_for(kind, config)` runs BEFORE
handle_request. This test file verifies that the correct sandbox
fires for each kind, that ICEBREAKER_GUI_SKIP_SANDBOX skips, and
that SandboxError is caught + logged (never crashes the worker).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from controller.gui_worker import _apply_sandbox_for, _SKIP_SANDBOX_ENV


# ── ICEBREAKER_GUI_SKIP_SANDBOX opt-out ─────────────────────────────


def test_skip_env_var_unconditionally_skips(monkeypatch, capsys):
    monkeypatch.setenv(_SKIP_SANDBOX_ENV, "1")
    with patch("gui_agent.sandbox.apply_gui_sandbox") as apply_gui, \
         patch("rpa_bridge.sandbox.apply_rpa_sandbox") as apply_rpa:
        _apply_sandbox_for("gui", {})
        _apply_sandbox_for("rpa", {})
        apply_gui.assert_not_called()
        apply_rpa.assert_not_called()
    err = capsys.readouterr().err
    assert "skipping sandbox" in err


# ── Correct sandbox for kind ────────────────────────────────────────


def test_kind_gui_calls_apply_gui_sandbox(monkeypatch):
    monkeypatch.delenv(_SKIP_SANDBOX_ENV, raising=False)
    with patch("gui_agent.sandbox.apply_gui_sandbox") as apply_gui, \
         patch("rpa_bridge.sandbox.apply_rpa_sandbox") as apply_rpa:
        _apply_sandbox_for("gui", {"screenshot_dir": "/tmp/x-gui"})
        apply_gui.assert_called_once()
        apply_rpa.assert_not_called()
        call_kwargs = apply_gui.call_args.kwargs
        assert call_kwargs["scratch_dir"] == "/tmp/x-gui"
        assert "home_dir" in call_kwargs


def test_kind_rpa_calls_apply_rpa_sandbox(monkeypatch):
    monkeypatch.delenv(_SKIP_SANDBOX_ENV, raising=False)
    with patch("gui_agent.sandbox.apply_gui_sandbox") as apply_gui, \
         patch("rpa_bridge.sandbox.apply_rpa_sandbox") as apply_rpa:
        _apply_sandbox_for("rpa", {"scratch_dir": "/tmp/x-rpa"})
        apply_rpa.assert_called_once()
        apply_gui.assert_not_called()
        call_kwargs = apply_rpa.call_args.kwargs
        assert call_kwargs["scratch_dir"] == "/tmp/x-rpa"


def test_unknown_kind_is_silent_no_op(monkeypatch):
    monkeypatch.delenv(_SKIP_SANDBOX_ENV, raising=False)
    with patch("gui_agent.sandbox.apply_gui_sandbox") as apply_gui, \
         patch("rpa_bridge.sandbox.apply_rpa_sandbox") as apply_rpa:
        # An unknown kind means _dispatch will raise its own clearer
        # error; skip the sandbox rather than double-fault.
        _apply_sandbox_for("wingdings", {})
        apply_gui.assert_not_called()
        apply_rpa.assert_not_called()


# ── Config → scratch path resolution ────────────────────────────────


def test_gui_default_scratch_when_config_missing(monkeypatch):
    monkeypatch.delenv(_SKIP_SANDBOX_ENV, raising=False)
    with patch("gui_agent.sandbox.apply_gui_sandbox") as apply_gui:
        _apply_sandbox_for("gui", {})
        apply_gui.assert_called_once()
        assert apply_gui.call_args.kwargs["scratch_dir"] == "/tmp/icebreaker-gui"


def test_rpa_default_scratch_when_config_missing(monkeypatch):
    monkeypatch.delenv(_SKIP_SANDBOX_ENV, raising=False)
    with patch("rpa_bridge.sandbox.apply_rpa_sandbox") as apply_rpa:
        _apply_sandbox_for("rpa", {})
        apply_rpa.assert_called_once()
        assert apply_rpa.call_args.kwargs["scratch_dir"] == "/tmp/icebreaker-rpa"


def test_home_dir_derived_from_env(monkeypatch):
    monkeypatch.delenv(_SKIP_SANDBOX_ENV, raising=False)
    monkeypatch.setenv("HOME", "/tmp/synthetic-home")
    with patch("gui_agent.sandbox.apply_gui_sandbox") as apply_gui:
        _apply_sandbox_for("gui", {})
        assert apply_gui.call_args.kwargs["home_dir"] == "/tmp/synthetic-home"


# ── SandboxError handling ────────────────────────────────────────────


def test_sandbox_error_caught_and_logged(monkeypatch, capsys):
    """On non-Linux (or Landlock kernel missing) SandboxError should
    NOT crash the worker — log and continue."""
    monkeypatch.delenv(_SKIP_SANDBOX_ENV, raising=False)
    from gui_agent.sandbox import SandboxError
    with patch("gui_agent.sandbox.apply_gui_sandbox",
               side_effect=SandboxError("test: not Linux")):
        # Must not raise.
        _apply_sandbox_for("gui", {})
    err = capsys.readouterr().err
    assert "SandboxError" in err
    assert "Continuing WITHOUT sandbox" in err


def test_unexpected_exception_swallowed(monkeypatch, capsys):
    """Any unexpected exception during sandbox setup must be caught +
    logged. Worker MUST continue so the tool call produces a
    structured error, not a silent worker exit."""
    monkeypatch.delenv(_SKIP_SANDBOX_ENV, raising=False)
    with patch("gui_agent.sandbox.apply_gui_sandbox",
               side_effect=RuntimeError("something weird")):
        _apply_sandbox_for("gui", {})
    err = capsys.readouterr().err
    assert "unexpected sandbox failure" in err
    assert "RuntimeError" in err
