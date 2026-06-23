"""IcebreakerApp — Adw.Application subclass and main window scaffold.

Provides:
  - LibAdwaita initialization with the Icebreaker theme
  - A main window stub that sub-commands (--chatbot, --settings, etc.) populate
  - Daemon connection lifecycle management
"""

from __future__ import annotations

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


class IcebreakerApp(Adw.Application):
    """Root GTK application for all desktop GUI entry points."""

    def __init__(
        self,
        *,
        application_id: str = "org.icebreaker.desktop",
        sock_path: str = "",
        dark: bool = True,
    ) -> None:
        super().__init__(
            application_id=application_id,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self._sock_path = sock_path
        self._theme = IcebreakerTheme(dark=dark)
        self._client: Optional[GtkDaemonClient] = None
        self._window: Optional[Adw.ApplicationWindow] = None

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

        if self._sock_path:
            self._client = GtkDaemonClient(self._sock_path)
            try:
                self._client.connect()
            except Exception:
                self._client = None

        self._window = Adw.ApplicationWindow(application=self)
        self._window.set_title("Icebreaker")
        self._window.set_default_size(800, 600)
        self._window.add_css_class("ib-window")

        _load_widget_classes()
        from .widgets import StatusDot

        header = Adw.HeaderBar()
        self._status_dot = StatusDot(connected=self._client is not None)
        header.pack_end(self._status_dot)

        content = Adw.ToolbarView()
        content.add_top_bar(header)

        placeholder = Gtk.Label(label="Icebreaker Desktop")
        placeholder.add_css_class("ib-muted-text")
        content.set_content(placeholder)

        self._window.set_content(content)
        self._window.present()

    def do_shutdown(self) -> None:
        if self._client is not None:
            self._client.close()
        Adw.Application.do_shutdown(self)

    def set_window_content(self, widget: Gtk.Widget) -> None:
        """Replace the main content area of the window."""
        if self._window is None:
            return
        toolbar = self._window.get_content()
        if isinstance(toolbar, Adw.ToolbarView):
            toolbar.set_content(widget)
