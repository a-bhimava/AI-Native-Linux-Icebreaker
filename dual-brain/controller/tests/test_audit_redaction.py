"""Tests for enhanced audit redaction (M5.3, G5.6).

Covers:
  - Shannon-entropy gate: high-entropy strings redacted.
  - Low-entropy long strings NOT redacted (e.g. file paths).
  - Per-tool field allowlist: fs.write `content` param redacted, `path` preserved.
  - Existing key-name and value-pattern tests are preserved (test_audit.py).
  - Entropy threshold configurable.
"""

from __future__ import annotations

import json

import pytest

from controller.audit import (
    REDACTED_PLACEHOLDER,
    AuditFields,
    AuditLog,
    Outcome,
    _high_entropy,
    _redact_params,
)


def _fields(**overrides) -> AuditFields:
    base = dict(
        session_id="sess-redact",
        turn_index=0,
        intent_id="intent-redact",
        action="fs.read",
        target="/etc/hostname",
        tier=0,
        reason="user_requested",
        risk_level="read_only",
        outcome=Outcome.EXECUTED,
        duration_ms=5.0,
        backend="local",
        model="test-model",
        tokens_in=10,
        tokens_out=5,
        cost_estimate_usd=0.0,
    )
    base.update(overrides)
    return AuditFields(**base)


# ── _high_entropy ────────────────────────────────────────────────────────────

def test_high_entropy_base64_blob():
    blob = "aGVsbG8gd29ybGQgdGhpcyBpcyBhIGJhc2U2NCBlbmNvZGVkIHN0cmluZw=="  # pragma: allowlist secret
    assert _high_entropy(blob) is True


def test_high_entropy_mixed_case_secret():
    secret = "aB3cD4eF5gH6iJ7kL8mN9oP0qRsTuVwXyZ1a2b3c"  # pragma: allowlist secret
    assert _high_entropy(secret) is True


def test_low_entropy_file_path():
    path = "/home/user/documents/project/src/main.py"
    assert _high_entropy(path) is False


def test_low_entropy_short_string():
    assert _high_entropy("hello") is False


def test_low_entropy_repeated_chars():
    assert _high_entropy("aaaaaaaaaaaaaaaaaaaaaa") is False


def test_high_entropy_non_string():
    assert _high_entropy(12345) is False


def test_high_entropy_custom_threshold():
    s = "abcdefghijklmnopqrstuvwxyz"
    assert _high_entropy(s, min_len=20, bits=3.0) is True
    assert _high_entropy(s, min_len=20, bits=5.0) is False


# ── Per-tool field allowlist ─────────────────────────────────────────────────

def test_fs_write_content_redacted():
    params = {"path": "/tmp/output.txt", "content": "sensitive data here"}
    redacted = _redact_params(params, action="fs.write")
    assert redacted["path"] == "/tmp/output.txt"
    assert redacted["content"] == REDACTED_PLACEHOLDER


def test_fs_write_path_preserved():
    params = {"path": "/tmp/output.txt"}
    redacted = _redact_params(params, action="fs.write")
    assert redacted["path"] == "/tmp/output.txt"


def test_fs_delete_path_preserved():
    params = {"path": "/tmp/file.txt"}
    redacted = _redact_params(params, action="fs.delete")
    assert redacted["path"] == "/tmp/file.txt"


def test_no_allowlist_preserves_all_non_secret():
    params = {"path": "/etc/hostname", "lines": 100, "follow": False}
    redacted = _redact_params(params, action="fs.read")
    assert redacted == params


def test_no_allowlist_still_redacts_secrets():
    params = {"path": "/etc/hostname", "api_key": "sk-secret123"}
    redacted = _redact_params(params, action="fs.read")
    assert redacted["path"] == "/etc/hostname"
    assert redacted["api_key"] == REDACTED_PLACEHOLDER


# ── Entropy-based redaction in params ────────────────────────────────────────

def test_high_entropy_param_value_redacted():
    params = {"data": "aGVsbG8gd29ybGQgdGhpcyBpcyBhIGJhc2U2NCBlbmNvZGVkIHN0cmluZw=="}  # pragma: allowlist secret
    redacted = _redact_params(params, action="system.status")
    assert redacted["data"] == REDACTED_PLACEHOLDER


def test_low_entropy_param_value_preserved():
    params = {"message": "hello world this is a normal message"}
    redacted = _redact_params(params, action="system.status")
    assert redacted["message"] == "hello world this is a normal message"


# ── End-to-end: written to disk ──────────────────────────────────────────────

def test_high_entropy_redacted_on_disk(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        log.write_fields(_fields(
            action="system.status",
            extra={"params": {"blob": "aB3cD4eF5gH6iJ7kL8mN9oP0qRsTuVwXyZ1a2b3c"}},  # pragma: allowlist secret
        ))
    finally:
        log.close()
    entry = json.loads(p.read_text().strip())
    assert entry["params"]["blob"] == REDACTED_PLACEHOLDER


def test_fs_write_content_redacted_on_disk(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        log.write_fields(_fields(
            action="fs.write",
            extra={"params": {"path": "/tmp/f.txt", "content": "file body"}},
        ))
    finally:
        log.close()
    entry = json.loads(p.read_text().strip())
    assert entry["params"]["path"] == "/tmp/f.txt"
    assert entry["params"]["content"] == REDACTED_PLACEHOLDER
