"""Tests for ``backends._unix_http`` — HTTP-over-UNIX-socket adapter.

Covers:
* parse_unix_endpoint: valid input, relative path, empty path, HTTP URL,
  shell metacharacters
* _UnixHTTPConnection: AF_UNIX socket creation
* UnixHTTPAdapter: mounts on correct prefix
* Adapter round-trip with mocked socket
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from controller.backends._unix_http import (
    UnixHTTPAdapter,
    _UnixConnectionPool,
    _UnixHTTPConnection,
    parse_unix_endpoint,
)


# ── parse_unix_endpoint ─────────────────────────────────────────────────────


def test_parse_valid_endpoint():
    sock_path, http_base = parse_unix_endpoint("unix:///run/icebreaker/pbd.sock")
    assert sock_path == "/run/icebreaker/pbd.sock"
    assert http_base == "http+unix://localhost"


def test_parse_relative_path_raises():
    with pytest.raises(ValueError, match="absolute"):
        parse_unix_endpoint("unix://relative/path")


def test_parse_empty_path_raises():
    with pytest.raises(ValueError, match="empty"):
        parse_unix_endpoint("unix://")


def test_parse_http_endpoint_raises():
    with pytest.raises(ValueError, match="unix://"):
        parse_unix_endpoint("http://localhost:8080")


@pytest.mark.parametrize("bad_path", [
    "unix:///tmp/foo;rm -rf /",
    "unix:///tmp/$(whoami).sock",
    "unix:///tmp/test`id`.sock",
    "unix:///tmp/test|cat.sock",
    "unix:///tmp/test&bg.sock",
])
def test_parse_shell_metachar_raises(bad_path):
    with pytest.raises(ValueError, match="metacharacters"):
        parse_unix_endpoint(bad_path)


def test_parse_deep_path():
    sock_path, _ = parse_unix_endpoint("unix:///var/run/icebreaker/nested/deep/pbd.sock")
    assert sock_path == "/var/run/icebreaker/nested/deep/pbd.sock"


# ── _UnixHTTPConnection ─────────────────────────────────────────────────────


def test_connection_uses_af_unix():
    conn = _UnixHTTPConnection("/tmp/test.sock")
    mock_sock = MagicMock()
    with patch("socket.socket", return_value=mock_sock) as mock_ctor:
        conn.connect()
    mock_ctor.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
    mock_sock.connect.assert_called_once_with("/tmp/test.sock")
    assert conn.sock is mock_sock


# ── UnixHTTPAdapter ──────────────────────────────────────────────────────────


def test_adapter_mounts_on_prefix():
    import requests

    adapter = UnixHTTPAdapter("/tmp/test.sock")
    session = requests.Session()
    session.mount("http+unix://", adapter)
    matched = session.get_adapter("http+unix://localhost/health")
    assert matched is adapter


def test_adapter_get_connection_returns_unix_pool():
    adapter = UnixHTTPAdapter("/tmp/test.sock")
    pool = adapter.get_connection("http+unix://localhost/health")
    assert isinstance(pool, _UnixConnectionPool)


def test_adapter_get_connection_with_tls_context_returns_unix_pool():
    adapter = UnixHTTPAdapter("/tmp/test.sock")
    mock_request = MagicMock()
    mock_request.url = "http+unix://localhost/health"
    pool = adapter.get_connection_with_tls_context(mock_request, verify=False)
    assert isinstance(pool, _UnixConnectionPool)
