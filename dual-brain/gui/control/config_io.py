"""Shared TOML I/O for Control Center pages.

All settings pages write to ``~/.config/icebreaker/controller.toml`` (a
user-layered override that takes precedence over the system-wide
``/etc/icebreaker/controller.toml``). This module centralises the read/
merge/write logic so each page can focus on its own widgets.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib as _toml_reader  # Python 3.11+
except ImportError:
    import tomli as _toml_reader  # type: ignore

try:
    import tomli_w as _toml_writer
except ImportError:
    _toml_writer = None  # type: ignore


USER_CONFIG_DIR = Path.home() / ".config" / "icebreaker"
USER_CONFIG_PATH = USER_CONFIG_DIR / "controller.toml"
SYSTEM_CONFIG_PATH = Path("/etc/icebreaker/controller.toml")


class ConfigReadError(RuntimeError):
    """Raised when a TOML config file exists but cannot be parsed.

    Callers should surface this to the user (a red bar in the Control
    Center page) rather than silently degrade to defaults — a corrupt
    ``~/.config/icebreaker/controller.toml`` used to swallow every user
    override and reset the GUI to defaults on the next open, wiping
    settings the user had deliberately configured.
    """


def read_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file.

    Returns ``{}`` when the file does not exist (a legitimately-fresh
    config layer). Raises ``ConfigReadError`` when the file exists but
    cannot be parsed — silent degradation to defaults is a data-loss
    bug (F-53 Scope A.P2). The GUI should catch this at the page
    boundary and render a persistent error explaining which layer is
    broken and where to look.
    """
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return _toml_reader.load(f)
    except Exception as exc:
        raise ConfigReadError(
            f"Failed to parse TOML at {path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def walk(cfg: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Walk into ``cfg`` by section path, returning None on any miss."""
    cur: Any = cfg
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def effective(system: dict[str, Any], user: dict[str, Any],
              section: tuple[str, ...], key: str, default: Any) -> Any:
    """Look up ``[section].key`` — user override wins over system config,
    with a hard-coded ``default`` if neither has the field."""
    v = walk(user, section + (key,))
    if v is not None:
        return v
    v = walk(system, section + (key,))
    if v is not None:
        return v
    return default


def set_user_override(section: tuple[str, ...], key: str, value: Any) -> None:
    """Write ``value`` into ``~/.config/icebreaker/controller.toml`` at
    ``[section].key``. Creates the directory + file as needed."""
    if _toml_writer is None:
        raise RuntimeError(
            "tomli_w not installed — cannot write TOML overrides. "
            f"Edit {USER_CONFIG_PATH} directly."
        )
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = read_toml(USER_CONFIG_PATH)
    cur = cfg
    for part in section:
        cur = cur.setdefault(part, {})
    cur[key] = value
    with USER_CONFIG_PATH.open("wb") as f:
        _toml_writer.dump(cfg, f)


def restart_controller(timeout: float = 30) -> tuple[bool, str]:
    """pkexec-elevated restart of the icebreaker-controller service.

    Returns (success, message). Any Control Center page can call this
    after saving pending config changes.
    """
    try:
        proc = subprocess.run(
            ["pkexec", "systemctl", "restart", "icebreaker-controller"],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode == 0:
            return True, "Controller restarted. Changes are live."
        return False, (proc.stderr.strip() or "unknown error")
    except subprocess.TimeoutExpired:
        return False, "Timed out — polkit prompt cancelled?"
    except FileNotFoundError:
        return False, "pkexec not available."
