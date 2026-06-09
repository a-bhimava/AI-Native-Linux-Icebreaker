from __future__ import annotations

import re
import select
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .audit import Outcome
from .risk_classifier import ClassificationResult, Tier

_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_GREEN  = "\033[32m"
_RESET  = "\033[0m"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class Decision(str, Enum):
    APPROVED = "approved"
    DENIED   = "denied"
    TIMEOUT  = "timeout"
    NON_TTY  = "non_tty"

    def to_outcome(self) -> Optional[Outcome]:
        return {
            Decision.DENIED:  Outcome.HITL_DENIED,
            Decision.TIMEOUT: Outcome.HITL_TIMEOUT,
            Decision.NON_TTY: Outcome.HITL_NON_TTY,
        }.get(self)  # APPROVED → None; caller sets downstream Outcome


@dataclass(frozen=True)
class HitlDisplayData:
    """Immutable snapshot passed to any presenter — ANSI already stripped."""
    action: str
    target: str
    tier: Tier
    risk_level: str
    reversible: bool
    backend: str
    reason: str
    blocked_pattern: Optional[str]
    cow_summary: Optional[str]


class HitlPresenter(ABC):
    """Swap TerminalPresenter for a GUI presenter in Phase 5."""

    def pre_check(self) -> Optional[Decision]:
        """Return a Decision to short-circuit ask() before any I/O starts.

        TerminalPresenter uses this for the TTY guard. GUI presenters return None.
        """
        return None

    @abstractmethod
    def show_prompt(self, data: HitlDisplayData) -> None: ...

    @abstractmethod
    def lockout(self, seconds: int) -> None:
        """Block for `seconds`; show visible progress (INV-6)."""
        ...

    @abstractmethod
    def read_decision(self, timeout_seconds: int) -> Decision:
        """Return APPROVED, DENIED, TIMEOUT, or NON_TTY."""
        ...


class TerminalPresenter(HitlPresenter):
    """Apple-like terminal UI with live countdown and color coding."""

    def __init__(self) -> None:
        self._color = sys.stdout.isatty()

    def pre_check(self) -> Optional[Decision]:
        return Decision.NON_TTY if not sys.stdin.isatty() else None

    def show_prompt(self, data: HitlDisplayData) -> None:
        print(self._render(data), flush=True)

    def lockout(self, seconds: int) -> None:
        for remaining in range(seconds, 0, -1):
            print(f"\r  Approve available in {remaining}s... ", end="", flush=True)
            time.sleep(1)
        c = self._color
        avail = f"{_GREEN if c else ''}[A]pprove  [D]eny{_RESET if c else ''}"
        print(f"\r  {avail}  — waiting...  ", flush=True)

    def read_decision(self, timeout_seconds: int) -> Decision:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("\n  ✗ Timed out — operation denied.", flush=True)
                return Decision.TIMEOUT
            print(f"\r  [{int(remaining):2d}s]  [A]pprove  [D]eny  ", end="", flush=True)
            try:
                ready, _, _ = select.select([sys.stdin], [], [], min(remaining, 1.0))
            except (OSError, ValueError, BrokenPipeError):
                return Decision.NON_TTY
            if not ready:
                continue
            line = sys.stdin.readline()
            if not line:
                return Decision.DENIED
            key = line.strip().upper()
            if key == "A":
                c = self._color
                print(
                    f"\r  {_GREEN if c else ''}✓ Approved{_RESET if c else ''}                   ",
                    flush=True,
                )
                return Decision.APPROVED
            elif key == "D":
                print("\r  ✗ Denied                    ", flush=True)
                return Decision.DENIED
            elif key in ("E", "M"):
                label = "Explain more" if key == "E" else "Modify command"
                print(f"\n  [{label}] — not implemented in V1 (Phase 5)", flush=True)
            else:
                print(f"\n  Unknown key '{line.strip()}'. Press [A] or [D].", flush=True)

    def _render(self, data: HitlDisplayData) -> str:
        c = self._color

        def bold(s: str) -> str:
            return f"{_BOLD}{s}{_RESET}" if c else s

        def dim(s: str) -> str:
            return f"{_DIM}{s}{_RESET}" if c else s

        tier_label = {
            Tier.HIGH:      f"{_RED if c else ''}CRITICAL — DESTRUCTIVE{_RESET if c else ''}",
            Tier.MEDIUM:    f"{_YELLOW if c else ''}MEDIUM{_RESET if c else ''}",
            Tier.LOW:       "LOW",
            Tier.READ_ONLY: f"{_DIM if c else ''}READ-ONLY{_RESET if c else ''}",
        }.get(data.tier, data.risk_level.upper())

        lines = [
            "",
            "  " + "─" * 58,
            f"  {bold(chr(0x26a0) + '  SYSTEM ACTION REQUIRES YOUR APPROVAL')}",
            "  " + "─" * 58,
            f"  {dim('Action')}     {bold(data.action)}",
            f"  {dim('Target')}     {bold(data.target)}",
            f"  {dim('Risk')}       {tier_label}",
            f"  {dim('Reversible')}  {'No' if not data.reversible else 'Yes'}",
        ]
        if data.blocked_pattern:
            lines.append(f"  {dim('Pattern')}    {data.blocked_pattern}")
        if data.backend:
            lines.append(f"  {dim('Backend')}    {data.backend}")
        if data.reason:
            lines.append(f"  {dim('Reason')}     {data.reason}")
        if data.cow_summary:
            lines += ["", f"  {dim('Dry-run preview:')}"]
            for ln in data.cow_summary.splitlines()[:20]:
                lines.append(f"    {ln}")
        lines += [
            "",
            "  " + "─" * 58,
            f"  {dim('[A]pprove   [D]eny      (approve available in 3 s)')}",
            "",
        ]
        return "\n".join(lines)


class HitlPrompt:
    """Coordinates the HITL flow: build display data → present → lockout → read."""

    LOCKOUT_SECONDS: int = 3
    TIMEOUT_SECONDS: int = 30

    def __init__(
        self,
        intent: dict,
        classification: ClassificationResult,
        *,
        backend: str = "",
        cow_summary: Optional[str] = None,
        lockout_seconds: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        presenter: Optional[HitlPresenter] = None,
    ) -> None:
        self._intent = intent
        self._cls = classification
        self._backend = backend
        # Strip ANSI escapes from mcpd output before any presenter sees it
        self._cow_summary = _ANSI_ESCAPE.sub("", cow_summary) if cow_summary else None
        self._lockout_seconds = (
            lockout_seconds if lockout_seconds is not None else self.LOCKOUT_SECONDS
        )
        self._timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else self.TIMEOUT_SECONDS
        )
        self._presenter: HitlPresenter = presenter or TerminalPresenter()

    def ask(self) -> Decision:
        early = self._presenter.pre_check()
        if early is not None:
            return early
        data = self._build_display_data()
        try:
            self._presenter.show_prompt(data)
            self._presenter.lockout(self._lockout_seconds)
            return self._presenter.read_decision(self._timeout_seconds)
        except KeyboardInterrupt:
            print("\n  ✗ Interrupted — operation denied.", flush=True)
            return Decision.DENIED

    def _render(self) -> str:
        """Convenience for tests and smoke — renders via TerminalPresenter."""
        p = (
            self._presenter
            if isinstance(self._presenter, TerminalPresenter)
            else TerminalPresenter()
        )
        return p._render(self._build_display_data())

    def _build_display_data(self) -> HitlDisplayData:
        return HitlDisplayData(
            action=self._intent.get("action", "?"),
            target=self._intent.get("target", "?"),
            tier=self._cls.tier,
            risk_level=self._intent.get("risk_level", "unknown"),
            reversible=self._cls.reversible,
            backend=self._backend,
            reason=self._intent.get("reason", ""),
            blocked_pattern=self._cls.blocked_pattern,
            cow_summary=self._cow_summary,
        )
