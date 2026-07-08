"""Icebreaker Control Center application.

Adwaita-based GTK4 app. Entry: ``python -m gui.control`` or the desktop
launcher shipped in v6.7.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk

from ..theme import IcebreakerTheme, _generate_css
from . import themes
from .pages.theme_page import load_active_theme_name
from .window import ControlWindow


APP_ID = "com.icebreaker.ControlCenter"


class ControlApp(Adw.Application):
    """Small Adwaita application hosting a single ControlWindow."""

    __slots__ = ("_window", "_active_provider")

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self._window: Optional[ControlWindow] = None
        self._active_provider: Optional[Gtk.CssProvider] = None

    def do_activate(self) -> None:
        # Apply persisted theme BEFORE the window renders so first paint is
        # already themed (avoids the "flash of default palette" seen when
        # theming is applied post-show).
        active_name = load_active_theme_name()
        if active_name and themes.get(active_name):
            self.apply_theme(active_name)
        elif themes.names():
            self.apply_theme(themes.names()[0])

        if self._window is None:
            self._window = ControlWindow(app=self, apply_theme=self.apply_theme)
        self._window.present()

    def apply_theme(self, name: str) -> None:
        """Swap the currently loaded CSS provider for the chosen theme."""
        theme = themes.get(name)
        if theme is None:
            return

        display = Gdk.Display.get_default()

        # Remove previous provider so old rules don't linger + compound.
        if self._active_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._active_provider)

        css_text = _generate_css(theme.tokens, {"sans": "Inter, sans-serif", "mono": "JetBrains Mono, monospace"}, "12px")
        provider = Gtk.CssProvider()
        provider.load_from_string(css_text)
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._active_provider = provider


def main() -> int:
    app = ControlApp()
    return app.run(None)
