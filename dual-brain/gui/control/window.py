"""Main Control Center window — AdwPreferencesWindow with tabbed pages."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from . import themes
from .pages.keys_page import KeysPage
from .pages.status_page import StatusPage
from .pages.theme_page import ThemePage, load_active_theme_name


class ControlWindow(Adw.PreferencesWindow):
    """Icebreaker Control Center — status, keys, theme.

    Uses AdwPreferencesWindow so each page gets a sidebar entry + icon out
    of the box (matches macOS System Preferences / GNOME Settings style).
    """

    __slots__ = ("_apply_theme",)

    def __init__(self, app: Adw.Application, apply_theme: Callable[[str], None]) -> None:
        super().__init__(application=app)
        self.set_title("Icebreaker Control Center")
        self.set_default_size(760, 620)

        self._apply_theme = apply_theme

        # Pages
        self.add(StatusPage())
        self.add(KeysPage())
        self.add(ThemePage(on_theme_change=self._on_theme_change))

    def _on_theme_change(self, name: str) -> None:
        theme = themes.get(name)
        if theme is not None:
            self._apply_theme(name)
