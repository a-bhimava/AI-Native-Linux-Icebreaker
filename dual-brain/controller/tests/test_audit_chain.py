"""Tests for audit hash-chain integrity (M5.3, G5.6).

Covers:
  - verify_chain OK on untouched log.
  - verify_chain detects edited line (hash mismatch).
  - verify_chain detects deleted line (seq gap).
  - verify_chain detects reordered lines.
  - verify_chain detects truncation (missing tail).
  - Chain survives reopen (tail recovery).
  - Genesis line has prev_hash="GENESIS".
  - seq is monotonic.
  - CLI entry point exits correctly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from controller.audit import (
    GENESIS_HASH,
    AuditFields,
    AuditLog,
    Outcome,
    _canonical_line,
    _hash_line,
)


def _fields(**overrides) -> AuditFields:
    base = dict(
        session_id="sess-chain",
        turn_index=0,
        intent_id="intent-chain",
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


# ── Genesis ──────────────────────────────────────────────────────────────────

def test_genesis_line_has_genesis_hash(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        log.write_fields(_fields())
    finally:
        log.close()
    entry = json.loads(p.read_text().strip())
    assert entry["seq"] == 0
    assert entry["prev_hash"] == GENESIS_HASH


def test_seq_is_monotonic(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        for i in range(5):
            log.write_fields(_fields(turn_index=i))
    finally:
        log.close()
    lines = p.read_text().strip().split("\n")
    seqs = [json.loads(l)["seq"] for l in lines]
    assert seqs == [0, 1, 2, 3, 4]


# ── verify_chain: intact ─────────────────────────────────────────────────────

def test_verify_chain_ok_on_untouched_log(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        for i in range(10):
            log.write_fields(_fields(turn_index=i, intent_id=f"intent-{i}"))
    finally:
        log.close()
    ok, bad_seq = AuditLog.verify_chain(p)
    assert ok is True
    assert bad_seq is None


def test_verify_chain_ok_on_empty_file(tmp_path):
    p = tmp_path / "audit.log"
    p.touch()
    ok, bad_seq = AuditLog.verify_chain(p)
    assert ok is True
    assert bad_seq is None


def test_verify_chain_ok_single_entry(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        log.write_fields(_fields())
    finally:
        log.close()
    ok, bad_seq = AuditLog.verify_chain(p)
    assert ok is True


# ── verify_chain: detects tampering ──────────────────────────────────────────

def test_verify_chain_detects_edited_line(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        for i in range(5):
            log.write_fields(_fields(turn_index=i))
    finally:
        log.close()
    lines = p.read_text().strip().split("\n")
    entry = json.loads(lines[2])
    entry["action"] = "fs.delete"
    lines[2] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n")
    ok, bad_seq = AuditLog.verify_chain(p)
    assert ok is False
    assert bad_seq == 3  # seq 3's prev_hash won't match


def test_verify_chain_detects_deleted_line(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        for i in range(5):
            log.write_fields(_fields(turn_index=i))
    finally:
        log.close()
    lines = p.read_text().strip().split("\n")
    del lines[2]  # remove seq=2
    p.write_text("\n".join(lines) + "\n")
    ok, bad_seq = AuditLog.verify_chain(p)
    assert ok is False


def test_verify_chain_detects_reordered_lines(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        for i in range(5):
            log.write_fields(_fields(turn_index=i))
    finally:
        log.close()
    lines = p.read_text().strip().split("\n")
    lines[1], lines[2] = lines[2], lines[1]
    p.write_text("\n".join(lines) + "\n")
    ok, bad_seq = AuditLog.verify_chain(p)
    assert ok is False


def test_verify_chain_detects_truncation(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        for i in range(5):
            log.write_fields(_fields(turn_index=i))
    finally:
        log.close()
    # Truncate: keep only first 3 lines, but change seq numbering
    lines = p.read_text().strip().split("\n")
    # Append a line with wrong seq
    fake_entry = json.loads(lines[0])
    fake_entry["seq"] = 5  # wrong seq
    fake_entry["prev_hash"] = "fakehash"
    lines.append(json.dumps(fake_entry, sort_keys=True, separators=(",", ":")))
    p.write_text("\n".join(lines) + "\n")
    ok, bad_seq = AuditLog.verify_chain(p)
    assert ok is False


# ── Tail recovery: chain survives reopen ─────────────────────────────────────

def test_chain_survives_reopen(tmp_path):
    p = tmp_path / "audit.log"
    log1 = AuditLog(path=p, fsync_each_write=False)
    try:
        log1.write_fields(_fields(turn_index=0))
        log1.write_fields(_fields(turn_index=1))
    finally:
        log1.close()

    log2 = AuditLog(path=p, fsync_each_write=False)
    try:
        log2.write_fields(_fields(turn_index=2))
    finally:
        log2.close()

    ok, bad_seq = AuditLog.verify_chain(p)
    assert ok is True
    assert bad_seq is None
    lines = p.read_text().strip().split("\n")
    assert len(lines) == 3
    seqs = [json.loads(l)["seq"] for l in lines]
    assert seqs == [0, 1, 2]


# ── CLI: python -m controller.audit --verify ─────────────────────────────────

def test_cli_verify_ok(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        log.write_fields(_fields())
    finally:
        log.close()
    repo_root = Path(__file__).resolve().parent.parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "controller.audit", "--verify", str(p)],
        capture_output=True, text=True, timeout=10,
        env={**__import__("os").environ, "PYTHONPATH": str(repo_root)},
    )
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_cli_verify_tampered(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(path=p, fsync_each_write=False)
    try:
        log.write_fields(_fields(turn_index=0))
        log.write_fields(_fields(turn_index=1))
    finally:
        log.close()
    lines = p.read_text().strip().split("\n")
    entry = json.loads(lines[0])
    entry["action"] = "TAMPERED"
    lines[0] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n")
    repo_root = Path(__file__).resolve().parent.parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "controller.audit", "--verify", str(p)],
        capture_output=True, text=True, timeout=10,
        env={**__import__("os").environ, "PYTHONPATH": str(repo_root)},
    )
    assert result.returncode == 1
    assert "TAMPERED" in result.stdout


def test_cli_verify_file_not_found(tmp_path):
    p = tmp_path / "nonexistent.log"
    repo_root = Path(__file__).resolve().parent.parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "controller.audit", "--verify", str(p)],
        capture_output=True, text=True, timeout=10,
        env={**__import__("os").environ, "PYTHONPATH": str(repo_root)},
    )
    assert result.returncode == 2
