"""HITL approval gate — hardened single-keypress terminal UI.

Phase 5 M5.1b hardening (SF-1/SF-2/SF-3):
  - All rendered fields are sanitized (ANSI, C0/C1, \\r\\n stripped)
  - Input buffer flushed (tcflush) after lockout before reading
  - Single raw keypress via tty.setcbreak + os.read(..., 1)
  - Esc always → DENY, Ctrl+C → DENY, EOF → DENY, timeout → TIMEOUT
  - ASCII fallback when $NO_COLOR set or locale is non-UTF-8
"""

from __future__ import annotations

import os
import re
import select
import signal
import threading
import sys
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .audit import Outcome
from .keymap import Action, Keymap
from .risk_classifier import ClassificationResult, Tier

# ── ANSI / color helpers ───────────────────────────────────────────────────

_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_GREEN  = "\033[32m"
_RESET  = "\033[0m"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_C0_C1 = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f\x80-\x9f]")


# ── Display sanitization (SF-1) ───────────────────────────────────────────


def _sanitize_display(s: str, *, max_len: int = 256) -> str:
    """Strip ANSI escapes, C0/C1 control chars, neutralize \\r\\n, truncate."""
    if not isinstance(s, str):
        s = str(s)
    s = _ANSI_ESCAPE.sub("", s)
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = _C0_C1.sub("", s)
    if len(s) > max_len:
        s = s[:max_len] + ("..." if _ascii_safe() else "…")
    return s


def _confusable_warn(target: str) -> str:
    """Return a warning suffix if target contains non-ASCII (potential homoglyph)."""
    if target and not target.isascii():
        return " [contains non-ASCII characters]" if _ascii_safe() else " ⚠ contains non-ASCII/look-alike chars"
    return ""


# ── Environment detection ─────────────────────────────────────────────────


def _ascii_safe() -> bool:
    """Return True if we should use ASCII-only output (no UTF-8 box chars)."""
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        val = os.environ.get(var, "")
        if val and "utf" in val.lower():
            return False
    return True


def _colors_enabled() -> bool:
    """Return True if ANSI color codes should be used."""
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


# ── Decision enum ─────────────────────────────────────────────────────────


class Decision(str, Enum):
    APPROVED = "approved"
    DENIED   = "denied"
    TIMEOUT  = "timeout"
    NON_TTY  = "non_tty"
    MODIFY   = "modify"
    EXPLAIN  = "explain"
    TRUST    = "trust"

    def to_outcome(self) -> Optional[Outcome]:
        return {
            Decision.DENIED:  Outcome.HITL_DENIED,
            Decision.TIMEOUT: Outcome.HITL_TIMEOUT,
            Decision.NON_TTY: Outcome.HITL_NON_TTY,
            Decision.MODIFY:  Outcome.MODIFY_REQUESTED,
        }.get(self)  # APPROVED → None; caller sets downstream Outcome


# ── Display data ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HitlDisplayData:
    """Immutable snapshot passed to any presenter — all fields sanitized."""
    action: str
    target: str
    tier: Tier
    risk_level: str
    reversible: bool
    backend: str
    reason: str
    blocked_pattern: Optional[str]
    cow_summary: Optional[str]
    rpa_keyword_preview: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("action", "target", "reason", "backend", "risk_level"):
            val = getattr(self, field_name)
            if val:
                object.__setattr__(self, field_name, _sanitize_display(val))
        if self.blocked_pattern:
            object.__setattr__(self, "blocked_pattern",
                               _sanitize_display(self.blocked_pattern))
        if self.cow_summary:
            object.__setattr__(self, "cow_summary",
                               _sanitize_display(self.cow_summary, max_len=4096))
        if self.rpa_keyword_preview:
            object.__setattr__(self, "rpa_keyword_preview", tuple(
                _sanitize_display(kw, max_len=120) for kw in self.rpa_keyword_preview
            ))


# ── cbreak context manager ────────────────────────────────────────────────


@contextmanager
def _cbreak(stream):
    """Set terminal to cbreak mode; restore on exit."""
    try:
        import termios
        import tty
    except ImportError:
        yield
        return

    fd = stream.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Presenter ABC ─────────────────────────────────────────────────────────


class HitlPresenter(ABC):
    """Swap TerminalPresenter for a GUI presenter in Phase 5."""

    @property
    def last_key_class(self) -> str:
        return getattr(self, "_last_key_class", "")

    def pre_check(self) -> Optional[Decision]:
        """Return a Decision to short-circuit ask() before any I/O starts."""
        return None

    @abstractmethod
    def show_prompt(self, data: HitlDisplayData) -> None: ...

    @abstractmethod
    def lockout(self, seconds: int) -> None: ...

    @abstractmethod
    def read_decision(self, timeout_seconds: int) -> Decision: ...


# ── ASCII / Unicode glyph sets ────────────────────────────────────────────

_GLYPHS_UTF8 = {
    "warn":     "⚠",   # ⚠
    "critical": "⛔",   # ⛔
    "ok":       "✓",   # ✓
    "fail":     "✗",   # ✗
    "arrow":    "⤴",   # ⤴
    "hourglass": "⌛",  # ⌛
    "line":     "─",   # ─
    "dline":    "═",   # ═
}

_GLYPHS_ASCII = {
    "warn":     "!",
    "critical": "!!",
    "ok":       "[OK]",
    "fail":     "[X]",
    "arrow":    "->",
    "hourglass": "",
    "line":     "-",
    "dline":    "=",
}


def _g(name: str) -> str:
    """Return the glyph for the current terminal capability."""
    glyphs = _GLYPHS_ASCII if _ascii_safe() else _GLYPHS_UTF8
    return glyphs.get(name, "")


# ── Terminal presenter ────────────────────────────────────────────────────


class TerminalPresenter(HitlPresenter):
    """Hardened terminal UI with single-keypress input, sanitization, and
    configurable keymap."""

    def __init__(self, *, keymap: Optional[Keymap] = None) -> None:
        self._color = _colors_enabled()
        self._keymap = keymap or Keymap()
        self._last_key_class: str = ""

    def pre_check(self) -> Optional[Decision]:
        if not sys.stdin.isatty():
            self._last_key_class = "non_tty"
            return Decision.NON_TTY
        return None

    def show_prompt(self, data: HitlDisplayData) -> None:
        print(self._render(data), flush=True)

    def lockout(self, seconds: int) -> None:
        for remaining in range(seconds, 0, -1):
            print(f"\r  Approve available in {remaining}s... ", end="", flush=True)
            time.sleep(1)
        legend = self._keymap.legend(include_trust=True)
        c = self._color
        if c:
            avail = f"{_GREEN}{legend}{_RESET}"
        else:
            avail = legend
        print(f"\r  {avail}  ", flush=True)

    def read_decision(self, timeout_seconds: int) -> Decision:
        fd = sys.stdin.fileno()

        # SF-2: flush any bytes buffered during lockout
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
                c = self._color
                fail = f"{_RED}{_g('fail')}{_RESET}" if c else _g("fail")
                print(f"\n  {fail} Timed out — operation denied.", flush=True)
                self._last_key_class = "timeout"
                return Decision.TIMEOUT

            legend_str = self._keymap.legend(include_trust=True)
            print(f"\r  [{int(remaining):2d}s]  {legend_str}  ", end="", flush=True)

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
                print(f"\r  {_g('fail')} Denied (Esc)                    ", flush=True)
                self._last_key_class = "esc"
                return Decision.DENIED

            action = self._keymap.lookup(key)
            if action is None:
                print(f"\r  Unknown key. Press ? for help.                ", flush=True)
                continue

            return self._resolve_action(action, key)

    def _resolve_action(self, action: Action, key: str) -> Decision:
        key_class = "numeric" if key.isdigit() else "mnemonic"
        self._last_key_class = key_class
        c = self._color

        if action == Action.APPROVE:
            ok = f"{_GREEN}{_g('ok')} Approved{_RESET}" if c else f"{_g('ok')} Approved"
            print(f"\r  {ok}                       ", flush=True)
            return Decision.APPROVED

        if action == Action.DENY:
            print(f"\r  {_g('fail')} Denied                         ", flush=True)
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
            print(f"\n  {legend}", flush=True)
            print(f"  Esc = Deny (always)", flush=True)
            return Decision.EXPLAIN  # re-prompt after showing help — handled by ask()

        return Decision.DENIED  # unreachable fallback

    def _render(self, data: HitlDisplayData) -> str:
        c = self._color
        ascii_mode = _ascii_safe()

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

        line_char = _g("line")
        sep = "  " + line_char * 58

        if data.tier == Tier.HIGH:
            dline = _g("dline")
            sep_top = "  " + dline * 58
            icon = _g("critical")
            header = f"  {bold(icon + '  CRITICAL — destructive system change')}"
        else:
            sep_top = sep
            icon = _g("warn")
            header = f"  {bold(icon + '  System action requires your approval')}"

        confusable = _confusable_warn(data.target)
        target_display = bold(data.target) + (dim(confusable) if confusable else "")

        lines = [
            "",
            sep_top,
            header,
            sep,
            f"  {dim('Action')}     {bold(data.action)}",
            f"  {dim('Target')}     {target_display}",
            f"  {dim('Risk')}       {tier_label}",
            f"  {dim('Reversible')} {'No' if not data.reversible else 'Yes'}",
        ]
        if data.blocked_pattern:
            lines.append(f"  {dim('Pattern')}    {data.blocked_pattern}")
        if data.backend:
            lines.append(f"  {dim('Backend')}    {data.backend}")
        if data.reason:
            lines.append(f"  {dim('Why')}        {data.reason}")
        if data.rpa_keyword_preview:
            rpa_line = line_char if not ascii_mode else "-"
            lines += ["", f"  {dim('RPA Workflow:')}"]
            for i, kw in enumerate(data.rpa_keyword_preview, 1):
                lines.append(f"    {i:2d}. {kw}")
        if data.cow_summary:
            lines += ["", f"  {dim('Dry-run preview:')}"]
            for ln in data.cow_summary.splitlines()[:20]:
                lines.append(f"    {ln}")
        lines += [
            "",
            sep,
            f"  {dim(self._keymap.legend(include_trust=True))}",
            "",
        ]
        return "\n".join(lines)


# ── HitlPrompt coordinator ───────────────────────────────────────────────


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
        self._cow_summary = _ANSI_ESCAPE.sub("", cow_summary) if cow_summary else None
        self._lockout_seconds = (
            lockout_seconds if lockout_seconds is not None else self.LOCKOUT_SECONDS
        )
        self._timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else self.TIMEOUT_SECONDS
        )
        self._presenter: HitlPresenter = presenter or TerminalPresenter()

        self.decision_id: str = str(uuid.uuid4())
        self.key_pressed_class: str = ""
        self.decision_latency_ms: float = 0.0

    def ask(self) -> Decision:
        early = self._presenter.pre_check()
        if early is not None:
            self.key_pressed_class = self._presenter.last_key_class or "non_tty"
            self.decision_latency_ms = 0.0
            return early

        data = self._build_display_data()
        try:
            self._presenter.show_prompt(data)
            prompt_shown_at = time.monotonic()
            self._presenter.lockout(self._lockout_seconds)
            elapsed = time.monotonic() - prompt_shown_at
            if elapsed < self._lockout_seconds:
                time.sleep(self._lockout_seconds - elapsed)

            # F-45 (2026-07-09): install SIGINT-as-deny handler only when we
            # are on the MAIN thread. `signal.signal()` raises ValueError with
            # message "signal only works in main thread of the main
            # interpreter" from any other thread. Under the daemon's asyncio
            # worker pool a Tier 3 HITL prompt used to crash the entire turn
            # with this error — a critical INV-6 regression because the safety
            # gate itself was unenforceable. Now: on the main thread we still
            # install the deny handler; on any worker thread we skip it and
            # rely on the user's explicit keyboard decision (Approve/Deny via
            # the keymap). Ctrl+C on a worker-thread prompt just does whatever
            # the inherited handler does — acceptable since HITL is otherwise
            # driven by explicit key input.
            old_handler = None
            sigint_fired = [False]

            def _sigint_deny(signum, frame):
                sigint_fired[0] = True

            _on_main_thread = threading.current_thread() is threading.main_thread()
            _signal_installed = False
            try:
                if _on_main_thread:
                    old_handler = signal.getsignal(signal.SIGINT)
                    try:
                        signal.signal(signal.SIGINT, _sigint_deny)
                        _signal_installed = True
                    except ValueError:
                        # Defensive: signal.signal can also fail if the main
                        # thread has been reparented (extremely rare, but
                        # bpython embedding does this). Fall through to
                        # keyboard-only decision path.
                        _signal_installed = False
                decision = self._presenter.read_decision(self._timeout_seconds)
                if sigint_fired[0]:
                    self.key_pressed_class = "sigint"
                    self.decision_latency_ms = (time.monotonic() - prompt_shown_at) * 1000
                    c = _colors_enabled()
                    fail = f"{_RED}{_g('fail')}{_RESET}" if c else _g("fail")
                    print(f"\n  {fail} Interrupted — operation denied.", flush=True)
                    return Decision.DENIED
            finally:
                if _signal_installed and old_handler is not None:
                    signal.signal(signal.SIGINT, old_handler)

            self.key_pressed_class = self._presenter.last_key_class
            self.decision_latency_ms = (time.monotonic() - prompt_shown_at) * 1000

            # EXPLAIN loops: show help and re-read without exiting ask()
            while decision == Decision.EXPLAIN:
                decision = self._presenter.read_decision(self._timeout_seconds)
                self.key_pressed_class = self._presenter.last_key_class
                self.decision_latency_ms = (time.monotonic() - prompt_shown_at) * 1000

            return decision
        except KeyboardInterrupt:
            self.key_pressed_class = "sigint"
            self.decision_latency_ms = 0.0
            c = _colors_enabled()
            fail = f"{_RED}{_g('fail')}{_RESET}" if c else _g("fail")
            print(f"\n  {fail} Interrupted — operation denied.", flush=True)
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
