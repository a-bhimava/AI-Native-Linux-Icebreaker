"""HTTP-over-UNIX-socket adapter for requests.Session.

Allows ``requests.Session.get/post()`` to route through an AF_UNIX socket
instead of TCP. Used when ``transport="unix"`` in the backend config.

Usage::

    from ._unix_http import UnixHTTPAdapter, parse_unix_endpoint

    socket_path, http_base = parse_unix_endpoint("unix:///run/icebreaker/pbd.sock")
    adapter = UnixHTTPAdapter(socket_path)
    session = requests.Session()
    session.mount("http+unix://", adapter)
    session.get(f"{http_base}/health")
"""

from __future__ import annotations

import http.client
import re
import socket
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPConnectionPool
from urllib3.connection import HTTPConnection


_SHELL_METACHAR_RE = re.compile(r"[;&|`$(){}\[\]!<>\"'\\*?~#]")


def parse_unix_endpoint(endpoint: str) -> tuple[str, str]:
    """Parse ``unix:///path/to.sock`` into ``(socket_path, http_base)``.

    Returns:
        Tuple of (absolute socket path, ``"http+unix://localhost"``).

    Raises:
        ValueError: on malformed input (not ``unix://``, relative path,
            shell metacharacters).
    """
    if not endpoint.startswith("unix://"):
        raise ValueError(
            f"UNIX endpoint must start with 'unix://'; got {endpoint!r}"
        )
    path = endpoint[len("unix://"):]
    if not path:
        raise ValueError("UNIX endpoint has empty path")
    if not path.startswith("/"):
        raise ValueError(
            f"UNIX endpoint path must be absolute; got {path!r}"
        )
    if _SHELL_METACHAR_RE.search(path):
        raise ValueError(
            f"UNIX endpoint path contains shell metacharacters: {path!r}"
        )
    return path, "http+unix://localhost"


class _UnixHTTPConnection(HTTPConnection):
    """urllib3 HTTPConnection subclass that connects over AF_UNIX."""

    def __init__(self, socket_path: str, **kwargs: Any) -> None:
        super().__init__("localhost", **kwargs)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._socket_path)


class _UnixConnectionPool(HTTPConnectionPool):
    """Connection pool that produces ``_UnixHTTPConnection`` instances."""

    def __init__(self, socket_path: str, **kwargs: Any) -> None:
        super().__init__("localhost", **kwargs)
        self._socket_path = socket_path

    def _new_conn(self) -> _UnixHTTPConnection:
        return _UnixHTTPConnection(self._socket_path)


class UnixHTTPAdapter(HTTPAdapter):
    """requests adapter that routes HTTP through an AF_UNIX socket.

    Overrides both ``get_connection_with_tls_context`` (requests >= 2.32)
    and ``get_connection`` (requests < 2.32) so the adapter works across
    the ``requests>=2.31`` range declared in requirements.txt.
    """

    def __init__(self, socket_path: str, **kwargs: Any) -> None:
        self._socket_path = socket_path
        super().__init__(**kwargs)

    def get_connection_with_tls_context(
        self, request: Any, verify: Any, proxies: Any = None, cert: Any = None
    ) -> _UnixConnectionPool:
        return _UnixConnectionPool(self._socket_path)

    def get_connection(
        self, url: str, proxies: Any = None
    ) -> _UnixConnectionPool:
        return _UnixConnectionPool(self._socket_path)
