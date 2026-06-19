"""Integration tests for RPA Bridge (PR #28)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


_FAKE_BRIDGE = os.path.join(
    os.path.dirname(__file__), "fake_rpa_bridge.py",
)


def _send_recv(proc, request: dict) -> dict:
    """Send a JSON-RPC request and read the response."""
    line = json.dumps(request) + "\n"
    proc.stdin.write(line)
    proc.stdin.flush()
    resp_line = proc.stdout.readline()
    return json.loads(resp_line)


class TestBridgePing:
    def test_fake_bridge_responds_to_ping(self):
        proc = subprocess.Popen(
            [sys.executable, _FAKE_BRIDGE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "FAKE_RPA_BRIDGE_MODE": "normal"},
        )
        try:
            resp = _send_recv(proc, {
                "jsonrpc": "2.0", "id": 1, "method": "rpa.ping", "params": {},
            })
            assert resp["result"]["status"] == "ok"
            assert resp["result"]["robot_framework_available"] is True
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)


class TestWorkflowExecution:
    def test_three_keyword_workflow(self):
        proc = subprocess.Popen(
            [sys.executable, _FAKE_BRIDGE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "FAKE_RPA_BRIDGE_MODE": "normal"},
        )
        try:
            resp = _send_recv(proc, {
                "jsonrpc": "2.0", "id": 2,
                "method": "rpa.execute_workflow",
                "params": {
                    "keywords": [
                        {"name": "Click Element", "args": ["id=btn"]},
                        {"name": "Input Text", "args": ["id=field", "hello"]},
                        {"name": "Click Element", "args": ["id=submit"]},
                    ],
                    "workflow_name": "test_flow",
                },
            })
            result = resp["result"]
            assert result["success"] is True
            assert result["timed_out"] is False
            assert result["keywords_executed"] == 3
            assert result["keywords_total"] == 3
            assert len(result["keyword_results"]) == 3
            for kr in result["keyword_results"]:
                assert kr["status"] == "pass"
                assert kr["screenshot_hash"]
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)


class TestTimeout:
    def test_timeout_returns_partial_results(self):
        proc = subprocess.Popen(
            [sys.executable, _FAKE_BRIDGE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "FAKE_RPA_BRIDGE_MODE": "timeout"},
        )
        try:
            resp = _send_recv(proc, {
                "jsonrpc": "2.0", "id": 3,
                "method": "rpa.execute_workflow",
                "params": {
                    "keywords": [
                        {"name": "Click Element", "args": ["id=a"]},
                        {"name": "Click Element", "args": ["id=b"]},
                        {"name": "Click Element", "args": ["id=c"]},
                        {"name": "Click Element", "args": ["id=d"]},
                        {"name": "Click Element", "args": ["id=e"]},
                    ],
                    "timeout_seconds": 2,
                },
            })
            result = resp["result"]
            assert result["timed_out"] is True
            assert result["success"] is False
            assert result["keywords_executed"] < result["keywords_total"]
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)

    def test_keyword_error_returns_failure(self):
        proc = subprocess.Popen(
            [sys.executable, _FAKE_BRIDGE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "FAKE_RPA_BRIDGE_MODE": "keyword_error"},
        )
        try:
            resp = _send_recv(proc, {
                "jsonrpc": "2.0", "id": 4,
                "method": "rpa.execute_workflow",
                "params": {
                    "keywords": [
                        {"name": "Click Element", "args": ["id=a"]},
                        {"name": "Click Element", "args": ["id=b"]},
                        {"name": "Click Element", "args": ["id=c"]},
                    ],
                },
            })
            result = resp["result"]
            assert result["success"] is False
            assert result["timed_out"] is False
            failed = [kr for kr in result["keyword_results"] if kr["status"] == "fail"]
            assert len(failed) >= 1
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)


class TestSandboxFailure:
    def test_sandbox_fail_exits_nonzero(self):
        proc = subprocess.Popen(
            [sys.executable, _FAKE_BRIDGE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "FAKE_RPA_BRIDGE_MODE": "sandbox_fail"},
        )
        rc = proc.wait(timeout=5)
        assert rc == 1
        stderr = proc.stderr.read()
        assert "sandbox failed" in stderr
