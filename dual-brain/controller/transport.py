"""Transport abstraction for daemon<->client communication.

Provides a bidirectional, newline-delimited message channel over AF_UNIX.
The daemon and client code never touch raw sockets — only this module does.
"""

from __future__ import annotations

import os
import select
import socket
import struct
import sys
import threading
from abc import ABC, abstractmethod


class TransportClosed(Exception):
    """Raised when the peer has closed the connection or the transport is shut down."""


class Transport(ABC):
    """Bidirectional newline-delimited JSON message channel."""

    @abstractmethod
    def send(self, data: bytes) -> None:
        """Send one newline-terminated message. Raises TransportClosed on failure."""

    @abstractmethod
    def recv(self, timeout: float | None = None) -> bytes | None:
        """Receive one newline-terminated message.

        Returns None on timeout. Raises TransportClosed on peer disconnect or
        closed transport.
        """

    @abstractmethod
    def close(self) -> None:
        """Close the transport. Idempotent."""

    @abstractmethod
    def is_open(self) -> bool:
        """True if the transport is still usable."""

    @property
    @abstractmethod
    def peer_uid(self) -> int | None:
        """UID of the connected peer via SO_PEERCRED. None if unavailable."""


class UnixSocketTransport(Transport):
    """AF_UNIX stream socket transport with SO_PEERCRED authentication.

    Thread-safe for one concurrent sender and one concurrent receiver (separate
    locks). NOT safe for multiple concurrent senders or multiple concurrent
    receivers.
    """

    __slots__ = (
        "_sock", "_lock_send", "_lock_recv", "_buf", "_closed", "_peer_uid",
    )

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._lock_send = threading.Lock()
        self._lock_recv = threading.Lock()
        self._buf = bytearray()
        self._closed = False
        self._peer_uid: int | None = self._get_peercred()

    def _get_peercred(self) -> int | None:
        if sys.platform != "linux":
            return None
        try:
            SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
            creds = self._sock.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, 12)
            _pid, uid, _gid = struct.unpack("iII", creds)
            return uid
        except (OSError, struct.error):
            return None

    def send(self, data: bytes) -> None:
        if self._closed:
            raise TransportClosed("transport is closed")
        with self._lock_send:
            try:
                self._sock.sendall(data)
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                self._closed = True
                raise TransportClosed(str(exc)) from exc

    def recv(self, timeout: float | None = None) -> bytes | None:
        if self._closed:
            raise TransportClosed("transport is closed")
        with self._lock_recv:
            while True:
                nl = self._buf.find(b"\n")
                if nl >= 0:
                    line = bytes(self._buf[: nl + 1])
                    del self._buf[: nl + 1]
                    return line

                try:
                    ready, _, _ = select.select([self._sock], [], [], timeout)
                except (ValueError, OSError):
                    self._closed = True
                    raise TransportClosed("select failed on closed socket")
                if not ready:
                    return None

                try:
                    chunk = self._sock.recv(4096)
                except (ConnectionResetError, OSError) as exc:
                    self._closed = True
                    raise TransportClosed(str(exc)) from exc
                if not chunk:
                    self._closed = True
                    raise TransportClosed("peer closed connection")
                self._buf.extend(chunk)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    def is_open(self) -> bool:
        return not self._closed

    @property
    def peer_uid(self) -> int | None:
        return self._peer_uid
