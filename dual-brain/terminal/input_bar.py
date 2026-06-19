"""Top input bar with mode indicator (Shell/NL) and text input."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Input, Label, Static


class InputBar(Static):
    """Top-positioned input bar with Shell/NL mode indicator.

    Routing (#-prefix detection, F2 toggle) is PR #24 scope.
    This PR provides the visual scaffold and forwards submitted text.
    """

    class Submitted(Message):
        """Fired when user presses Enter in the input field."""

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    DEFAULT_CSS = ""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._nl_mode = False

    @property
    def nl_mode(self) -> bool:
        return self._nl_mode

    @nl_mode.setter
    def nl_mode(self, value: bool) -> None:
        self._nl_mode = value
        self.set_class(value, "nl-mode")
        label = self.query_one("#mode-label", Label)
        label.update("NL" if value else "Shell")

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Label("Shell", id="mode-label")
            yield Input(
                placeholder="Type a command or # for natural language",
                id="input-field",
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        event.input.clear()
        self.post_message(self.Submitted(value))

    def set_working(self, working: bool) -> None:
        inp = self.query_one("#input-field", Input)
        inp.disabled = working
        if working:
            inp.placeholder = "Working..."
        else:
            inp.placeholder = "Type a command or # for natural language"
