"""v6.17 M7.6a-1b — tests for the intent.run JSON-RPC method.

Covers the daemon-side surface added by M7.6a-1b: the intent.run RPC
that receives a pre-formed intent from the submit_intent MCP tool and
drives Controller.run_turn_from_intent.

Tests deliberately do NOT depend on M7.6a-1c (Controller.run_turn_from_intent)
being implemented — they either mock the method on the Controller or verify
the graceful-error behavior when the method is missing.

Uses the same test scaffolding as test_daemon.py: real Daemon on a temp
AF_UNIX socket, mock Controller, real client connections.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from controller.config import DaemonConfig, SessionConfig
from controller.daemon import Daemon
from controller.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    JsonRpcRequest,
)
from controller.turn_events import (
    CotEvent,
    ProgressEvent,
    ResultEvent,
)
from controller.main import TurnResult
from controller.audit import Outcome


# ── Fixtures (mirror test_daemon.py conventions) ─────────────────────


def _short_tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="ibd_intent_"))


def _tmp_daemon_cfg(tmp_dir: Path | None = None) -> DaemonConfig:
    d = tmp_dir or _short_tmp()
    return DaemonConfig(
        socket_path=str(d / "d.sock"),
        pid_file=str(d / "d.pid"),
        max_connections=1,
    )


def _mock_controller_cfg():
    cfg = MagicMock()
    cfg.qb.name = "opencode_oc"  # simulate OC edition
    cfg.session = SessionConfig()
    return cfg


def _mock_controller():
    """Bare-bones mock Controller. Tests either assign
    `run_turn_from_intent` explicitly or leave it absent to test the
    graceful-error path."""
    ctrl = MagicMock(spec=[
        "_presenter_factory", "_cfg",
    ])
    ctrl._presenter_factory = None
    ctrl._cfg = MagicMock()
    ctrl._cfg.session = SessionConfig()
    return ctrl


def _mock_controller_with_intent_impl(events):
    """Mock Controller with run_turn_from_intent that yields the given
    events. Used by happy-path + streaming tests."""
    ctrl = _mock_controller()

    def _run_from_intent(intent, session_state):
        for ev in events:
            yield ev

    ctrl.run_turn_from_intent = _run_from_intent
    return ctrl


def _connect_client(sock_path: str, timeout: float = 5.0) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client.connect(sock_path)
            return client
        except (ConnectionRefusedError, FileNotFoundError):
            time.sleep(0.05)
    raise TimeoutError(f"could not connect to {sock_path}")


def _send_request(sock: socket.socket, method: str, params: dict | None = None,
                  request_id: str = "req-1") -> str:
    req = JsonRpcRequest(method=method, params=params or {}, id=request_id)
    sock.sendall(req.to_bytes())
    return req.id


def _recv_until_response(sock: socket.socket, request_id: str,
                        timeout: float = 3.0) -> tuple[dict, list[dict]]:
    """Read messages until we see the response matching request_id.
    Returns (response, notifications_before_response)."""
    notifications: list[dict] = []
    buf = b""
    sock.settimeout(timeout)
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            msg = json.loads(line)
            if "method" in msg and "id" not in msg:
                notifications.append(msg)
            elif msg.get("id") == request_id:
                return msg, notifications


@pytest.fixture
def running_daemon_with_controller():
    """Yields (daemon, sock_path, controller_setter) — the setter lets
    each test install its own Controller into the daemon before
    connecting."""
    cfg = _tmp_daemon_cfg()
    ctrl_cfg = _mock_controller_cfg()
    # Start with a minimal controller; tests replace via setter
    initial_ctrl = _mock_controller()
    daemon = Daemon(cfg, initial_ctrl, ctrl_cfg, MagicMock())

    daemon._server_sock = daemon._create_socket()
    daemon._write_pid_file()

    def _set_controller(new_ctrl):
        daemon._controller = new_ctrl

    with patch.object(Daemon, "_authenticate", return_value=True):
        t = threading.Thread(target=daemon._accept_loop, daemon=True)
        t.start()
        yield daemon, cfg.socket_path, _set_controller
        daemon.shutdown()
        t.join(timeout=5)


# ── 1. Dispatch registration ─────────────────────────────────────────

def test_intent_run_registered_in_dispatch_table():
    """intent.run must be in Daemon._METHODS pointing at
    _handle_intent_run — the dispatcher won't route the RPC otherwise."""
    assert "intent.run" in Daemon._METHODS
    assert Daemon._METHODS["intent.run"] == "_handle_intent_run"
    assert hasattr(Daemon, "_handle_intent_run")


# ── 2. Params validation ─────────────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available on Windows")
class TestIntentRunParamsValidation:

    def test_intent_run_missing_intent_returns_invalid_params(
        self, running_daemon_with_controller,
    ):
        daemon, sock_path, _ = running_daemon_with_controller
        client = _connect_client(sock_path)
        try:
            req_id = _send_request(client, "intent.run", {"session_id": "s1"})
            resp, _ = _recv_until_response(client, req_id)
            assert "error" in resp
            assert resp["error"]["code"] == INVALID_PARAMS
            assert "intent" in resp["error"]["message"].lower()
        finally:
            client.close()

    def test_intent_run_empty_intent_returns_invalid_params(
        self, running_daemon_with_controller,
    ):
        daemon, sock_path, _ = running_daemon_with_controller
        client = _connect_client(sock_path)
        try:
            req_id = _send_request(client, "intent.run",
                                  {"session_id": "s1", "intent": {}})
            resp, _ = _recv_until_response(client, req_id)
            assert "error" in resp
            assert resp["error"]["code"] == INVALID_PARAMS
        finally:
            client.close()

    def test_intent_run_non_dict_intent_returns_invalid_params(
        self, running_daemon_with_controller,
    ):
        daemon, sock_path, _ = running_daemon_with_controller
        client = _connect_client(sock_path)
        try:
            req_id = _send_request(client, "intent.run",
                                  {"session_id": "s1", "intent": "not-a-dict"})
            resp, _ = _recv_until_response(client, req_id)
            assert "error" in resp
            assert resp["error"]["code"] == INVALID_PARAMS
        finally:
            client.close()

    def test_intent_run_missing_session_id_returns_invalid_params(
        self, running_daemon_with_controller,
    ):
        daemon, sock_path, _ = running_daemon_with_controller
        client = _connect_client(sock_path)
        try:
            req_id = _send_request(client, "intent.run",
                                  {"intent": {"action": "fs.list"}})
            resp, _ = _recv_until_response(client, req_id)
            assert "error" in resp
            assert resp["error"]["code"] == INVALID_PARAMS
            assert "session_id" in resp["error"]["message"].lower()
        finally:
            client.close()

    def test_intent_run_empty_session_id_returns_invalid_params(
        self, running_daemon_with_controller,
    ):
        daemon, sock_path, _ = running_daemon_with_controller
        client = _connect_client(sock_path)
        try:
            req_id = _send_request(client, "intent.run",
                                  {"intent": {"action": "fs.list"},
                                   "session_id": ""})
            resp, _ = _recv_until_response(client, req_id)
            assert "error" in resp
            assert resp["error"]["code"] == INVALID_PARAMS
        finally:
            client.close()


# ── 3. Graceful error when Controller.run_turn_from_intent missing ────

@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available on Windows")
def test_intent_run_returns_internal_error_when_controller_method_missing(
    running_daemon_with_controller,
):
    """Before M7.6a-1c ships Controller.run_turn_from_intent, the
    intent.run handler must degrade gracefully — return INTERNAL_ERROR
    with a clear message rather than crashing the worker thread. This
    also protects against a config with an older Controller build.
    """
    daemon, sock_path, set_controller = running_daemon_with_controller
    # Controller with NO run_turn_from_intent method
    ctrl = _mock_controller()
    assert not hasattr(ctrl, "run_turn_from_intent")
    set_controller(ctrl)

    client = _connect_client(sock_path)
    try:
        req_id = _send_request(client, "intent.run", {
            "session_id": "s1",
            "intent": {"action": "fs.list", "reason": "user_requested",
                       "risk_level": "read_only"},
        })
        resp, _ = _recv_until_response(client, req_id)
        assert "error" in resp
        assert resp["error"]["code"] == INTERNAL_ERROR
        assert "run_turn_from_intent" in resp["error"]["message"]
        assert "M7.6a-1c" in resp["error"]["message"]
    finally:
        client.close()


# ── 4. Happy path: mock Controller yields ResultEvent → RPC response ───

@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available on Windows")
def test_intent_run_happy_path_returns_result_response(
    running_daemon_with_controller,
):
    daemon, sock_path, set_controller = running_daemon_with_controller

    # Build a synthetic ResultEvent — same shape as Controller emits.
    turn_result = TurnResult(
        success=True,
        output="Listed 5 files in /tmp",
        outcome=Outcome.EXECUTED,
        tier=0,
        backend="opencode_oc",
        duration_ms=42.0,
        cost_usd=0.001,
        tokens_in=100,
        tokens_out=50,
    )
    events = [ResultEvent(result=turn_result)]
    ctrl = _mock_controller_with_intent_impl(events)
    set_controller(ctrl)

    client = _connect_client(sock_path)
    try:
        req_id = _send_request(client, "intent.run", {
            "session_id": "s1",
            "intent": {"action": "fs.list", "target": "/tmp",
                       "reason": "user_requested", "risk_level": "read_only"},
        })
        resp, notifs = _recv_until_response(client, req_id)
        assert "result" in resp, f"expected result, got {resp}"
        assert resp["result"]["success"] is True
        assert resp["result"]["output"] == "Listed 5 files in /tmp"
        assert resp["result"]["outcome"] == "executed"
        assert resp["result"]["tier"] == 0
    finally:
        client.close()


# ── 5. Streaming: notifications precede response ─────────────────────

@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available on Windows")
def test_intent_run_streams_progress_and_cot_events(
    running_daemon_with_controller,
):
    daemon, sock_path, set_controller = running_daemon_with_controller

    events = [
        ProgressEvent(step_name="schema_validation", step_label="Validating",
                     step_index=1, total_steps=11, elapsed_ms=5.0),
        CotEvent(step_index=2, step_name="risk_classification",
                step_state="done", heading="Risk", body="Tier 0",
                data={}, timestamp_ms=0.0),
        ResultEvent(result=TurnResult(
            success=True, output="ok", outcome=Outcome.EXECUTED,
            tier=0, backend="opencode_oc", duration_ms=10.0,
        )),
    ]
    ctrl = _mock_controller_with_intent_impl(events)
    set_controller(ctrl)

    client = _connect_client(sock_path)
    try:
        req_id = _send_request(client, "intent.run", {
            "session_id": "s1",
            "intent": {"action": "fs.list", "reason": "user_requested",
                       "risk_level": "read_only"},
        })
        resp, notifs = _recv_until_response(client, req_id)
        assert "result" in resp
        # Both notifications should arrive before the response.
        methods = [n["method"] for n in notifs]
        assert "turn.progress" in methods, f"got {methods}"
        assert "turn.cot" in methods, f"got {methods}"
    finally:
        client.close()


# ── 6. Turn-lock contention ──────────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available on Windows")
def test_intent_run_second_call_while_first_active_returns_internal_error(
    running_daemon_with_controller,
):
    """If a turn is already in progress on this session, intent.run must
    return INTERNAL_ERROR ('turn already in progress') — same discipline
    as turn.run. Belt-and-braces against opencode double-dispatching."""
    daemon, sock_path, set_controller = running_daemon_with_controller

    # Controller that BLOCKS in the middle of yielding — event iterator
    # holds the turn lock until we release.
    release_event = threading.Event()

    def _slow_run(intent, session_state):
        yield ProgressEvent(step_name="pb_tool_call", step_label="PB",
                           step_index=6, total_steps=11, elapsed_ms=5.0)
        # Block until the test signals us to proceed.
        release_event.wait(timeout=5.0)
        yield ResultEvent(result=TurnResult(
            success=True, output="done", outcome=Outcome.EXECUTED,
            tier=0, backend="opencode_oc",
        ))

    ctrl = _mock_controller()
    ctrl.run_turn_from_intent = _slow_run
    set_controller(ctrl)

    # First client: start a turn that will block on release_event.
    client1 = _connect_client(sock_path)
    req_id_1 = _send_request(client1, "intent.run", {
        "session_id": "s1",
        "intent": {"action": "fs.list", "reason": "user_requested",
                   "risk_level": "read_only"},
    }, request_id="req-first")
    # Wait for the progress notification to prove the first turn is running.
    time.sleep(0.2)

    # Try to start a SECOND turn on the same connection while the first
    # is still holding the turn lock.
    req_id_2 = _send_request(client1, "intent.run", {
        "session_id": "s2",
        "intent": {"action": "fs.list", "reason": "user_requested",
                   "risk_level": "read_only"},
    }, request_id="req-second")

    try:
        resp2, _ = _recv_until_response(client1, req_id_2, timeout=3.0)
        assert "error" in resp2
        assert resp2["error"]["code"] == INTERNAL_ERROR
        assert "in progress" in resp2["error"]["message"].lower()
    finally:
        # Release the first turn + collect its response so cleanup is clean.
        release_event.set()
        try:
            _recv_until_response(client1, req_id_1, timeout=3.0)
        except Exception:
            pass
        client1.close()


# ── 7. INV-1 guard: intent kwargs forwarded verbatim ─────────────────

@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available on Windows")
def test_intent_run_forwards_intent_verbatim_to_controller(
    running_daemon_with_controller,
):
    """The intent dict opencode sends via submit_intent must reach
    Controller.run_turn_from_intent exactly as passed — no fields
    added, dropped, or mutated at the transport layer. Regression lock
    against future refactors that "helpfully" pre-fill intent fields."""
    daemon, sock_path, set_controller = running_daemon_with_controller

    captured: dict = {}

    def _capture_run(intent, session_state):
        captured["intent"] = intent
        yield ResultEvent(result=TurnResult(
            success=True, output="captured", outcome=Outcome.EXECUTED,
            tier=0, backend="opencode_oc",
        ))

    ctrl = _mock_controller()
    ctrl.run_turn_from_intent = _capture_run
    set_controller(ctrl)

    original_intent = {
        "action": "package.install",
        "target": "htop",
        "params": {"package": "htop"},
        "pb_hint": "put package name in params.package",
        "reason": "user_requested",
        "risk_level": "high",
    }
    client = _connect_client(sock_path)
    try:
        req_id = _send_request(client, "intent.run", {
            "session_id": "s1",
            "intent": original_intent,
        })
        resp, _ = _recv_until_response(client, req_id)
        assert "result" in resp
    finally:
        client.close()

    # Controller must have received the intent EXACTLY as sent.
    assert captured["intent"] == original_intent
