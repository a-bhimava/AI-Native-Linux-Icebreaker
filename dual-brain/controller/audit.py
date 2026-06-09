"""
audit.py — Append-only JSONL audit log for the Controller (INV-8).

INV-8 (CLAUDE.md):
  "The audit log MUST be opened with O_APPEND. It MUST record every
   intent, including rejected ones, with timestamp, intent ID, risk
   level, user, and outcome. The audit log MUST NOT be writable by the
   AI models or their inference processes."

Round-2 spec expands the entry shape so that backend cost + provenance
are tracked from day one (M2.4 onwards will populate the backend fields):

  {
    ts                  ISO-8601 in UTC + monotonic ns since boot
    session_id          UUID stamped at session start (M2.11)
    turn_index          0-based turn counter within session (M2.11)
    intent_id           UUID from validated Intent Object
    action              MCP tool name
    target              path / unit name / pid / package
    tier                0 / 1 / 2 / 3 — Tier IntEnum value
    reason              user_requested | ai_autonomous | scheduled
    risk_level          read_only | low | medium | high | critical
    outcome             Outcome enum value (see below)
    duration_ms         wall-clock for the whole turn
    user                effective uname (from os / pwd)
    backend             local | anthropic | gemini (M2.4+)
    model               specific model id (e.g. claude-haiku-4-5)
    tokens_in           int — input tokens for the QB call this turn
    tokens_out          int — output tokens for the QB call this turn
    cost_estimate_usd   float — running cost estimate for this turn
  }

Three other audit logs exist on disk; this one is the *intent-level*
record only and must NOT collide with them:

  ~/.local/state/icebreaker/controller-audit.log   ← this module
  /var/log/mcpd/audit.log                          ← mcpd (tool-level)
  ~/.pb_audit.jsonl                                ← shell/pb_audit.py
                                                     (V1 shell trigger)

Durability (P2-F16): every write is os.write() + os.fsync(). The fsync
overhead is ~1 ms on the V1 SSD — within the Tier 0/1 latency budget.
A SIGKILL between write and fsync would still preserve all completed
prior entries because each line is atomically appended (single os.write
of one complete line ≤ PIPE_BUF).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional


# ── Outcome enum ────────────────────────────────────────────────────────────

class Outcome(str, Enum):
    """Terminal state of a single intent attempt.

    str-mixin so json.dumps can serialise the value directly.
    """

    # Validation gates
    SCHEMA_REJECTED         = "schema_rejected"          # M2.2 IntentValidationError
    CLASSIFICATION_FAILED   = "classification_failed"    # classifier raised (defensive)

    # HITL gates (M2.9)
    HITL_DENIED             = "hitl_denied"              # user pressed [D]
    HITL_TIMEOUT            = "hitl_timeout"             # 30 s decision timeout = deny
    HITL_NON_TTY            = "hitl_non_tty"             # no TTY → default deny

    # mcpd dispatch outcomes
    EXECUTED                = "executed"                 # mcpd returned success envelope
    TOOL_ERROR              = "tool_error"               # mcpd returned JSON-RPC error
    TOOL_TIMEOUT            = "tool_timeout"             # M2.1 McpdTimeoutError
    COW_REQUIRED            = "cow_required"             # status=requires_cow_approval

    # Brain failures
    BRAIN_ERROR             = "brain_error"              # QB / PB exception or truncation
    QB_VERIFIER_REJECTED    = "qb_verifier_rejected"     # M2.12 round-trip "no"
    PB_SCHEMA_ERROR         = "pb_schema_error"          # M2.12 PB output failed tool schema check

    # Process / session
    BACKEND_SWAPPED         = "backend_swapped"          # P2-F22 — start of new session


# ── Required entry fields ──────────────────────────────────────────────────

# Order matters only for readability in the on-disk log; json.dumps preserves
# dict insertion order in Python 3.7+.
REQUIRED_FIELDS: tuple[str, ...] = (
    "ts",
    "session_id",
    "turn_index",
    "intent_id",
    "action",
    "target",
    "tier",
    "reason",
    "risk_level",
    "outcome",
    "duration_ms",
    "user",
    "backend",
    "model",
    "tokens_in",
    "tokens_out",
    "cost_estimate_usd",
)


# ── Redaction patterns ─────────────────────────────────────────────────────

# Key-name heuristic — case-insensitive substring match against the key's
# alnum-only normalised form. Catches `password`, `password_hash`, `api_key`,
# `X-Api-Key`, `OPENAI_API_KEY`, `auth_token`, `refresh_token`, `credentials`,
# `secret`, ... regardless of dash/underscore/case differences.
_SECRET_KEY_SUBSTRINGS = (
    "password", "passwd",
    "secret",
    "token",
    "apikey",
    "auth",
    "credential",
    "privatekey",
    "sessionkey",
)


def _normalise_key(key: str) -> str:
    """Lower-case + drop every non-alphanumeric char, so dash vs underscore
    vs case can't be used to slip a key past the redaction heuristic."""
    return "".join(c for c in key.lower() if c.isalnum())

# Value-pattern heuristic — known secret prefixes. Catches API keys that
# leak via free-form string params even when the key name is innocuous.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^sk-(?:ant-)?[A-Za-z0-9_\-]{16,}"),          # OpenAI / Anthropic
    re.compile(r"^AIza[0-9A-Za-z_\-]{30,}"),                  # Google / Gemini
    re.compile(r"^xoxb-[A-Za-z0-9\-]{8,}"),                   # Slack bot tokens
    re.compile(r"^gh[ps]_[A-Za-z0-9]{36,}"),                  # GitHub personal/server
    re.compile(r"^AKIA[0-9A-Z]{16}$"),                        # AWS access key id
    re.compile(r"^ya29\.[A-Za-z0-9_\-]{50,}"),                # Google OAuth access token
    re.compile(r"^eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
)

REDACTED_PLACEHOLDER = "<REDACTED>"


def _key_looks_secret(key: str) -> bool:
    k = _normalise_key(key)
    return any(s in k for s in _SECRET_KEY_SUBSTRINGS)


def _value_looks_secret(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return any(p.match(value) for p in _SECRET_VALUE_PATTERNS)


def _redact_params(params: Any) -> Any:
    """Walk a params dict redacting suspected secret keys + values.

    Only one level deep — the Intent Object schema forbids nested objects
    in params (M2.2's nested-rejection test), so this is sufficient.
    """
    if not isinstance(params, dict):
        return params
    out: dict[str, Any] = {}
    for k, v in params.items():
        if _key_looks_secret(k) or _value_looks_secret(v):
            out[k] = REDACTED_PLACEHOLDER
        else:
            out[k] = v
    return out


# ── Entry builder ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AuditFields:
    """Typed argument carrier for make_entry().

    Using a dataclass instead of **kwargs so the canonical shape is visible
    at the call site and a missing field raises at construction, not at
    write time.
    """

    session_id: str
    turn_index: int
    intent_id: str
    action: str
    target: str
    tier: int
    reason: str
    risk_level: str
    outcome: Outcome
    duration_ms: float
    backend: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_estimate_usd: float
    user: Optional[str] = None  # auto-derived from os if not given
    extra: Optional[Mapping[str, Any]] = None  # additional fields (rejection_*, cow_intent_id, ...)


def _current_user() -> str:
    # Effective user, robust to no-TTY contexts (no os.getlogin) and
    # passwd-less environments.
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, ImportError, OSError):
        return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


def _iso_now_utc() -> str:
    # ISO-8601 with explicit Z for UTC. Drop microseconds to keep lines small.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_entry(fields: AuditFields) -> dict:
    """Build a canonical audit entry dict in REQUIRED_FIELDS order, with
    redacted params if present in fields.extra, and auto-stamped ts/user.

    The orchestration layer in M2.12 is the primary caller. Tests call this
    directly to construct deterministic fixtures.
    """
    entry: dict[str, Any] = {
        "ts": _iso_now_utc(),
        "session_id": fields.session_id,
        "turn_index": fields.turn_index,
        "intent_id": fields.intent_id,
        "action": fields.action,
        "target": fields.target,
        "tier": fields.tier,
        "reason": fields.reason,
        "risk_level": fields.risk_level,
        "outcome": fields.outcome.value if isinstance(fields.outcome, Outcome) else fields.outcome,
        "duration_ms": float(fields.duration_ms),
        "user": fields.user or _current_user(),
        "backend": fields.backend,
        "model": fields.model,
        "tokens_in": int(fields.tokens_in),
        "tokens_out": int(fields.tokens_out),
        "cost_estimate_usd": float(fields.cost_estimate_usd),
    }

    if fields.extra:
        # Redact params before merging.
        for k, v in fields.extra.items():
            if k == "params":
                entry[k] = _redact_params(v)
            else:
                entry[k] = v

    return entry


# ── AuditLog ───────────────────────────────────────────────────────────────

class AuditLog:
    """Append-only JSONL audit log.

    Path defaults to:
      $XDG_STATE_HOME/icebreaker/controller-audit.log  if XDG set
      ~/.local/state/icebreaker/controller-audit.log   otherwise

    File mode 0o640 (owner rw, group r). Parent dir 0o700 (owner only).
    O_APPEND | O_CREAT means concurrent writers (in this Controller or a
    later daemon mode) cannot interleave bytes within one os.write() call.

    Per-line os.fsync forces the kernel to flush both data + metadata to
    storage before write() returns. The cost is ~1 ms on SSD. Disable
    only in tests via the fsync_each_write=False kwarg.
    """

    FILE_MODE = 0o640
    PARENT_MODE = 0o700
    DEFAULT_FILENAME = "controller-audit.log"

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        fsync_each_write: bool = True,
    ):
        self._path = (Path(path).expanduser() if path else self._default_path()).resolve()
        self._fsync = fsync_each_write
        self._lock = threading.Lock()
        self._fd: int = -1
        self._open()

    # ── Path resolution ────────────────────────────────────────────────

    @classmethod
    def _default_path(cls) -> Path:
        xdg = os.environ.get("XDG_STATE_HOME")
        if xdg:
            return Path(xdg) / "icebreaker" / cls.DEFAULT_FILENAME
        return Path.home() / ".local" / "state" / "icebreaker" / cls.DEFAULT_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    # ── Lifecycle ──────────────────────────────────────────────────────

    def _open(self) -> None:
        # mkdir is idempotent; we set mode only on creation. If the parent
        # exists with looser perms we don't tighten — that's an admin call.
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=self.PARENT_MODE)
        existed_before = self._path.exists()
        # O_APPEND | O_CREAT — file mode applied only on creation.
        self._fd = os.open(
            str(self._path),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            self.FILE_MODE,
        )
        if not existed_before:
            # Set explicit mode in case umask masked it on creation.
            os.chmod(self._path, self.FILE_MODE)

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        # Safety net for leaks; explicit close() is preferred.
        try:
            if self._fd >= 0:
                os.close(self._fd)
        except Exception:  # noqa: BLE001 — destructor must not raise
            pass

    # ── Writes ─────────────────────────────────────────────────────────

    def write(self, entry: Mapping[str, Any]) -> None:
        """Append one JSON-encoded line. Required fields must all be present.

        The caller is expected to use make_entry() to construct entry; this
        method enforces only the structural floor (all REQUIRED_FIELDS set).
        Free-form additional fields (rejection_*, cow_intent_id, etc.) are
        passed through unchanged.

        Raises:
            ValueError: a required field is missing.
            OSError: the underlying write failed (disk full, fd closed).
        """
        missing = [f for f in REQUIRED_FIELDS if f not in entry]
        if missing:
            raise ValueError(f"audit entry missing required field(s): {missing}")

        # Serialise once; the os.write below is atomic for our line sizes.
        line = json.dumps(entry, separators=(",", ":"), default=str) + "\n"
        encoded = line.encode("utf-8")

        with self._lock:
            if self._fd < 0:
                raise OSError("AuditLog is closed")
            os.write(self._fd, encoded)
            if self._fsync:
                os.fsync(self._fd)

    def write_fields(self, fields: AuditFields) -> None:
        """Convenience: build the entry then write it. Equivalent to
        ``log.write(make_entry(fields))``."""
        self.write(make_entry(fields))


# ── Module-level convenience ───────────────────────────────────────────────

_default: Optional[AuditLog] = None


def get_default() -> AuditLog:
    """Return a process-wide AuditLog singleton at the default path.

    Tests should create their own AuditLog(path=tmp_path) instead of
    calling this — the singleton would otherwise leak across tests.
    """
    global _default
    if _default is None:
        _default = AuditLog()
    return _default


def write_default(entry: Mapping[str, Any]) -> None:
    get_default().write(entry)
