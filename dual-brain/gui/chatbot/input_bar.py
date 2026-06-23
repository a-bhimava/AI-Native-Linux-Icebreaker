"""Input bar — text entry + send button for the chatbot window.

Enter sends the message. Shift+Enter inserts a newline.
The send button shows a spinner while awaiting a daemon response.
Ctrl+L clears conversation history (emitted as a signal).
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk


class InputBar(Gtk.Box):
    """Chat input bar with text view and send button."""

    def __init__(self, on_send: Callable[[str], None],
                 on_clear: Optional[Callable[[], None]] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(8)
        self.set_margin_bottom(8)

        self._on_send = on_send
        self._on_clear = on_clear
        self._busy = False

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_hexpand(True)
        self._scroll.set_max_content_height(100)
        self._scroll.set_propagate_natural_height(True)
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._text_view = Gtk.TextView()
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._text_view.set_accepts_tab(False)
        self._text_view.add_css_class("ib-input")
        self._text_view.set_top_margin(8)
        self._text_view.set_bottom_margin(8)
        self._text_view.set_left_margin(12)
        self._text_view.set_right_margin(12)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self._text_view.add_controller(key_ctrl)

        self._scroll.set_child(self._text_view)
        self.append(self._scroll)

        self._send_btn = Gtk.Button(label="Send")
        self._send_btn.add_css_class("ib-primary-button")
        self._send_btn.set_valign(Gtk.Align.END)
        self._send_btn.connect("clicked", self._on_send_clicked)
        self.append(self._send_btn)

        self._spinner = Gtk.Spinner()
        self._spinner.set_visible(False)
        self._spinner.set_valign(Gtk.Align.CENTER)
        self.append(self._spinner)

    def _on_key_pressed(self, _ctrl: Gtk.EventControllerKey,
                        keyval: int, _keycode: int,
                        state: Gdk.ModifierType) -> bool:
        if keyval == Gdk.KEY_Return and not (state & Gdk.ModifierType.SHIFT_MASK):
            self._submit()
            return True
        if keyval == Gdk.KEY_l and (state & Gdk.ModifierType.CONTROL_MASK):
            if self._on_clear:
                self._on_clear()
            return True
        return False

    def _on_send_clicked(self, _btn: Gtk.Button) -> None:
        self._submit()

    def _submit(self) -> None:
        if self._busy:
            return
        buf = self._text_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not text:
            return
        buf.set_text("")
        self._on_send(text)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._send_btn.set_sensitive(not busy)
        self._send_btn.set_visible(not busy)
        self._spinner.set_visible(busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()
            self._text_view.grab_focus()

    def get_text(self) -> str:
        buf = self._text_view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def focus(self) -> None:
        self._text_view.grab_focus()
