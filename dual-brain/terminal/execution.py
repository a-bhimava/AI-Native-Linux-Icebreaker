"""Left panel — shell execution via subprocess with RichLog output.

Subprocess fallback (P6T-R2 mitigation): runs $SHELL, captures stdout/stderr
line-by-line, renders in a Textual RichLog. No embedded PTY — tab completion
and interactive programs (vim, htop) are degraded. PTY embedding is a
separate spike that can be swapped in without changing the TUI layout.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import RichLog, Static

_PS1_COLOR = "#5e8787"


class ExecutionPanel(Static):
    """Left panel: subprocess shell with scrollback."""

    DEFAULT_CSS = ""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._proc: asyncio.subprocess.Process | None = None
        # V6B Stage 2: track the shell state we'll forward to QB on NL
        # turns. `cwd` starts at the process cwd and updates whenever the
        # user runs a `cd` command; `recent` keeps the last 5 non-empty
        # commands, newest last (matches the QB prompt's "oldest first").
        self._cwd: str = os.getcwd()
        self._recent: list[str] = []

    def shell_context(self) -> dict:
        """Snapshot of shell state for the daemon run_turn RPC."""
        import socket as _socket
        ctx = {
            "cwd": self._cwd,
            "recent_commands": list(self._recent),
            "user": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
            "hostname": _socket.gethostname(),
        }
        # V6.3 Stage 3: best-effort focused window title via wmctrl.
        # Fails silently on non-X11 sessions, missing wmctrl, or timeouts —
        # empty string never harms the QB preamble.
        try:
            import subprocess as _sp
            r = _sp.run(
                ["wmctrl", "-l"], capture_output=True, text=True,
                timeout=0.5,
            )
            if r.returncode == 0 and r.stdout:
                # Simplest heuristic: the first line is often the topmost/focused
                # window under a stacking WM. Good-enough hint for QB context.
                first = r.stdout.splitlines()[0]
                # Format: "0x0400002 0 hostname Terminal – ~"
                parts = first.split(None, 3)
                if len(parts) == 4:
                    ctx["active_window"] = parts[3].strip()[:200]
        except Exception:  # noqa: BLE001
            # F-53 Scope A.P2: wmctrl probe is genuinely best-effort —
            # non-X11 session (Wayland-only), missing wmctrl binary, or a
            # 0.5 s timeout are all common and expected. Silent swallow
            # is correct here: `active_window` is optional context that
            # QB tolerates being absent. Logging every miss would spam
            # journalctl on every NL turn without helping.
            pass
        return ctx

    def compose(self) -> ComposeResult:
        yield RichLog(
            highlight=True,
            markup=False,
            wrap=True,
            id="shell-output",
        )

    def on_mount(self) -> None:
        log = self.query_one("#shell-output", RichLog)
        log.write(
            Text("Icebreaker AI Terminal — shell ready\n", style=f"bold {_PS1_COLOR}")
        )

    async def run_command(self, command: str) -> None:
        """Execute a shell command and stream output to the RichLog."""
        log = self.query_one("#shell-output", RichLog)

        prompt = Text(f"$ {command}", style=f"bold {_PS1_COLOR}")
        log.write(prompt)

        # V6B Stage 2: intercept `cd` so the panel's tracked cwd updates
        # (each subprocess runs in a fresh child — its cd wouldn't persist).
        stripped = command.strip()
        cd_target = self._cd_target(stripped)
        if cd_target is not None:
            new_cwd = self._resolve_cd(cd_target)
            if new_cwd is not None:
                self._cwd = new_cwd
                self._push_recent(stripped)
                log.write(Text(new_cwd, style="dim"))
                return
            log.write(Text(f"cd: {cd_target}: No such directory", style="bold red"))
            return

        self._push_recent(stripped)

        shell = os.environ.get("SHELL", "/bin/sh")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                shell, "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._scrubbed_env(),
                cwd=self._cwd,
            )
        except OSError as exc:
            log.write(Text(f"Error: {exc}", style="bold red"))
            return

        assert self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            log.write(line.decode("utf-8", errors="replace").rstrip("\n"))

        returncode = await self._proc.wait()
        self._proc = None

        if returncode != 0:
            log.write(
                Text(f"[exit {returncode}]", style="dim")
            )

    # ── V6B Stage 2 helpers ─────────────────────────────────────────────
    def _push_recent(self, cmd: str) -> None:
        if not cmd:
            return
        # Cap length; keep last 5.
        self._recent.append(cmd[:200])
        if len(self._recent) > 5:
            self._recent = self._recent[-5:]

    def _cd_target(self, command: str) -> str | None:
        """If command is a bare `cd [dir]`, return the target (or '' for HOME)."""
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        if not parts or parts[0] != "cd":
            return None
        if len(parts) == 1:
            return ""       # bare 'cd' → $HOME
        if len(parts) == 2:
            return parts[1]
        return None         # 'cd a b' → let the subshell error

    def _resolve_cd(self, target: str) -> str | None:
        if target == "" or target == "~":
            candidate = os.environ.get("HOME") or ""
        elif target.startswith("~/"):
            candidate = os.path.join(os.environ.get("HOME") or "", target[2:])
        elif target == "-":
            # Not tracking OLDPWD in-panel; skip the trick and let subshell handle it
            return None
        elif os.path.isabs(target):
            candidate = target
        else:
            candidate = os.path.join(self._cwd, target)
        candidate = os.path.normpath(candidate)
        if not candidate or not os.path.isdir(candidate):
            return None
        return candidate

    def _scrubbed_env(self) -> dict[str, str]:
        """Return environment with sensitive vars removed (BP-8)."""
        env = dict(os.environ)
        for key in list(env):
            low = key.lower()
            if any(s in low for s in ("api_key", "secret", "token", "password", "credential")):
                del env[key]
        return env
