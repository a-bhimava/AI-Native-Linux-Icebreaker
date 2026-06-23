"""Input bar — Copilot-style text entry with backend selector.

Centered, elevated card with placeholder text, model dropdown, and
send action. Enter sends. Shift+Enter for newline. Ctrl+L clears.
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk

_BACKENDS = ["Gemini", "OpenAI", "Anthropic", "Local"]


class InputBar(Gtk.Box):
    """Elevated input bar with text entry and backend selector."""

    def __init__(self, on_send: Callable[[str], None],
                 on_clear: Optional[Callable[[], None]] = None,
                 backend: str = "Gemini") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_halign(Gtk.Align.CENTER)
        self.set_margin_start(24)
        self.set_margin_end(24)
        self.set_margin_top(8)
        self.set_margin_bottom(16)

        self._on_send = on_send
        self._on_clear = on_clear
        self._busy = False

        self._card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._card.add_css_class("ib-elevated-input")
        self._card.set_size_request(560, -1)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_hexpand(True)
        self._scroll.set_max_content_height(120)
        self._scroll.set_propagate_natural_height(True)
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._text_view = Gtk.TextView()
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._text_view.set_accepts_tab(False)
        self._text_view.set_top_margin(12)
        self._text_view.set_bottom_margin(8)
        self._text_view.set_left_margin(16)
        self._text_view.set_right_margin(16)

        buf = self._text_view.get_buffer()
        buf.connect("changed", self._on_text_changed)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self._text_view.add_controller(key_ctrl)

        self._scroll.set_child(self._text_view)
        self._card.append(self._scroll)

        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bottom_bar.set_margin_start(12)
        bottom_bar.set_margin_end(12)
        bottom_bar.set_margin_bottom(8)
        bottom_bar.set_margin_top(4)

        self._backend_btn = Gtk.MenuButton()
        self._backend_btn.set_label(backend)
        self._backend_btn.add_css_class("flat")

        backend_menu = Gtk.PopoverMenu()
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        menu_box.set_margin_start(4)
        menu_box.set_margin_end(4)
        menu_box.set_margin_top(4)
        menu_box.set_margin_bottom(4)
        for b in _BACKENDS:
            btn = Gtk.Button(label=b)
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_backend_selected, b, backend_menu)
            menu_box.append(btn)
        backend_menu.set_child(menu_box)
        self._backend_btn.set_popover(backend_menu)
        bottom_bar.append(self._backend_btn)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bottom_bar.append(spacer)

        self._spinner = Gtk.Spinner()
        self._spinner.set_visible(False)
        bottom_bar.append(self._spinner)

        self._send_btn = Gtk.Button(icon_name="go-up-symbolic")
        self._send_btn.add_css_class("ib-primary-button")
        self._send_btn.add_css_class("circular")
        self._send_btn.set_sensitive(False)
        self._send_btn.set_tooltip_text("Send (Enter)")
        self._send_btn.connect("clicked", self._on_send_clicked)
        bottom_bar.append(self._send_btn)

        self._card.append(bottom_bar)
        self.append(self._card)

    def _on_backend_selected(self, btn: Gtk.Button, name: str,
                             popover: Gtk.PopoverMenu) -> None:
        self._backend_btn.set_label(name)
        popover.popdown()

    def _on_text_changed(self, buf: object) -> None:
        text = self._get_text()
        self._send_btn.set_sensitive(bool(text.strip()) and not self._busy)

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
        text = self._get_text().strip()
        if not text:
            return
        buf = self._text_view.get_buffer()
        buf.set_text("")
        self._on_send(text)

    def _get_text(self) -> str:
        buf = self._text_view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._send_btn.set_visible(not busy)
        self._spinner.set_visible(busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()
            self._text_view.grab_focus()

    @property
    def backend(self) -> str:
        return self._backend_btn.get_label()

    def get_text(self) -> str:
        return self._get_text()

    def focus(self) -> None:
        self._text_view.grab_focus()
