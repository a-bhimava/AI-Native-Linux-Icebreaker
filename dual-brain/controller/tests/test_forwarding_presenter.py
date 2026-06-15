"""Tests for controller.forwarding_presenter — HITL over JSON-RPC."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from controller.forwarding_presenter import ForwardingPresenter
from controller.hitl import Decision, HitlDisplayData, HitlPresenter
from controller.risk_classifier import Tier
from controller.transport import TransportClosed, UnixSocketTransport


def _make_pair() -> tuple[UnixSocketTransport, UnixSocketTransport]:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return UnixSocketTransport(a), UnixSocketTransport(b)


def _sample_data() -> HitlDisplayData:
    return HitlDisplayData(
        action="fs.write",
        target="/home/user/test.txt",
        tier=Tier.MEDIUM,
        risk_level="medium",
        reversible=True,
        backend="local",
        reason="user requested",
        blocked_pattern=None,
        cow_summary=None,
    )


# ── ABC contract ─────────────────────────────────────────────────────────


def test_is_hitl_presenter():
    server, client = _make_pair()
    try:
        fp = ForwardingPresenter(server)
        assert isinstance(fp, HitlPresenter)
    finally:
        server.close()
        client.close()


# ── show_prompt sends notification ───────────────────────────────────────


def test_show_prompt_sends_hitl_prompt_notification():
    server, client = _make_pair()
    try:
        fp = ForwardingPresenter(server)
        fp.show_prompt(_sample_data())

        raw = client.recv(timeout=2.0)
        assert raw is not None
        msg = json.loads(raw)
        assert msg["method"] == "hitl.prompt"
        assert msg["params"]["action"] == "fs.write"
        assert msg["params"]["target"] == "/home/user/test.txt"
        assert msg["params"]["risk_level"] == "medium"
        assert msg["params"]["reversible"] is True
        assert "id" not in msg
    finally:
        server.close()
        client.close()


def test_show_prompt_includes_cow_summary():
    server, client = _make_pair()
    try:
        data = HitlDisplayData(
            action="fs.delete", target="/tmp/x", tier=Tier.HIGH,
            risk_level="high", reversible=False, backend="local",
            reason="cleanup", blocked_pattern=None,
            cow_summary="- /tmp/x will be deleted",
        )
        fp = ForwardingPresenter(server)
        fp.show_prompt(data)

        msg = json.loads(client.recv(timeout=2.0))
        assert msg["params"]["cow_summary"] == "- /tmp/x will be deleted"
    finally:
        server.close()
        client.close()


# ── lockout sends notification and sleeps ────────────────────────────────


def test_lockout_sends_notification_and_sleeps():
    server, client = _make_pair()
    try:
        fp = ForwardingPresenter(server)
        t0 = time.monotonic()
        fp.lockout(1)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.9

        raw = client.recv(timeout=2.0)
        msg = json.loads(raw)
        assert msg["method"] == "hitl.lockout"
        assert msg["params"]["seconds"] == 1
    finally:
        server.close()
        client.close()


# ── read_decision blocks until receive_decision ─────────────────────────


def test_read_decision_blocks_until_receive_decision():
    server, client = _make_pair()
    try:
        fp = ForwardingPresenter(server)
        result = [None]

        def reader():
            result[0] = fp.read_decision(timeout_seconds=5)

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.1)
        fp.receive_decision(Decision.APPROVED)
        t.join(timeout=5)

        assert result[0] == Decision.APPROVED
        assert fp.last_key_class == "remote"
    finally:
        server.close()
        client.close()


def test_read_decision_timeout_returns_timeout():
    server, client = _make_pair()
    try:
        fp = ForwardingPresenter(server)
        decision = fp.read_decision(timeout_seconds=0.1)
        assert decision == Decision.TIMEOUT
        assert fp.last_key_class == "timeout"
    finally:
        server.close()
        client.close()


def test_read_decision_closed_transport_returns_denied():
    server, client = _make_pair()
    client.close()
    server.close()
    fp = ForwardingPresenter(server)
    decision = fp.read_decision(timeout_seconds=0.1)
    assert decision == Decision.DENIED
    assert fp.last_key_class == "transport_closed"


# ── pre_check ────────────────────────────────────────────────────────────


def test_pre_check_returns_none_when_open():
    server, client = _make_pair()
    try:
        fp = ForwardingPresenter(server)
        assert fp.pre_check() is None
    finally:
        server.close()
        client.close()


def test_pre_check_returns_denied_when_closed():
    server, client = _make_pair()
    server.close()
    client.close()
    fp = ForwardingPresenter(server)
    assert fp.pre_check() == Decision.DENIED


# ── Thread safety ────────────────────────────────────────────────────────


def test_receive_decision_from_another_thread():
    server, client = _make_pair()
    try:
        fp = ForwardingPresenter(server)
        decisions = []

        def worker():
            decisions.append(fp.read_decision(timeout_seconds=5))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.1)

        for _ in range(3):
            fp.receive_decision(Decision.DENIED)
            time.sleep(0.05)

        for t in threads:
            t.join(timeout=5)

        assert all(d == Decision.DENIED for d in decisions)
    finally:
        server.close()
        client.close()
