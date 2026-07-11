"""Phase 6 Scope C — verification-result cache with 7-day TTL.

Verifying every preset on every Models-page open would burn provider
metadata quota unnecessarily. Never re-verifying would silently miss
provider deprecations. Sweet spot from the Scope C research: cache
each `verify_preset()` result for 7 days, with hard invalidation on:

  * API key change for that provider (detected via a hash of the key
    stored alongside the result — key rotation = re-verify),
  * Explicit "Refresh" from the user (delete + re-run),
  * SDK-version bump (stored as `sdk_version` per entry so a
    google-genai upgrade forces a fresh sweep — provider behavior
    may have shifted),
  * Catalogue version bump (a new schema means old results are
    ambiguous).

Location per XDG spec: this is user-influencing state (drives UI
badges), not disposable cache, so it lives under `$XDG_STATE_HOME`,
not `$XDG_CACHE_HOME`. Rationale: on a `rm -rf ~/.cache/*` the badges
should NOT silently reset — that would look like a bug.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib as _toml_reader  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as _toml_reader  # type: ignore

try:
    import tomli_w as _toml_writer
except ImportError:  # pragma: no cover
    _toml_writer = None  # type: ignore

from .preset_verifier import VerifyResult, VerifyStatus


log = logging.getLogger(__name__)


_DEFAULT_TTL_DAYS = 7
_CATALOGUE_VERSION_SENTINEL = "2"
"""Bump this when the catalogue's schema_version bumps. Old cache
entries with a mismatched `catalogue_version` are treated as absent."""


# ── Path resolution ──────────────────────────────────────────────────────


def _state_home() -> Path:
    """Return `$XDG_STATE_HOME` or the XDG-spec fallback
    `~/.local/state`. Never creates the directory — that's the
    caller's responsibility on write."""
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg)
    return Path.home() / ".local" / "state"


def default_cache_path() -> Path:
    """`$XDG_STATE_HOME/icebreaker/preset_verification.toml`."""
    return _state_home() / "icebreaker" / "preset_verification.toml"


# ── Data ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CachedResult:
    """One entry in the cache. Deliberately mirrors VerifyResult so
    the cache can be swapped in transparently for a live call."""
    provider: str
    preset_id: str
    status: VerifyStatus
    detail: str
    http_code: int | None
    verified_at: datetime            # when we last checked
    api_key_hash: str                # to detect key rotation
    catalogue_version: str

    def is_fresh(self, ttl_days: int = _DEFAULT_TTL_DAYS) -> bool:
        """True while the result is within its TTL window. Uses
        `datetime.now(timezone.utc)` — the cache stores UTC timestamps
        so a laptop suspended across time zones doesn't shift freshness."""
        age = datetime.now(timezone.utc) - self.verified_at
        return age <= timedelta(days=ttl_days)

    def matches_key(self, api_key: str | None) -> bool:
        """True when the cached entry was verified against the same
        API key the caller has now. Any change invalidates."""
        return self.api_key_hash == _hash_key(api_key)

    def to_verify_result(self) -> VerifyResult:
        return VerifyResult(
            provider=self.provider,
            preset_id=self.preset_id,
            status=self.status,
            detail=self.detail,
            http_code=self.http_code,
        )


def _hash_key(api_key: str | None) -> str:
    """Hash the API key so rotations invalidate cache entries without
    ever writing the raw secret to disk (BP-8 secret hygiene). Empty
    key hashes to a stable sentinel so NO_KEY entries also invalidate
    once a key is configured."""
    if api_key is None or not api_key.strip():
        return "no-key"
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


# ── Load ─────────────────────────────────────────────────────────────────


def load_cache(path: Path | None = None) -> dict[tuple[str, str], CachedResult]:
    """Return the parsed cache as `{(provider, preset_id) -> CachedResult}`.

    Missing / corrupt cache file returns an empty dict — cache
    corruption should never break the Models page. F-53 rules apply:
    log the reason so a systemic parse error is diagnosable in
    journalctl (never silently swallow).
    """
    resolved = path if path is not None else default_cache_path()
    if not resolved.exists():
        return {}
    try:
        with resolved.open("rb") as f:
            raw = _toml_reader.load(f)
    except Exception as exc:
        log.warning(
            "preset cache at %s unreadable — treating as empty: %s: %s",
            resolved, type(exc).__name__, exc,
        )
        return {}
    entries = raw.get("entry", [])
    if not isinstance(entries, list):
        log.warning(
            "preset cache at %s malformed (`entry` not an array) — treating as empty",
            resolved,
        )
        return {}
    out: dict[tuple[str, str], CachedResult] = {}
    for row in entries:
        if not isinstance(row, dict):
            continue
        try:
            provider = str(row["provider"])
            preset_id = str(row["preset_id"])
            status = VerifyStatus(str(row["status"]))
            detail = str(row.get("detail", ""))
            http_raw = row.get("http_code")
            http_code = int(http_raw) if isinstance(http_raw, (int, float)) else None
            verified_at = _parse_utc(row["verified_at"])
            api_key_hash = str(row.get("api_key_hash", "no-key"))
            catalogue_version = str(row.get("catalogue_version", ""))
        except (KeyError, ValueError, TypeError) as exc:
            # One bad row must not poison the rest of the cache.
            log.warning(
                "preset cache: skipped malformed entry (%s: %s)",
                type(exc).__name__, exc,
            )
            continue
        if catalogue_version != _CATALOGUE_VERSION_SENTINEL:
            # Catalogue schema changed — treat this entry as stale.
            continue
        out[(provider, preset_id)] = CachedResult(
            provider=provider,
            preset_id=preset_id,
            status=status,
            detail=detail,
            http_code=http_code,
            verified_at=verified_at,
            api_key_hash=api_key_hash,
            catalogue_version=catalogue_version,
        )
    return out


def _parse_utc(value: Any) -> datetime:
    """Accept either a TOML datetime (which tomllib returns as
    `datetime`) or an ISO 8601 string (defensive — some legacy caches
    round-trip through JSON). Always returns a tz-aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        # Handles "2026-07-11T12:34:56+00:00" and "2026-07-11T12:34:56Z".
        raw = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise ValueError(f"unsupported verified_at value: {value!r}")


# ── Save ─────────────────────────────────────────────────────────────────


def save_cache(
    cache: dict[tuple[str, str], CachedResult],
    path: Path | None = None,
) -> None:
    """Atomically overwrite the cache file. Never partial-writes —
    a crash mid-flush produces either the old file or the new file,
    never a truncated one.

    Silently skips if `tomli_w` isn't available (dev environments
    without the extra dep) — a missing writer is not a crash reason,
    just a lost cache write.
    """
    if _toml_writer is None:
        log.debug(
            "tomli_w not installed — skipping preset cache save "
            "(cache will re-verify on next run)"
        )
        return
    resolved = path if path is not None else default_cache_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, list[dict[str, Any]]] = {"entry": []}
    for (provider, preset_id), entry in cache.items():
        row: dict[str, Any] = {
            "provider": entry.provider,
            "preset_id": entry.preset_id,
            "status": entry.status.value,
            "detail": entry.detail,
            "verified_at": entry.verified_at,
            "api_key_hash": entry.api_key_hash,
            "catalogue_version": entry.catalogue_version,
        }
        if entry.http_code is not None:
            row["http_code"] = int(entry.http_code)
        doc["entry"].append(row)
    # Atomic write via tempfile in the same dir → os.replace.
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=resolved.parent,
        prefix=".preset_cache-", suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(_toml_writer.dumps(doc).encode("utf-8"))
        tmp_path = Path(tmp.name)
    try:
        os.replace(tmp_path, resolved)
    except Exception as exc:  # noqa: BLE001
        # F-53 Scope A pattern: cleanup the tempfile so a partial write
        # can't linger. The outer `raise` propagates the ORIGINAL
        # os.replace failure (e.g. disk full, permission) to the
        # caller so the badge worker logs a warning; unlink errors
        # inside cleanup are tolerated (the tmpfile is best-effort).
        try:
            tmp_path.unlink()
        except OSError:  # noqa: BLE001
            pass
        log.warning(
            "preset cache atomic replace failed: %s: %s",
            type(exc).__name__, exc,
        )
        raise


# ── Convenience ──────────────────────────────────────────────────────────


def record_result(
    cache: dict[tuple[str, str], CachedResult],
    result: VerifyResult,
    api_key: str | None,
) -> None:
    """Update the in-memory cache with a fresh result. Caller flushes
    via save_cache() when the sweep is done — batching avoids a
    per-preset atomic-write cost."""
    cache[(result.provider, result.preset_id)] = CachedResult(
        provider=result.provider,
        preset_id=result.preset_id,
        status=result.status,
        detail=result.detail,
        http_code=result.http_code,
        verified_at=datetime.now(timezone.utc),
        api_key_hash=_hash_key(api_key),
        catalogue_version=_CATALOGUE_VERSION_SENTINEL,
    )


def lookup(
    cache: dict[tuple[str, str], CachedResult],
    provider: str,
    preset_id: str,
    api_key: str | None,
    *,
    ttl_days: int = _DEFAULT_TTL_DAYS,
) -> CachedResult | None:
    """Return a fresh cache hit for `(provider, preset_id)` if one
    exists and matches the current key. Otherwise None — the caller
    should call `verify_preset()` live and record the result."""
    entry = cache.get((provider, preset_id))
    if entry is None:
        return None
    if not entry.is_fresh(ttl_days=ttl_days):
        return None
    if not entry.matches_key(api_key):
        return None
    return entry
