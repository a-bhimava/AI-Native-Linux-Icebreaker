"""Shared TOML I/O for Control Center pages.

All settings pages write to ``~/.config/icebreaker/controller.toml`` (a
user-layered override that takes precedence over the system-wide
``/etc/icebreaker/controller.toml``). This module centralises the read/
merge/write logic so each page can focus on its own widgets.

Phase 6 Scope D hardening (2026-07-11):
  * Env-var overrides ``ICEBREAKER_USER_CONFIG_PATH`` and
    ``ICEBREAKER_SYSTEM_CONFIG_PATH`` let tests point the module at a
    hermetic tempdir without monkey-patching the frozen constants.
  * ``set_user_override`` writes atomically via ``tempfile`` +
    ``os.replace`` — a mid-write crash can no longer truncate the
    user's config file.
  * ``restart_controller`` honours ``ICEBREAKER_DRY_RUN=1`` and returns
    (True, "dry-run: <command>") without invoking pkexec. Lets CI +
    Scope D smoke tests exercise the whole save→restart flow.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
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


_log = logging.getLogger(__name__)


def _resolve_user_config_dir() -> Path:
    """`ICEBREAKER_USER_CONFIG_PATH` env var wins for tests; otherwise
    XDG_CONFIG_HOME or ~/.config fallback."""
    override = os.environ.get("ICEBREAKER_USER_CONFIG_PATH")
    if override:
        return Path(override).expanduser().parent
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "icebreaker"


def _resolve_user_config_path() -> Path:
    override = os.environ.get("ICEBREAKER_USER_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return _resolve_user_config_dir() / "controller.toml"


def _resolve_system_config_path() -> Path:
    override = os.environ.get("ICEBREAKER_SYSTEM_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return Path("/etc/icebreaker/controller.toml")


# Historically these were module-level Path constants that many pages
# imported by name. Preserve the import name for backward compat, but
# resolve lazily via the properties above so env-var overrides
# actually work when a test sets them *after* import.
USER_CONFIG_DIR = _resolve_user_config_dir()
USER_CONFIG_PATH = _resolve_user_config_path()
SYSTEM_CONFIG_PATH = _resolve_system_config_path()


def _refresh_paths_from_env() -> None:
    """Test-only: re-resolve module constants after a test sets an
    ``ICEBREAKER_*_CONFIG_PATH`` env var. Not for production use — the
    constants are frozen at import in normal runs."""
    global USER_CONFIG_DIR, USER_CONFIG_PATH, SYSTEM_CONFIG_PATH
    USER_CONFIG_DIR = _resolve_user_config_dir()
    USER_CONFIG_PATH = _resolve_user_config_path()
    SYSTEM_CONFIG_PATH = _resolve_system_config_path()


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
    """Write ``value`` into the user config TOML at ``[section].key``.
    Creates the directory + file as needed.

    Phase 6 Scope D atomic write: uses a same-directory tempfile +
    ``os.replace`` so a crash mid-write (out of space, kernel panic,
    SIGKILL from OOM) cannot truncate the user's config. Either the
    old file or the new file — never a half-written one.

    Env override: ``ICEBREAKER_USER_CONFIG_PATH=/tmp/foo.toml`` points
    the write at a hermetic location — used by Scope D smoke tests to
    exercise every save path without touching the real user config.
    """
    if _toml_writer is None:
        raise RuntimeError(
            "tomli_w not installed — cannot write TOML overrides. "
            f"Edit {_resolve_user_config_path()} directly."
        )
    target = _resolve_user_config_path()
    # Phase 6 Scope D hardening (2026-07-11): reject symlink targets.
    # A malicious symlink at ~/.config/icebreaker/controller.toml pointing
    # to /etc/passwd would let the atomic-write path silently truncate
    # the target file. Refuse to write through a symlink; the user must
    # rm and recreate the file to opt into the new location.
    if target.is_symlink():
        raise RuntimeError(
            f"refusing to write through symlink at {target}. "
            "Remove the symlink and retry."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    # Empty section tuple would produce `cur[""]` etc. — the top-level
    # setter (empty section, key at root) is legal in TOML but a
    # legitimate caller would use ("some_section",). Reject to catch a
    # typo that would otherwise write a top-level key with an unlikely
    # name.
    if len(section) == 0:
        raise ValueError(
            "set_user_override requires a non-empty section tuple; "
            "top-level keys are not supported"
        )
    cfg = read_toml(target)
    cur = cfg
    for part in section:
        # Handle a legacy misconfig where a section slot holds a scalar
        # (e.g. `[qb]` written as `qb = 42`). Overwrite rather than
        # AttributeError on `.setdefault`.
        existing = cur.get(part) if isinstance(cur, dict) else None
        if not isinstance(existing, dict):
            cur[part] = {}
        cur = cur[part]
    cur[key] = value
    # Atomic write: write to sibling tempfile, fsync (best-effort), then
    # os.replace. Same directory so the replace is atomic on all POSIX
    # filesystems (cross-fs os.replace is not atomic).
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=target.parent,
        prefix=".controller-", suffix=".toml.tmp",
        delete=False,
    ) as tmp:
        tmp.write(_toml_writer.dumps(cfg).encode("utf-8"))
        try:
            tmp.flush()
            os.fsync(tmp.fileno())
        except OSError as exc:  # noqa: BLE001
            # Fsync failure on the tempfile isn't fatal — the replace
            # below will still succeed most of the time — but log so
            # a systemic issue is diagnosable in journalctl.
            _log.debug(
                "config_io.fsync of %s failed: %s: %s",
                tmp.name, type(exc).__name__, exc,
            )
        tmp_path = Path(tmp.name)
    try:
        os.replace(tmp_path, target)
    except Exception as exc:  # noqa: BLE001
        # F-53 pattern — cleanup + surface. Best-effort unlink of the
        # tempfile to avoid leaving a dot-file lying around.
        try:
            tmp_path.unlink()
        except OSError:  # noqa: BLE001
            pass
        _log.warning(
            "config_io.set_user_override atomic replace failed for %s: %s: %s",
            target, type(exc).__name__, exc,
        )
        raise


def restart_controller(timeout: float = 30) -> tuple[bool, str]:
    """pkexec-elevated restart of the icebreaker-controller service.

    Returns (success, message). Any Control Center page can call this
    after saving pending config changes.

    Phase 6 Scope D: ``ICEBREAKER_DRY_RUN=1`` env var makes this
    return ``(True, "dry-run: <command>")`` without invoking pkexec.
    Lets Scope D smoke tests exercise the whole save→restart flow
    without a polkit agent in headless CI.
    """
    cmd = ["pkexec", "systemctl", "restart", "icebreaker-controller"]
    # Phase 6 Scope D/E debug logging (lazy import — this module can be
    # imported by pages that don't have controller on their PYTHONPATH).
    try:
        from controller import debug_log as _debug_log
        if _debug_log.is_enabled():
            _debug_log.record(
                "restart_controller", "gui.config_io",
                {"cmd": cmd, "dry_run": os.environ.get("ICEBREAKER_DRY_RUN") == "1"},
            )
    except Exception:  # noqa: BLE001
        # Debug logging must never take down the restart flow.
        pass
    if os.environ.get("ICEBREAKER_DRY_RUN") == "1":
        return True, f"dry-run: would run: {' '.join(cmd)}"
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode == 0:
            return True, "Controller restarted. Changes are live."
        return False, (proc.stderr.strip() or "unknown error")
    except subprocess.TimeoutExpired:
        return False, "Timed out — polkit prompt cancelled?"
    except FileNotFoundError:
        return False, "pkexec not available."
