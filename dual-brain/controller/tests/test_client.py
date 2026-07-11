"""Tests for controller.client — DaemonClient round-trip over AF_UNIX."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from controller.client import ClientRepl, DaemonClient
from controller.protocol import (
    JsonRpcNotification,
    JsonRpcResponse,
    parse_message,
)
from controller.transport import TransportClosed, UnixSocketTransport


def _short_tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="ibc_"))


def _make_server(sock_path: str) -> socket.socket:
    """Create and bind a listening AF_UNIX socket."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    return srv


def _accept_and_wrap(srv: socket.socket, timeout: float = 5.0) -> UnixSocketTransport:
    srv.settimeout(timeout)
    conn, _ = srv.accept()
    return UnixSocketTransport(conn)


# ── Basic connectivity ──────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available")
def test_connect_and_status():
    d = _short_tmp()
    sock_path = str(d / "c.sock")
    srv = _make_server(sock_path)

    def _server():
        transport = _accept_and_wrap(srv)
        try:
            raw = transport.recv(timeout=5.0)
            msg = json.loads(raw)
            resp = JsonRpcResponse(id=msg["id"], result={
                "pid": 12345, "session_id": "abc", "turn_index": 0, "backend": "local",
            })
            transport.send(resp.to_bytes())
        finally:
            transport.close()

    t = threading.Thread(target=_server, daemon=True)
    t.start()

    client = DaemonClient(sock_path)
    try:
        client.connect()
        resp = client.status()
        assert resp["result"]["pid"] == 12345
        assert resp["result"]["backend"] == "local"
    finally:
        client.close()
        srv.close()
    t.join(timeout=5)


# ── Request timeout ─────────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available")
def test_request_timeout():
    d = _short_tmp()
    sock_path = str(d / "c.sock")
    srv = _make_server(sock_path)

    def _server():
        transport = _accept_and_wrap(srv)
        time.sleep(3)
        transport.close()

    t = threading.Thread(target=_server, daemon=True)
    t.start()

    client = DaemonClient(sock_path)
    try:
        client.connect()
        with pytest.raises(TimeoutError):
            client.send_request("daemon.status", timeout=0.2)
    finally:
        client.close()
        srv.close()
    t.join(timeout=5)


# ── Notifications ───────────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available")
def test_notifications_dispatched():
    d = _short_tmp()
    sock_path = str(d / "c.sock")
    srv = _make_server(sock_path)

    received = {"progress": [], "token": [], "info": []}

    class TestClient(DaemonClient):
        def _on_progress(self, params):
            received["progress"].append(params)
        def _on_token(self, params):
            received["token"].append(params)
        def _on_info(self, params):
            received["info"].append(params)

    def _server():
        transport = _accept_and_wrap(srv)
        try:
            notif1 = JsonRpcNotification("turn.progress", {
                "step_name": "qb_intent", "step_label": "Generating...",
                "step_index": 0, "total_steps": 14, "elapsed_ms": 10.0,
            })
            transport.send(notif1.to_bytes())
            notif2 = JsonRpcNotification("turn.token", {
                "token": "hello", "accumulated": "hello", "final": False,
            })
            transport.send(notif2.to_bytes())
            notif3 = JsonRpcNotification("turn.info", {
                "message": "Trust granted.",
            })
            transport.send(notif3.to_bytes())
            time.sleep(0.5)
        finally:
            transport.close()

    t = threading.Thread(target=_server, daemon=True)
    t.start()

    client = TestClient(sock_path)
    try:
        client.connect()
        time.sleep(1)
    finally:
        client.close()
        srv.close()
    t.join(timeout=5)

    assert len(received["progress"]) == 1
    assert received["progress"][0]["step_name"] == "qb_intent"
    assert len(received["token"]) == 1
    assert received["token"][0]["token"] == "hello"
    assert len(received["info"]) >= 1
    assert received["info"][0]["message"] == "Trust granted."


# ── run_turn with streaming ─────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available")
def test_run_turn_receives_result():
    d = _short_tmp()
    sock_path = str(d / "c.sock")
    srv = _make_server(sock_path)

    def _server():
        transport = _accept_and_wrap(srv)
        try:
            raw = transport.recv(timeout=5.0)
            msg = json.loads(raw)
            progress = JsonRpcNotification("turn.progress", {
                "step_name": "qb_intent", "step_label": "Generating...",
                "step_index": 0, "total_steps": 14, "elapsed_ms": 5.0,
            })
            transport.send(progress.to_bytes())
            resp = JsonRpcResponse(id=msg["id"], result={
                "success": True,
                "output": "Done.",
                "outcome": "completed",
                "tier": 0,
                "backend": "local",
                "duration_ms": 150.0,
                "cost_usd": None,
                "tokens_in": 50,
                "tokens_out": 30,
            })
            transport.send(resp.to_bytes())
        finally:
            transport.close()

    t = threading.Thread(target=_server, daemon=True)
    t.start()

    client = DaemonClient(sock_path)
    try:
        client.connect()
        resp = client.run_turn("list files")
        assert resp["result"]["success"] is True
        assert resp["result"]["output"] == "Done."
    finally:
        client.close()
        srv.close()
    t.join(timeout=5)


# ── Connection closed ───────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available")
def test_close_unblocks_pending_request():
    d = _short_tmp()
    sock_path = str(d / "c.sock")
    srv = _make_server(sock_path)

    def _server():
        transport = _accept_and_wrap(srv)
        time.sleep(5)
        transport.close()

    t = threading.Thread(target=_server, daemon=True)
    t.start()

    client = DaemonClient(sock_path)
    result = [None]

    def _requester():
        try:
            result[0] = client.send_request("daemon.status", timeout=10.0)
        except (TransportClosed, TimeoutError):
            result[0] = "closed"

    client.connect()
    req_t = threading.Thread(target=_requester, daemon=True)
    req_t.start()
    time.sleep(0.2)
    client.close()
    req_t.join(timeout=5)
    assert result[0] == "closed"
    srv.close()
    t.join(timeout=5)


# ── session.new ─────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available")
def test_new_session():
    d = _short_tmp()
    sock_path = str(d / "c.sock")
    srv = _make_server(sock_path)

    def _server():
        transport = _accept_and_wrap(srv)
        try:
            raw = transport.recv(timeout=5.0)
            msg = json.loads(raw)
            resp = JsonRpcResponse(id=msg["id"], result={
                "session_id": "new-sess-id", "backend": "gemini",
            })
            transport.send(resp.to_bytes())
        finally:
            transport.close()

    t = threading.Thread(target=_server, daemon=True)
    t.start()

    client = DaemonClient(sock_path)
    try:
        client.connect()
        resp = client.new_session("gemini")
        assert resp["result"]["session_id"] == "new-sess-id"
        assert resp["result"]["backend"] == "gemini"
    finally:
        client.close()
        srv.close()
    t.join(timeout=5)


# ── is_connected property ──────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available")
def test_is_connected():
    d = _short_tmp()
    sock_path = str(d / "c.sock")
    srv = _make_server(sock_path)

    def _server():
        transport = _accept_and_wrap(srv)
        time.sleep(2)
        transport.close()

    t = threading.Thread(target=_server, daemon=True)
    t.start()

    client = DaemonClient(sock_path)
    assert not client.is_connected
    client.connect()
    assert client.is_connected
    client.close()
    assert not client.is_connected
    srv.close()
    t.join(timeout=5)


# ── Multiple requests on one connection ─────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available")
def test_multiple_requests():
    d = _short_tmp()
    sock_path = str(d / "c.sock")
    srv = _make_server(sock_path)

    def _server():
        transport = _accept_and_wrap(srv)
        try:
            for _ in range(3):
                raw = transport.recv(timeout=5.0)
                if raw is None:
                    break
                msg = json.loads(raw)
                resp = JsonRpcResponse(id=msg["id"], result={"ok": True})
                transport.send(resp.to_bytes())
        finally:
            transport.close()

    t = threading.Thread(target=_server, daemon=True)
    t.start()

    client = DaemonClient(sock_path)
    try:
        client.connect()
        for _ in range(3):
            resp = client.send_request("session.reset")
            assert resp["result"]["ok"] is True
    finally:
        client.close()
        srv.close()
    t.join(timeout=5)
