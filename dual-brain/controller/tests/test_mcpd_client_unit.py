"""Unit tests for McpdClient against the Python fake_mcpd shim.

These tests do NOT require the real mcpd binary; they run on any platform
that has Python 3.10+. They cover the transport, framing, error paths,
timeout, EOF behavior, and JSON-RPC response parsing — every code path
in mcpd_client.py that doesn't depend on real Linux syscalls.

The integration test suite (test_mcpd_client_integration.py) covers the
contract against the real mcpd binary on the VM.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from controller.mcpd_client import (
    DEFAULT_TIMEOUT_SECONDS,
    JsonRpcError,
    McpdClient,
    McpdProcessError,
    McpdProtocolError,
    McpdTimeoutError,
    ToolResult,
)


FAKE_MCPD = Path(__file__).parent / "fake_mcpd.py"


def _spawn_fake(mode: str = "normal", *, default_timeout: float = 5.0, **env) -> McpdClient:
    """Spawn the Python fake_mcpd shim as if it were the real mcpd.

    McpdClient's spawn() takes a path to an executable; we point it at a
    wrapper script that re-exec's the shim under the current interpreter.
    """
    env_overrides = {"FAKE_MCPD_MODE": mode}
    for k, v in env.items():
        env_overrides[k.upper()] = str(v)

    # Use sys.executable + fake_mcpd.py as the "binary". McpdClient's
    # spawn() expects a single executable path, so we create a tiny shim
    # path: we'll use the Python interpreter itself and pass the script
    # by overloading extra_env. But spawn() runs `[binary]` with no args.
    # Simplest: write a temp shim that exec's python + script, OR call
    # McpdClient.__init__ directly after manually Popen-ing.
    #
    # We go with manual Popen to keep this honest and avoid filesystem
    # artefacts. This mirrors what spawn() does internally.
    import subprocess as _sp
    proc_env = os.environ.copy()
    proc_env.update(env_overrides)
    proc = _sp.Popen(
        [sys.executable, str(FAKE_MCPD)],
        stdin=_sp.PIPE,
        stdout=_sp.PIPE,
        stderr=_sp.DEVNULL,
        bufsize=0,
        env=proc_env,
    )
    return McpdClient(proc, binary_path=FAKE_MCPD, default_timeout=default_timeout)


# ── Spawn / lifecycle ───────────────────────────────────────────────────────

def test_spawn_validates_binary_exists():
    with pytest.raises(McpdProcessError, match="not found"):
        McpdClient.spawn("/nonexistent/path/to/mcpd")


def test_spawn_validates_binary_is_executable(tmp_path):
    p = tmp_path / "not_executable"
    p.write_text("#!/bin/sh\nexit 0\n")
    # Don't chmod +x — should fail the executable check.
    with pytest.raises(McpdProcessError, match="not executable"):
        McpdClient.spawn(p)


def test_dead_binary_detected_on_first_call(tmp_path):
    # spawn() does not eagerly poll (would add 200-400 ms latency for
    # every healthy spawn). Instead, the first call() detects death via
    # EOF on stdout.
    shim = tmp_path / "die_mcpd.sh"
    shim.write_text("#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)
    client = McpdClient.spawn(shim)
    try:
        with pytest.raises(McpdProcessError):
            client.call("anything")
    finally:
        client.close()


def test_context_manager_closes_process():
    with _spawn_fake("normal") as client:
        result = client.call("system.status")
        assert result.result["status"] == "ok"
    # After context exit, process should be reaped.
    assert not client.is_alive


def test_explicit_close_returns_exit_code():
    client = _spawn_fake("normal")
    client.call("system.status")
    rc = client.close()
    assert rc == 0
    assert client._closed
    assert not client.is_alive
    # Second close is idempotent.
    assert client.close() == 0


def test_call_after_close_raises():
    client = _spawn_fake("normal")
    client.close()
    with pytest.raises(McpdProcessError, match="closed"):
        client.call("system.status")


# ── Happy path: normal RPC ─────────────────────────────────────────────────

def test_simple_call_round_trip():
    with _spawn_fake("normal") as client:
        result = client.call("system.status")
        assert isinstance(result, ToolResult)
        assert result.result["status"] == "ok"
        assert result.result["hostname"] == "fake-vm"
        assert result.request_id == 1


def test_list_tools_returns_envelope():
    with _spawn_fake("normal") as client:
        env = client.list_tools()
        assert env["schema_version"] == "1.0.0-fake"
        assert len(env["tools"]) == 22


def test_call_increments_id_per_request():
    with _spawn_fake("normal") as client:
        r1 = client.call("system.status")
        r2 = client.call("system.uptime")
        r3 = client.call("system.disk")
        assert (r1.request_id, r2.request_id, r3.request_id) == (1, 2, 3)


def test_call_echoes_params():
    with _spawn_fake("normal") as client:
        result = client.call("fs.read", {"path": "/etc/hostname"})
        assert result.result["echo"] == {"path": "/etc/hostname"}


def test_call_with_none_params_sends_empty_object():
    with _spawn_fake("normal") as client:
        # Should not raise; mcpd treats null params as {}
        result = client.call("system.status", params=None)
        assert result.result["status"] == "ok"


def test_call_rejects_non_dict_params():
    with _spawn_fake("normal") as client:
        with pytest.raises(TypeError, match="params must be a dict"):
            client.call("system.status", params=["not", "a", "dict"])


def test_call_rejects_empty_method():
    with _spawn_fake("normal") as client:
        with pytest.raises(TypeError, match="non-empty string"):
            client.call("")


def test_call_rejects_negative_timeout():
    with _spawn_fake("normal") as client:
        with pytest.raises(ValueError, match="positive"):
            client.call("system.status", timeout=-1)


# ── COW envelope handling ──────────────────────────────────────────────────

def test_requires_cow_approval_flagged_for_fs_delete():
    with _spawn_fake("normal") as client:
        result = client.call("fs.delete", {"path": "/tmp/x"})
        assert result.requires_cow_approval
        assert result.status == "requires_cow_approval"
        assert result.cow_intent_id == "00000000-0000-0000-0000-000000000001"
        # M7.0.1b/d: preview now includes a diff sub-object. Verify the
        # classic fields (operation/path) survive alongside diff.
        assert result.cow_preview["operation"] == "fs.delete"
        assert result.cow_preview["path"] == "/tmp/x"


def test_requires_cow_approval_flagged_for_package_install():
    with _spawn_fake("normal") as client:
        result = client.call("package.install", {"package": "htop"})
        assert result.requires_cow_approval
        assert result.cow_preview["package"] == "htop"


def test_non_cow_result_has_no_cow_helpers():
    with _spawn_fake("normal") as client:
        result = client.call("system.status")
        assert not result.requires_cow_approval
        assert result.cow_intent_id is None
        assert result.cow_preview is None
        assert result.dry_run_diff is None


# ── M7.0.1d: dry_run_diff + commit_cow ────────────────────────────────────

def test_dry_run_diff_populated_for_fs_delete():
    """The diff sub-object from mcpd v6.16+ is exposed as ToolResult.dry_run_diff."""
    with _spawn_fake("normal") as client:
        result = client.call("fs.delete", {"path": "/tmp/x"})
        diff = result.dry_run_diff
        assert diff is not None
        assert diff["operation"] == "fs.delete"
        assert diff["bytes_delta"] == -1200
        assert diff["file_count_delta"] == -3
        assert diff["risk"] == "LOW"
        assert diff["reversible"] is False
        assert "1.2 KB" in diff["human_summary"]


def test_dry_run_diff_populated_for_package_install():
    with _spawn_fake("normal") as client:
        result = client.call("package.install", {"package": "htop"})
        diff = result.dry_run_diff
        assert diff is not None
        assert diff["operation"] == "package.install"
        assert diff["file_count_delta"] == 4
        assert diff["risk"] == "MED"


def test_dry_run_diff_none_when_no_diff_field():
    """Old mcpd (pre-v6.16) sends tickets without a diff — must return None."""
    from controller.mcpd_client import ToolResult
    old_ticket = ToolResult(
        result={
            "status": "requires_cow_approval",
            "intent_id": "deadbeef-dead-beef-dead-beefdeadbeef",
            "preview": {"operation": "fs.delete", "path": "/tmp/y"},
        },
        request_id=1,
    )
    assert old_ticket.requires_cow_approval
    assert old_ticket.dry_run_diff is None


def test_commit_cow_round_trip_via_fake():
    """commit_cow() sends the right params + returns the fake's ok envelope."""
    with _spawn_fake("normal") as client:
        result = client.commit_cow(
            "00000000-0000-0000-0000-000000000001",
            "fs.delete",
            "/tmp/x",
        )
        assert result.status == "ok"
        assert result.result["operation"] == "fs.delete"
        assert result.result["intent_id"] == "00000000-0000-0000-0000-000000000001"


# ── JSON-RPC error path ────────────────────────────────────────────────────

def test_jsonrpc_internal_error_raises_jsonrpc_error():
    with _spawn_fake("error_internal") as client:
        with pytest.raises(JsonRpcError) as exc_info:
            client.call("anything")
        assert exc_info.value.code == -32603
        assert "synthetic" in exc_info.value.message


def test_jsonrpc_method_not_found_raises_jsonrpc_error():
    with _spawn_fake("unknown_method") as client:
        with pytest.raises(JsonRpcError) as exc_info:
            client.call("does.not.exist")
        assert exc_info.value.code == -32601


def test_jsonrpc_invalid_params_raises_jsonrpc_error():
    with _spawn_fake("invalid_params") as client:
        with pytest.raises(JsonRpcError) as exc_info:
            client.call("anything")
        assert exc_info.value.code == -32602


def test_jsonrpc_error_does_not_close_client():
    # An -326xx error is not a transport failure; subsequent calls work.
    with _spawn_fake("error_internal") as client:
        with pytest.raises(JsonRpcError):
            client.call("a")
        assert client.is_alive
        with pytest.raises(JsonRpcError):
            client.call("b")  # second call still works


# ── Timeout path ───────────────────────────────────────────────────────────

def test_timeout_kills_process_and_raises():
    # Slow shim sleeps 3s; client timeout 0.3s
    client = _spawn_fake("slow", default_timeout=0.3, FAKE_MCPD_SLEEP="3")
    try:
        t0 = time.monotonic()
        with pytest.raises(McpdTimeoutError):
            client.call("system.status")
        elapsed = time.monotonic() - t0
        # Should be ~0.3s + tiny kill overhead, definitely < 3s
        assert elapsed < 2.0, f"timeout took {elapsed:.2f}s — kill not enforced"
        # Process must be dead and client marked closed.
        assert not client.is_alive
        # Subsequent call raises McpdProcessError.
        with pytest.raises(McpdProcessError):
            client.call("system.status")
    finally:
        client.close()


def test_per_call_timeout_overrides_default():
    client = _spawn_fake("slow", default_timeout=5.0, FAKE_MCPD_SLEEP="2")
    try:
        t0 = time.monotonic()
        with pytest.raises(McpdTimeoutError):
            client.call("system.status", timeout=0.3)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0
    finally:
        client.close()


# ── EOF / protocol violations ──────────────────────────────────────────────

def test_eof_before_response_raises_process_error():
    with pytest.raises(McpdProcessError, match="closed stdout"):
        with _spawn_fake("eof_immediate") as client:
            client.call("anything")


def test_partial_response_then_eof_raises_protocol_error():
    with pytest.raises(McpdProtocolError, match="mid-response"):
        with _spawn_fake("eof_partial") as client:
            client.call("anything")


def test_garbage_response_raises_protocol_error():
    with _spawn_fake("garbage") as client:
        with pytest.raises(McpdProtocolError, match="not valid JSON"):
            client.call("anything")
        # Garbage is a transport-level violation: subsequent reads would
        # also be garbage. The client doesn't automatically close on
        # protocol error; that's the caller's policy decision.


def test_bad_jsonrpc_version_in_response_raises():
    with _spawn_fake("bad_jsonrpc") as client:
        with pytest.raises(McpdProtocolError, match="jsonrpc"):
            client.call("anything")


def test_id_mismatch_raises_protocol_error():
    with _spawn_fake("id_mismatch") as client:
        with pytest.raises(McpdProtocolError, match="id"):
            client.call("anything")


def test_response_with_neither_result_nor_error_raises():
    with _spawn_fake("no_result") as client:
        with pytest.raises(McpdProtocolError, match="result"):
            client.call("anything")


# ── Multi-call durability ──────────────────────────────────────────────────

def test_many_sequential_calls():
    with _spawn_fake("normal") as client:
        for i in range(50):
            result = client.call("system.uptime", {"i": i})
            assert result.result["echo"] == {"i": i}
            assert result.request_id == i + 1


# ── Direct lifecycle / introspection ───────────────────────────────────────

def test_pid_and_is_alive_track_process():
    client = _spawn_fake("normal")
    try:
        assert client.is_alive
        assert isinstance(client.pid, int)
        assert client.pid > 0
    finally:
        client.close()
    assert not client.is_alive
    assert client.pid is None


def test_default_timeout_constant():
    assert DEFAULT_TIMEOUT_SECONDS == 10.0
