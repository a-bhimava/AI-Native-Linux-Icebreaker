"""Tests for controller.transport — Transport ABC + UnixSocketTransport."""

from __future__ import annotations

import socket
import threading
import time
from unittest.mock import patch

import pytest

from controller.transport import Transport, TransportClosed, UnixSocketTransport


def _make_pair() -> tuple[UnixSocketTransport, UnixSocketTransport]:
    """Create a connected pair of UnixSocketTransports via socketpair."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return UnixSocketTransport(a), UnixSocketTransport(b)


# ── Basic send/recv ──────────────────────────────────────────────────────


class TestSendRecv:
    def test_roundtrip(self):
        t1, t2 = _make_pair()
        try:
            t1.send(b'{"method":"a"}\n')
            got = t2.recv(timeout=2.0)
            assert got == b'{"method":"a"}\n'
        finally:
            t1.close()
            t2.close()

    def test_multiple_messages(self):
        t1, t2 = _make_pair()
        try:
            t1.send(b'{"a":1}\n')
            t1.send(b'{"b":2}\n')
            assert t2.recv(timeout=2.0) == b'{"a":1}\n'
            assert t2.recv(timeout=2.0) == b'{"b":2}\n'
        finally:
            t1.close()
            t2.close()

    def test_batched_send_split_by_newlines(self):
        t1, t2 = _make_pair()
        try:
            t1.send(b'{"a":1}\n{"b":2}\n')
            assert t2.recv(timeout=2.0) == b'{"a":1}\n'
            assert t2.recv(timeout=2.0) == b'{"b":2}\n'
        finally:
            t1.close()
            t2.close()


# ── Timeout ──────────────────────────────────────────────────────────────


class TestTimeout:
    def test_recv_timeout_returns_none(self):
        t1, t2 = _make_pair()
        try:
            result = t2.recv(timeout=0.05)
            assert result is None
        finally:
            t1.close()
            t2.close()


# ── Peer close / TransportClosed ─────────────────────────────────────────


class TestTransportClosed:
    def test_recv_raises_on_peer_close(self):
        t1, t2 = _make_pair()
        t1.close()
        with pytest.raises(TransportClosed):
            t2.recv(timeout=2.0)

    def test_send_raises_on_peer_close(self):
        t1, t2 = _make_pair()
        t2.close()
        time.sleep(0.01)
        with pytest.raises(TransportClosed):
            t1.send(b'{"a":1}\n')

    def test_send_raises_after_close(self):
        t1, t2 = _make_pair()
        t1.close()
        with pytest.raises(TransportClosed):
            t1.send(b'data\n')
        t2.close()

    def test_recv_raises_after_close(self):
        t1, t2 = _make_pair()
        t1.close()
        with pytest.raises(TransportClosed):
            t1.recv(timeout=0.1)
        t2.close()


# ── is_open ──────────────────────────────────────────────────────────────


class TestIsOpen:
    def test_open_after_create(self):
        t1, t2 = _make_pair()
        assert t1.is_open()
        assert t2.is_open()
        t1.close()
        t2.close()

    def test_closed_after_close(self):
        t1, t2 = _make_pair()
        t1.close()
        assert not t1.is_open()
        t2.close()

    def test_close_idempotent(self):
        t1, t2 = _make_pair()
        t1.close()
        t1.close()
        assert not t1.is_open()
        t2.close()


# ── Partial message buffering ────────────────────────────────────────────


class TestPartialBuffering:
    def test_partial_message_waits_for_newline(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        t1 = UnixSocketTransport(a)
        t2 = UnixSocketTransport(b)
        try:
            a.sendall(b'{"partial":')
            result = t2.recv(timeout=0.05)
            assert result is None

            a.sendall(b'true}\n')
            result = t2.recv(timeout=2.0)
            assert result == b'{"partial":true}\n'
        finally:
            t1.close()
            t2.close()


# ── Peer UID ─────────────────────────────────────────────────────────────


class TestPeerUid:
    def test_peercred_returns_int_or_none(self):
        t1, t2 = _make_pair()
        try:
            uid = t1.peer_uid
            assert uid is None or isinstance(uid, int)
        finally:
            t1.close()
            t2.close()

    def test_peercred_non_linux_returns_none(self):
        t1, t2 = _make_pair()
        try:
            with patch("controller.transport.sys") as mock_sys:
                mock_sys.platform = "darwin"
                fresh = UnixSocketTransport.__new__(UnixSocketTransport)
                fresh._sock = t1._sock
                fresh._lock_send = threading.Lock()
                fresh._lock_recv = threading.Lock()
                fresh._buf = bytearray()
                fresh._closed = False
                fresh._peer_uid = fresh._get_peercred()
                assert fresh._peer_uid is None
        finally:
            t1.close()
            t2.close()


# ── Concurrent send + recv ───────────────────────────────────────────────


class TestConcurrency:
    def test_concurrent_send_and_recv(self):
        t1, t2 = _make_pair()
        results: list[bytes] = []
        errors: list[Exception] = []

        def sender():
            try:
                for i in range(20):
                    t1.send(f'{{"n":{i}}}\n'.encode())
            except Exception as e:
                errors.append(e)

        def receiver():
            try:
                for _ in range(20):
                    msg = t2.recv(timeout=5.0)
                    if msg is not None:
                        results.append(msg)
            except Exception as e:
                errors.append(e)

        send_thread = threading.Thread(target=sender)
        recv_thread = threading.Thread(target=receiver)
        send_thread.start()
        recv_thread.start()
        send_thread.join(timeout=10)
        recv_thread.join(timeout=10)

        t1.close()
        t2.close()

        assert not errors, f"errors: {errors}"
        assert len(results) == 20


# ── ABC contract ─────────────────────────────────────────────────────────


def test_unix_socket_transport_is_transport():
    t1, t2 = _make_pair()
    try:
        assert isinstance(t1, Transport)
    finally:
        t1.close()
        t2.close()
