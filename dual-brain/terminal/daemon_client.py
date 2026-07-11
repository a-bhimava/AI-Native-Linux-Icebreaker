"""TextualDaemonClient — Textual-thread-safe subclass of DaemonClient.

The reader thread receives JSON-RPC notifications from the daemon.
This subclass marshals every callback through ``app.call_from_thread()``
so Textual widgets can safely update from the main loop.
"""

from __future__ import annotations

from typing import Any, Callable

from controller.client import DaemonClient


class TextualDaemonClient(DaemonClient):
    """DaemonClient that dispatches notifications on the Textual event loop."""

    __slots__ = ("_callbacks", "_app")

    def __init__(self, sock_path: str, **kwargs: Any) -> None:
        # Phase 6 Scope B: pass through turn_timeout_seconds,
        # reader_recv_timeout_seconds, max_reconnect_delay_seconds.
        super().__init__(sock_path, **kwargs)
        self._callbacks: dict[str, list[Callable[..., None]]] = {}
        self._app: Any = None

    def set_app(self, app: Any) -> None:
        self._app = app

    def on(self, event: str, callback: Callable[..., None]) -> None:
        self._callbacks.setdefault(event, []).append(callback)

    def _fire(self, event: str, params: dict) -> None:
        for cb in self._callbacks.get(event, []):
            if self._app is not None:
                self._app.call_from_thread(cb, params)
            else:
                cb(params)

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
