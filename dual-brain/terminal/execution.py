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

        shell = os.environ.get("SHELL", "/bin/sh")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                shell, "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._scrubbed_env(),
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

    def _scrubbed_env(self) -> dict[str, str]:
        """Return environment with sensitive vars removed (BP-8)."""
        env = dict(os.environ)
        for key in list(env):
            low = key.lower()
            if any(s in low for s in ("api_key", "secret", "token", "password", "credential")):
                del env[key]
        return env
