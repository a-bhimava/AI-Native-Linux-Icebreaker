"""Tests for AuditLog — the intent-level audit log (INV-8, P2-F16).

Covers:
  - Path resolution (default + XDG_STATE_HOME + explicit override).
  - File mode 0o640 and parent dir mode 0o700.
  - O_APPEND semantics: restart preserves prior lines; concurrent writes
    don't interleave.
  - Per-line os.fsync durability: a SIGKILL between write and process
    exit still leaves the entry on disk.
  - REQUIRED_FIELDS enforced at write time.
  - Redaction: secret-looking param keys and known-secret value patterns
    replaced with the placeholder.
  - Outcome enum coverage.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

from controller.audit import (
    REDACTED_PLACEHOLDER,
    REQUIRED_FIELDS,
    AuditFields,
    AuditLog,
    Outcome,
    make_entry,
)


def _fields(**overrides) -> AuditFields:
    base = dict(
        session_id="sess-test",
        turn_index=0,
        intent_id="intent-test",
        action="fs.read",
        target="/etc/hostname",
        tier=0,
        reason="user_requested",
        risk_level="read_only",
        outcome=Outcome.EXECUTED,
        duration_ms=12.5,
        backend="local",
        model="phi-4-mini-instruct-Q4_K_M",
        tokens_in=10,
        tokens_out=5,
        cost_estimate_usd=0.0,
    )
    base.update(overrides)
    return AuditFields(**base)


# ── Path resolution ─────────────────────────────────────────────────────────

def test_default_path_uses_local_state(monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    p = AuditLog._default_path()
    assert p == Path.home() / ".local" / "state" / "icebreaker" / "controller-audit.log"


def test_xdg_state_home_overrides_default(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    p = AuditLog._default_path()
    assert p == tmp_path / "state" / "icebreaker" / "controller-audit.log"


def test_explicit_path_overrides_defaults(tmp_path):
    custom = tmp_path / "custom" / "audit.log"
    log = AuditLog(path=custom)
    try:
        assert log.path == custom.resolve()
    finally:
        log.close()


# ── File + parent permissions ──────────────────────────────────────────────

def test_file_created_with_mode_0640(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p)
    try:
        log.write_fields(_fields())
    finally:
        log.close()
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o640, f"file mode is 0o{mode:o}, expected 0o640"


def test_parent_dir_created_with_mode_0700(tmp_path):
    # Build a path with NO existing parent so we exercise the mkdir branch.
    p = tmp_path / "fresh-state" / "icebreaker" / "audit.log"
    log = AuditLog(path=p)
    try:
        log.write_fields(_fields())
    finally:
        log.close()
    # The deepest dir (icebreaker/) must be created with 0o700.
    # umask can still mask bits, so we check it's no looser than 0o700.
    mode = stat.S_IMODE(p.parent.stat().st_mode)
    # On the developer Mac the umask may produce 0o700 or 0o755 depending
    # on the user's environment. The contract is: not world-writable,
    # not group-writable.
    assert mode & 0o022 == 0, f"parent dir mode 0o{mode:o} is group-or-world-writable"


def test_existing_file_mode_preserved(tmp_path):
    # If the file already exists, AuditLog opens with O_APPEND — does not
    # chmod it. Confirm we don't accidentally tighten a looser mode set
    # by the operator (e.g. for log shipping).
    p = tmp_path / "audit.log"
    p.touch(mode=0o644)
    os.chmod(p, 0o644)
    log = AuditLog(path=p)
    try:
        log.write_fields(_fields())
    finally:
        log.close()
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o644, "AuditLog should not change pre-existing file mode"


# ── O_APPEND semantics ─────────────────────────────────────────────────────

def test_writes_append_in_order(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p)
    try:
        for i in range(5):
            log.write_fields(_fields(turn_index=i, intent_id=f"intent-{i}"))
    finally:
        log.close()
    lines = p.read_text().splitlines()
    assert len(lines) == 5
    parsed = [json.loads(l) for l in lines]
    assert [e["turn_index"] for e in parsed] == [0, 1, 2, 3, 4]
    assert [e["intent_id"] for e in parsed] == [f"intent-{i}" for i in range(5)]


def test_restart_appends_does_not_truncate(tmp_path):
    p = tmp_path / "audit.log"
    log1 = AuditLog(path=p)
    log1.write_fields(_fields(turn_index=0))
    log1.write_fields(_fields(turn_index=1))
    log1.close()
    # Restart with a fresh AuditLog instance on the same path.
    log2 = AuditLog(path=p)
    log2.write_fields(_fields(turn_index=2))
    log2.close()
    lines = p.read_text().splitlines()
    assert len(lines) == 3
    assert [json.loads(l)["turn_index"] for l in lines] == [0, 1, 2]


def test_concurrent_writers_do_not_interleave(tmp_path):
    """Multiple threads each writing one entry must produce intact lines.

    Lines are well under PIPE_BUF; the internal lock + os.write atomicity
    guarantee no byte-level interleaving."""
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    n_threads = 8
    per_thread = 25
    barrier = threading.Barrier(n_threads)

    def worker(tid: int):
        barrier.wait()
        for i in range(per_thread):
            log.write_fields(_fields(
                session_id=f"sess-t{tid}",
                turn_index=i,
                intent_id=f"intent-t{tid}-{i}",
            ))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    log.close()

    lines = p.read_text().splitlines()
    assert len(lines) == n_threads * per_thread
    # Every line must be parseable JSON — if any thread's bytes interleaved
    # with another's, json.loads would fail.
    for line in lines:
        json.loads(line)


# ── Required field enforcement ─────────────────────────────────────────────

def test_write_rejects_entry_missing_required_field(tmp_path):
    log = AuditLog(path=tmp_path / "audit.log")
    try:
        entry = make_entry(_fields())
        entry.pop("backend")
        with pytest.raises(ValueError, match="missing required"):
            log.write(entry)
    finally:
        log.close()


def test_make_entry_includes_all_required_fields():
    entry = make_entry(_fields())
    for f in REQUIRED_FIELDS:
        assert f in entry, f"required field {f} missing from make_entry output"


def test_make_entry_ts_is_iso_utc():
    entry = make_entry(_fields())
    assert entry["ts"].endswith("Z")
    # Format: YYYY-MM-DDTHH:MM:SS.mmmZ
    assert len(entry["ts"]) == 24


def test_make_entry_user_auto_filled():
    entry = make_entry(_fields(user=None))
    assert entry["user"]
    assert isinstance(entry["user"], str)


def test_make_entry_user_explicit_preserved():
    entry = make_entry(_fields(user="explicit"))
    assert entry["user"] == "explicit"


def test_outcome_enum_serialised_as_string():
    entry = make_entry(_fields(outcome=Outcome.HITL_DENIED))
    assert entry["outcome"] == "hitl_denied"


def test_all_outcome_enum_values_present():
    # Pin the canonical set so a future enum change is intentional.
    expected = {
        "schema_rejected", "classification_failed",
        "hitl_denied", "hitl_timeout", "hitl_non_tty",
        "executed", "tool_error", "tool_timeout", "cow_required",
        "brain_error", "qb_verifier_rejected", "pb_schema_error",
        "backend_swapped",
        "trust_applied", "trust_granted", "modify_requested",
        "limit_exceeded", "cost_exceeded",
        "cancelled",
        "undo_requested", "undo_unavailable", "undone",
    }
    actual = {o.value for o in Outcome}
    assert actual == expected, f"Outcome enum drifted: {actual.symmetric_difference(expected)}"


# ── Redaction ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", [
    "password", "api_key", "apikey", "token", "auth_token",
    "secret", "credentials", "private_key", "X-Api-Key",
    "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
])
def test_secret_key_substrings_redacted(tmp_path, key):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p)
    try:
        log.write_fields(_fields(extra={"params": {key: "totallylegitvalue123"}}))
    finally:
        log.close()
    entry = json.loads(p.read_text().strip())
    assert entry["params"][key] == REDACTED_PLACEHOLDER


@pytest.mark.parametrize("value", [
    "sk-ant-abc123def4567890hijklmn",                  # Anthropic  # pragma: allowlist secret
    "sk-1234567890abcdef1234567890",                   # OpenAI  # pragma: allowlist secret
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",        # GitHub personal  # pragma: allowlist secret
    "ghs_abcdefghijklmnopqrstuvwxyz0123456789",        # GitHub server-to-server  # pragma: allowlist secret
    "AKIAIOSFODNN7EXAMPLE",                            # AWS access key id  # pragma: allowlist secret
    "ya29.a0AfH6SMBxxx" + "x" * 40,                    # Google OAuth  # pragma: allowlist secret
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOjF9.SflKxwRJSMeKKF2",  # JWT-ish  # pragma: allowlist secret
])
def test_secret_value_patterns_redacted(tmp_path, value):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p)
    try:
        log.write_fields(_fields(extra={"params": {"innocent_name": value}}))
    finally:
        log.close()
    entry = json.loads(p.read_text().strip())
    assert entry["params"]["innocent_name"] == REDACTED_PLACEHOLDER


def test_non_secret_params_preserved(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p)
    try:
        log.write_fields(_fields(extra={"params": {
            "path": "/etc/hostname",
            "lines": 100,
            "follow": False,
        }}))
    finally:
        log.close()
    entry = json.loads(p.read_text().strip())
    assert entry["params"] == {
        "path": "/etc/hostname",
        "lines": 100,
        "follow": False,
    }


def test_redaction_does_not_mutate_input(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p)
    try:
        params = {"api_key": "sk-secret"}
        log.write_fields(_fields(extra={"params": params}))
        # Caller's input dict MUST NOT be mutated.
        assert params == {"api_key": "sk-secret"}
    finally:
        log.close()


# ── Extra-field passthrough (for rejection envelopes, cow_intent_id, ...) ──

def test_extra_fields_passed_through(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p)
    try:
        log.write_fields(_fields(
            outcome=Outcome.SCHEMA_REJECTED,
            extra={
                "rejection_field": "/target",
                "rejection_type": "pattern",
                "rejection_message": "shell metachar rejected",
            },
        ))
    finally:
        log.close()
    entry = json.loads(p.read_text().strip())
    assert entry["rejection_field"] == "/target"
    assert entry["rejection_type"] == "pattern"
    assert entry["rejection_message"] == "shell metachar rejected"


# ── Lifecycle / errors ─────────────────────────────────────────────────────

def test_close_is_idempotent(tmp_path):
    log = AuditLog(path=tmp_path / "audit.log")
    log.close()
    log.close()  # no exception


def test_write_after_close_raises(tmp_path):
    log = AuditLog(path=tmp_path / "audit.log")
    log.close()
    with pytest.raises(OSError):
        log.write_fields(_fields())


def test_context_manager_closes(tmp_path):
    p = tmp_path / "audit.log"
    with AuditLog(path=p) as log:
        log.write_fields(_fields())
    # After __exit__, write raises.
    with pytest.raises(OSError):
        log.write_fields(_fields())


# ── Durability — SIGKILL between write and process exit (P2-F16) ───────────

def test_fsync_persists_through_sigkill(tmp_path):
    """Spawn a subprocess that writes one entry then SIGKILLs itself.
    The fsync MUST persist the entry to disk before the kill takes effect."""
    audit_path = tmp_path / "audit.log"
    repo_root = Path(__file__).resolve().parent.parent.parent
    # The subprocess imports our AuditLog directly. Set PYTHONPATH so it
    # can find the controller package.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    script = textwrap.dedent(f"""
        import os, signal, sys
        from controller.audit import AuditLog, AuditFields, Outcome
        log = AuditLog(path=r"{audit_path}")
        log.write_fields(AuditFields(
            session_id="sigkill-test", turn_index=0, intent_id="intent-sigkill",
            action="system.status", target="", tier=0,
            reason="user_requested", risk_level="read_only",
            outcome=Outcome.EXECUTED, duration_ms=1.0,
            backend="local", model="test", tokens_in=0, tokens_out=0,
            cost_estimate_usd=0.0,
        ))
        # The entry is on disk now (os.fsync inside write). SIGKILL ourselves
        # without giving the AuditLog destructor a chance to run.
        os.kill(os.getpid(), signal.SIGKILL)
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        timeout=10,
    )
    # SIGKILL exit is -9 on POSIX.
    assert proc.returncode == -9, f"expected SIGKILL, got {proc.returncode}"
    # The entry MUST be on disk despite the kill.
    assert audit_path.exists(), "audit file missing after SIGKILL"
    lines = audit_path.read_text().splitlines()
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}; content={lines!r}"
    entry = json.loads(lines[0])
    assert entry["intent_id"] == "intent-sigkill"
    assert entry["outcome"] == "executed"


# ── Schema-rejected entry shape (M2.2 ↔ M2.3 wiring sanity) ────────────────

def test_schema_rejected_entry_shape(tmp_path):
    """When the orchestration layer (M2.12) writes a schema-rejection
    audit row, every required field must still be populated even though
    the rejected candidate never reached the classifier."""
    log = AuditLog(path=tmp_path / "audit.log")
    try:
        log.write_fields(_fields(
            intent_id="unknown",                # rejected before intent_store
            action="bad.action.toolong" + "z" * 50,
            target="malformed",
            tier=3,                              # treat malformed as worst-case
            reason="user_requested",
            risk_level="critical",
            outcome=Outcome.SCHEMA_REJECTED,
            duration_ms=2.0,
            tokens_in=0,
            tokens_out=0,
            extra={
                "rejection_field": "/action",
                "rejection_type": "maxLength",
                "rejection_message": "string too long",
            },
        ))
    finally:
        log.close()
    entry = json.loads((tmp_path / "audit.log").read_text().strip())
    assert entry["outcome"] == "schema_rejected"
    assert entry["rejection_field"] == "/action"
    for f in REQUIRED_FIELDS:
        assert f in entry
