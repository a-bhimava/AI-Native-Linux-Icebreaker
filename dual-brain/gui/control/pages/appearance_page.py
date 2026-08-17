"""Appearance preferences for the Frosted Graphite desktop profile."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ...appearance import AppearanceSettings, load_appearance, save_appearance


class AppearancePage(Adw.PreferencesPage):
    """Safe, user-session controls; desktop activation is handled by XDG."""

    def __init__(self) -> None:
        super().__init__(title="Appearance", icon_name="preferences-desktop-appearance-symbolic")
        self.set_name("appearance")
        self._settings = load_appearance()

        profile_group = Adw.PreferencesGroup(
            title="Visual profile",
            description="Frosted Graphite is the default. Safety and audit surfaces stay opaque.",
        )
        self.add(profile_group)
        profile_row = Adw.ActionRow(title="Profile", subtitle="Choose the Icebreaker application palette")
        self._profile = Gtk.DropDown.new_from_strings(["Frosted Graphite", "Classic"])
        self._profile.set_selected(0 if self._settings.profile == "frosted" else 1)
        self._profile.connect("notify::selected", self._on_profile_changed)
        profile_row.add_suffix(self._profile)
        profile_row.set_activatable_widget(self._profile)
        profile_group.add(profile_row)

        extras = Adw.PreferencesGroup(
            title="Desktop extras",
            description="Changes apply on the next login. Effects automatically fall back when unavailable.",
        )
        self.add(extras)
        self._switches: dict[str, Adw.SwitchRow] = {}
        for key, title, subtitle in (
            ("dock_enabled", "Dock", "Dock-first launcher with a recoverable XFCE system rail."),
            ("notifications_enabled", "Rich notifications", "Use Dunst instead of the XFCE notification daemon."),
            ("overview_enabled", "Workspace overview", "Enable the Xfdashboard search and overview shortcut."),
            ("compositor_enabled", "Glass effects", "Use Picom when an X11 GLX self-check succeeds."),
        ):
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.set_active(bool(getattr(self._settings, key)))
            row.connect("notify::active", self._on_switch_changed, key)
            extras.add(row)
            self._switches[key] = row

    def _updated(self, **changes: object) -> AppearanceSettings:
        values = {
            "profile": self._settings.profile,
            "dock_enabled": self._settings.dock_enabled,
            "notifications_enabled": self._settings.notifications_enabled,
            "overview_enabled": self._settings.overview_enabled,
            "compositor_enabled": self._settings.compositor_enabled,
        }
        values.update(changes)
        self._settings = AppearanceSettings(**values)
        save_appearance(self._settings)
        return self._settings

    def _on_profile_changed(self, dropdown: Gtk.DropDown, _param: object) -> None:
        self._updated(profile="frosted" if dropdown.get_selected() == 0 else "classic")

    def _on_switch_changed(self, row: Adw.SwitchRow, _param: object, key: str) -> None:
        self._updated(**{key: row.get_active()})
