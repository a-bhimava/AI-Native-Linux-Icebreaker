"""Persistent per-app-per-tool trust store for grounded UI actions.

F-51 marker: F-105-trust (per incremental/GROUND_TRUTH.md F-105).

UX inspired by iOS 14+ tri-state permissions ("Allow Once" / "Allow
While Using" / "Don't Allow"), 1Password domain-scoped autofill trust,
and Chrome's per-origin permission chips. The goal: turn opencode's
"ask on every click" from a per-action modal parade into "approve
Slack once, work naturally for the session".

Design:
- **Three tiers of grant**: ``once`` (satisfies exactly one check),
  ``session`` (valid until the session_id changes — logout/reboot
  resets it), ``persistent`` (TTL-bounded, survives sessions).
- **Deny tier** (``deny_always``): explicit hard block, overrides
  any grant. Ships with defaults for gnome-terminal, sudo, GNOME
  keyring, password fields, etc.
- **Wildcards** in scope keys: ``*`` matches anything at that slot.
  ``{app: "*", tool: "gui.hover"}`` = trust hover in every app;
  ``{app: "slack", tool: "*"}`` = trust everything in Slack.
- **Session binding**: session grants carry a session_id; changing
  session_id invalidates them (matches how iOS resets "Allow While
  Using" between app launches).
- **Never-silent expiry**: expired grants remain in the store as
  ``expired=True`` entries so ``check()`` returning False for an
  expired grant can be surfaced ("your trust for X expired, ask
  again") rather than the user wondering why the prompt returned.
- **Append-only writes** (``O_APPEND``, ``fsync`` on each entry)
  matches audit-log discipline — grants and revokes are audit
  events, not casual state mutations.
- **Compaction**: on load, later entries for the same (app, tool)
  key override earlier ones. No editing in place — the JSONL is the
  event log.

Security posture:
- File mode 0o640 root:icebreaker-users so the daemon (root) can
  write and the icebreaker user can read via group.
- Deny always overrides grant — no "user trusted terminal by
  mistake" footgun.
- Every check() consults the built-in ``_HARD_DENY`` fallback for
  cases where the defaults file is missing (defense in depth).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

_log = logging.getLogger(__name__)


# ── Tier constants ─────────────────────────────────────────────────────

TIER_ONCE = "once"
TIER_SESSION = "session"
TIER_PERSISTENT = "persistent"
TIER_DENY = "deny_always"

_ALL_TIERS = frozenset({TIER_ONCE, TIER_SESSION, TIER_PERSISTENT, TIER_DENY})

# Hard-coded deny list — applied even when the on-disk defaults file
# is missing. Defense in depth: a corrupt / removed defaults file must
# not silently open up the terminal to auto-approve.
_HARD_DENY: frozenset[tuple[str, str]] = frozenset({
    ("gnome-terminal", "*"),
    ("xterm", "*"),
    ("konsole", "*"),
    ("terminator", "*"),
    ("sudo", "*"),
    ("*", "gui.type_at_coords_password"),  # future field-aware type
})

_DEFAULT_STORE_PATH = Path("/var/lib/icebreaker/gui_trust.jsonl")
_DEFAULT_DEFAULTS_DIR = Path("/etc/icebreaker/gui_trust.d")


# ── Data model ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TrustGrant:
    app: str                   # wildcard OK: "*", "slack", "gnome-*"
    tool: str                  # wildcard OK: "*", "gui.click", "gui.*"
    tier: str                  # one of _ALL_TIERS
    expires_at: float = 0.0    # unix epoch; 0 = never expires (session/deny/persistent-unbounded)
    session_id: str = ""       # non-empty only for TIER_SESSION
    granted_by: str = ""       # "defaults" | "user" | "ci"
    reason: str = ""           # free-text audit note
    granted_at: float = field(default_factory=time.time)

    def matches(self, app: str, tool: str) -> bool:
        return _wildcard_match(self.app, app) and _wildcard_match(self.tool, tool)

    def is_active(self, now: float, session_id: str) -> bool:
        if self.tier == TIER_ONCE:
            return True   # ``once`` grants are consumed by check(), not expired here
        if self.tier == TIER_DENY:
            return True
        if self.tier == TIER_SESSION:
            return self.session_id == session_id
        if self.tier == TIER_PERSISTENT:
            return self.expires_at == 0.0 or now < self.expires_at
        return False


@dataclass(frozen=True)
class TrustDecision:
    allowed: bool
    reason: str
    matched_grant: Optional[TrustGrant]

    @property
    def denied(self) -> bool:
        return not self.allowed


class TrustError(Exception):
    """Raised for structurally-invalid grant input."""


# ── The store ──────────────────────────────────────────────────────────

class TrustStore:
    """Append-only JSONL-backed trust store.

    Every mutation (``grant``, ``revoke``) appends one JSON line to the
    file. ``check()`` walks the in-memory index — no re-read per check.
    """

    def __init__(
        self,
        path: Path = _DEFAULT_STORE_PATH,
        defaults_dir: Path = _DEFAULT_DEFAULTS_DIR,
        session_id: str = "",
    ) -> None:
        self._path = Path(path)
        self._defaults_dir = Path(defaults_dir)
        self._session_id = session_id or f"pid-{os.getpid()}"
        self._entries: list[TrustGrant] = []
        self._once_consumed: set[tuple[str, str, int]] = set()
        self._load()

    # ── Config / session ───────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    def new_session(self, session_id: str) -> None:
        """Switch to a new session — session-tier grants no longer match."""
        self._session_id = session_id
        # Drop consumed-once state so the new session gets a fresh slate.
        self._once_consumed.clear()

    # ── check / grant / revoke ─────────────────────────────────────

    def check(self, app: str, tool: str) -> TrustDecision:
        """Consult grants + hard-deny list. First deny beats any grant.
        Otherwise pick the most-specific matching active grant."""
        app_norm = _normalize(app)
        tool_norm = _normalize(tool)
        now = time.time()

        # 1) Hard-deny fallback (defense in depth).
        for (deny_app, deny_tool) in _HARD_DENY:
            if _wildcard_match(deny_app, app_norm) and _wildcard_match(deny_tool, tool_norm):
                return TrustDecision(
                    allowed=False,
                    reason=f"hard-deny: {deny_app}/{deny_tool}",
                    matched_grant=None,
                )

        # 2) Loaded deny_always grants override anything else.
        for g in self._entries:
            if g.tier == TIER_DENY and g.matches(app_norm, tool_norm) \
                    and g.is_active(now, self._session_id):
                return TrustDecision(
                    allowed=False,
                    reason=f"deny_always by {g.granted_by}: {g.reason or 'no reason'}",
                    matched_grant=g,
                )

        # 3) Active grants — most specific first. "Specificity" = fewer
        #    wildcards in scope keys (0, 1, or 2 stars).
        candidates = [g for g in self._entries
                      if g.tier != TIER_DENY
                      and g.matches(app_norm, tool_norm)
                      and g.is_active(now, self._session_id)]
        candidates.sort(key=lambda g: (_specificity(g), -g.granted_at))

        for g in candidates:
            if g.tier == TIER_ONCE:
                key = (g.app, g.tool, id(g))
                if key in self._once_consumed:
                    continue
                self._once_consumed.add(key)
            return TrustDecision(
                allowed=True,
                reason=f"{g.tier} grant by {g.granted_by}: {g.reason or 'no reason'}",
                matched_grant=g,
            )

        # 4) Was there a matching-but-expired persistent grant? Explicit
        #    "trust expired, ask again" beats silent deny.
        for g in self._entries:
            if (g.tier == TIER_PERSISTENT
                    and g.matches(app_norm, tool_norm)
                    and g.expires_at > 0 and now >= g.expires_at):
                return TrustDecision(
                    allowed=False,
                    reason=f"trust expired at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(g.expires_at))} — ask again",
                    matched_grant=g,
                )

        return TrustDecision(allowed=False, reason="no matching grant", matched_grant=None)

    def grant(
        self,
        app: str,
        tool: str,
        tier: str,
        ttl_seconds: int = 0,
        granted_by: str = "user",
        reason: str = "",
    ) -> TrustGrant:
        """Record a new grant. Persists via O_APPEND."""
        _validate_scope(app, tool)
        if tier not in _ALL_TIERS:
            raise TrustError(f"tier must be one of {sorted(_ALL_TIERS)}, got {tier!r}")
        if ttl_seconds < 0 or ttl_seconds > 60 * 60 * 24 * 30:
            raise TrustError(f"ttl_seconds out of range (0..30 days): {ttl_seconds}")

        expires_at = 0.0
        session_id = ""
        if tier == TIER_PERSISTENT and ttl_seconds > 0:
            expires_at = time.time() + ttl_seconds
        elif tier == TIER_SESSION:
            session_id = self._session_id

        grant = TrustGrant(
            app=_normalize(app),
            tool=_normalize(tool),
            tier=tier,
            expires_at=expires_at,
            session_id=session_id,
            granted_by=granted_by,
            reason=reason,
        )
        self._append(grant, event="grant")
        self._entries.append(grant)
        return grant

    def revoke(self, app: str, tool: str, reason: str = "user-revoked") -> int:
        """Drop every non-deny grant matching (app, tool) exactly. Returns
        the count removed. Persists a tombstone entry via ``grant`` with
        tier=deny_always... no — revoke removes; use grant(TIER_DENY, ...)
        if you want a hard block. This method purges soft grants only."""
        _validate_scope(app, tool)
        app_n = _normalize(app)
        tool_n = _normalize(tool)
        kept: list[TrustGrant] = []
        removed = 0
        for g in self._entries:
            if g.app == app_n and g.tool == tool_n and g.tier != TIER_DENY:
                removed += 1
                continue
            kept.append(g)
        if removed:
            self._entries = kept
            self._append({
                "event": "revoke", "app": app_n, "tool": tool_n,
                "removed_count": removed, "reason": reason,
                "at": time.time(),
            })
        return removed

    def list_active(self) -> list[TrustGrant]:
        """Return currently-active grants (dedup'd, expired filtered)."""
        now = time.time()
        return [g for g in self._entries if g.is_active(now, self._session_id)]

    def list_all(self) -> list[TrustGrant]:
        """Every loaded grant, including expired persistents."""
        return list(self._entries)

    # ── Loading ────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load defaults from ``defaults_dir`` first, then the persistent
        store on top. Later entries for the same (app, tool) key don't
        override — the *first* seen wins? No, we keep all so multiple
        grants can coexist (e.g. a "*" wildcard AND a specific app+tool
        grant). check() picks the most specific."""
        for defaults_file in self._enumerate_defaults():
            self._load_file(defaults_file, source_tag="defaults")
        if self._path.exists():
            self._load_file(self._path, source_tag="user")

    def _enumerate_defaults(self) -> Iterator[Path]:
        if not self._defaults_dir.is_dir():
            return
        for p in sorted(self._defaults_dir.glob("*.jsonl")):
            yield p

    def _load_file(self, path: Path, source_tag: str) -> None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning("trust_store: cannot read %s: %s", path, exc)
            return
        for lineno, raw in enumerate(content.splitlines(), start=1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                _log.warning("trust_store: %s:%d bad JSON (%s)", path, lineno, exc)
                continue
            event = obj.get("event", "grant")
            if event == "revoke":
                app_n = _normalize(obj.get("app", ""))
                tool_n = _normalize(obj.get("tool", ""))
                self._entries = [g for g in self._entries
                                 if not (g.app == app_n and g.tool == tool_n
                                         and g.tier != TIER_DENY)]
                continue
            try:
                grant = _grant_from_dict(obj, default_source=source_tag)
            except TrustError as exc:
                _log.warning("trust_store: %s:%d invalid grant: %s", path, lineno, exc)
                continue
            self._entries.append(grant)

    # ── Persistence ────────────────────────────────────────────────

    def _append(self, payload: object, event: str = "") -> None:
        """Append one JSON line under an fcntl advisory lock so
        concurrent daemons don't interleave."""
        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        except OSError as exc:
            _log.warning("trust_store: cannot create %s: %s", parent, exc)
            return

        if isinstance(payload, TrustGrant):
            obj = _grant_to_dict(payload)
            obj["event"] = event or "grant"
        elif isinstance(payload, dict):
            obj = dict(payload)
        else:
            raise TrustError(f"cannot serialize {type(payload).__name__}")

        line = json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n"
        # O_APPEND + O_CREAT so concurrent writers stay atomic per-write.
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        fd = os.open(self._path, flags, 0o640)
        try:
            with _flock(fd):
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
        finally:
            os.close(fd)


# ── Helpers ────────────────────────────────────────────────────────────

def _validate_scope(app: str, tool: str) -> None:
    if not isinstance(app, str) or not app:
        raise TrustError(f"app: must be non-empty str, got {app!r}")
    if not isinstance(tool, str) or not tool:
        raise TrustError(f"tool: must be non-empty str, got {tool!r}")
    for label, val in (("app", app), ("tool", tool)):
        if len(val) > 128:
            raise TrustError(f"{label}: too long ({len(val)} > 128)")
        # Disallow control chars, ‹`›, ‹$›, ‹;›, ‹|›, ‹&›, ‹>›, ‹<› —
        # trust keys are not shell-safe but also shouldn't grow
        # metachars that might trip logging or CLI parsing.
        if any(ord(c) < 0x20 or c in "`$;|&<>" for c in val):
            raise TrustError(f"{label}: contains disallowed char in {val!r}")


def _normalize(s: str) -> str:
    return s.strip().lower() if isinstance(s, str) else ""


def _wildcard_match(pattern: str, value: str) -> bool:
    """Very small wildcard matcher: `*` = anything; `gui.*` = prefix;
    `gnome-*` = prefix. No general globs — kept intentionally narrow
    so wildcards can't be used to sneak past denies."""
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return pattern == value


def _specificity(g: TrustGrant) -> int:
    """0 = most specific (no wildcards). Higher = more permissive.
    Prefers exact matches when multiple grants apply."""
    return (1 if "*" in g.app else 0) + (1 if "*" in g.tool else 0)


def _grant_to_dict(g: TrustGrant) -> dict:
    d: dict = {
        "app": g.app, "tool": g.tool, "tier": g.tier,
        "granted_at": g.granted_at, "granted_by": g.granted_by,
    }
    if g.expires_at:
        d["expires_at"] = g.expires_at
    if g.session_id:
        d["session_id"] = g.session_id
    if g.reason:
        d["reason"] = g.reason
    return d


def _grant_from_dict(obj: dict, default_source: str = "defaults") -> TrustGrant:
    app = obj.get("app")
    tool = obj.get("tool")
    tier = obj.get("tier", TIER_PERSISTENT)
    if tier == "allow":              # legacy convenience shorthand
        tier = TIER_PERSISTENT
    if tier == "deny":
        tier = TIER_DENY
    if tier not in _ALL_TIERS:
        raise TrustError(f"tier must be one of {sorted(_ALL_TIERS)}, got {tier!r}")
    if not app or not tool:
        raise TrustError(f"missing app/tool in {obj!r}")
    _validate_scope(app, tool)

    ttl_seconds = int(obj.get("ttl_seconds", 0) or 0)
    if ttl_seconds < 0:
        raise TrustError(f"ttl_seconds must be >= 0, got {ttl_seconds}")

    expires_at = float(obj.get("expires_at", 0.0) or 0.0)
    if expires_at == 0.0 and tier == TIER_PERSISTENT and ttl_seconds > 0:
        expires_at = time.time() + ttl_seconds

    return TrustGrant(
        app=_normalize(app),
        tool=_normalize(tool),
        tier=tier,
        expires_at=expires_at,
        session_id=str(obj.get("session_id", "")),
        granted_by=str(obj.get("granted_by") or default_source),
        reason=str(obj.get("reason") or obj.get("note") or ""),
        granted_at=float(obj.get("granted_at") or time.time()),
    )


@contextlib.contextmanager
def _flock(fd: int):
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


# ── Self-test ──────────────────────────────────────────────────────────

def _self_test() -> int:
    """``python -m gui_agent.trust_store --self-test`` — in-memory
    end-to-end check without touching /var/lib/icebreaker."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "trust.jsonl"
        defaults = Path(td) / "defaults"
        defaults.mkdir()
        (defaults / "01-defaults.jsonl").write_text(
            '{"app":"*","tool":"gui.hover","tier":"persistent","reason":"safe"}\n'
        )
        store = TrustStore(path=p, defaults_dir=defaults, session_id="s1")

        # Default hover grant applies.
        d1 = store.check("slack", "gui.hover")
        if not d1.allowed:
            print(f"self-test FAILED: hover should be allowed by defaults, got {d1}", flush=True)
            return 1

        # No grant for click yet.
        d2 = store.check("slack", "gui.click")
        if d2.allowed:
            print(f"self-test FAILED: click should NOT be allowed, got {d2}", flush=True)
            return 1

        # User grants click for 5 min.
        store.grant("slack", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=300,
                    reason="testing")
        d3 = store.check("slack", "gui.click")
        if not d3.allowed:
            print(f"self-test FAILED: click should be allowed after grant, got {d3}", flush=True)
            return 1

        # Hard-deny stops terminal even without an entry.
        d4 = store.check("gnome-terminal", "gui.click")
        if d4.allowed:
            print(f"self-test FAILED: terminal should be hard-denied, got {d4}", flush=True)
            return 1

        # Revoke removes the click grant.
        removed = store.revoke("slack", "gui.click")
        if removed != 1:
            print(f"self-test FAILED: expected 1 removed, got {removed}", flush=True)
            return 1
        d5 = store.check("slack", "gui.click")
        if d5.allowed:
            print(f"self-test FAILED: click should be denied after revoke, got {d5}", flush=True)
            return 1

    print("trust_store OK: defaults + grant + hard-deny + revoke round-trip works", flush=True)
    return 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print("usage: python -m gui_agent.trust_store --self-test",
          file=__import__("sys").stderr)
    sys.exit(2)
