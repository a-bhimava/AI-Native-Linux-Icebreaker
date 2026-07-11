"""IcebreakerApp — Adw.Application subclass and main window scaffold.

Provides:
  - LibAdwaita initialization with the Icebreaker theme
  - A main window stub that sub-commands (--chatbot, --settings, etc.) populate
  - Daemon connection lifecycle management
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from .daemon_client import GtkDaemonClient
from .theme import IcebreakerTheme
from .widgets import _load_widget_classes


log = logging.getLogger(__name__)


class IcebreakerApp(Adw.Application):
    """Root GTK application for all desktop GUI entry points."""

    def __init__(
        self,
        *,
        application_id: str = "org.icebreaker.desktop",
        sock_path: str = "",
        dark: bool = True,
        window_mode: str = "chatbot",
    ) -> None:
        super().__init__(
            application_id=application_id,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self._sock_path = sock_path
        self._theme = IcebreakerTheme(dark=dark)
        self._window_mode = window_mode
        self._client: Optional[GtkDaemonClient] = None
        self._window: Optional[Adw.ApplicationWindow] = None
        
        # Startup logger init: audit log path may be non-writable when
        # running as a plain user, and the SystemLogger constructor also
        # eagerly opens the file. F-53 Scope A.P2: surface *why* so a
        # broken audit path shows up in journalctl instead of a mystery
        # `None` logger silently dropping every subsequent event.
        self._logger_init_error: str | None = None
        try:
            from controller.logger import SystemLogger
            self.logger = SystemLogger("/var/log/icebreaker/system.jsonl")
            self.logger.log("gui_rpa", "app_startup", {"mode": window_mode})
        except Exception as exc:
            self.logger = None
            self._logger_init_error = (
                f"{type(exc).__name__}: {exc}"
            )[:400]
            log.warning(
                "gui.logger_init failed: %s: %s",
                type(exc).__name__, exc,
            )

    @property
    def client(self) -> Optional[GtkDaemonClient]:
        return self._client

    @property
    def theme(self) -> IcebreakerTheme:
        return self._theme

    def do_activate(self) -> None:
        self._theme.apply()

        style_manager = Adw.StyleManager.get_default()
        if self._theme.is_dark:
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        else:
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)

        # F-53 Scope A.P2: connection failure was silently nulling the
        # client, which then triggered every downstream "Not connected"
        # warning without telling the user *why*. Record the exception
        # so the persistent connection bar in the main window can render
        # a specific reason (missing socket, wrong perms, daemon down)
        # instead of the generic "Not connected".
        self._daemon_connect_error: str | None = None
        if self._sock_path:
            # Phase 6 Scope B: load config to pass user-tuned client
            # timeouts. If load fails, fall back to DaemonClient defaults.
            _client_kwargs = {}
            try:
                from controller.config import load_layered
                _cfg = load_layered()
                _client_kwargs = {
                    "turn_timeout_seconds": float(_cfg.run.turn_timeout_seconds),
                    "reader_recv_timeout_seconds": float(
                        _cfg.daemon.reader_recv_timeout_seconds
                    ),
                    "max_reconnect_delay_seconds": float(
                        _cfg.daemon.max_reconnect_delay_seconds
                    ),
                }
            except Exception as cfg_exc:  # noqa: BLE001
                # Config-driven client timeouts are best-effort — the
                # DaemonClient defaults preserve pre-Scope-B behavior if
                # the layered config isn't loadable (e.g. running the GUI
                # against a locally-built dev tree). Log so the
                # discrepancy is diagnosable.
                log.debug(
                    "gui.app: falling back to DaemonClient default "
                    "timeouts (config load failed: %s: %s)",
                    type(cfg_exc).__name__, cfg_exc,
                )
            self._client = GtkDaemonClient(self._sock_path, **_client_kwargs)
            try:
                self._client.connect()
            except Exception as exc:
                self._client = None
                self._daemon_connect_error = (
                    f"{type(exc).__name__}: {exc}"
                )[:400]
                log.warning(
                    "gui.daemon_connect failed at %s: %s: %s",
                    self._sock_path, type(exc).__name__, exc,
                )

        _load_widget_classes()
        self._register_actions()

        if self._window_mode == "settings":
            from .settings.window import SettingsWindow
            win = SettingsWindow(app=self)
            win.present()
            return

        if self._window_mode == "wizard":
            from .wizard.window import WizardWindow
            win = WizardWindow(app=self, client=self._client)
            win.present()
            return

        if self._window_mode == "audit":
            from .audit.window import AuditWindow
            win = AuditWindow(app=self)
            win.present()
            return

        from .chatbot.window import ChatbotWindow
        self._window = ChatbotWindow(app=self, client=self._client)
        self._window.present()

    def do_shutdown(self) -> None:
        if self._client is not None:
            self._client.close()
        Adw.Application.do_shutdown(self)

    def _register_actions(self) -> None:
        for name, handler in [
            ("open-chatbot", self._action_open_chatbot),
            ("open-settings", self._action_open_settings),
            ("open-audit", self._action_open_audit),
            ("quit", lambda *_: self.quit()),
        ]:
            action = Gio.SimpleAction(name=name)
            action.connect("activate", handler)
            self.add_action(action)

    def _action_open_chatbot(self, *_args: object) -> None:
        from .chatbot.window import ChatbotWindow
        win = ChatbotWindow(app=self, client=self._client)
        win.present()

    def _action_open_settings(self, *_args: object) -> None:
        from .settings.window import SettingsWindow
        win = SettingsWindow(app=self)
        win.present()

    def _action_open_audit(self, *_args: object) -> None:
        from .audit.window import AuditWindow
        win = AuditWindow(app=self)
        win.present()

    def set_window_content(self, widget: Gtk.Widget) -> None:
        """Replace the main content area of the window."""
        if self._window is None:
            return
        toolbar = self._window.get_content()
        if isinstance(toolbar, Adw.ToolbarView):
            toolbar.set_content(widget)
