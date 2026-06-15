"""Tests for the ``/undo`` REPL command handler (``Repl._handle_undo``).

Covers:
  1. /undo when disabled: prints disabled message
  2. /undo with no history: prints "No operations to undo."
  3. /undo with history: shows last undoable + unavailable message
  4. /undo list empty: prints "No undo history."
  5. /undo list populated: renders table
  6. /undo <N> valid: shows entry
  7. /undo <N> invalid: prints not found
  8. /undo list shows "not undoable" for Tier 0 entries
  9. /undo when all entries non-undoable: prints "No operations to undo."
 10. /undo shows action and target
 11. /undo <N> shows tier and status
 12. /undo list header is present
 13. /undo with undoable entry shows rollback unavailable message
 14. /undo dispatched from _handle_slash
 15. /undo list reversed order (newest first)
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

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

from controller.repl import Repl
from controller.undo import UndoEntry, UndoHistory, UndoStatus


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_cfg(undo_enabled=False):
    return SimpleNamespace(
        color="never",
        ephemeral_history=True,
        history_path="~/.local/state/icebreaker/repl_history",
        session_ttl_seconds=1800,
        max_turns=50,
        show_spinner=True,
        prompt_prefix="icebreaker",
        session=SimpleNamespace(
            show_progress=True,
            stream_output=True,
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
        undo=SimpleNamespace(enabled=undo_enabled, max_history=10),
        run=SimpleNamespace(
            audit_log="~/.local/state/icebreaker/controller-audit.log",
        ),
    )


def _entry(
    turn_index: int = 0,
    action: str = "system.status",
    target: str = "",
    tier: int = 0,
    undoable: bool = False,
    undo_status: UndoStatus = UndoStatus.UNAVAILABLE,
) -> UndoEntry:
    return UndoEntry(
        turn_index=turn_index,
        intent_id=f"intent-{turn_index}",
        action=action,
        target=target,
        outcome="executed",
        timestamp=time.monotonic(),
        tier=tier,
        undoable=undoable,
        undo_status=undo_status,
    )


def _build_repl(undo_enabled=False):
    ctrl = MagicMock()
    ctrl.backend_name.return_value = "local"
    cfg = _mock_cfg(undo_enabled=undo_enabled)
    repl = Repl(ctrl, cfg)
    return repl


# ── 1: /undo when disabled ─────────────────────────────────────────────────


def test_undo_disabled(capsys):
    """/undo when disabled prints disabled message."""
    repl = _build_repl(undo_enabled=False)
    repl._handle_undo("")
    captured = capsys.readouterr()
    assert "disabled" in captured.out.lower()
    assert "enabled = true" in captured.out.lower() or "enabled" in captured.out.lower()


# ── 2: /undo with no history ───────────────────────────────────────────────


def test_undo_no_history(capsys):
    """/undo with no history prints 'No operations to undo.'"""
    repl = _build_repl(undo_enabled=True)
    repl._handle_undo("")
    captured = capsys.readouterr()
    assert "No operations to undo" in captured.out


# ── 3: /undo with history ──────────────────────────────────────────────────


def test_undo_with_undoable_entry(capsys):
    """/undo shows last undoable entry and unavailable message."""
    repl = _build_repl(undo_enabled=True)
    repl._session.undo_history.record(
        _entry(turn_index=1, action="fs.write", target="/tmp/test", tier=2, undoable=True)
    )
    repl._handle_undo("")
    captured = capsys.readouterr()
    assert "fs.write" in captured.out
    assert "/tmp/test" in captured.out or "(none)" in captured.out
    assert "not yet available" in captured.out.lower() or "Undo is not yet" in captured.out


# ── 4: /undo list empty ────────────────────────────────────────────────────


def test_undo_list_empty(capsys):
    """/undo list with no history prints 'No undo history.'"""
    repl = _build_repl(undo_enabled=True)
    repl._handle_undo("list")
    captured = capsys.readouterr()
    assert "No undo history" in captured.out


# ── 5: /undo list populated ────────────────────────────────────────────────


def test_undo_list_populated(capsys):
    """/undo list renders a table of entries."""
    repl = _build_repl(undo_enabled=True)
    repl._session.undo_history.record(
        _entry(turn_index=1, action="system.status", tier=0)
    )
    repl._session.undo_history.record(
        _entry(turn_index=2, action="fs.write", target="/tmp/x", tier=2, undoable=True)
    )
    repl._handle_undo("list")
    captured = capsys.readouterr()
    assert "system.status" in captured.out
    assert "fs.write" in captured.out
    assert "Turn" in captured.out or "turn" in captured.out.lower()


# ── 6: /undo <N> valid ─────────────────────────────────────────────────────


def test_undo_by_turn_valid(capsys):
    """/undo <N> with a valid turn number shows the entry."""
    repl = _build_repl(undo_enabled=True)
    repl._session.undo_history.record(
        _entry(turn_index=3, action="service.restart", target="nginx", tier=2)
    )
    repl._handle_undo("3")
    captured = capsys.readouterr()
    assert "service.restart" in captured.out
    assert "nginx" in captured.out or "(none)" in captured.out


# ── 7: /undo <N> invalid ───────────────────────────────────────────────────


def test_undo_by_turn_invalid(capsys):
    """/undo <N> with an invalid turn number prints not found."""
    repl = _build_repl(undo_enabled=True)
    repl._session.undo_history.record(_entry(turn_index=1))
    repl._handle_undo("99")
    captured = capsys.readouterr()
    assert "No operation found" in captured.out or "not found" in captured.out.lower()


# ── 8: /undo list shows "not undoable" for Tier 0 entries ──────────────────


def test_undo_list_not_undoable_label(capsys):
    """/undo list shows 'not undoable' for entries with undoable=False."""
    repl = _build_repl(undo_enabled=True)
    repl._session.undo_history.record(
        _entry(turn_index=1, action="system.status", tier=0, undoable=False)
    )
    repl._handle_undo("list")
    captured = capsys.readouterr()
    assert "not undoable" in captured.out


# ── 9: /undo when all non-undoable ─────────────────────────────────────────


def test_undo_all_non_undoable(capsys):
    """/undo when all entries are non-undoable prints 'No operations to undo.'"""
    repl = _build_repl(undo_enabled=True)
    repl._session.undo_history.record(_entry(turn_index=1, undoable=False))
    repl._session.undo_history.record(_entry(turn_index=2, undoable=False))
    repl._handle_undo("")
    captured = capsys.readouterr()
    assert "No operations to undo" in captured.out


# ── 10: /undo shows action and target ──────────────────────────────────────


def test_undo_shows_action_and_target(capsys):
    """/undo shows the action and target of the last undoable entry."""
    repl = _build_repl(undo_enabled=True)
    repl._session.undo_history.record(
        _entry(turn_index=5, action="fs.delete", target="/tmp/old", tier=3, undoable=True)
    )
    repl._handle_undo("")
    captured = capsys.readouterr()
    assert "fs.delete" in captured.out


# ── 11: /undo <N> shows tier and status ────────────────────────────────────


def test_undo_by_turn_shows_tier_and_status(capsys):
    """/undo <N> shows tier and undo_status."""
    repl = _build_repl(undo_enabled=True)
    repl._session.undo_history.record(
        _entry(turn_index=4, action="package.install", tier=2,
               undoable=True, undo_status=UndoStatus.AVAILABLE)
    )
    repl._handle_undo("4")
    captured = capsys.readouterr()
    assert "Tier 2" in captured.out or "tier" in captured.out.lower()
    assert "available" in captured.out.lower()


# ── 12: /undo list header ──────────────────────────────────────────────────


def test_undo_list_has_header(capsys):
    """/undo list includes a header row."""
    repl = _build_repl(undo_enabled=True)
    repl._session.undo_history.record(_entry(turn_index=1))
    repl._handle_undo("list")
    captured = capsys.readouterr()
    # Header should contain column labels
    assert "Action" in captured.out or "action" in captured.out.lower()


# ── 13: /undo with undoable entry shows rollback unavailable ───────────────


def test_undo_rollback_unavailable_message(capsys):
    """The unavailable-rollback message is shown for undoable entries."""
    repl = _build_repl(undo_enabled=True)
    repl._session.undo_history.record(
        _entry(turn_index=1, action="fs.write", target="/tmp/file", tier=2, undoable=True)
    )
    repl._handle_undo("")
    captured = capsys.readouterr()
    assert "not yet available" in captured.out.lower() or "mcpd" in captured.out.lower()


# ── 14: /undo dispatched from _handle_slash ────────────────────────────────


def test_undo_dispatched_from_handle_slash(capsys):
    """/undo is correctly dispatched from _handle_slash."""
    repl = _build_repl(undo_enabled=True)
    result = repl._handle_slash("/undo")
    assert result is False  # /undo does not exit the REPL
    captured = capsys.readouterr()
    assert "No operations to undo" in captured.out


# ── 15: /undo list reversed order ──────────────────────────────────────────


def test_undo_list_reversed_order(capsys):
    """/undo list shows entries in reverse order (newest first)."""
    repl = _build_repl(undo_enabled=True)
    repl._session.undo_history.record(
        _entry(turn_index=1, action="first.action")
    )
    repl._session.undo_history.record(
        _entry(turn_index=2, action="second.action")
    )
    repl._session.undo_history.record(
        _entry(turn_index=3, action="third.action")
    )
    repl._handle_undo("list")
    captured = capsys.readouterr()
    # third.action should appear before first.action in the output
    pos_third = captured.out.find("third.action")
    pos_first = captured.out.find("first.action")
    assert pos_third < pos_first, "Entries should be in reverse order (newest first)"
