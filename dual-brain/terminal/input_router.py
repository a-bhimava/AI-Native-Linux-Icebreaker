"""Input router — detects NL commands and manages Shell/NL mode state.

Routing rules:
  - ``#`` at column 0 → strip prefix, route to daemon as NL (one-shot)
  - Sticky NL mode (F2 toggle) → all input routes to daemon as NL
  - Ctrl+Space → one-shot NL mode (next submission only, then revert)
  - Everything else → route to execution panel as shell command

Edge cases:
  - Leading whitespace before ``#`` → shell (intentional: lets users type
    shell comments without accidental NL routing)
  - ``#`` after any non-whitespace → shell
  - Empty ``#`` (just the prefix, no text after) → ignored
"""

from __future__ import annotations

from enum import Enum, auto


class RouteTarget(Enum):
    SHELL = auto()
    NL = auto()


class InputRouter:
    """Manages NL mode state and routes user input."""

    def __init__(self, nl_prefix: str = "#") -> None:
        self._nl_prefix = nl_prefix
        self._sticky_nl = False
        self._oneshot_nl = False

    @property
    def sticky_nl(self) -> bool:
        return self._sticky_nl

    @sticky_nl.setter
    def sticky_nl(self, value: bool) -> None:
        self._sticky_nl = value
        if value:
            self._oneshot_nl = False

    @property
    def oneshot_pending(self) -> bool:
        return self._oneshot_nl

    def arm_oneshot(self) -> None:
        if not self._sticky_nl:
            self._oneshot_nl = True

    def toggle_sticky(self) -> bool:
        self._sticky_nl = not self._sticky_nl
        if self._sticky_nl:
            self._oneshot_nl = False
        return self._sticky_nl

    def route(self, raw_input: str) -> tuple[RouteTarget, str]:
        """Classify input and return (target, cleaned text).

        Returns ``(SHELL, raw_input)`` for shell commands or
        ``(NL, stripped_text)`` for natural language.
        """
        if self._sticky_nl:
            return RouteTarget.NL, raw_input

        if self._oneshot_nl:
            self._oneshot_nl = False
            return RouteTarget.NL, raw_input

        if raw_input.startswith(self._nl_prefix):
            text = raw_input[len(self._nl_prefix):].strip()
            if text:
                return RouteTarget.NL, text

        return RouteTarget.SHELL, raw_input
