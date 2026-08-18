"""Strict local-command parser for the PB-first offline lane.

This is intentionally *not* a shell parser.  It recognizes a very small,
read-only language after the Icebreaker ``#`` prefix and returns a
controller-owned Intent Object.  Everything else remains QB-first.  In
particular, this module never executes a command and never passes the typed
command to PB.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final


_MAX_COMMAND_CHARS: Final = 256
_UNSAFE_PATH_CHARS: Final = re.compile(r"[\x00-\x1f\x7f;&|`$<>*?!'\"\\\\()]")
_PATH_COMMAND: Final = re.compile(r"^(ls|cat)(?: ([^\s]+))?$")
_FIXED_ACTIONS: Final = {
    "uptime": "system.uptime",
    "df -h": "system.disk",
    "free -h": "system.memory",
    "ps": "process.list",
    "ip addr": "network.status",
}
_LIST_ALIASES: Final = frozenset({
    "what's in this folder",
    "what is in this folder",
    "list files here",
    "list this folder",
})


@dataclass(frozen=True)
class DirectCommandMatch:
    """A parser-produced, schema-shaped Intent plus route metadata."""

    intent: dict[str, object]
    display_command: str


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _safe_cwd(cwd: str, home: Path) -> Path:
    """Return a canonical cwd only when it remains inside ``home``."""
    try:
        logical = Path(os.path.normpath(cwd)) if cwd else home
        candidate = logical.resolve(strict=False)
        home_real = home.resolve(strict=False)
    except (OSError, ValueError):
        return home
    return logical if _is_within(candidate, home_real) else home


def _resolve_home_path(token: str, *, cwd: Path, home: Path) -> Path | None:
    """Resolve one unquoted path token, refusing metacharacters and escapes."""
    if not token or len(token) > 512 or _UNSAFE_PATH_CHARS.search(token):
        return None
    # ``~`` is deliberately unsupported: accepting it turns this into a
    # shell-adjacent language.  Relative paths use the authenticated cwd.
    if token.startswith("~"):
        return None
    try:
        logical = Path(os.path.normpath(
            token if token.startswith("/") else str(cwd / token)
        ))
        candidate = logical.resolve(strict=False)
        home_real = home.resolve(strict=False)
    except (OSError, ValueError):
        return None
    return logical if _is_within(candidate, home_real) else None


def parse_direct_command(
    raw_input: str,
    *,
    cwd: str,
    home: str,
) -> DirectCommandMatch | None:
    """Return a fixed read-only Intent for a supported ``#`` command.

    ``None`` means "not part of the offline grammar".  Callers must route
    that case through the normal QB path; they must not interpret it as a
    shell command or attempt fuzzy matching.
    """
    if not isinstance(raw_input, str) or not raw_input.startswith("#"):
        return None
    command = raw_input[1:].strip()
    if not command or len(command) > _MAX_COMMAND_CHARS:
        return None

    try:
        home_path = Path(os.path.normpath(home))
    except (OSError, ValueError):
        return None
    if not home_path.is_absolute():
        return None
    cwd_path = _safe_cwd(cwd, home_path)
    normalized = " ".join(command.split())
    lowered = normalized.casefold()

    action = _FIXED_ACTIONS.get(lowered)
    target = ""
    if action is None and lowered in _LIST_ALIASES:
        action = "fs.list"
        target = str(cwd_path)
    elif action is None:
        match = _PATH_COMMAND.fullmatch(normalized)
        if match is None:
            return None
        verb, token = match.groups()
        if token and token.startswith("-"):
            return None
        resolved = _resolve_home_path(token or ".", cwd=cwd_path, home=home_path)
        if resolved is None:
            return None
        action = "fs.list" if verb == "ls" else "fs.read"
        target = str(resolved)

    return DirectCommandMatch(
        intent={
            # The Controller overwrites all server-owned fields before schema
            # validation.  This placeholder keeps the object visibly shaped
            # like every other ingress intent without leaking user text.
            "intent_id": "00000000-0000-4000-8000-000000000000",
            "action": action,
            "target": target,
            "params": {},
            "reason": "user_requested",
            "risk_level": "read_only",
        },
        display_command=normalized,
    )
