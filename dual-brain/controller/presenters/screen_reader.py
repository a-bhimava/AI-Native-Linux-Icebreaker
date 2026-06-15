"""Screen-reader presenter — accessible HITL approval UI.

Design principles:
  - No ANSI escapes — screen readers read raw text; escapes become garbled
  - No box-drawing characters — read aloud as "box drawings light horizontal"
  - Structured, predictable output — same field order every time
  - No countdown timers — \\r-overwritten countdowns create screen reader chaos
  - Explicit key announcements — plain text, not formatted tables

Nielsen #2 (match real world): labels use natural language.
Nielsen #4 (consistency): same field order always, no conditional layout.
Nielsen #8 (minimalist): no decorative separators, no icons, no color.
"""

from __future__ import annotations

import os
import select
import sys
import time
from typing import Optional

from ..hitl import Decision, HitlDisplayData, HitlPresenter, _cbreak
from ..keymap import Action, Keymap
from .registry import register_presenter


@register_presenter("screen_reader")
class ScreenReaderPresenter(HitlPresenter):
    """Accessible presenter for screen reader users."""

    def __init__(self, *, keymap: Optional[Keymap] = None) -> None:
        self._keymap = keymap or Keymap()
        self._last_key_class: str = ""

    @property
    def last_key_class(self) -> str:
        return self._last_key_class

    def pre_check(self) -> Optional[Decision]:
        if not sys.stdin.isatty():
            self._last_key_class = "non_tty"
            return Decision.NON_TTY
        return None

    def show_prompt(self, data: HitlDisplayData) -> None:
        lines = [
            "System action requires your approval.",
            f"Action: {data.action}",
            f"Target: {data.target}" if data.target else "Target: none",
            f"Risk: {data.risk_level}",
            f"Reversible: {'Yes' if data.reversible else 'No'}",
        ]
        if data.reason:
            lines.append(f"Reason: {data.reason}")
        if data.backend:
            lines.append(f"Backend: {data.backend}")
        if data.cow_summary:
            lines.append(f"Preview: {data.cow_summary[:500]}")
        print("\n".join(lines), flush=True)

    def lockout(self, seconds: int) -> None:
        print(f"Approve available in {seconds} seconds.", flush=True)
        time.sleep(seconds)
        legend = self._keymap.legend(include_trust=True)
        print(f"Ready. {legend}", flush=True)

    def read_decision(self, timeout_seconds: int) -> Decision:
        fd = sys.stdin.fileno()

        try:
            import termios
            termios.tcflush(fd, termios.TCIFLUSH)
        except (ImportError, OSError):
            pass

        with _cbreak(sys.stdin):
            return self._read_loop(fd, timeout_seconds)

    def _read_loop(self, fd: int, timeout_seconds: int) -> Decision:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("Timed out. Operation denied.", flush=True)
                self._last_key_class = "timeout"
                return Decision.TIMEOUT

            try:
                ready, _, _ = select.select([sys.stdin], [], [], min(remaining, 1.0))
            except (OSError, ValueError, BrokenPipeError):
                self._last_key_class = "non_tty"
                return Decision.NON_TTY
            if not ready:
                continue

            try:
                raw = os.read(fd, 1)
            except OSError:
                self._last_key_class = "eof"
                return Decision.DENIED

            if not raw:
                self._last_key_class = "eof"
                return Decision.DENIED

            key = raw.decode("utf-8", errors="replace")

            if key == "\x1b":
                print("Denied.", flush=True)
                self._last_key_class = "esc"
                return Decision.DENIED

            action = self._keymap.lookup(key)
            if action is None:
                print("Unknown key. Press question mark for help.", flush=True)
                continue

            return self._resolve_action(action, key)

    def _resolve_action(self, action: Action, key: str) -> Decision:
        key_class = "numeric" if key.isdigit() else "mnemonic"
        self._last_key_class = key_class

        if action == Action.APPROVE:
            print("Approved.", flush=True)
            return Decision.APPROVED

        if action == Action.DENY:
            print("Denied.", flush=True)
            return Decision.DENIED

        if action == Action.MODIFY:
            return Decision.MODIFY

        if action == Action.EXPLAIN:
            return Decision.EXPLAIN

        if action == Action.TRUST:
            return Decision.TRUST

        if action == Action.HELP:
            self._last_key_class = "mnemonic"
            legend = self._keymap.legend(include_trust=True)
            print(f"{legend}", flush=True)
            print("Escape key always denies.", flush=True)
            return Decision.EXPLAIN

        return Decision.DENIED
