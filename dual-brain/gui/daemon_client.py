"""GtkDaemonClient — GTK-thread-safe subclass of DaemonClient.

The reader thread receives JSON-RPC notifications from the daemon.
This subclass marshals every callback through ``GLib.idle_add()`` so
the GTK widgets can safely update from the main loop (ADR-21).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib

from controller.client import DaemonClient


class GtkDaemonClient(DaemonClient):
    """DaemonClient that dispatches notifications on the GLib main loop."""

    __slots__ = ("_callbacks",)

    def __init__(self, sock_path: str) -> None:
        super().__init__(sock_path)
        self._callbacks: dict[str, list[Callable[..., None]]] = {}

    def on(self, event: str, callback: Callable[..., None]) -> None:
        """Register a callback for a notification event name.

        ``event`` is the JSON-RPC method minus the ``turn.`` prefix, e.g.
        ``"progress"``, ``"token"``, ``"hitl_prompt"``.
        """
        self._callbacks.setdefault(event, []).append(callback)

    def _fire(self, event: str, params: dict) -> None:
        for cb in self._callbacks.get(event, []):
            GLib.idle_add(cb, params)

    def _on_progress(self, params: dict) -> None:
        self._fire("progress", params)

    def _on_token(self, params: dict) -> None:
        self._fire("token", params)

    def _on_info(self, params: dict) -> None:
        self._fire("info", params)

    def _on_cot(self, params: dict) -> None:
        self._fire("cot", params)

    def _on_gui(self, params: dict) -> None:
        self._fire("gui", params)

    def _on_rpa(self, params: dict) -> None:
        self._fire("rpa", params)

    def _on_hitl_prompt(self, params: dict) -> None:
        self._fire("hitl_prompt", params)

    def _on_hitl_lockout(self, params: dict) -> None:
        self._fire("hitl_lockout", params)
