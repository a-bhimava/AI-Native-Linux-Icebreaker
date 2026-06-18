"""AiTerminalPresenter — HITL approval gate rendered inside the Textual TUI.

Registered as ``"ai_terminal"`` via the presenter registry (BP-1). When the
daemon sends ``hitl.prompt`` / ``hitl.lockout`` notifications, the TUI app
delegates to this presenter to show the approval UI in-band rather than
dropping to raw terminal I/O.

Threading: the presenter methods are called from the daemon client's reader
thread. They communicate with the Textual app via thread-safe callbacks
(``call_from_thread``). The ``read_decision`` method blocks the reader thread
until the user presses a key — the Textual app posts the decision back via
a threading.Event.
"""

from __future__ import annotations

import threading
from typing import Optional

from controller.hitl import Decision, HitlDisplayData, HitlPresenter
from controller.presenters.registry import register_presenter


@register_presenter("ai_terminal")
class AiTerminalPresenter(HitlPresenter):
    """Renders HITL approval inside the AI Terminal's companion panel."""

    def __init__(self, *, keymap=None) -> None:
        self._keymap = keymap
        self._last_key_class: str = ""
        self._decision_event = threading.Event()
        self._decision_value: Decision | None = None
        self._show_callback: Optional[callable] = None
        self._lockout_callback: Optional[callable] = None

    @property
    def last_key_class(self) -> str:
        return self._last_key_class

    def set_callbacks(
        self,
        show_cb: callable,
        lockout_cb: callable,
    ) -> None:
        self._show_callback = show_cb
        self._lockout_callback = lockout_cb

    def pre_check(self) -> Optional[Decision]:
        return None

    def show_prompt(self, data: HitlDisplayData) -> None:
        if self._show_callback is not None:
            self._show_callback(data)

    def lockout(self, seconds: int) -> None:
        if self._lockout_callback is not None:
            self._lockout_callback(seconds)

    def read_decision(self, timeout_seconds: int) -> Decision:
        self._decision_event.clear()
        self._decision_value = None
        if self._decision_event.wait(timeout=timeout_seconds):
            if self._decision_value is not None:
                self._last_key_class = self._decision_value.value
                return self._decision_value
        self._last_key_class = "timeout"
        return Decision.TIMEOUT

    def submit_decision(self, decision: Decision) -> None:
        self._decision_value = decision
        self._decision_event.set()
