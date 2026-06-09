#!/usr/bin/env python3
"""
fake_mcpd.py — Stdio JSON-RPC shim emulating mcpd for unit tests.

NOT a real mcpd. This is a tiny Python program the McpdClient unit tests
spawn instead of the real Rust binary on platforms where mcpd can't run
(e.g. macOS, where Landlock + Linux-only syscalls preclude it).

The shim implements just enough of mcpd's wire protocol to exercise the
McpdClient's transport, framing, timeout, EOF, and error paths:

  - Line-delimited JSON-RPC 2.0 over stdin / stdout
  - Exits 0 on stdin EOF (matches mcpd's server.rs:70-73)
  - Mirrors the same error codes (-32700, -32601, -32602, -32603)

Behavior modes are selected by env var FAKE_MCPD_MODE so a single shim
script can be reused across tests:

  normal              — happy path. Echoes params back as {"echo": <params>}
                        for arbitrary method names. Special-cases:
                          tools/list   → canned 22-tool catalogue
                          fs.delete    → requires_cow_approval envelope
                          system.status → small sample envelope
  slow                — sleeps FAKE_MCPD_SLEEP seconds before every response
                        (drives the timeout test).
  garbage             — writes non-JSON ("not json\n") on every request.
  eof_immediate       — closes stdout after the first request (no response).
  eof_partial         — writes "{\"jsonrpc\":\"2.0\"" (no newline) then exits.
  die_immediate       — exits 0 immediately on startup (before any I/O).
  error_internal      — always returns JSON-RPC -32603 internal error.
  unknown_method      — always returns -32601 method not found.
  invalid_params      — always returns -32602 invalid params.
  bad_jsonrpc         — responds with jsonrpc="1.0" (protocol violation).
  id_mismatch         — responds with id = request_id + 100.
  no_result           — responds with neither result nor error (protocol violation).

Tests spawn this via sys.executable so it runs under the same interpreter.
"""

from __future__ import annotations

import json
import os
import sys
import time

MODE = os.environ.get("FAKE_MCPD_MODE", "normal")


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _send_raw(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def _canned_tools_list() -> dict:
    """Cheap stand-in for the real tools/list response. Only enough fields
    for the McpdClient layer; the classifier catalogue test uses the real
    mcpd via the export script, not this shim."""
    tool_names = [
        "system.status", "system.uptime", "system.cpu", "system.memory", "system.disk",
        "process.list", "process.inspect",
        "fs.read", "fs.list", "fs.stat", "fs.write", "fs.delete",
        "service.start", "service.stop", "service.restart", "service.logs",
        "network.status", "network.dns.read",
        "package.query", "package.install", "package.remove", "package.upgrade",
    ]
    return {
        "schema_version": "1.0.0-fake",
        "tools": [{"name": n, "description": "fake", "params_schema": {}} for n in tool_names],
    }


def _build_result(method: str, params: dict) -> dict:
    if method == "tools/list":
        return _canned_tools_list()
    if method == "system.status":
        return {
            "status": "ok",
            "uptime_seconds": 12345,
            "hostname": "fake-vm",
            "memory_mb": {"total_mb": 16384, "used_mb": 4096},
        }
    if method == "fs.delete":
        return {
            "status": "requires_cow_approval",
            "intent_id": "00000000-0000-0000-0000-000000000001",
            "preview": {"operation": "fs.delete", "path": params.get("path", "")},
        }
    if method == "package.install":
        return {
            "status": "requires_cow_approval",
            "intent_id": "00000000-0000-0000-0000-000000000002",
            "preview": {"operation": "package.install", "package": params.get("package", "")},
        }
    return {"status": "ok", "echo": params}


def main() -> int:
    if MODE == "die_immediate":
        return 0

    sleep_secs = float(os.environ.get("FAKE_MCPD_SLEEP", "0") or "0")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        if MODE == "slow":
            time.sleep(sleep_secs)

        if MODE == "garbage":
            _send_raw("not json\n")
            continue

        if MODE == "eof_immediate":
            # No response — just close stdout.
            sys.stdout.close()
            return 0

        if MODE == "eof_partial":
            _send_raw('{"jsonrpc":"2.0"')  # no newline, no closing brace
            sys.stdout.close()
            return 0

        # All other modes require a parsed request to echo the id.
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue

        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {}) or {}

        if MODE == "error_internal":
            _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": "Internal error: synthetic"}})
            continue
        if MODE == "unknown_method":
            _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})
            continue
        if MODE == "invalid_params":
            _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Invalid params: synthetic"}})
            continue
        if MODE == "bad_jsonrpc":
            _send({"jsonrpc": "1.0", "id": req_id, "result": {"status": "ok"}})
            continue
        if MODE == "id_mismatch":
            _send({"jsonrpc": "2.0", "id": (req_id or 0) + 100, "result": {"status": "ok"}})
            continue
        if MODE == "no_result":
            _send({"jsonrpc": "2.0", "id": req_id})
            continue

        # normal / slow happy path
        _send({"jsonrpc": "2.0", "id": req_id, "result": _build_result(method, params)})

    return 0


if __name__ == "__main__":
    sys.exit(main())
