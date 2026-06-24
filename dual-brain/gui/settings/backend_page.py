"""Backend page — QB backend selector with per-backend config rows.

BP-8: api_key_env field shows env var NAME only, never resolves values.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

_BACKENDS = ["gemini", "openai", "anthropic", "local"]

_API_DEFAULTS: dict[str, dict[str, Any]] = {
    "gemini": {
        "model": "gemini-2.5-flash",
        "api_key_env": "GEMINI_API_KEY",
        "max_tokens": 4096,
        "timeout_seconds": 30,
    },
    "openai": {
        "model": "gpt-4o",
        "api_key_env": "OPENAI_API_KEY",
        "max_tokens": 4096,
        "timeout_seconds": 30,
    },
    "anthropic": {
        "model": "claude-sonnet-4-6",
        "api_key_env": "ANTHROPIC_API_KEY",
        "max_tokens": 4096,
        "timeout_seconds": 30,
    },
}

_LOCAL_DEFAULTS: dict[str, Any] = {
    "model_id": "phi4-mini",
    "endpoint": "http://127.0.0.1:8080",
    "max_tokens": 2048,
    "timeout_seconds": 60,
    "transport": "http",
}


class BackendPage(Adw.PreferencesPage):
    """QB backend selector + dynamic per-backend config groups."""

    def __init__(self, raw: dict) -> None:
        super().__init__()
        self.set_title("AI Backend")
        self.set_icon_name("preferences-system-symbolic")

        self._dirty_cb: Optional[Callable[[], None]] = None
        self._raw_qb = raw.get("qb", {})
        self._current_backend = self._raw_qb.get("backend", "gemini")

        selector_group = Adw.PreferencesGroup(title="Quarantined Brain Backend")
        self.add(selector_group)

        self._backend_row = Adw.ComboRow(title="Backend")
        model = Gtk.StringList()
        for b in _BACKENDS:
            model.append(b.capitalize())
        self._backend_row.set_model(model)
        try:
            idx = _BACKENDS.index(self._current_backend)
        except ValueError:
            idx = 0
        self._backend_row.set_selected(idx)
        self._backend_row.connect("notify::selected", self._on_backend_changed)
        selector_group.add(self._backend_row)

        self._config_group = Adw.PreferencesGroup(title="Backend Configuration")
        self.add(self._config_group)

        self._entries: dict[str, Gtk.Widget] = {}
        self._build_config_rows()

    def connect_dirty(self, cb: Callable[[], None]) -> None:
        self._dirty_cb = cb

    def _signal_dirty(self, *_args: object) -> None:
        if self._dirty_cb:
            self._dirty_cb()

    def _clear_config_group(self) -> None:
        while True:
            child = self._config_group.get_first_child()
            if child is None:
                break
            self._config_group.remove(child)
        self._entries.clear()

    def _build_config_rows(self) -> None:
        self._clear_config_group()
        backend = _BACKENDS[self._backend_row.get_selected()]
        section = self._raw_qb.get(backend, {})

        if backend == "local":
            defaults = _LOCAL_DEFAULTS
            self._add_entry_row("model_id", "Model ID", section.get("model_id", defaults["model_id"]),
                                subtitle="Catalogue key from catalogue.toml")
            self._add_entry_row("endpoint", "Endpoint", section.get("endpoint", defaults["endpoint"]))
            self._add_spin_row("max_tokens", "Max Tokens",
                               section.get("max_tokens", defaults["max_tokens"]), 1, 32768)
            self._add_spin_row("timeout_seconds", "Timeout (s)",
                               section.get("timeout_seconds", defaults["timeout_seconds"]), 1, 600)
            self._add_combo_row("transport", "Transport", ["http", "unix"],
                                section.get("transport", defaults["transport"]))
        else:
            defaults = _API_DEFAULTS.get(backend, _API_DEFAULTS["gemini"])
            self._add_entry_row("model", "Model", section.get("model", defaults["model"]))
            self._add_entry_row("api_key_env", "API Key Env Var",
                                section.get("api_key_env", defaults["api_key_env"]),
                                subtitle="Name of the environment variable holding your API key (value is never stored)")
            self._add_spin_row("max_tokens", "Max Tokens",
                               section.get("max_tokens", defaults["max_tokens"]), 1, 32768)
            self._add_spin_row("timeout_seconds", "Timeout (s)",
                               section.get("timeout_seconds", defaults["timeout_seconds"]), 1, 600)

    def _add_entry_row(self, key: str, title: str, value: str,
                       subtitle: str = "") -> None:
        row = Adw.EntryRow(title=title)
        row.set_text(value)
        if subtitle:
            row.set_tooltip_text(subtitle)
        row.connect("changed", self._signal_dirty)
        self._config_group.add(row)
        self._entries[key] = row

    def _add_spin_row(self, key: str, title: str, value: int,
                      lo: int, hi: int) -> None:
        adj = Gtk.Adjustment(value=value, lower=lo, upper=hi, step_increment=1)
        row = Adw.SpinRow(title=title, adjustment=adj)
        row.connect("notify::value", self._signal_dirty)
        self._config_group.add(row)
        self._entries[key] = row

    def _add_combo_row(self, key: str, title: str, options: list[str],
                       current: str) -> None:
        row = Adw.ComboRow(title=title)
        model = Gtk.StringList()
        for opt in options:
            model.append(opt)
        row.set_model(model)
        try:
            idx = options.index(current)
        except ValueError:
            idx = 0
        row.set_selected(idx)
        row.connect("notify::selected", self._signal_dirty)
        self._config_group.add(row)
        self._entries[key] = row

    def _on_backend_changed(self, row: Adw.ComboRow, _pspec: object) -> None:
        self._current_backend = _BACKENDS[row.get_selected()]
        self._build_config_rows()
        self._signal_dirty()

    def _get_entry_value(self, key: str) -> Any:
        widget = self._entries.get(key)
        if widget is None:
            return None
        if isinstance(widget, Adw.EntryRow):
            return widget.get_text()
        if isinstance(widget, Adw.SpinRow):
            return int(widget.get_value())
        if isinstance(widget, Adw.ComboRow):
            model = widget.get_model()
            return model.get_string(widget.get_selected())
        return None

    def collect_into(self, data: dict) -> None:
        backend = _BACKENDS[self._backend_row.get_selected()]
        qb: dict[str, Any] = {"backend": backend}

        section: dict[str, Any] = {}
        for key in self._entries:
            val = self._get_entry_value(key)
            if val is not None:
                section[key] = val
        qb[backend] = section
        data["qb"] = qb
