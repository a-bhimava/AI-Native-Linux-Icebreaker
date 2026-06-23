"""System tray indicator — daemon health polling + quick actions menu.

Polls daemon health every 10 seconds via GtkDaemonClient.
Shows status as a colored dot with tooltip.
Right-click menu provides quick access to Chatbot, Settings, Audit.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from ..daemon_client import GtkDaemonClient


class DaemonState:
    READY = "ready"
    LOADING = "loading"
    OFFLINE = "offline"


_STATE_LABELS = {
    DaemonState.READY: "Icebreaker: Ready",
    DaemonState.LOADING: "Icebreaker: PB warming up",
    DaemonState.OFFLINE: "Icebreaker: Daemon offline",
}

_STATE_COLORS = {
    DaemonState.READY: (0.34, 0.74, 0.34),
    DaemonState.LOADING: (0.91, 0.54, 0.32),
    DaemonState.OFFLINE: (0.6, 0.6, 0.6),
}

_POLL_INTERVAL_SECONDS = 10


class TrayIndicator(Gtk.Box):
    """Status indicator with health polling and quick-action popover.

    Designed to be embedded in a panel or used standalone. On GNOME,
    native shell extensions handle actual system tray; this provides
    the health-polling logic and menu that any host can present.
    """

    def __init__(self, app: Adw.Application,
                 client: Optional[GtkDaemonClient] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._app = app
        self._client = client
        self._state = DaemonState.OFFLINE if client is None else DaemonState.READY
        self._poll_id: int = 0

        self._dot = Gtk.DrawingArea()
        self._dot.set_size_request(16, 16)
        self._dot.set_draw_func(self._draw_dot)
        self._dot.set_halign(Gtk.Align.CENTER)
        self._dot.set_tooltip_text(_STATE_LABELS[self._state])
        self.append(self._dot)

        self._menu_btn = Gtk.MenuButton()
        self._menu_btn.set_icon_name("view-more-symbolic")
        self._menu_btn.add_css_class("flat")
        self._menu_btn.set_halign(Gtk.Align.CENTER)

        menu = Gio.Menu()
        menu.append("Open Chatbot", "app.open-chatbot")
        menu.append("Open Settings", "app.open-settings")
        menu.append("View Audit Log", "app.open-audit")
        sep = Gio.Menu()
        sep.append("Quit", "app.quit")
        combined = Gio.Menu()
        combined.append_section(None, menu)
        combined.append_section(None, sep)
        self._menu_btn.set_menu_model(combined)
        self.append(self._menu_btn)

        if client is not None:
            self._start_polling()

    def _draw_dot(self, area: Gtk.DrawingArea, cr: object,
                  width: int, height: int) -> None:
        color = _STATE_COLORS.get(self._state, _STATE_COLORS[DaemonState.OFFLINE])
        cr.arc(width / 2, height / 2, min(width, height) / 2 - 1, 0, 6.283)
        cr.set_source_rgb(*color)
        cr.fill()

    def _start_polling(self) -> None:
        self._poll_id = GLib.timeout_add_seconds(
            _POLL_INTERVAL_SECONDS, self._poll_health
        )

    def _poll_health(self) -> bool:
        if self._client is None:
            self._set_state(DaemonState.OFFLINE)
            return True

        try:
            self._client.request("health.check", {}, callback=self._on_health)
        except Exception:
            self._set_state(DaemonState.OFFLINE)
        return True

    def _on_health(self, result: dict) -> None:
        GLib.idle_add(self._handle_health, result)

    def _handle_health(self, result: dict) -> bool:
        status = result.get("status", "unknown")
        if status == "ready":
            self._set_state(DaemonState.READY)
        elif status == "loading":
            self._set_state(DaemonState.LOADING)
        else:
            self._set_state(DaemonState.OFFLINE)
        return False

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self._dot.set_tooltip_text(_STATE_LABELS.get(state, "Unknown"))
            self._dot.queue_draw()

    @property
    def state(self) -> str:
        return self._state

    def stop_polling(self) -> None:
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0
