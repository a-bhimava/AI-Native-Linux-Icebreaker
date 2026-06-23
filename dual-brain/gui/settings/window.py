"""SettingsWindow — AdwPreferencesWindow with five config pages.

Loads the current controller.toml into page widgets, validates edits
against the JSON Schema before saving, and offers an unsaved-changes
guard on close.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .backend_page import BackendPage
from .daemon_page import DaemonPage
from .keymap_page import KeymapPage
from .model_page import ModelPage
from .session_page import SessionPage

_SCHEMA_PATH = Path(__file__).parent.parent.parent / "controller" / "schemas" / "controller_config.json"
_USER_CONFIG_PATH = Path.home() / ".config" / "icebreaker" / "controller.toml"


def _load_schema() -> dict:
    with _SCHEMA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_raw_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _dict_to_toml(data: dict, indent: int = 0) -> str:
    """Minimal TOML serializer for flat/nested dicts of primitives."""
    lines: list[str] = []
    prefix = "  " * indent
    scalars: list[tuple[str, Any]] = []
    tables: list[tuple[str, dict]] = []

    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            scalars.append((k, v))

    for k, v in scalars:
        if isinstance(v, bool):
            lines.append(f"{prefix}{k} = {'true' if v else 'false'}")
        elif isinstance(v, int):
            lines.append(f"{prefix}{k} = {v}")
        elif isinstance(v, float):
            lines.append(f"{prefix}{k} = {v}")
        elif isinstance(v, str):
            lines.append(f'{prefix}{k} = "{v}"')
        elif isinstance(v, list):
            items = ", ".join(f'"{i}"' if isinstance(i, str) else str(i) for i in v)
            lines.append(f"{prefix}{k} = [{items}]")

    for k, v in tables:
        lines.append("")
        if indent == 0:
            lines.append(f"[{k}]")
        else:
            lines.append(f"{prefix}[{k}]")
        lines.append(_dict_to_toml(v, indent + 1))

    return "\n".join(lines)


class SettingsWindow(Adw.PreferencesWindow):
    """Five-page preferences window backed by controller.toml."""

    def __init__(self, app: Adw.Application, config_path: Optional[Path] = None) -> None:
        super().__init__(application=app)
        self._config_path = config_path or _USER_CONFIG_PATH
        self._raw: dict = _load_raw_config(self._config_path)
        self._dirty = False

        self.set_title("Icebreaker Settings")
        self.set_default_size(720, 640)
        self.set_search_enabled(True)
        self.add_css_class("ib-window")

        self._backend_page = BackendPage(self._raw)
        self._model_page = ModelPage(self._raw)
        self._keymap_page = KeymapPage(self._raw)
        self._session_page = SessionPage(self._raw)
        self._daemon_page = DaemonPage(self._raw)

        self.add(self._backend_page)
        self.add(self._model_page)
        self.add(self._keymap_page)
        self.add(self._session_page)
        self.add(self._daemon_page)

        self._apply_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._apply_bar.set_halign(Gtk.Align.END)
        self._apply_bar.set_margin_top(8)
        self._apply_bar.set_margin_bottom(8)
        self._apply_bar.set_margin_end(12)

        self._apply_btn = Gtk.Button(label="Apply")
        self._apply_btn.add_css_class("ib-primary-button")
        self._apply_btn.connect("clicked", self._on_apply)
        self._apply_btn.set_sensitive(False)
        self._apply_bar.append(self._apply_btn)

        for page in (self._backend_page, self._model_page, self._keymap_page,
                     self._session_page, self._daemon_page):
            page.connect_dirty(self._mark_dirty)

        self.connect("close-request", self._on_close_request)

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._apply_btn.set_sensitive(True)

    def _collect(self) -> dict:
        """Collect current state from all pages into a raw TOML dict."""
        data: dict = {}
        self._backend_page.collect_into(data)
        self._model_page.collect_into(data)
        self._keymap_page.collect_into(data)
        self._session_page.collect_into(data)
        self._daemon_page.collect_into(data)
        return data

    def _validate(self, data: dict) -> Optional[str]:
        """Validate against the JSON Schema. Returns error message or None."""
        import jsonschema
        schema = _load_schema()
        validator = jsonschema.Draft7Validator(schema)
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
        if errors:
            first = errors[0]
            path = " → ".join(str(p) for p in first.absolute_path) or "root"
            return f"{path}: {first.message}"
        return None

    def _on_apply(self, _btn: Gtk.Button) -> None:
        data = self._collect()
        err = self._validate(data)
        if err:
            toast = Adw.Toast(title=f"Validation error: {err}")
            toast.set_timeout(5)
            self.add_toast(toast)
            return

        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        toml_text = _dict_to_toml(data)
        self._config_path.write_text(toml_text + "\n", encoding="utf-8")

        self._dirty = False
        self._apply_btn.set_sensitive(False)
        toast = Adw.Toast(title="Settings saved")
        toast.set_timeout(2)
        self.add_toast(toast)

    def _on_close_request(self, _win: Adw.PreferencesWindow) -> bool:
        if not self._dirty:
            return False
        dialog = Adw.AlertDialog(
            heading="Unsaved Changes",
            body="You have unsaved changes. Discard them?",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_discard_response)
        dialog.present(self)
        return True

    def _on_discard_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response == "discard":
            self._dirty = False
            self.close()
