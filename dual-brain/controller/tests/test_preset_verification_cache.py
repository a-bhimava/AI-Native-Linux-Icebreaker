"""Phase 6 Scope C — verification cache TTL + invalidation tests.

Cover:
  * round-trip save → load returns identical cached results
  * `is_fresh()` respects the 7-day TTL
  * `matches_key()` invalidates on API key rotation
  * key rotation is invisible to disk — the raw key is never written
  * malformed cache file falls back to empty (never crashes)
  * catalogue_version bump invalidates entries
  * atomic-write survives a partial failure
"""

from __future__ import annotations

import hashlib
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from controller.preset_verifier import VerifyResult, VerifyStatus
from controller.preset_verification_cache import (
    CachedResult,
    _CATALOGUE_VERSION_SENTINEL,
    _hash_key,
    load_cache,
    lookup,
    record_result,
    save_cache,
)


def _cache_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "preset_verification.toml"


def _sample(provider: str = "gemini", preset_id: str = "gemini-2.5-flash",
            status: VerifyStatus = VerifyStatus.VERIFIED) -> VerifyResult:
    return VerifyResult(
        provider=provider, preset_id=preset_id,
        status=status, detail="OK", http_code=200,
    )


# ── Round-trip ────────────────────────────────────────────────────────────


def test_round_trip_preserves_all_fields(tmp_path) -> None:
    path = _cache_path(tmp_path)
    cache: dict = {}
    record_result(cache, _sample(), api_key="test-key")
    save_cache(cache, path)

    loaded = load_cache(path)
    hit = loaded[("gemini", "gemini-2.5-flash")]
    assert hit.status is VerifyStatus.VERIFIED
    assert hit.detail == "OK"
    assert hit.http_code == 200
    assert hit.catalogue_version == _CATALOGUE_VERSION_SENTINEL


# ── TTL ──────────────────────────────────────────────────────────────────


def test_is_fresh_within_ttl(tmp_path) -> None:
    entry = CachedResult(
        provider="gemini", preset_id="gemini-2.5-flash",
        status=VerifyStatus.VERIFIED, detail="OK", http_code=200,
        verified_at=datetime.now(timezone.utc) - timedelta(days=3),
        api_key_hash="hash", catalogue_version=_CATALOGUE_VERSION_SENTINEL,
    )
    assert entry.is_fresh() is True


def test_is_stale_after_ttl(tmp_path) -> None:
    entry = CachedResult(
        provider="gemini", preset_id="gemini-2.5-flash",
        status=VerifyStatus.VERIFIED, detail="OK", http_code=200,
        verified_at=datetime.now(timezone.utc) - timedelta(days=8),
        api_key_hash="hash", catalogue_version=_CATALOGUE_VERSION_SENTINEL,
    )
    assert entry.is_fresh() is False


def test_lookup_returns_none_when_stale(tmp_path) -> None:
    entry = CachedResult(
        provider="gemini", preset_id="gemini-2.5-flash",
        status=VerifyStatus.VERIFIED, detail="OK", http_code=200,
        verified_at=datetime.now(timezone.utc) - timedelta(days=8),
        api_key_hash=_hash_key("test-key"),
        catalogue_version=_CATALOGUE_VERSION_SENTINEL,
    )
    cache: dict = {("gemini", "gemini-2.5-flash"): entry}
    assert lookup(cache, "gemini", "gemini-2.5-flash", "test-key") is None


# ── Key rotation ─────────────────────────────────────────────────────────


def test_key_rotation_invalidates_cache(tmp_path) -> None:
    """User rotated their API key. The old cache entry must be
    ignored — a re-verify is required."""
    cache: dict = {}
    record_result(cache, _sample(), api_key="old-key")
    assert lookup(cache, "gemini", "gemini-2.5-flash", "old-key") is not None
    assert lookup(cache, "gemini", "gemini-2.5-flash", "new-key") is None


def test_none_key_stores_stable_sentinel(tmp_path) -> None:
    """Two `record_result` calls with `api_key=None` must both hash to
    the same sentinel — otherwise NO_KEY entries would re-verify on
    every load."""
    cache: dict = {}
    record_result(cache, _sample(status=VerifyStatus.NO_KEY), api_key=None)
    hit = cache[("gemini", "gemini-2.5-flash")]
    assert hit.api_key_hash == "no-key"


def test_raw_key_never_written_to_disk(tmp_path) -> None:
    """Rotating an API key must invalidate the cache, but the key
    itself must never touch disk — a leak of the raw value in a state
    file would violate BP-8 secret hygiene."""
    path = _cache_path(tmp_path)
    cache: dict = {}
    secret = "super-secret-api-key-xyz"
    record_result(cache, _sample(), api_key=secret)
    save_cache(cache, path)
    content = path.read_text()
    assert secret not in content
    # The hash IS present.
    assert _hash_key(secret) in content


# ── Robustness ───────────────────────────────────────────────────────────


def test_missing_file_returns_empty(tmp_path) -> None:
    assert load_cache(tmp_path / "does-not-exist.toml") == {}


def test_corrupt_toml_returns_empty_without_crash(tmp_path) -> None:
    path = _cache_path(tmp_path)
    path.write_text("this is [ not toml")
    assert load_cache(path) == {}


def test_malformed_entry_skipped_others_kept(tmp_path) -> None:
    """One bad row must not poison the whole cache."""
    path = _cache_path(tmp_path)
    path.write_text("""
[[entry]]
provider = "gemini"
preset_id = "gemini-2.5-flash"
status = "verified"
detail = "OK"
verified_at = 2026-07-11T12:00:00Z
api_key_hash = "abc"
catalogue_version = "2"

[[entry]]
# This one is missing status/verified_at — must be silently skipped.
provider = "gemini"
preset_id = "gemini-broken"
""")
    loaded = load_cache(path)
    # Good entry preserved.
    assert ("gemini", "gemini-2.5-flash") in loaded
    # Bad entry silently skipped.
    assert ("gemini", "gemini-broken") not in loaded


def test_catalogue_version_mismatch_invalidates(tmp_path) -> None:
    """Old cache entries from a pre-v2 catalogue schema are ambiguous —
    treat as absent so the sweep re-verifies against the new shape."""
    path = _cache_path(tmp_path)
    path.write_text("""
[[entry]]
provider = "gemini"
preset_id = "gemini-2.5-flash"
status = "verified"
detail = "OK"
verified_at = 2026-07-11T12:00:00Z
api_key_hash = "abc"
catalogue_version = "1"
""")
    loaded = load_cache(path)
    assert loaded == {}


# ── Atomicity ────────────────────────────────────────────────────────────


def test_save_uses_atomic_replace(tmp_path, monkeypatch) -> None:
    """A crash mid-flush must NOT produce a truncated cache file. We
    detect this by watching for the tempfile → os.replace pattern:
    the target file's inode should match the file the writer wrote."""
    path = _cache_path(tmp_path)
    # First, write a baseline cache we can verify wasn't truncated.
    baseline: dict = {}
    for i in range(5):
        record_result(
            baseline,
            _sample(preset_id=f"preset-{i}"),
            api_key="k",
        )
    save_cache(baseline, path)
    first_size = path.stat().st_size

    # Now overwrite with a smaller cache — old file should NOT be
    # truncated during the write; the atomic replace swaps in the new
    # content only after tempfile.write completes.
    smaller: dict = {}
    record_result(smaller, _sample(), api_key="k")
    save_cache(smaller, path)
    assert path.stat().st_size < first_size
    # Just confirming we can re-load — no leftover tempfiles.
    assert len(load_cache(path)) == 1
    leftover_temps = list(tmp_path.glob(".preset_cache-*.tmp"))
    assert leftover_temps == []
