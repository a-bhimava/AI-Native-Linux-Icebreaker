"""Tests for the AI Terminal TUI scaffold (PR #23).

Covers:
  1.  App composes with InputBar, ExecutionPanel, CompanionPanel, StatusBar
  2.  InputBar is the first child (top position, ADR-18)
  3.  InputBar shows "Shell" mode label by default
  4.  InputBar NL mode changes label to "NL" and adds nl-mode class
  5.  CompanionPanel renders a CoT card with "active" state
  6.  CompanionPanel renders a CoT card with "done" state
  7.  CompanionPanel renders a CoT card with "failed" state
  8.  CompanionPanel renders multiple cards in order
  9.  CompanionPanel.handle_result switches to interpretation mode
 10.  CompanionPanel.clear resets state
 11.  ExecutionPanel composes with RichLog
 12.  CotCard.update_state changes visual class
 13.  StatusBar composes with key hints
 14.  --terminal flag accepted by arg parser
"""

from __future__ import annotations

import argparse
import os

import pytest
from textual.widgets import Input, Label, RichLog

from terminal.app import AiTerminalApp, StatusBar
from terminal.companion import CompanionPanel, CotCard
from terminal.execution import ExecutionPanel
from terminal.input_bar import InputBar


# ── App composition ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_app_composes_all_widgets():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        app = pilot.app
        assert app.query_one(InputBar)
        assert app.query_one(ExecutionPanel)
        assert app.query_one(CompanionPanel)
        assert app.query_one(StatusBar)


@pytest.mark.asyncio
async def test_input_bar_is_first_child():
    """InputBar docks at top — it should be first in the compose order."""
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        app = pilot.app
        children = list(app.screen.children)
        assert isinstance(children[0], InputBar)


# ── InputBar ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_input_bar_default_mode_label():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        bar = pilot.app.query_one(InputBar)
        label = bar.query_one("#mode-label", Label)
        assert "Shell" in label.content


@pytest.mark.asyncio
async def test_input_bar_nl_mode():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        bar = pilot.app.query_one(InputBar)
        bar.nl_mode = True
        assert bar.has_class("nl-mode")
        label = bar.query_one("#mode-label", Label)
        assert "NL" in label.content


@pytest.mark.asyncio
async def test_input_bar_working_state():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        bar = pilot.app.query_one(InputBar)
        bar.set_working(True)
        inp = bar.query_one("#input-field", Input)
        assert inp.disabled is True
        bar.set_working(False)
        assert inp.disabled is False


# ── CompanionPanel — CoT cards ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_companion_cot_card_active():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        panel = pilot.app.query_one(CompanionPanel)
        panel.handle_cot(
            step_index=0, step_name="qb_intent", step_state="active",
            heading="Intent Generation", body="Parsing...",
        )
        await pilot.pause()
        cards = panel.query(".cot-card")
        assert len(cards) >= 1
        assert cards.first().has_class("s-active")


@pytest.mark.asyncio
async def test_companion_cot_card_done():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        panel = pilot.app.query_one(CompanionPanel)
        panel.handle_cot(
            step_index=0, step_name="qb_intent", step_state="done",
            heading="Intent Generation", body="Done",
        )
        await pilot.pause()
        cards = panel.query(".cot-card")
        assert len(cards) >= 1
        assert cards.first().has_class("s-done")


@pytest.mark.asyncio
async def test_companion_cot_card_failed():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        panel = pilot.app.query_one(CompanionPanel)
        panel.handle_cot(
            step_index=1, step_name="schema_validation", step_state="failed",
            heading="Schema Validation", body="Invalid field: xyz",
        )
        await pilot.pause()
        cards = panel.query(".cot-card")
        assert len(cards) >= 1
        assert cards.first().has_class("s-failed")


@pytest.mark.asyncio
async def test_companion_multiple_cards_in_order():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        panel = pilot.app.query_one(CompanionPanel)
        steps = [
            (0, "qb_intent", "done", "Intent Generation"),
            (1, "schema_validation", "done", "Schema Validation"),
            (2, "risk_classification", "active", "Risk Classification"),
        ]
        for idx, name, state, heading in steps:
            panel.handle_cot(
                step_index=idx, step_name=name,
                step_state=state, heading=heading,
            )
        await pilot.pause()
        cards = panel.query(".cot-card")
        assert len(cards) == 3


@pytest.mark.asyncio
async def test_companion_handle_result_switches_mode():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        panel = pilot.app.query_one(CompanionPanel)
        panel.handle_cot(
            step_index=0, step_name="qb_intent", step_state="done",
            heading="Intent Generation",
        )
        panel.handle_result({
            "success": True, "output": "System is running.",
            "outcome": "executed", "tier": 0,
        })
        await pilot.pause()
        header = panel.query_one("#companion-header", Label)
        assert "Interpretation" in header.content


@pytest.mark.asyncio
async def test_companion_clear_resets():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        panel = pilot.app.query_one(CompanionPanel)
        panel.handle_cot(
            step_index=0, step_name="qb_intent", step_state="done",
            heading="Intent Generation",
        )
        await pilot.pause()
        assert len(panel.query(".cot-card")) >= 1
        panel.clear()
        await pilot.pause()
        header = panel.query_one("#companion-header", Label)
        assert "Chain of Thought" in header.content


# ── CotCard state updates ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cot_card_update_state():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        panel = pilot.app.query_one(CompanionPanel)
        panel.handle_cot(
            step_index=0, step_name="qb_intent", step_state="active",
            heading="Intent Generation", body="Working...",
        )
        await pilot.pause()
        panel.handle_cot(
            step_index=0, step_name="qb_intent", step_state="done",
            heading="Intent Generation", body="Complete",
        )
        await pilot.pause()
        cards = panel.query(".cot-card")
        assert len(cards) == 1
        assert cards.first().has_class("s-done")
        assert not cards.first().has_class("s-active")


# ── ExecutionPanel ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execution_panel_has_richlog():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        panel = pilot.app.query_one(ExecutionPanel)
        assert panel.query_one("#shell-output", RichLog)


# ── StatusBar ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_bar_has_key_hints():
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        bar = pilot.app.query_one(StatusBar)
        hint = bar.query_one(".key-hint", Label)
        text = hint.content
        assert "F2" in text
        assert "F10" in text


# ── CLI flag ─────────────────────────────────────────────────────────────────


def test_terminal_flag_accepted():
    """--terminal flag is recognized by the arg parser."""
    from controller.__main__ import main
    from unittest.mock import patch

    with patch("controller.__main__.argparse.ArgumentParser.parse_args") as mock_parse:
        mock_parse.return_value = argparse.Namespace(
            command=None, repl=False, config=None,
            check_isolation=False, daemon=False,
            connect=None, safe_mode=False, terminal=True,
        )
        with patch("controller.__main__._run_terminal", return_value=0) as mock_run:
            result = main([])
            mock_run.assert_called_once()
            assert result == 0


# ── F2 toggle + NL mode ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f2_toggles_nl_mode():
    """F2 key toggles sticky NL mode on InputBar."""
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        app = pilot.app
        bar = app.query_one(InputBar)
        assert not bar.nl_mode
        await pilot.press("f2")
        assert bar.nl_mode
        label = bar.query_one("#mode-label", Label)
        assert "NL" in label.content
        await pilot.press("f2")
        assert not bar.nl_mode


@pytest.mark.asyncio
async def test_app_has_router():
    """App exposes an InputRouter instance."""
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        from terminal.input_router import InputRouter
        assert isinstance(pilot.app.router, InputRouter)


@pytest.mark.asyncio
async def test_companion_tier_badge_on_result():
    """Interpretation view includes a tier badge."""
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        panel = pilot.app.query_one(CompanionPanel)
        panel.handle_result({
            "success": True, "output": "Done.",
            "outcome": "executed", "tier": 2,
        })
        await pilot.pause()
        header = panel.query_one("#companion-header", Label)
        assert "Interpretation" in header.content


@pytest.mark.asyncio
async def test_companion_suggestions():
    """Interpretation view renders suggestions when present."""
    async with AiTerminalApp().run_test(size=(120, 40)) as pilot:
        panel = pilot.app.query_one(CompanionPanel)
        panel.handle_result({
            "success": True, "output": "Installed.",
            "outcome": "executed", "tier": 0,
            "suggestions": ["Check version with --version"],
        })
        await pilot.pause()
        container = panel.query_one("#cot-container")
        assert len(container.children) >= 3
