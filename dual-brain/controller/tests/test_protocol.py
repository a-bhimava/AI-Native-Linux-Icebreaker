"""Tests for controller.protocol — JSON-RPC 2.0 message types."""

from __future__ import annotations

import json

import pytest

from controller.protocol import (
    AUTH_REJECTED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    is_notification,
    is_request,
    is_response,
    make_error,
    parse_message,
)


# ── JsonRpcRequest ────────────────────────────────────────────────────────


class TestJsonRpcRequest:
    def test_to_bytes_roundtrip(self):
        req = JsonRpcRequest(method="turn.run", params={"input": "hello"})
        raw = req.to_bytes()
        assert raw.endswith(b"\n")
        msg = json.loads(raw)
        assert msg["jsonrpc"] == JSONRPC_VERSION
        assert msg["method"] == "turn.run"
        assert msg["params"] == {"input": "hello"}
        assert msg["id"] == req.id

    def test_auto_generates_id(self):
        r1 = JsonRpcRequest(method="a")
        r2 = JsonRpcRequest(method="a")
        assert r1.id != r2.id

    def test_explicit_id(self):
        req = JsonRpcRequest(method="a", id="fixed-id")
        assert req.id == "fixed-id"

    def test_default_params_empty_dict(self):
        req = JsonRpcRequest(method="a")
        assert req.params == {}

    def test_frozen(self):
        req = JsonRpcRequest(method="a")
        with pytest.raises(AttributeError):
            req.method = "b"  # type: ignore[misc]


# ── JsonRpcNotification ──────────────────────────────────────────────────


class TestJsonRpcNotification:
    def test_to_bytes_roundtrip(self):
        notif = JsonRpcNotification(
            method="turn.progress", params={"step": "qb_intent"}
        )
        raw = notif.to_bytes()
        assert raw.endswith(b"\n")
        msg = json.loads(raw)
        assert msg["jsonrpc"] == JSONRPC_VERSION
        assert msg["method"] == "turn.progress"
        assert msg["params"] == {"step": "qb_intent"}
        assert "id" not in msg

    def test_default_params_empty_dict(self):
        notif = JsonRpcNotification(method="a")
        assert notif.params == {}

    def test_frozen(self):
        notif = JsonRpcNotification(method="a")
        with pytest.raises(AttributeError):
            notif.method = "b"  # type: ignore[misc]


# ── JsonRpcResponse ──────────────────────────────────────────────────────


class TestJsonRpcResponse:
    def test_result_response(self):
        resp = JsonRpcResponse(id="123", result={"ok": True})
        raw = resp.to_bytes()
        assert raw.endswith(b"\n")
        msg = json.loads(raw)
        assert msg["jsonrpc"] == JSONRPC_VERSION
        assert msg["id"] == "123"
        assert msg["result"] == {"ok": True}
        assert "error" not in msg

    def test_error_response(self):
        resp = JsonRpcResponse(
            id="456", error={"code": -32600, "message": "bad request"}
        )
        raw = resp.to_bytes()
        msg = json.loads(raw)
        assert msg["error"] == {"code": -32600, "message": "bad request"}
        assert "result" not in msg

    def test_empty_result_defaults_to_empty_dict(self):
        resp = JsonRpcResponse(id="789")
        msg = json.loads(resp.to_bytes())
        assert msg["result"] == {}

    def test_frozen(self):
        resp = JsonRpcResponse(id="a")
        with pytest.raises(AttributeError):
            resp.id = "b"  # type: ignore[misc]


# ── parse_message ────────────────────────────────────────────────────────


class TestParseMessage:
    def test_parses_valid_json(self):
        data = b'{"jsonrpc":"2.0","method":"a","id":"1"}'
        msg = parse_message(data)
        assert msg["method"] == "a"

    def test_raises_on_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            parse_message(b"not json")


# ── Classification helpers ───────────────────────────────────────────────


class TestMessageClassification:
    def test_is_request(self):
        msg = {"jsonrpc": "2.0", "method": "turn.run", "id": "1", "params": {}}
        assert is_request(msg) is True
        assert is_notification(msg) is False
        assert is_response(msg) is False

    def test_is_notification(self):
        msg = {"jsonrpc": "2.0", "method": "turn.progress", "params": {}}
        assert is_notification(msg) is True
        assert is_request(msg) is False
        assert is_response(msg) is False

    def test_is_response(self):
        msg = {"jsonrpc": "2.0", "id": "1", "result": {}}
        assert is_response(msg) is True
        assert is_request(msg) is False
        assert is_notification(msg) is False


# ── make_error ───────────────────────────────────────────────────────────


class TestMakeError:
    def test_make_error_structure(self):
        resp = make_error("req-1", INVALID_PARAMS, "missing input")
        msg = json.loads(resp.to_bytes())
        assert msg["id"] == "req-1"
        assert msg["error"]["code"] == INVALID_PARAMS
        assert msg["error"]["message"] == "missing input"


# ── Error code constants ─────────────────────────────────────────────────


def test_error_codes_are_negative():
    for code in (PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND,
                 INVALID_PARAMS, INTERNAL_ERROR, AUTH_REJECTED):
        assert code < 0


# ── Newline termination ─────────────────────────────────────────────────


def test_all_messages_newline_terminated():
    req = JsonRpcRequest(method="a")
    notif = JsonRpcNotification(method="b")
    resp = JsonRpcResponse(id="c", result={"ok": True})
    err = make_error("d", -32600, "bad")
    for msg in (req, notif, resp, err):
        raw = msg.to_bytes()
        assert raw.endswith(b"\n"), f"{type(msg).__name__} missing newline"
        assert raw.count(b"\n") == 1, f"{type(msg).__name__} has extra newlines"
