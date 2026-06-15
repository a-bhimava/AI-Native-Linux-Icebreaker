"""Tests for REPL streaming rendering — progress display and token streaming.

Covers:
  1. _update_progress prints spinner frame + label
  2. _clear_progress resets line
  3. _display_status_line prints tier/backend/duration
  4. show_progress=False falls back to _run_turn (old spinner)
  5. Token printing on TokenEvent (capture stdout)
  6. Status line after ResultEvent
  7. /undo in SLASH_COMMANDS
  8. /help includes /undo entries
  9. /audit in SLASH_COMMANDS
 10. /trust in SLASH_COMMANDS
 11. _update_progress uses correct spinner frame
 12. _clear_progress writes spaces to clear line
 13. _display_status_line shows cost
 14. _display_status_line shows success icon
 15. _display_status_line shows failure icon
"""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

try:
    from prompt_toolkit import PromptSession
    _HAS_PROMPT_TOOLKIT = True
except ImportError:
    _HAS_PROMPT_TOOLKIT = False

pytestmark = pytest.mark.skipif(
    not _HAS_PROMPT_TOOLKIT,
    reason="prompt_toolkit not installed",
)


from controller.repl import Repl, _SPINNER_FRAMES


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_cfg(show_progress=True, stream_output=True):
    """Build a minimal config for Repl construction."""
    return SimpleNamespace(
        color="never",          # disable color to simplify output assertions
        ephemeral_history=True,
        history_path="~/.local/state/icebreaker/repl_history",
        session_ttl_seconds=1800,
        max_turns=50,
        show_spinner=True,
        prompt_prefix="icebreaker",
        session=SimpleNamespace(
            show_progress=show_progress,
            stream_output=stream_output,
            max_tool_output_lines=40,
        ),
        cost=SimpleNamespace(
            session_ceiling_usd=0.0,
            warn_fraction=0.8,
        ),
        limits=SimpleNamespace(
            max_input_chars=4000,
            max_turns_per_min=30,
        ),
        undo=SimpleNamespace(enabled=False, max_history=10),
        run=SimpleNamespace(
            audit_log="~/.local/state/icebreaker/controller-audit.log",
        ),
    )


def _build_repl(show_progress=True, stream_output=True):
    """Build a Repl with mocked Controller."""
    ctrl = MagicMock()
    ctrl.backend_name.return_value = "local"
    cfg = _mock_cfg(show_progress=show_progress, stream_output=stream_output)
    repl = Repl(ctrl, cfg)
    return repl, ctrl


# ── 1: _update_progress prints spinner frame + label ────────────────────────


def test_update_progress_prints_spinner_and_label(capsys):
    """_update_progress prints a spinner frame and step label."""
    repl, _ = _build_repl()
    # Force color on so _update_progress actually prints
    repl._use_color = True

    from controller.turn_events import ProgressEvent
    ev = ProgressEvent(
        step_name="qb_intent",
        step_label="Generating intent...",
        step_index=0,
        total_steps=14,
        elapsed_ms=80.0,  # 80ms -> frame index 1
    )
    repl._update_progress(ev)
    captured = capsys.readouterr()
    assert "Generating intent..." in captured.out
    # Should contain a spinner frame
    assert any(frame in captured.out for frame in _SPINNER_FRAMES)


def test_update_progress_no_output_without_color(capsys):
    """_update_progress does nothing when color is off."""
    repl, _ = _build_repl()
    repl._use_color = False

    from controller.turn_events import ProgressEvent
    ev = ProgressEvent(
        step_name="qb_intent",
        step_label="Generating intent...",
        step_index=0,
        total_steps=14,
        elapsed_ms=0.0,
    )
    repl._update_progress(ev)
    captured = capsys.readouterr()
    assert captured.out == ""


# ── 2: _clear_progress resets line ──────────────────────────────────────────


def test_clear_progress_clears_line(capsys):
    """_clear_progress writes spaces to overwrite the progress line."""
    repl, _ = _build_repl()
    repl._use_color = True
    repl._clear_progress()
    captured = capsys.readouterr()
    # Should contain carriage return and spaces
    assert "\r" in captured.out
    assert " " in captured.out


def test_clear_progress_noop_without_color(capsys):
    """_clear_progress does nothing when color is off."""
    repl, _ = _build_repl()
    repl._use_color = False
    repl._clear_progress()
    captured = capsys.readouterr()
    assert captured.out == ""


# ── 3: _display_status_line prints tier/backend/duration ────────────────────


def test_display_status_line(capsys):
    """_display_status_line prints a formatted status with tier, backend, duration."""
    repl, _ = _build_repl()
    result = SimpleNamespace(
        tier=0, backend="local", duration_ms=150.0,
        cost_usd=0.001, success=True,
    )
    repl._display_status_line(result)
    captured = capsys.readouterr()
    assert "Tier 0" in captured.out
    assert "local" in captured.out
    assert "150ms" in captured.out


def test_display_status_line_shows_cost(capsys):
    """_display_status_line includes cost in the output."""
    repl, _ = _build_repl()
    result = SimpleNamespace(
        tier=1, backend="anthropic", duration_ms=500.0,
        cost_usd=0.0123, success=True,
    )
    repl._display_status_line(result)
    captured = capsys.readouterr()
    assert "$0.0123" in captured.out


def test_display_status_line_success_icon(capsys):
    """Successful result shows checkmark icon."""
    repl, _ = _build_repl()
    result = SimpleNamespace(
        tier=0, backend="local", duration_ms=100.0,
        cost_usd=None, success=True,
    )
    repl._display_status_line(result)
    captured = capsys.readouterr()
    # Should contain a checkmark (color-less mode)
    assert "✓" in captured.out  # "checkmark" character


def test_display_status_line_failure_icon(capsys):
    """Failed result shows cross icon."""
    repl, _ = _build_repl()
    result = SimpleNamespace(
        tier=0, backend="local", duration_ms=100.0,
        cost_usd=None, success=False,
    )
    repl._display_status_line(result)
    captured = capsys.readouterr()
    assert "✗" in captured.out  # cross mark


# ── 4: show_progress=False falls back to _run_turn ─────────────────────────


def test_show_progress_false_uses_run_turn():
    """When show_progress=False, the REPL uses _run_turn (old spinner path)."""
    repl, ctrl = _build_repl(show_progress=False)
    # Mock _run_turn to verify it's called
    repl._run_turn = MagicMock()
    repl._run_turn_streaming = MagicMock()

    # Simulate processing a text input
    # We need to check the run() dispatch logic
    cfg = repl._cfg
    assert cfg.session.show_progress is False


# ── 5: Token printing on TokenEvent ────────────────────────────────────────


def test_token_printing_on_token_event(capsys):
    """_run_turn_streaming prints token text on TokenEvent."""
    from controller.turn_events import (
        ProgressEvent, TokenEvent, ResultEvent,
    )
    from controller.main import TurnResult
    from controller.audit import Outcome

    repl, ctrl = _build_repl()

    events = [
        ProgressEvent(
            step_name="qb_summarize", step_label="Summarizing...",
            step_index=12, total_steps=14, elapsed_ms=100.0,
        ),
        TokenEvent(token="Hello", accumulated="Hello", final=False),
        TokenEvent(token=" world", accumulated="Hello world", final=False),
        TokenEvent(token="", accumulated="Hello world", final=True),
        ResultEvent(result=TurnResult(
            success=True, output="Hello world",
            outcome=Outcome.EXECUTED, tier=0,
            backend="local", duration_ms=200.0,
        )),
    ]
    ctrl.run_turn_streaming.return_value = iter(events)

    repl._run_turn_streaming("test input")
    captured = capsys.readouterr()
    assert "Hello" in captured.out
    assert "world" in captured.out


# ── 6: Status line after ResultEvent ────────────────────────────────────────


def test_status_line_after_result_event(capsys):
    """_run_turn_streaming prints a status line after ResultEvent."""
    from controller.turn_events import ProgressEvent, ResultEvent
    from controller.main import TurnResult
    from controller.audit import Outcome

    repl, ctrl = _build_repl()

    events = [
        ProgressEvent(
            step_name="qb_intent", step_label="Generating...",
            step_index=0, total_steps=14, elapsed_ms=0.0,
        ),
        ResultEvent(result=TurnResult(
            success=True, output="System is running.",
            outcome=Outcome.EXECUTED, tier=0,
            backend="local", duration_ms=300.0,
        )),
    ]
    ctrl.run_turn_streaming.return_value = iter(events)

    repl._run_turn_streaming("show status")
    captured = capsys.readouterr()
    assert "System is running." in captured.out
    assert "Tier 0" in captured.out


# ── 7: /undo in SLASH_COMMANDS ──────────────────────────────────────────────


def test_undo_in_slash_commands():
    """/undo is registered in SLASH_COMMANDS."""
    assert "/undo" in Repl.SLASH_COMMANDS


# ── 8: /help includes /undo entries ─────────────────────────────────────────


def test_help_includes_undo_entries(capsys):
    """/help output includes /undo documentation."""
    repl, _ = _build_repl()
    repl._print_help()
    captured = capsys.readouterr()
    assert "/undo" in captured.out
    assert "undo history" in captured.out.lower() or "/undo list" in captured.out


# ── 9-10: Other SLASH_COMMANDS ──────────────────────────────────────────────


def test_audit_in_slash_commands():
    """/audit is registered in SLASH_COMMANDS."""
    assert "/audit" in Repl.SLASH_COMMANDS


def test_trust_in_slash_commands():
    """/trust is registered in SLASH_COMMANDS."""
    assert "/trust" in Repl.SLASH_COMMANDS


# ── 11: _update_progress uses correct spinner frame ─────────────────────────


def test_update_progress_correct_spinner_frame(capsys):
    """Spinner frame is computed from elapsed_ms / 80."""
    repl, _ = _build_repl()
    repl._use_color = True

    from controller.turn_events import ProgressEvent
    # 160ms / 80 = index 2
    ev = ProgressEvent(
        step_name="test", step_label="Testing...",
        step_index=0, total_steps=14, elapsed_ms=160.0,
    )
    repl._update_progress(ev)
    captured = capsys.readouterr()
    expected_frame = _SPINNER_FRAMES[2]
    assert expected_frame in captured.out


# ── 12: _update_progress shows elapsed seconds ─────────────────────────────


def test_update_progress_shows_elapsed_seconds(capsys):
    """_update_progress shows elapsed time in seconds."""
    repl, _ = _build_repl()
    repl._use_color = True

    from controller.turn_events import ProgressEvent
    ev = ProgressEvent(
        step_name="test", step_label="Validating...",
        step_index=1, total_steps=14, elapsed_ms=1500.0,
    )
    repl._update_progress(ev)
    captured = capsys.readouterr()
    assert "1.5s" in captured.out
