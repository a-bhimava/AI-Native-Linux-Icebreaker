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
import threading
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Label, Static

from .companion import CompanionPanel
from .execution import ExecutionPanel
from .input_bar import InputBar
from .input_router import InputRouter, RouteTarget

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
        Binding("f2", "toggle_nl_mode", "Toggle NL mode", show=False),
        Binding("f3", "toggle_companion", "Toggle companion panel"),
        Binding("f10", "quit", "Quit"),
    ]

    def __init__(self, daemon_client: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._companion_visible = True
        self._no_color = bool(os.environ.get("NO_COLOR"))
        self._router = InputRouter()
        self._daemon_client = daemon_client

    @property
    def router(self) -> InputRouter:
        return self._router

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
        self._wire_daemon_callbacks()

    def _wire_daemon_callbacks(self) -> None:
        client = self._daemon_client
        if client is None or not hasattr(client, "on"):
            return
        if hasattr(client, "set_app"):
            client.set_app(self)
        client.on("cot", self._on_cot_event)
        client.on("token", self._on_token_event)
        client.on("progress", self._on_progress_event)
        client.on("gui", self._on_gui_event)
        client.on("rpa", self._on_rpa_event)
        client.on("info", self._on_info_event)

    def _on_cot_event(self, params: dict) -> None:
        try:
            companion = self.query_one(CompanionPanel)
            companion.handle_cot(
                step_index=params.get("step_index", 0),
                step_name=params.get("step_name", ""),
                step_state=params.get("step_state", "pending"),
                heading=params.get("heading", ""),
                body=params.get("body", ""),
                data=params.get("data"),
            )
        except Exception:
            pass

    def _on_token_event(self, params: dict) -> None:
        text = params.get("accumulated", "")
        if not text:
            return
        try:
            from rich.text import Text
            log = self.query_one(ExecutionPanel).query_one("#shell-output")
            log.write(Text(text, style="#c0c0c0"))
        except Exception:
            pass

    def _on_progress_event(self, params: dict) -> None:
        pass

    def _on_gui_event(self, params: dict) -> None:
        try:
            companion = self.query_one(CompanionPanel)
            companion.handle_gui(params)
        except Exception:
            pass

    def _on_rpa_event(self, params: dict) -> None:
        try:
            companion = self.query_one(CompanionPanel)
            companion.handle_rpa(params)
        except Exception:
            pass

    def _on_info_event(self, params: dict) -> None:
        msg = params.get("message", "")
        if not msg:
            return
        try:
            from rich.text import Text
            log = self.query_one(ExecutionPanel).query_one("#shell-output")
            log.write(Text(f"[info] {msg}", style="#888888"))
        except Exception:
            pass

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

    def action_toggle_nl_mode(self) -> None:
        """F2: toggle sticky NL mode."""
        is_nl = self._router.toggle_sticky()
        input_bar = self.query_one(InputBar)
        input_bar.nl_mode = is_nl

    async def on_input_bar_submitted(self, event: InputBar.Submitted) -> None:
        """Route submitted text via the InputRouter."""
        target, text = self._router.route(event.value)
        input_bar = self.query_one(InputBar)

        if self._router.oneshot_pending:
            pass
        if not self._router.sticky_nl:
            input_bar.nl_mode = False

        if target == RouteTarget.NL:
            await self._run_nl_turn(text)
        else:
            await self._run_shell(text)

    async def _run_shell(self, command: str) -> None:
        input_bar = self.query_one(InputBar)
        execution = self.query_one(ExecutionPanel)
        input_bar.set_working(True)
        try:
            await execution.run_command(command)
        finally:
            input_bar.set_working(False)

    async def _run_nl_turn(self, text: str) -> None:
        """Send NL text to the daemon and render results in companion panel."""
        input_bar = self.query_one(InputBar)
        companion = self.query_one(CompanionPanel)
        execution = self.query_one(ExecutionPanel)

        if self._daemon_client is None:
            from rich.text import Text
            log = execution.query_one("#shell-output")
            log.write(Text(
                "No daemon connection — NL mode requires --terminal with a running daemon.",
                style="bold #f87171",
            ))
            return

        input_bar.set_working(True)
        companion.clear()

        def _do_turn() -> dict:
            return self._daemon_client.run_turn(text)

        try:
            resp = await self.run_in_thread(_do_turn)
        except Exception as exc:
            from rich.text import Text
            log = execution.query_one("#shell-output")
            log.write(Text(f"NL error: {exc}", style="bold #f87171"))
            return
        finally:
            input_bar.set_working(False)

        if "result" in resp:
            result = resp["result"]
            companion.handle_result(result)
            output = result.get("output", "")
            if output:
                from rich.text import Text
                log = execution.query_one("#shell-output")
                log.write(Text(output, style="#c0c0c0"))
        elif "error" in resp:
            from rich.text import Text
            log = execution.query_one("#shell-output")
            msg = resp["error"].get("message", "unknown error")
            log.write(Text(f"Error: {msg}", style="bold #f87171"))


def run(daemon_client: Any = None) -> None:
    """Entry point for `python -m terminal`."""
    app = AiTerminalApp(daemon_client=daemon_client)
    app.run()
