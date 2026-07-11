"""Theme picker — swap the whole app palette in one click.

Themes live in ``gui/control/themes/*.py``. Adding a new one is a single
file drop-in. Selection persists to ``~/.config/icebreaker/control.toml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from .. import themes

_SETTINGS_FILE = Path.home() / ".config" / "icebreaker" / "control.toml"


def load_active_theme_name() -> str:
    """Read the last-selected theme name; defaults to first available."""
    if _SETTINGS_FILE.exists():
        for line in _SETTINGS_FILE.read_text().splitlines():
            if line.startswith("theme"):
                _, _, value = line.partition("=")
                return value.strip().strip('"')
    return themes.names()[0] if themes.names() else ""


def save_active_theme_name(name: str) -> None:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(f'theme = "{name}"\n')


class ThemePage(Adw.PreferencesPage):
    """Radio-button list of registered themes with a live preview swatch."""

    def __init__(self, on_theme_change: Callable[[str], None]) -> None:
        super().__init__(title="Theme", icon_name="preferences-desktop-appearance-symbolic")
        self.set_name("theme")
        self._on_theme_change = on_theme_change

        intro = Adw.PreferencesGroup(
            title="Appearance",
            description=(
                "Themes are Python files in gui/control/themes/ — add a new "
                "one by copying default.py and swapping TOKENS. Registry is "
                "auto-discovered on start."
            ),
        )
        self.add(intro)

        chooser = Adw.PreferencesGroup(title="Available themes")
        self.add(chooser)

        self._active = load_active_theme_name()
        radio_group: Gtk.CheckButton | None = None

        for name in themes.names():
            theme = themes.get(name)
            row = Adw.ActionRow(title=theme.name, subtitle=theme.description or "")
            radio = Gtk.CheckButton()
            if radio_group is None:
                radio_group = radio
            else:
                radio.set_group(radio_group)
            radio.set_active(name == self._active)
            radio.connect("toggled", self._on_toggled, name)
            row.add_prefix(radio)

            # Swatch: 3 colored chips showing primary/secondary/background.
            swatch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for key in ("primary", "secondary", "background"):
                chip = Gtk.Label(label="  ")
                chip.set_size_request(24, 24)
                css = f".swatch-{name}-{key} {{ background-color: {theme.tokens[key]}; border-radius: 6px; border: 1px solid rgba(255,255,255,0.15); }}"
                provider = Gtk.CssProvider()
                provider.load_from_string(css)
                chip.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                chip.add_css_class(f"swatch-{name}-{key}")
                swatch.append(chip)
            row.add_suffix(swatch)

            chooser.add(row)

    def _on_toggled(self, button: Gtk.CheckButton, name: str) -> None:
        if not button.get_active():
            return
        self._active = name
        save_active_theme_name(name)
        self._on_theme_change(name)
