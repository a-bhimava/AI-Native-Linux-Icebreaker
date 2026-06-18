"""AiTerminalApp — split-pane Textual TUI for the Icebreaker Controller.

Layout (ADR-18, ADR-19):
    Vertical(
        InputBar,               # top — mode indicator + text input
        Horizontal(             # main split
            ExecutionPanel,     # left — shell subprocess output
            CompanionPanel,     # right — CoT cards / interpretation
        ),
        StatusBar,              # bottom — keybinding legend
    )
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Label, Static

from .companion import CompanionPanel
from .execution import ExecutionPanel
from .input_bar import InputBar

_CSS_PATH = Path(__file__).parent / "styles.tcss"

_MIN_COMPANION_WIDTH = 80


class StatusBar(Static):
    """Bottom status bar with keybinding hints."""

    DEFAULT_CSS = ""

    def compose(self) -> ComposeResult:
        yield Label(
            " F2 NL Mode │ F3 Panel │ F9 Resize │ F10 Quit ",
            classes="key-hint",
        )


class AiTerminalApp(App):
    """Split-pane AI Terminal."""

    CSS_PATH = str(_CSS_PATH) if _CSS_PATH.exists() else None
    TITLE = "Icebreaker AI Terminal"

    BINDINGS = [
        Binding("f3", "toggle_companion", "Toggle companion panel"),
        Binding("f10", "quit", "Quit"),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._companion_visible = True
        if os.environ.get("NO_COLOR"):
            self._no_color = True
        else:
            self._no_color = False

    def compose(self) -> ComposeResult:
        yield InputBar()
        with Horizontal(id="main-split"):
            yield ExecutionPanel()
            yield CompanionPanel()
        yield StatusBar()

    def on_mount(self) -> None:
        if self._no_color:
            self.screen.add_class("-no-color")
        self._check_companion_width()

    def on_resize(self) -> None:
        self._check_companion_width()

    def _check_companion_width(self) -> None:
        """Auto-hide companion panel when terminal is too narrow."""
        if self.size.width < _MIN_COMPANION_WIDTH:
            if self._companion_visible:
                self._companion_visible = False
                self._set_companion_hidden(True)
        else:
            if not self._companion_visible:
                self._companion_visible = True
                self._set_companion_hidden(False)

    def _set_companion_hidden(self, hidden: bool) -> None:
        try:
            panel = self.query_one(CompanionPanel)
            panel.set_class(hidden, "-hidden")
        except Exception:
            pass

    def action_toggle_companion(self) -> None:
        self._companion_visible = not self._companion_visible
        self._set_companion_hidden(not self._companion_visible)

    async def on_input_bar_submitted(self, event: InputBar.Submitted) -> None:
        """Route submitted text to the execution panel."""
        input_bar = self.query_one(InputBar)
        execution = self.query_one(ExecutionPanel)

        input_bar.set_working(True)
        try:
            await execution.run_command(event.value)
        finally:
            input_bar.set_working(False)


def run() -> None:
    """Entry point for `python -m terminal`."""
    app = AiTerminalApp()
    app.run()
