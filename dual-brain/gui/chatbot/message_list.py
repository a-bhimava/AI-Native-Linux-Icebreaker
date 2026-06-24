"""Message list — scrollable conversation history.

Wraps a Gtk.ListBox in a ScrolledWindow. Auto-scrolls to bottom on
new messages. Supports three message roles: user, assistant, system.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from .message_row import MessageRow


class MessageList(Gtk.ScrolledWindow):
    """Scrollable list of conversation messages."""

    def __init__(self) -> None:
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._listbox.add_css_class("boxed-list")
        self.set_child(self._listbox)

        self._rows: list[MessageRow] = []

    def add_message(self, role: str, text: str, **kwargs: object) -> MessageRow:
        row = MessageRow(role, text, **kwargs)
        self._listbox.append(row)
        self._rows.append(row)
        GLib.idle_add(self._scroll_to_bottom)
        return row

    def add_system(self, text: str) -> MessageRow:
        return self.add_message("system", text)

    def add_user(self, text: str) -> MessageRow:
        return self.add_message("user", text)

    def add_assistant(self, text: str, **kwargs: object) -> MessageRow:
        return self.add_message("assistant", text, **kwargs)

    def clear(self) -> None:
        self._listbox.remove_all()
        self._rows.clear()

    @property
    def last_row(self) -> Optional[MessageRow]:
        return self._rows[-1] if self._rows else None

    @property
    def count(self) -> int:
        return len(self._rows)

    def _scroll_to_bottom(self) -> bool:
        adj = self.get_vadjustment()
        if adj is not None:
            adj.set_value(adj.get_upper())
        return False
