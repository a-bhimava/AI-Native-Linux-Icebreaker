"""Phase 6 Scope D/E — debug_log module regression tests.

Cover:
  * Off by default — record() is a no-op until configure(enabled=True).
  * XDG_STATE_HOME override.
  * Env-var log path override.
  * JSONL round-trip: every record parses back as JSON.
  * Secret redaction: `api_key`, `token`, etc. never land on disk.
  * Size cap + rotation.
  * Concurrent writes don't interleave (lock guarantees).
  * clear() truncates without deleting the file.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from controller import debug_log


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Reset module-level state around each test — the debug_log holds
    process-global state (`_enabled`, `_log_path`) so tests can leak
    into each other otherwise."""
    monkeypatch.delenv("ICEBREAKER_DEBUG_LOG_PATH", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    debug_log.configure(enabled=False)
    yield
    debug_log.configure(enabled=False)


# ── Off by default ───────────────────────────────────────────────────────


def test_off_by_default_record_is_noop(tmp_path) -> None:
    """configure() never called → is_enabled() False → record()
    writes nothing. BP-2 default-safe."""
    assert debug_log.is_enabled() is False
    # No log path resolved, so record() falls into the None guard.
    debug_log.record("test_event", "test.source", {"key": "value"})
    # Nothing in tmp_path.
    assert list(tmp_path.iterdir()) == []


def test_enabled_writes_to_configured_path(tmp_path) -> None:
    log_path = tmp_path / "debug.jsonl"
    debug_log.configure(enabled=True, log_path=log_path)
    debug_log.record("test_event", "test.source", {"key": "value"})
    assert log_path.exists()
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event_type"] == "test_event"
    assert parsed["source"] == "test.source"
    assert parsed["data"]["key"] == "value"
    assert "ts" in parsed


def test_reconfigure_disables(tmp_path) -> None:
    """Enable → disable → record() is silent again."""
    log_path = tmp_path / "debug.jsonl"
    debug_log.configure(enabled=True, log_path=log_path)
    debug_log.record("first", "src", {})
    debug_log.configure(enabled=False, log_path=log_path)
    debug_log.record("second", "src", {})
    assert len(log_path.read_text().splitlines()) == 1


# ── Path resolution ──────────────────────────────────────────────────────


def test_xdg_state_home_used_when_no_explicit_path(monkeypatch, tmp_path) -> None:
    """`XDG_STATE_HOME=/tmp/foo` → log goes to
    `/tmp/foo/icebreaker/debug.jsonl` — not `~/.local/state`."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    debug_log.configure(enabled=True)
    expected = tmp_path / "icebreaker" / "debug.jsonl"
    assert debug_log.current_log_path() == expected


def test_env_var_overrides_xdg(monkeypatch, tmp_path) -> None:
    """`ICEBREAKER_DEBUG_LOG_PATH` beats XDG_STATE_HOME."""
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-should-lose")
    override = tmp_path / "custom-debug.jsonl"
    monkeypatch.setenv("ICEBREAKER_DEBUG_LOG_PATH", str(override))
    debug_log.configure(enabled=True)
    assert debug_log.current_log_path() == override


def test_explicit_path_wins_over_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ICEBREAKER_DEBUG_LOG_PATH", "/should/not/win")
    explicit = tmp_path / "explicit.jsonl"
    debug_log.configure(enabled=True, log_path=explicit)
    assert debug_log.current_log_path() == explicit


# ── JSONL round-trip ─────────────────────────────────────────────────────


def test_multiple_records_produce_valid_jsonl(tmp_path) -> None:
    log_path = tmp_path / "debug.jsonl"
    debug_log.configure(enabled=True, log_path=log_path)
    for i in range(20):
        debug_log.record("iter", "test", {"i": i})
    lines = log_path.read_text().splitlines()
    assert len(lines) == 20
    for i, line in enumerate(lines):
        parsed = json.loads(line)
        assert parsed["data"]["i"] == i


def test_non_serializable_values_use_repr_fallback(tmp_path) -> None:
    """A Path object in the payload must NOT crash the record — the
    fallback repr()s it. Losing type fidelity is fine; losing the
    whole event is not."""
    log_path = tmp_path / "debug.jsonl"
    debug_log.configure(enabled=True, log_path=log_path)
    debug_log.record("path_event", "test", {"path": Path("/tmp/foo")})
    parsed = json.loads(log_path.read_text())
    # repr of Path is "PosixPath('/tmp/foo')" on POSIX.
    assert "/tmp/foo" in parsed["data"]["path"]


# ── Secret redaction ─────────────────────────────────────────────────────


@pytest.mark.parametrize("secret_key", [
    "api_key",
    "API_KEY",
    "ApiKey",
    "auth_token",
    "authtoken",
    "secret",
    "password",
    "bearer_value",
    "gemini_api_key",
    "user_password",
])
def test_secret_keys_redacted(secret_key, tmp_path) -> None:
    """Every variant of a secret-looking key must have its value
    replaced with '<REDACTED>' before landing on disk. BP-8.
    Sentinel is deliberately NOT credential-shaped — the redactor
    keys off the field name, not the value shape."""
    log_path = tmp_path / "debug.jsonl"
    debug_log.configure(enabled=True, log_path=log_path)
    sentinel = "REDACT_TARGET_TOP_LEVEL_VALUE"
    debug_log.record("test", "src", {secret_key: sentinel})
    text = log_path.read_text()
    assert sentinel not in text
    assert "<REDACTED>" in text


def test_nested_secret_redacted(tmp_path) -> None:
    """Redaction walks nested dicts and lists. The sentinel strings
    are DELIBERATELY not credential-shaped — a `sk-` prefix would trip
    entropy-based secret scanners. The redactor keys off the field
    NAME (`api_key`, `token`), not the value shape, so bare sentinels
    exercise the code path just as well."""
    log_path = tmp_path / "debug.jsonl"
    debug_log.configure(enabled=True, log_path=log_path)
    api_sentinel = "REDACT_TARGET_NESTED_INNER_VALUE"
    tok_sentinel = "REDACT_TARGET_LIST_ITEM_VALUE"
    debug_log.record("test", "src", {
        "outer": {"inner": {"api_key": api_sentinel}},
        "list_of_dicts": [{"token": tok_sentinel}, {"safe": "keep"}],
    })
    text = log_path.read_text()
    assert api_sentinel not in text
    assert tok_sentinel not in text
    assert "keep" in text  # safe key survives


def test_non_secret_keys_not_redacted(tmp_path) -> None:
    """Normal keys pass through verbatim."""
    log_path = tmp_path / "debug.jsonl"
    debug_log.configure(enabled=True, log_path=log_path)
    debug_log.record("test", "src", {
        "hop_index": 0,
        "backend": "gemini",
        "primary": "anthropic",
    })
    parsed = json.loads(log_path.read_text())
    assert parsed["data"]["backend"] == "gemini"
    assert parsed["data"]["hop_index"] == 0


# ── Size cap + rotation ──────────────────────────────────────────────────


def test_size_cap_triggers_rotation(tmp_path) -> None:
    """When log exceeds max_size, the old file rotates to .old and a
    fresh file starts."""
    log_path = tmp_path / "debug.jsonl"
    # Configure with 1 MB cap (module clamps 0 → 1 MB floor).
    debug_log.configure(enabled=True, log_path=log_path, max_size_mb=1)
    # Write a payload larger than 1 MB in a single event.
    big_payload = "a" * (2 * 1024 * 1024)  # 2 MB
    debug_log.record("first", "src", {"payload": big_payload})
    # Second record → the rotation check kicks in (file is 2 MB > 1 MB cap).
    debug_log.record("second", "src", {"payload": "b" * 100})

    # first event rotates to .old, second lives in the main file.
    old = log_path.with_suffix(".jsonl.old")
    assert old.exists()
    # Fresh file has at most one line (the second event).
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1


# ── Concurrency ──────────────────────────────────────────────────────────


def test_concurrent_records_no_interleaving(tmp_path) -> None:
    """The lock in _write_line must prevent two threads from
    interleaving a record. Every line must be independently
    parseable."""
    log_path = tmp_path / "debug.jsonl"
    debug_log.configure(enabled=True, log_path=log_path)

    errors: list[str] = []

    def _writer(tid: int):
        for i in range(50):
            try:
                debug_log.record(
                    "concurrent", "src",
                    {"tid": tid, "i": i, "payload": "x" * 100},
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

    threads = [threading.Thread(target=_writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    lines = log_path.read_text().splitlines()
    assert len(lines) == 200
    # Every line is valid JSON.
    for line in lines:
        parsed = json.loads(line)
        assert parsed["event_type"] == "concurrent"


# ── clear() ──────────────────────────────────────────────────────────────


def test_clear_truncates_existing_log(tmp_path) -> None:
    log_path = tmp_path / "debug.jsonl"
    debug_log.configure(enabled=True, log_path=log_path)
    debug_log.record("before_clear", "src", {})
    assert log_path.stat().st_size > 0

    ok, msg = debug_log.clear()
    assert ok is True
    assert log_path.exists()
    assert log_path.stat().st_size == 0


def test_clear_when_no_log_exists_still_ok(tmp_path) -> None:
    """Clearing a non-existent log is idempotent success."""
    log_path = tmp_path / "does-not-exist.jsonl"
    debug_log.configure(enabled=True, log_path=log_path)
    ok, msg = debug_log.clear()
    assert ok is True


# ── Off-path safety ──────────────────────────────────────────────────────


def test_write_failure_does_not_raise(tmp_path, monkeypatch) -> None:
    """If the disk fills / perms change, record() must never propagate
    an OSError — the hot path can't be taken down by debug logging."""
    log_path = tmp_path / "readonly" / "debug.jsonl"
    log_path.parent.mkdir()
    debug_log.configure(enabled=True, log_path=log_path)

    # Simulate a permission error on open.
    real_open = Path.open

    def _bad_open(self, *args, **kwargs):
        if self.name == "debug.jsonl":
            raise PermissionError("simulated read-only fs")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _bad_open)

    # Must NOT raise.
    debug_log.record("event", "src", {})
