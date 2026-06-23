"""Keymap page — visual HITL keybinding editor.

Reuses validation logic from controller.keymap. Esc (DENY) and ? (HELP)
are shown as locked — they cannot be remapped.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk

from controller.keymap import DEFAULTS, Action


class _KeyCaptureDialog(Adw.AlertDialog):
    """Modal dialog that captures a single keypress."""

    def __init__(self, action: Action, callback: Callable[[str], None]) -> None:
        super().__init__(
            heading=f"Press a key for {action.value.capitalize()}",
            body="Press any single character key, or Esc to cancel.",
        )
        self._callback = callback
        self._action = action
        self.add_response("cancel", "Cancel")
        self.set_default_response("cancel")
        self.set_close_response("cancel")

    def capture_key(self, keyval: int) -> bool:
        char = chr(keyval) if 32 <= keyval < 127 else ""
        if char and char not in ("\x1b",):
            self._callback(char)
            self.force_close()
            return True
        return False


class _KeymapRow(Adw.ActionRow):
    """Row for a single action's key bindings."""

    def __init__(self, action: Action, keys: tuple[str, ...],
                 locked: bool, on_change: Callable[[], None]) -> None:
        super().__init__()
        self.action = action
        self.keys = list(keys)
        self._locked = locked
        self._on_change = on_change

        self.set_title(action.value.capitalize())
        self._update_subtitle()

        if not locked:
            change_btn = Gtk.Button(label="Change")
            change_btn.set_valign(Gtk.Align.CENTER)
            change_btn.add_css_class("flat")
            change_btn.connect("clicked", self._on_change_clicked)
            self.add_suffix(change_btn)

            reset_btn = Gtk.Button(label="Reset")
            reset_btn.set_valign(Gtk.Align.CENTER)
            reset_btn.add_css_class("flat")
            reset_btn.connect("clicked", self._on_reset)
            self.add_suffix(reset_btn)
        else:
            lock_icon = Gtk.Image.new_from_icon_name("changes-prevent-symbolic")
            lock_icon.set_valign(Gtk.Align.CENTER)
            lock_icon.set_tooltip_text("Reserved — cannot be remapped")
            self.add_suffix(lock_icon)

    def _update_subtitle(self) -> None:
        display = "  ".join(f"[{k}]" for k in self.keys)
        if self._locked:
            display += "  (locked)"
        self.set_subtitle(display)

    def _on_change_clicked(self, _btn: Gtk.Button) -> None:
        dialog = _KeyCaptureDialog(self.action, self._apply_key)
        win = self.get_root()
        if win is not None:
            key_ctrl = Gtk.EventControllerKey()
            key_ctrl.connect("key-pressed", self._on_key_in_dialog, dialog)
            dialog_widget = dialog
            if hasattr(win, "add_controller"):
                win.add_controller(key_ctrl)
                self._temp_ctrl = key_ctrl
            dialog.present(win)
        else:
            dialog.present()

    def _on_key_in_dialog(self, _ctrl: Gtk.EventControllerKey,
                          keyval: int, _keycode: int,
                          _state: Gdk.ModifierType,
                          dialog: _KeyCaptureDialog) -> bool:
        return dialog.capture_key(keyval)

    def _apply_key(self, char: str) -> None:
        if char not in self.keys:
            self.keys.append(char)
        self._update_subtitle()
        self._on_change()
        if hasattr(self, "_temp_ctrl"):
            win = self.get_root()
            if win and hasattr(win, "remove_controller"):
                win.remove_controller(self._temp_ctrl)
            del self._temp_ctrl

    def _on_reset(self, _btn: Gtk.Button) -> None:
        defaults = DEFAULTS.get(self.action, ())
        self.keys = list(defaults)
        self._update_subtitle()
        self._on_change()


class KeymapPage(Adw.PreferencesPage):
    """Visual keymap editor for HITL approval bindings."""

    def __init__(self, raw: dict) -> None:
        super().__init__()
        self.set_title("Keybindings")
        self.set_icon_name("input-keyboard-symbolic")

        self._dirty_cb: Optional[Callable[[], None]] = None
        self._raw_keymap = raw.get("keymap", {})

        group = Adw.PreferencesGroup(
            title="HITL Approval Keybindings",
            description="Key bindings for the human-in-the-loop approval gate",
        )
        self.add(group)

        self._rows: dict[Action, _KeymapRow] = {}
        for action in Action:
            default_keys = DEFAULTS.get(action, ())
            configured = self._raw_keymap.get(action.value)
            keys = tuple(configured) if configured else default_keys
            locked = action in (Action.HELP,)
            row = _KeymapRow(action, keys, locked, self._signal_dirty)
            self._rows[action] = row
            group.add(row)

        self._error_banner = Adw.Banner(title="")
        self._error_banner.set_revealed(False)

    def connect_dirty(self, cb: Callable[[], None]) -> None:
        self._dirty_cb = cb

    def _signal_dirty(self) -> None:
        self._validate()
        if self._dirty_cb:
            self._dirty_cb()

    def _validate(self) -> bool:
        seen: dict[str, str] = {}
        for action, row in self._rows.items():
            for key in row.keys:
                if key in seen:
                    self._error_banner.set_title(
                        f"Duplicate key '{key}' bound to both "
                        f"{seen[key]} and {action.value}"
                    )
                    self._error_banner.set_revealed(True)
                    return False
                seen[key] = action.value
        self._error_banner.set_revealed(False)
        return True

    def collect_into(self, data: dict) -> None:
        keymap: dict[str, list[str]] = {}
        for action, row in self._rows.items():
            if row.keys != list(DEFAULTS.get(action, ())):
                keymap[action.value] = list(row.keys)
        if keymap:
            data["keymap"] = keymap
