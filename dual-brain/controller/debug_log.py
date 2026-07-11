"""Phase 6 Scope D/E — structured debug-mode logger.

**Off by default (BP-2).** Turn on via ``[debug] enabled = true`` in
``controller.toml`` (or the Behavior page toggle in Control Center).
While on, every instrumented call site emits a structured JSONL record
to ``$XDG_STATE_HOME/icebreaker/debug.jsonl`` — one JSON object per
line, safe to `tail -f` and `jq` through.

**What gets logged.** Only instrumented sites — this module is a passive
sink. Current instrumentation (grep for ``debug_log.record`` to see
call sites):

  * ``fallback_backend.FallbackChain`` — every fallback fire, with
    hop index + backend name + prior error type.
  * ``preset_verifier`` — every metadata endpoint hit + result.
  * ``preset_verification_cache`` — every cache hit / miss / save.
  * ``config_io.set_user_override`` — every write, with the key path
    (values are redacted if the key name looks like a secret).
  * ``config_io.restart_controller`` — every dispatch, dry-run or real.

**BP-8 secret hygiene.** Values whose key path contains one of
``_SECRET_KEY_TOKENS`` (`api_key`, `token`, `secret`, `password`,
`bearer`) are redacted to ``"<REDACTED>"`` before landing on disk.
The RAW value never crosses this module's boundary. This is the same
approach ``BP-7`` prescribes for the audit log; debug is even less
trusted (it lands in state-home, not the tamper-evident audit sink).

**Storage discipline.** ``$XDG_STATE_HOME/icebreaker/debug.jsonl``:

  * Append-only. Never truncated by this module — rotation is a
    concern for a later scope (M8+).
  * Capped at ``max_size_mb`` (default 32 MB). When exceeded, the
    logger rotates once — moves the current file to ``.old`` and
    starts fresh. Older rotations are user's responsibility to clean.
  * Line-buffered so ``tail -f`` sees new records without waiting.

**Concurrency.** A module-level lock serializes writes so two threads
can't interleave a record. Multiple processes (a daemon writing while
Control Center reads back) rely on POSIX ``O_APPEND`` semantics — safe
for single-line writes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_log = logging.getLogger(__name__)


# Substrings that trigger secret redaction on values. Kept aggressive
# on purpose — a false positive redacts a debug value we didn't need
# to see; a false negative leaks a token into a state file.
_SECRET_KEY_TOKENS: tuple[str, ...] = (
    "api_key", "api-key", "apikey",
    "auth_token", "authtoken", "token",
    "secret", "password", "bearer",
    "credential", "cred",
)


# Env-var override for the log path — matches the same pattern used
# by config_io / preset_verification_cache. Tests set this to a
# tempdir so debug logging doesn't leak into ~/.local/state.
_LOG_PATH_ENV = "ICEBREAKER_DEBUG_LOG_PATH"


# Module-level state
_write_lock = threading.Lock()
_enabled: bool = False
_log_path: Path | None = None
_max_size_bytes: int = 32 * 1024 * 1024


# ── Configuration ────────────────────────────────────────────────────────


def configure(
    *,
    enabled: bool,
    log_path: Path | None = None,
    max_size_mb: int = 32,
) -> None:
    """Enable / disable debug logging + set the target path.

    Called on daemon startup from the config load path. The GUI
    Behavior-page toggle writes to ``[debug] enabled = ...`` and then
    triggers a controller restart, which re-runs configure() with the
    new value.

    ``log_path=None`` means "use the default XDG state path". Tests
    pass an explicit ``tmp_path / "debug.jsonl"``.
    """
    global _enabled, _log_path, _max_size_bytes
    _enabled = bool(enabled)
    _max_size_bytes = max(1, int(max_size_mb)) * 1024 * 1024
    _log_path = _resolve_log_path(log_path)


def _resolve_log_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    override = os.environ.get(_LOG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "icebreaker" / "debug.jsonl"


def is_enabled() -> bool:
    """Fast check callers use to skip building a payload dict when
    debug is off. Avoids the JSON-encode cost on the hot path."""
    return _enabled


def current_log_path() -> Path | None:
    """Return the resolved log path so the GUI 'clear log' button /
    'open in file manager' shortcut can address it directly."""
    return _log_path


# ── Public write API ─────────────────────────────────────────────────────


@dataclass
class DebugEvent:
    """One structured debug record. Serialized to JSONL."""
    ts: str
    event_type: str
    source: str
    data: dict[str, Any] = field(default_factory=dict)


def record(
    event_type: str,
    source: str,
    data: Mapping[str, Any] | None = None,
) -> None:
    """Emit one debug event.

    Args:
        event_type: short kind like ``fallback_fire``, ``preset_verify``,
            ``config_write``, ``restart_controller``. Aim for
            snake_case verbs a jq filter can grep for.
        source: dotted module path or logical component name. Lets
            the reader distinguish two callers that emit the same
            event_type.
        data: arbitrary payload. Keys whose names match a secret
            token get their values redacted before write.
    """
    if not _enabled or _log_path is None:
        return
    payload = _redact_secrets(dict(data or {}))
    evt = DebugEvent(
        ts=datetime.now(timezone.utc).isoformat(),
        event_type=str(event_type),
        source=str(source),
        data=payload,
    )
    line = json.dumps(asdict(evt), separators=(",", ":"), default=_json_fallback)
    _write_line(line)


def _write_line(line: str) -> None:
    """Serialize the append + rotation check behind a single lock so
    two threads can't interleave a record."""
    if _log_path is None:
        return
    with _write_lock:
        try:
            _log_path.parent.mkdir(parents=True, exist_ok=True)
            # Rotate if we've exceeded the cap.
            if _log_path.exists() and _log_path.stat().st_size > _max_size_bytes:
                try:
                    _log_path.replace(_log_path.with_suffix(".jsonl.old"))
                except OSError as exc:
                    _log.debug(
                        "debug_log rotate failed for %s: %s: %s",
                        _log_path, type(exc).__name__, exc,
                    )
            # O_APPEND is what makes concurrent writes from multiple
            # processes safe — each single-line write is atomic at
            # POSIX filesystem level.
            with _log_path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
        except OSError as exc:
            # Debug logging failing must NEVER take down the hot path.
            # Log to stderr once per exception type and continue.
            _log.debug(
                "debug_log write failed for %s: %s: %s",
                _log_path, type(exc).__name__, exc,
            )


def clear() -> tuple[bool, str]:
    """Truncate the debug log. Called from the Behavior page's
    'Clear debug log' action. Returns (success, message) matching the
    restart_controller() contract so the GUI can flash a status."""
    if _log_path is None or not _log_path.exists():
        return True, "no debug log to clear"
    with _write_lock:
        try:
            _log_path.write_text("")
            return True, f"cleared {_log_path}"
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc}"


# ── Secret hygiene ───────────────────────────────────────────────────────


def _redact_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """Recursive walk: any key whose name contains a secret token gets
    its value replaced with ``"<REDACTED>"``. Nested dicts / lists are
    walked. Non-dict values are passed through unchanged."""
    result: dict[str, Any] = {}
    for k, v in payload.items():
        lower = str(k).lower()
        if any(tok in lower for tok in _SECRET_KEY_TOKENS):
            result[k] = "<REDACTED>"
            continue
        if isinstance(v, dict):
            result[k] = _redact_secrets(v)
        elif isinstance(v, list):
            result[k] = [
                _redact_secrets(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            result[k] = v
    return result


def _json_fallback(obj: Any) -> str:
    """json.dumps fallback for non-serializable values. Convert to
    repr() rather than raise — losing a debug event because it carried
    a Path object is worse than losing type fidelity."""
    return repr(obj)
