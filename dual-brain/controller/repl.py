"""Interactive multi-turn REPL for the Icebreaker Controller.

Requires prompt_toolkit. Install with: pip install prompt_toolkit

UX design: Apple-like terminal feel.
  - Prompt:     (N) [backend] prefix >   (dim turn, cyan backend, bold prefix)
  - Spinner:    braille frames at 80ms while QB/PB/mcpd process
  - Status:     ✓ [Tier 0 · local · 312ms · $0.00] after every turn
  - Colors:     Tier 0 dim, Tier 1 plain, Tier 2 yellow, Tier 3 red
  - Commands:   /help /exit /quit /reset /status /backend <name>
  - Ctrl+C:     cancels in-flight turn, does NOT exit
  - Ctrl+D:     graceful exit
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    _HAS_PROMPT_TOOLKIT = True
except ImportError:
    _HAS_PROMPT_TOOLKIT = False

if TYPE_CHECKING:
    from .config import SessionConfig
    from .session import SessionState

_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_CYAN   = "\033[36m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_GREEN  = "\033[32m"
_RESET  = "\033[0m"

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_BANNER = """\
  ╔══════════════════════════════════════════╗
  ║  Icebreaker  —  AI-Native OS Controller  ║
  ╚══════════════════════════════════════════╝
  Type natural-language commands. /help for options.
"""


class Repl:
    """Interactive multi-turn REPL.

    Usage::

        repl = Repl(controller, cfg)
        repl.run()         # blocks until /exit or Ctrl+D
    """

    SLASH_COMMANDS = {"/help", "/exit", "/quit", "/reset", "/status"}

    def __init__(self, controller: Any, cfg: Any) -> None:
        if not _HAS_PROMPT_TOOLKIT:
            raise ImportError(
                "prompt_toolkit is required for the REPL. "
                "Install it with: pip install prompt_toolkit"
            )
        from .session import SessionState

        self._ctrl = controller
        self._cfg = cfg
        self._use_color = (
            cfg.color == "always"
            or (cfg.color == "auto" and sys.stdout.isatty())
        )
        self._session: SessionState = SessionState.new(controller.backend_name(), cfg)
        history_path = Path(cfg.history_path).expanduser()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        self._prompt_session: PromptSession = PromptSession(
            history=FileHistory(str(history_path)),
            enable_history_search=True,
        )

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        self._print_banner()
        while True:
            self._check_session_health()
            try:
                raw = self._prompt_session.prompt(self._format_prompt())
            except EOFError:
                self._handle_exit()
                return
            except KeyboardInterrupt:
                print()
                continue

            text = raw.strip()
            if not text:
                continue
            if text.startswith("/"):
                if self._handle_slash(text):
                    return
                continue
            self._run_turn(text)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _check_session_health(self) -> None:
        from .session import SessionState

        if self._session.is_expired():
            self._print(
                f"\n  Session expired after {self._cfg.session_ttl_seconds}s "
                "of inactivity. Starting a new session.\n"
            )
            self._session = SessionState.new(self._ctrl.backend_name(), self._cfg)
        elif self._session.is_full():
            self._print(
                f"\n  Session reached max_turns={self._cfg.max_turns}. "
                "Starting a new session.\n"
            )
            self._session = SessionState.new(self._ctrl.backend_name(), self._cfg)

    def _format_prompt(self) -> str:
        turn = self._session.turn_index
        backend = self._session.backend
        prefix = self._cfg.prompt_prefix
        if self._use_color:
            return (
                f"{_DIM}({turn}){_RESET} "
                f"{_CYAN}[{backend}]{_RESET} "
                f"{_BOLD}{prefix} >{_RESET} "
            )
        return f"({turn}) [{backend}] {prefix} > "

    def _run_turn(self, user_input: str) -> None:
        with self._spinner("Thinking..."):
            try:
                result = self._ctrl.run_turn(user_input, self._session)
            except KeyboardInterrupt:
                self._print("\n  Cancelled.\n")
                return
            except Exception as exc:
                self._print(f"\n  Error: {exc}\n")
                return
        self._display_result(result)

    def _display_result(self, result: Any) -> None:
        c = self._use_color
        tier_val = getattr(result, "tier", 0)
        tier_labels = {
            0: f"{_DIM}Tier 0{_RESET}" if c else "Tier 0",
            1: "Tier 1",
            2: f"{_YELLOW}Tier 2{_RESET}" if c else "Tier 2",
            3: f"{_RED}Tier 3{_RESET}" if c else "Tier 3",
        }
        tier_str = tier_labels.get(tier_val, f"Tier {tier_val}")
        backend = getattr(result, "backend", self._session.backend)
        dur = getattr(result, "duration_ms", 0.0)
        cost = getattr(result, "cost_usd", None)
        cost_str = f"${cost:.4f}" if cost else "$0.00"
        success = getattr(result, "success", False)
        icon = f"{_GREEN}✓{_RESET}" if (c and success) else ("✓" if success else "✗")

        output = getattr(result, "output", "")
        if output:
            print(f"\n{output}\n")
        print(f"  {icon} [{tier_str} · {backend} · {dur:.0f}ms · {cost_str}]\n")

    def _handle_slash(self, cmd: str) -> bool:
        """Return True to exit the REPL loop."""
        parts = cmd.split(None, 1)
        verb = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if verb in ("/exit", "/quit"):
            self._handle_exit()
            return True
        elif verb == "/help":
            self._print_help()
        elif verb == "/reset":
            from .session import SessionState
            self._session = SessionState.new(self._session.backend, self._cfg)
            self._print("  Session memory cleared. New session started.\n")
        elif verb == "/status":
            self._print_status()
        elif verb == "/backend":
            if not arg:
                self._print("  Usage: /backend <local|anthropic|gemini>\n")
            else:
                from .session import SessionState
                self._session = SessionState.new(arg.strip(), self._cfg)
                self._print(f"  Switched to backend: {arg.strip()}\n")
        else:
            self._print(f"  Unknown command: {verb}. Type /help for options.\n")
        return False

    def _handle_exit(self) -> None:
        c = self._use_color
        print(f"\n  {_DIM if c else ''}Goodbye.{_RESET if c else ''}\n")

    def _print_banner(self) -> None:
        if self._use_color:
            print(f"{_BOLD}{_BANNER}{_RESET}")
        else:
            print(_BANNER)

    def _print_help(self) -> None:
        lines = [
            "",
            "  Commands:",
            "    /help             — show this message",
            "    /status           — show session info",
            "    /reset            — clear session memory, start fresh",
            "    /backend <name>   — switch backend (local|anthropic|gemini)",
            "    /exit  /quit      — exit the REPL",
            "",
            "  Tips:",
            "    Ctrl+C  — cancel current turn (does not exit)",
            "    Ctrl+D  — exit",
            r"    \       — line continuation for multi-line input",
            "",
        ]
        print("\n".join(lines))

    def _print_status(self) -> None:
        s = self._session
        c = self._use_color
        ttl = self._cfg.session_ttl_seconds
        ttl_str = f"{ttl}s" if ttl > 0 else "disabled"
        lines = [
            "",
            f"  Session ID:  {s.session_id}",
            f"  Backend:     {s.backend}",
            f"  Turn:        {s.turn_index} / {self._cfg.max_turns}",
            f"  TTL:         {ttl_str}",
            "",
        ]
        print("\n".join(lines))

    def _print(self, message: str) -> None:
        print(message, end="")

    @contextmanager
    def _spinner(self, message: str) -> Generator[None, None, None]:
        if not self._cfg.show_spinner or not self._use_color:
            yield
            return

        stop = threading.Event()
        pad = len(message) + 6

        def _spin() -> None:
            i = 0
            while not stop.is_set():
                frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
                print(f"\r  {frame} {message} ", end="", flush=True)
                i += 1
                time.sleep(0.08)
            print(f"\r{' ' * pad}\r", end="", flush=True)

        t = threading.Thread(target=_spin, daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join(timeout=0.3)
