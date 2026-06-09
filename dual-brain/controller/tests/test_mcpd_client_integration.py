"""Integration tests for McpdClient against the real mcpd binary.

These tests require:
  - A built mcpd binary, discoverable via the MCPD_BINARY env var or at
    one of the default paths in _candidate_binaries().
  - Linux (mcpd uses Landlock + seccomp; these are Linux kernel features).

If the binary isn't available, the suite is skipped — Mac developers
won't see failures. The canonical run is on the GCP VM.

Each test gets its own MCPD_AUDIT_LOG inside tmp_path so the suite is
hermetic and doesn't pollute /var/log/mcpd/audit.log.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

import pytest

from controller.mcpd_client import (
    JsonRpcError,
    McpdClient,
    McpdProcessError,
)


def _candidate_binaries() -> list[Path]:
    """Common locations where the mcpd binary may live."""
    candidates: list[Path] = []
    if envp := os.environ.get("MCPD_BINARY"):
        candidates.append(Path(envp).expanduser())
    candidates.extend([
        Path.home() / "icebreaker" / "src" / "mcpd" / "target" / "release" / "mcpd",
        Path.cwd() / "src" / "mcpd" / "target" / "release" / "mcpd",
        Path.cwd().parent / "src" / "mcpd" / "target" / "release" / "mcpd",
    ])
    return candidates


def _find_mcpd() -> Path | None:
    for c in _candidate_binaries():
        if c.exists() and os.access(c, os.X_OK):
            return c.resolve()
    return None


_MCPD = _find_mcpd()

# Skip the entire module if mcpd isn't available or we're not on Linux.
# mcpd's sandbox uses Landlock + seccomp; running it on macOS would crash.
pytestmark = [
    pytest.mark.skipif(
        platform.system() != "Linux",
        reason="mcpd integration tests require Linux (Landlock + seccomp)",
    ),
    pytest.mark.skipif(
        _MCPD is None,
        reason="mcpd binary not found — set MCPD_BINARY env var to enable",
    ),
]


@pytest.fixture
def mcpd_client(tmp_path):
    """Spawns a fresh mcpd per test with a hermetic audit log."""
    audit = tmp_path / "mcpd-audit.log"
    client = McpdClient.spawn(
        _MCPD,
        default_timeout=10.0,
        audit_log=audit,
        rust_log="mcpd=warn",  # quiet down stderr in tests
    )
    try:
        yield client
    finally:
        client.close()


# ── tools/list ──────────────────────────────────────────────────────────────

def test_tools_list_returns_22_tools(mcpd_client):
    env = mcpd_client.list_tools()
    assert isinstance(env, dict)
    assert "schema_version" in env
    assert "tools" in env
    assert isinstance(env["tools"], list)
    assert len(env["tools"]) == 22


def test_tools_list_schema_version_is_1_0_0(mcpd_client):
    # Phase 1 froze schema_version at 1.0.0; any bump means
    # _mcpd_tools.py must be regenerated.
    env = mcpd_client.list_tools()
    assert env["schema_version"] == "1.0.0"


def test_tools_list_includes_canonical_22(mcpd_client):
    expected = {
        "system.status", "system.uptime", "system.cpu", "system.memory", "system.disk",
        "process.list", "process.inspect",
        "fs.read", "fs.list", "fs.stat", "fs.write", "fs.delete",
        "service.start", "service.stop", "service.restart", "service.logs",
        "network.status", "network.dns.read",
        "package.query", "package.install", "package.remove", "package.upgrade",
    }
    env = mcpd_client.list_tools()
    actual = {t["name"] for t in env["tools"]}
    assert actual == expected


def test_tools_list_entries_carry_params_schema(mcpd_client):
    env = mcpd_client.list_tools()
    for tool in env["tools"]:
        assert "params_schema" in tool, f"tool {tool['name']} missing params_schema"
        assert isinstance(tool["params_schema"], dict)


# ── Read-only tools (Tier 0) ────────────────────────────────────────────────

def test_system_status(mcpd_client):
    result = mcpd_client.call("system.status")
    r = result.result
    assert isinstance(r.get("hostname"), str)
    assert isinstance(r.get("uptime_seconds"), int)
    assert r["uptime_seconds"] > 0


def test_system_uptime(mcpd_client):
    result = mcpd_client.call("system.uptime")
    assert isinstance(result.result.get("uptime_seconds"), int)


def test_process_list(mcpd_client):
    result = mcpd_client.call("process.list")
    procs = result.result.get("processes")
    assert isinstance(procs, list)
    assert len(procs) > 0  # at least mcpd + pytest are running


def test_fs_read_etc_hostname(mcpd_client):
    # /etc/hostname is in mcpd's read allowlist on Linux VMs.
    result = mcpd_client.call("fs.read", {"path": "/etc/hostname"})
    assert isinstance(result.result.get("content"), str)
    assert len(result.result["content"]) > 0


def test_fs_list_etc(mcpd_client):
    result = mcpd_client.call("fs.list", {"path": "/etc"})
    entries = result.result.get("entries")
    assert isinstance(entries, list)
    assert any(e["name"] == "hostname" for e in entries)


def test_network_status(mcpd_client):
    result = mcpd_client.call("network.status")
    assert isinstance(result.result.get("interfaces"), list)


# ── COW envelope on destructive / system-write tools ───────────────────────

def test_fs_delete_returns_cow_envelope(mcpd_client):
    # In Phase 1, fs.delete always returns requires_cow_approval (Phase 3
    # will wire the overlay). Path doesn't need to exist for the gate.
    result = mcpd_client.call("fs.delete", {"path": "/tmp/icebreaker-mcpd-it-nonexistent"})
    assert result.requires_cow_approval
    assert result.cow_intent_id is not None
    assert isinstance(result.cow_intent_id, str)
    # UUID-ish: 36 chars with hyphens
    assert len(result.cow_intent_id) == 36
    preview = result.cow_preview
    assert isinstance(preview, dict)
    assert preview["operation"] == "fs.delete"


def test_package_install_returns_cow_envelope(mcpd_client):
    result = mcpd_client.call("package.install", {"package": "htop"})
    assert result.requires_cow_approval
    assert result.cow_preview["operation"] == "package.install"
    assert result.cow_preview["package"] == "htop"


def test_package_remove_returns_cow_envelope(mcpd_client):
    result = mcpd_client.call("package.remove", {"package": "htop"})
    assert result.requires_cow_approval
    assert result.cow_preview["package"] == "htop"


def test_package_upgrade_returns_cow_envelope(mcpd_client):
    result = mcpd_client.call("package.upgrade", {"package": "htop"})
    assert result.requires_cow_approval


# ── JSON-RPC error paths ────────────────────────────────────────────────────

def test_unknown_method_returns_minus_32601(mcpd_client):
    with pytest.raises(JsonRpcError) as exc_info:
        mcpd_client.call("not.a.real.method")
    assert exc_info.value.code == -32601
    assert "Method not found" in exc_info.value.message


def test_missing_required_param_returns_minus_32602(mcpd_client):
    with pytest.raises(JsonRpcError) as exc_info:
        mcpd_client.call("process.inspect", {})  # `pid` missing
    assert exc_info.value.code == -32602


def test_wrong_type_param_returns_minus_32602(mcpd_client):
    with pytest.raises(JsonRpcError) as exc_info:
        mcpd_client.call("process.inspect", {"pid": "not-an-integer"})
    assert exc_info.value.code == -32602


def test_jsonrpc_error_does_not_kill_client(mcpd_client):
    # Schema-level errors are NOT transport failures; mcpd stays alive.
    with pytest.raises(JsonRpcError):
        mcpd_client.call("not.a.real.method")
    # Subsequent call still succeeds.
    result = mcpd_client.call("system.status")
    assert result.result.get("hostname")


# ── Sequential durability ───────────────────────────────────────────────────

def test_many_sequential_calls_share_one_subprocess(mcpd_client):
    pid_before = mcpd_client.pid
    for _ in range(25):
        mcpd_client.list_tools()
    assert mcpd_client.pid == pid_before
    assert mcpd_client.is_alive


# ── EOF / shutdown ──────────────────────────────────────────────────────────

def test_close_triggers_graceful_shutdown(mcpd_client):
    # mcpd exits 0 on stdin EOF (server.rs:70-73).
    rc = mcpd_client.close()
    assert rc == 0


def test_call_after_explicit_close_raises(mcpd_client):
    mcpd_client.close()
    with pytest.raises(McpdProcessError):
        mcpd_client.call("system.status")


# ── Latency budget (sanity) ─────────────────────────────────────────────────

def test_tools_list_under_100ms_p50(mcpd_client):
    """Sanity: warmed-up tools/list should complete well under 100 ms p50
    on the VM (Phase 1 G9 measured ~8 ms p50 on the same hardware).
    Catches gross regression in the client overhead."""
    import time
    durations = []
    for _ in range(20):
        t0 = time.perf_counter()
        mcpd_client.list_tools()
        durations.append((time.perf_counter() - t0) * 1000.0)
    durations.sort()
    p50 = durations[len(durations) // 2]
    assert p50 < 100.0, f"tools/list p50 = {p50:.2f} ms — too slow"


# ── MCPD_AUDIT_LOG override is honoured ─────────────────────────────────────

def test_audit_log_override_writes_to_tmpdir(mcpd_client, tmp_path):
    # The fixture already set MCPD_AUDIT_LOG to a tmp path. After we make
    # a call and close mcpd, the audit log file should exist there.
    mcpd_client.call("system.status")
    mcpd_client.close()
    audit_file = tmp_path / "mcpd-audit.log"
    assert audit_file.exists(), "mcpd should have written to tmp audit log"
    content = audit_file.read_text()
    assert "system.status" in content
