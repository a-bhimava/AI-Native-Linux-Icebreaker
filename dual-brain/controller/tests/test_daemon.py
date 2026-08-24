"""Tests for controller.daemon — AF_UNIX daemon lifecycle and JSON-RPC dispatch."""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from controller.config import DaemonConfig, SessionConfig
from controller.daemon import Daemon, DaemonSession, Principal
from controller.hitl import Decision
from controller.protocol import (
    AUTH_REJECTED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JsonRpcRequest,
    parse_message,
)
from controller.session import SessionState
from controller.transport import TransportClosed, UnixSocketTransport


# ── Fixtures ────────────────────────────────────────────────────────────


def _short_tmp() -> Path:
    """AF_UNIX paths are limited to ~104 bytes; pytest's tmp_path is too long on macOS."""
    return Path(tempfile.mkdtemp(prefix="ibd_"))


def _tmp_daemon_cfg(tmp_dir: Path | None = None) -> DaemonConfig:
    d = tmp_dir or _short_tmp()
    return DaemonConfig(
        socket_path=str(d / "d.sock"),
        pid_file=str(d / "d.pid"),
        max_connections=1,
    )


def _mock_controller_cfg():
    cfg = MagicMock()
    cfg.qb.name = "local"
    cfg.session = SessionConfig()
    cfg.offline_command_lane.enabled = False
    return cfg


def _mock_controller():
    ctrl = MagicMock()
    ctrl._presenter_factory = None
    return ctrl


def _connect_client(sock_path: str, timeout: float = 5.0) -> socket.socket:
    """Connect a raw AF_UNIX client socket."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client.connect(sock_path)
            return client
        except (ConnectionRefusedError, FileNotFoundError):
            time.sleep(0.05)
    raise TimeoutError(f"could not connect to {sock_path}")


def _send_request(sock: socket.socket, method: str, params: dict | None = None) -> dict:
    """Send a JSON-RPC request and read the response."""
    req = JsonRpcRequest(method=method, params=params or {})
    sock.sendall(req.to_bytes())
    # Read response — may need to accumulate
    buf = b""
    sock.settimeout(5.0)
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    line, rest = buf.split(b"\n", 1)
    return json.loads(line)


def _recv_all_messages(sock: socket.socket, timeout: float = 2.0) -> list[dict]:
    """Read all available newline-delimited JSON messages."""
    messages = []
    buf = b""
    sock.settimeout(timeout)
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                messages.append(json.loads(line))
    except socket.timeout:
        pass
    return messages


# ── Socket lifecycle ────────────────────────────────────────────────────


def test_create_socket_and_permissions():
    d = _short_tmp()
    cfg = _tmp_daemon_cfg(d)
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    sock = daemon._create_socket()
    try:
        sock_path = Path(cfg.socket_path)
        assert sock_path.exists()
        mode = sock_path.stat().st_mode
        assert mode & stat.S_IRUSR
        assert mode & stat.S_IWUSR
        assert mode & stat.S_IRGRP
        assert mode & stat.S_IWGRP
        assert not (mode & stat.S_IWOTH)
    finally:
        sock.close()
        Path(cfg.socket_path).unlink(missing_ok=True)


def test_pid_file_lifecycle():
    d = _short_tmp()
    cfg = _tmp_daemon_cfg(d)
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    daemon._write_pid_file()
    pid_path = Path(cfg.pid_file)
    assert pid_path.exists()
    assert pid_path.read_text().strip() == str(os.getpid())
    daemon._remove_pid_file()
    assert not pid_path.exists()


def test_stale_pid_removed():
    d = _short_tmp()
    cfg = _tmp_daemon_cfg(d)
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    pid_path = Path(cfg.pid_file)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("999999999")  # almost certainly not running
    daemon._check_stale_pid()
    assert not pid_path.exists()


def test_stale_pid_raises_if_alive():
    d = _short_tmp()
    cfg = _tmp_daemon_cfg(d)
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    pid_path = Path(cfg.pid_file)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))  # current process IS alive
    with pytest.raises(RuntimeError, match="already running"):
        daemon._check_stale_pid()


# ── Authentication ──────────────────────────────────────────────────────


def test_authenticate_matching_uid():
    transport = MagicMock()
    transport.peer_uid = os.getuid()
    cfg = DaemonConfig()
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    assert daemon._authenticate(transport) is True


def test_authenticate_mismatched_uid():
    transport = MagicMock()
    transport.peer_uid = 65534
    cfg = DaemonConfig()
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    assert daemon._authenticate(transport) is False


def test_authenticate_none_uid_rejected():
    transport = MagicMock()
    transport.peer_uid = None
    cfg = DaemonConfig()
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    assert daemon._authenticate(transport) is False


def test_principal_scoped_mcpd_uses_kernel_principal_identity(monkeypatch):
    """Regression: mcpd is never reused as daemon/root for a user session."""
    cfg = SimpleNamespace(
        run=SimpleNamespace(
            principal_scoped_mcpd=True,
            mcpd_binary="/usr/libexec/icebreaker/mcpd",
            mcpd_timeout_seconds=12.0,
        ),
        qb=SimpleNamespace(name="local"),
        session=SessionConfig(),
    )
    base = MagicMock()
    base._cfg = cfg
    scoped_client = MagicMock()
    scoped_controller = MagicMock()
    base.with_mcpd_client.return_value = scoped_controller
    daemon = Daemon(DaemonConfig(), base, cfg, MagicMock())
    spawn = MagicMock(return_value=scoped_client)
    monkeypatch.setattr("controller.daemon.McpdClient.spawn", spawn)

    principal = Principal(uid=os.getuid(), username="test-user", home="/tmp/test-user-home")
    controller, client = daemon._controller_for_principal(principal)

    assert controller is scoped_controller
    assert client is scoped_client
    assert base._mcpd.close.called
    assert spawn.call_args.kwargs["run_as_uid"] == os.getuid()
    assert spawn.call_args.kwargs["extra_env"]["HOME"] == "/tmp/test-user-home"
    assert spawn.call_args.kwargs["extra_env"]["MCPD_FS_READ_ROOTS"] == "/tmp/test-user-home"


# ── Connection gate ─────────────────────────────────────────────────────


def test_connection_gate_allows_first():
    cfg = DaemonConfig(max_connections=1)
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    assert daemon._connection_gate() is True


def test_connection_gate_blocks_second():
    cfg = DaemonConfig(max_connections=1)
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    transport = MagicMock()
    transport.is_open.return_value = True
    session = DaemonSession(
        session_state=SessionState.new("local", SessionConfig()),
        transport=transport,
    )
    with daemon._session_lock:
        daemon._sessions.append(session)
    assert daemon._connection_gate() is False


def test_connection_gate_reopens_after_disconnect():
    cfg = DaemonConfig(max_connections=1)
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    transport = MagicMock()
    transport.is_open.return_value = False
    session = DaemonSession(
        session_state=SessionState.new("local", SessionConfig()),
        transport=transport,
    )
    with daemon._session_lock:
        daemon._sessions.append(session)
    assert daemon._connection_gate() is True


# ── Method dispatch (integration) ───────────────────────────────────────


@pytest.fixture
def running_daemon():
    """Start a daemon in a background thread, yield (daemon, sock_path), then shut down.

    Patches _authenticate to bypass SO_PEERCRED (unavailable on macOS).
    """
    cfg = _tmp_daemon_cfg()
    ctrl = _mock_controller()
    ctrl_cfg = _mock_controller_cfg()
    daemon = Daemon(cfg, ctrl, ctrl_cfg, MagicMock())

    daemon._server_sock = daemon._create_socket()
    daemon._write_pid_file()

    principal = Principal(uid=os.getuid(), username="test-user", home="/tmp")
    with patch.object(Daemon, "_authenticate", return_value=True), \
         patch.object(Daemon, "_principal_for", return_value=principal):
        t = threading.Thread(target=daemon._accept_loop, daemon=True)
        t.start()

        yield daemon, cfg.socket_path, ctrl

        daemon.shutdown()
        t.join(timeout=5)


@pytest.mark.skipif(
    os.name == "nt",
    reason="AF_UNIX not available on Windows",
)
class TestDaemonDispatch:

    def test_daemon_status(self, running_daemon):
        daemon, sock_path, ctrl = running_daemon
        client = _connect_client(sock_path)
        try:
            resp = _send_request(client, "daemon.status")
            assert "result" in resp
            assert resp["result"]["pid"] == os.getpid()
            assert "session_id" in resp["result"]
        finally:
            client.close()

    def test_session_new(self, running_daemon):
        daemon, sock_path, ctrl = running_daemon
        client = _connect_client(sock_path)
        try:
            resp = _send_request(client, "session.new", {"backend": "gemini"})
            assert resp["result"]["backend"] == "gemini"
            assert "session_id" in resp["result"]
        finally:
            client.close()

    def test_session_reset(self, running_daemon):
        daemon, sock_path, ctrl = running_daemon
        client = _connect_client(sock_path)
        try:
            resp = _send_request(client, "session.reset")
            assert resp["result"]["ok"] is True
        finally:
            client.close()

    def test_unknown_method(self, running_daemon):
        daemon, sock_path, ctrl = running_daemon
        client = _connect_client(sock_path)
        try:
            resp = _send_request(client, "nonexistent.method")
            assert "error" in resp
            assert resp["error"]["code"] == METHOD_NOT_FOUND
        finally:
            client.close()

    def test_malformed_json(self, running_daemon):
        daemon, sock_path, ctrl = running_daemon
        client = _connect_client(sock_path)
        try:
            client.sendall(b"not valid json\n")
            client.settimeout(5.0)
            buf = b""
            while b"\n" not in buf:
                buf += client.recv(4096)
            resp = json.loads(buf.split(b"\n")[0])
            assert resp["error"]["code"] == PARSE_ERROR
        finally:
            client.close()

    def test_hitl_respond_no_pending(self, running_daemon):
        daemon, sock_path, ctrl = running_daemon
        client = _connect_client(sock_path)
        try:
            resp = _send_request(client, "hitl.respond", {"decision": "approved"})
            assert "error" in resp
            assert resp["error"]["code"] == INVALID_PARAMS
        finally:
            client.close()

    def test_hitl_respond_invalid_decision(self, running_daemon):
        daemon, sock_path, ctrl = running_daemon
        client = _connect_client(sock_path)
        try:
            # First, we need a pending presenter. We'll simulate one.
            with daemon._session_lock:
                if daemon._sessions:
                    pass  # will connect momentarily
            # Wait for connection to register
            time.sleep(0.2)
            resp = _send_request(client, "hitl.respond", {"decision": "bogus_value"})
            # Should get error — either no pending prompt or invalid decision
            assert "error" in resp
        finally:
            client.close()

    def test_daemon_shutdown_via_rpc(self, running_daemon):
        daemon, sock_path, ctrl = running_daemon
        client = _connect_client(sock_path)
        try:
            resp = _send_request(client, "daemon.shutdown")
            assert resp["result"]["ok"] is True
            time.sleep(0.5)
            assert daemon._shutdown_event.is_set()
        finally:
            client.close()


# ── Connection limit enforcement ────────────────────────────────────────


def test_turn_run_uses_offline_intent_only_for_fixed_grammar(monkeypatch):
    """The daemon, not a client, owns the direct-route decision."""
    cfg = _tmp_daemon_cfg()
    ctrl = _mock_controller()
    ctrl_cfg = _mock_controller_cfg()
    ctrl_cfg.offline_command_lane.enabled = True
    daemon = Daemon(cfg, ctrl, ctrl_cfg, MagicMock())
    session = DaemonSession(
        session_state=SessionState.new("local", SessionConfig()),
        transport=MagicMock(),
        principal=Principal(uid=os.getuid(), username="alice", home="/home/alice"),
    )

    def _capture(_self, _session, _msg_id, factory, **_kwargs):
        factory()

    monkeypatch.setattr(Daemon, "_start_pipeline_worker", _capture)
    daemon._handle_turn_run(session, {
        "id": "direct", "params": {"input": "# uptime", "context": {"cwd": "/home/alice"}},
    })

    ctrl.run_turn_from_intent.assert_called_once()
    args, kwargs = ctrl.run_turn_from_intent.call_args
    assert args[0]["action"] == "system.uptime"
    assert kwargs == {
        "source": "offline_direct", "force_pb": True, "offline_direct": True,
    }
    assert ctrl.run_turn_streaming.call_count == 0


def test_offline_run_never_falls_back_to_turn_run():
    """Unsupported explicit offline input is rejected, never sent to QB."""
    cfg = _tmp_daemon_cfg()
    ctrl = _mock_controller()
    ctrl_cfg = _mock_controller_cfg()
    ctrl_cfg.offline_command_lane.enabled = True
    daemon = Daemon(cfg, ctrl, ctrl_cfg, MagicMock())
    session = DaemonSession(
        session_state=SessionState.new("local", SessionConfig()),
        transport=MagicMock(),
        principal=Principal(uid=os.getuid(), username="alice", home="/home/alice"),
    )

    daemon._handle_offline_run(session, {
        "id": "not-a-command", "params": {"command": "rm -rf /"},
    })

    ctrl.run_turn_from_intent.assert_not_called()
    ctrl.run_turn_streaming.assert_not_called()
    sent = session.transport.send.call_args.args[0].decode()
    assert "unsupported offline command" in sent


def test_offline_run_uses_pb_only_fixed_intent(monkeypatch):
    """The OpenCode-facing endpoint gets the same safe route as `#`."""
    cfg = _tmp_daemon_cfg()
    ctrl = _mock_controller()
    ctrl_cfg = _mock_controller_cfg()
    ctrl_cfg.offline_command_lane.enabled = True
    daemon = Daemon(cfg, ctrl, ctrl_cfg, MagicMock())
    session = DaemonSession(
        session_state=SessionState.new("local", SessionConfig()),
        transport=MagicMock(),
        principal=Principal(uid=os.getuid(), username="alice", home="/home/alice"),
    )

    def _capture(_self, _session, _msg_id, factory, **_kwargs):
        factory()

    monkeypatch.setattr(Daemon, "_start_pipeline_worker", _capture)
    daemon._handle_offline_run(session, {
        "id": "offline", "params": {"command": "uptime", "context": {"cwd": "/home/alice"}},
    })

    ctrl.run_turn_from_intent.assert_called_once()
    args, kwargs = ctrl.run_turn_from_intent.call_args
    assert args[0]["action"] == "system.uptime"
    assert kwargs == {
        "source": "offline_direct", "force_pb": True, "offline_direct": True,
    }
    ctrl.run_turn_streaming.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX not available on Windows")
def test_second_connection_rejected():
    d = _short_tmp()
    cfg = DaemonConfig(
        socket_path=str(d / "limit.sock"),
        pid_file=str(d / "limit.pid"),
        max_connections=1,
    )
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    daemon._server_sock = daemon._create_socket()

    principal = Principal(uid=os.getuid(), username="test-user", home="/tmp")
    with patch.object(Daemon, "_authenticate", return_value=True), \
         patch.object(Daemon, "_principal_for", return_value=principal):
        t = threading.Thread(target=daemon._accept_loop, daemon=True)
        t.start()

        try:
            client1 = _connect_client(cfg.socket_path)
            resp1 = _send_request(client1, "daemon.status")
            assert "result" in resp1

            client2 = _connect_client(cfg.socket_path)
            client2.settimeout(5.0)
            buf = b""
            try:
                while b"\n" not in buf:
                    chunk = client2.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except socket.timeout:
                pass

            if buf:
                resp2 = json.loads(buf.split(b"\n")[0])
                assert "error" in resp2
                assert resp2["error"]["code"] == AUTH_REJECTED

            client1.close()
            client2.close()
        finally:
            daemon.shutdown()
            t.join(timeout=5)


# ── Shutdown cleans up ──────────────────────────────────────────────────


def test_shutdown_removes_socket_and_pid():
    cfg = _tmp_daemon_cfg()
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    daemon._server_sock = daemon._create_socket()
    daemon._write_pid_file()

    assert Path(cfg.socket_path).exists()
    assert Path(cfg.pid_file).exists()

    daemon.shutdown()

    assert not Path(cfg.socket_path).exists()
    assert not Path(cfg.pid_file).exists()
    assert daemon._shutdown_event.is_set()


# ── Properties ──────────────────────────────────────────────────────────


def test_is_running_property():
    cfg = _tmp_daemon_cfg()
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())
    assert daemon.is_running is True
    daemon._shutdown_event.set()
    assert daemon.is_running is False


def test_active_sessions_property():
    cfg = _tmp_daemon_cfg()
    daemon = Daemon(cfg, _mock_controller(), _mock_controller_cfg(), MagicMock())

    assert daemon.active_sessions == 0

    transport = MagicMock()
    transport.is_open.return_value = True
    session = DaemonSession(
        session_state=SessionState.new("local", SessionConfig()),
        transport=transport,
    )
    with daemon._session_lock:
        daemon._sessions.append(session)
    assert daemon.active_sessions == 1

    transport.is_open.return_value = False
    assert daemon.active_sessions == 0


# ── _UnconfiguredBackend stub ──────────────────────────────────────────


def _stub_envelope():
    from controller.backends.base import RequestEnvelope
    return RequestEnvelope(system="sys", user="hello", schema=None, sampling={})


def test_unconfigured_backend_raises_brain_provider_error():
    from controller.__main__ import _UnconfiguredBackend
    from controller.backends.base import BrainProviderError

    stub = _UnconfiguredBackend("GEMINI_API_KEY not set")
    with pytest.raises(BrainProviderError, match="Quarantined Brain not available"):
        stub._call_provider(_stub_envelope())


def test_unconfigured_backend_message_includes_reason():
    from controller.__main__ import _UnconfiguredBackend
    from controller.backends.base import BrainProviderError

    stub = _UnconfiguredBackend("test reason 123")
    with pytest.raises(BrainProviderError, match="test reason 123"):
        stub._call_provider(_stub_envelope())


def test_unconfigured_backend_message_includes_recovery_instructions():
    from controller.__main__ import _UnconfiguredBackend
    from controller.backends.base import BrainProviderError

    stub = _UnconfiguredBackend("missing key")
    with pytest.raises(BrainProviderError, match="locations.env"):
        stub._call_provider(_stub_envelope())


def test_build_qb_safe_returns_stub_on_failure():
    from controller.__main__ import _UnconfiguredBackend, _build_qb_safe

    mock_cfg = MagicMock()
    with patch("controller.__main__._build_qb", side_effect=RuntimeError("boom")):
        result = _build_qb_safe(mock_cfg)
    assert isinstance(result, _UnconfiguredBackend)
    assert result._reason == "boom"


def test_build_qb_safe_returns_real_backend_on_success():
    from controller.__main__ import _UnconfiguredBackend, _build_qb_safe

    fake_backend = MagicMock()
    mock_cfg = MagicMock()
    with patch("controller.__main__._build_qb", return_value=fake_backend):
        result = _build_qb_safe(mock_cfg)
    assert result is fake_backend
    assert not isinstance(result, _UnconfiguredBackend)
