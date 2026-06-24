"""Session page — UX and session preferences.

Color mode, history, spinner, streaming, prompt prefix, and related
display/behavior knobs.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk


class SessionPage(Adw.PreferencesPage):
    """Session and display preferences."""

    def __init__(self, raw: dict) -> None:
        super().__init__()
        self.set_title("Session")
        self.set_icon_name("preferences-desktop-appearance-symbolic")

        self._dirty_cb: Optional[Callable[[], None]] = None
        self._raw = raw.get("session", {})
        self._raw_desktop = raw.get("desktop", {})

        appearance = Adw.PreferencesGroup(title="Appearance")
        self.add(appearance)

        self._dark_switch = Adw.SwitchRow(
            title="Dark Mode",
            subtitle="Use dark color palette",
        )
        self._dark_switch.set_active(self._raw_desktop.get("dark", True))
        self._dark_switch.connect("notify::active", self._signal_dirty)
        appearance.add(self._dark_switch)

        self._color_row = Adw.ComboRow(title="Terminal Color")
        color_model = Gtk.StringList()
        for opt in ("auto", "always", "never"):
            color_model.append(opt)
        self._color_row.set_model(color_model)
        current_color = self._raw.get("color", "auto")
        self._color_row.set_selected(["auto", "always", "never"].index(current_color)
                                     if current_color in ("auto", "always", "never") else 0)
        self._color_row.connect("notify::selected", self._signal_dirty)
        appearance.add(self._color_row)

        self._prefix_row = Adw.EntryRow(title="Prompt Prefix")
        self._prefix_row.set_text(self._raw.get("prompt_prefix", "icebreaker"))
        self._prefix_row.connect("changed", self._signal_dirty)
        appearance.add(self._prefix_row)

        behavior = Adw.PreferencesGroup(title="Behavior")
        self.add(behavior)

        self._spinner_switch = Adw.SwitchRow(
            title="Show Spinner",
            subtitle="Live braille spinner while processing",
        )
        self._spinner_switch.set_active(self._raw.get("show_spinner", True))
        self._spinner_switch.connect("notify::active", self._signal_dirty)
        behavior.add(self._spinner_switch)

        self._progress_switch = Adw.SwitchRow(
            title="Show Progress",
            subtitle="Step-by-step pipeline progress",
        )
        self._progress_switch.set_active(self._raw.get("show_progress", True))
        self._progress_switch.connect("notify::active", self._signal_dirty)
        behavior.add(self._progress_switch)

        self._stream_switch = Adw.SwitchRow(
            title="Stream Output",
            subtitle="Stream QB summarisation tokens as they arrive",
        )
        self._stream_switch.set_active(self._raw.get("stream_output", True))
        self._stream_switch.connect("notify::active", self._signal_dirty)
        behavior.add(self._stream_switch)

        self._ephemeral_switch = Adw.SwitchRow(
            title="Ephemeral History",
            subtitle="In-memory only — no file-based readline history (SF-8)",
        )
        self._ephemeral_switch.set_active(self._raw.get("ephemeral_history", True))
        self._ephemeral_switch.connect("notify::active", self._signal_dirty)
        behavior.add(self._ephemeral_switch)

        limits = Adw.PreferencesGroup(title="Limits")
        self.add(limits)

        self._ttl_adj = Gtk.Adjustment(
            value=self._raw.get("session_ttl_seconds", 1800),
            lower=0, upper=86400, step_increment=60,
        )
        self._ttl_row = Adw.SpinRow(
            title="Session Timeout (s)",
            adjustment=self._ttl_adj,
        )
        self._ttl_row.set_subtitle("Inactivity timeout; 0 = disabled")
        self._ttl_row.connect("notify::value", self._signal_dirty)
        limits.add(self._ttl_row)

        self._turns_adj = Gtk.Adjustment(
            value=self._raw.get("max_turns", 50),
            lower=1, upper=1000, step_increment=1,
        )
        self._turns_row = Adw.SpinRow(
            title="Max Turns",
            adjustment=self._turns_adj,
        )
        self._turns_row.connect("notify::value", self._signal_dirty)
        limits.add(self._turns_row)

        self._output_adj = Gtk.Adjustment(
            value=self._raw.get("max_tool_output_lines", 40),
            lower=1, upper=10000, step_increment=10,
        )
        self._output_row = Adw.SpinRow(
            title="Max Tool Output Lines",
            adjustment=self._output_adj,
        )
        self._output_row.connect("notify::value", self._signal_dirty)
        limits.add(self._output_row)

    def connect_dirty(self, cb: Callable[[], None]) -> None:
        self._dirty_cb = cb

    def _signal_dirty(self, *_args: object) -> None:
        if self._dirty_cb:
            self._dirty_cb()

    def collect_into(self, data: dict) -> None:
        session: dict[str, Any] = {}
        session["color"] = ["auto", "always", "never"][self._color_row.get_selected()]
        session["prompt_prefix"] = self._prefix_row.get_text()
        session["show_spinner"] = self._spinner_switch.get_active()
        session["show_progress"] = self._progress_switch.get_active()
        session["stream_output"] = self._stream_switch.get_active()
        session["ephemeral_history"] = self._ephemeral_switch.get_active()
        session["session_ttl_seconds"] = int(self._ttl_row.get_value())
        session["max_turns"] = int(self._turns_row.get_value())
        session["max_tool_output_lines"] = int(self._output_row.get_value())
        data["session"] = session

        desktop = data.get("desktop", {})
        desktop["dark"] = self._dark_switch.get_active()
        data["desktop"] = desktop
