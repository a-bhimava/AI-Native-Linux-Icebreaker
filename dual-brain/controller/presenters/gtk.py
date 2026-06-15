"""GTK4 presenter scaffold — GUI approval dialog.

Delivers a working GTK4 dialog with lockout, approve/deny buttons, and
keyboard shortcuts matching the terminal keymap. GTK4 theme integration,
system tray, and Wayland edge cases are Phase 6.

The GTK main loop runs on a separate thread; the calling thread blocks
on a ``threading.Event`` until the user decides (R-P2.5 mitigation).

Requires PyGObject: ``pip install PyGObject`` or ``apt install python3-gi``.
Raises ``BrainConfigError`` at construction if PyGObject is not installed.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from ..backends.base import BrainConfigError
from ..hitl import Decision, HitlDisplayData, HitlPresenter
from ..keymap import Keymap
from .registry import register_presenter


@register_presenter("gtk")
class GtkPresenter(HitlPresenter):
    """GTK4 dialog presenter scaffold."""

    def __init__(self, *, keymap: Optional[Keymap] = None) -> None:
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk, GLib  # noqa: F401
            self._gi = gi
            self._Gtk = Gtk
            self._GLib = GLib
        except (ImportError, ValueError) as exc:
            raise BrainConfigError(
                f"GTK presenter requires PyGObject: {exc}. "
                "Install with: pip install PyGObject (or apt install python3-gi)"
            ) from None
        self._keymap = keymap or Keymap()
        self._last_key_class: str = ""
        self._decision: Optional[Decision] = None
        self._decision_event = threading.Event()

    @property
    def last_key_class(self) -> str:
        return self._last_key_class

    def show_prompt(self, data: HitlDisplayData) -> None:
        self._pending_data = data
        self._decision = None
        self._decision_event.clear()

    def lockout(self, seconds: int) -> None:
        time.sleep(seconds)

    def read_decision(self, timeout_seconds: int) -> Decision:
        data = getattr(self, "_pending_data", None)
        if data is None:
            self._last_key_class = "error"
            return Decision.DENIED

        gtk_thread = threading.Thread(
            target=self._run_dialog,
            args=(data, timeout_seconds),
            daemon=True,
        )
        gtk_thread.start()

        self._decision_event.wait(timeout=timeout_seconds)

        if self._decision is None:
            self._last_key_class = "timeout"
            return Decision.TIMEOUT

        return self._decision

    def _run_dialog(self, data: HitlDisplayData, timeout_seconds: int) -> None:
        Gtk = self._Gtk
        GLib = self._GLib

        app = Gtk.Application(application_id="org.icebreaker.hitl")

        def on_activate(app: object) -> None:
            window = Gtk.ApplicationWindow(application=app)
            window.set_title("Icebreaker — Action Approval Required")
            window.set_default_size(500, 400)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            box.set_margin_top(20)
            box.set_margin_bottom(20)
            box.set_margin_start(20)
            box.set_margin_end(20)

            fields = [
                ("Action", data.action),
                ("Target", data.target or "none"),
                ("Risk", data.risk_level),
                ("Reversible", "Yes" if data.reversible else "No"),
            ]
            if data.reason:
                fields.append(("Reason", data.reason))
            if data.backend:
                fields.append(("Backend", data.backend))

            for label, value in fields:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                lbl = Gtk.Label(label=f"{label}:")
                lbl.set_xalign(0)
                lbl.set_size_request(100, -1)
                val = Gtk.Label(label=value)
                val.set_xalign(0)
                val.set_wrap(True)
                row.append(lbl)
                row.append(val)
                box.append(row)

            if data.cow_summary:
                preview_label = Gtk.Label(label="Dry-run preview:")
                preview_label.set_xalign(0)
                box.append(preview_label)
                preview = Gtk.Label(label=data.cow_summary[:500])
                preview.set_xalign(0)
                preview.set_wrap(True)
                box.append(preview)

            button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            button_box.set_halign(Gtk.Align.CENTER)

            approve_btn = Gtk.Button(label="Approve")
            deny_btn = Gtk.Button(label="Deny")
            modify_btn = Gtk.Button(label="Modify")
            explain_btn = Gtk.Button(label="Explain")

            def _set_decision(decision: Decision, key_class: str = "gui") -> None:
                self._decision = decision
                self._last_key_class = key_class
                self._decision_event.set()
                app.quit()

            approve_btn.connect("clicked", lambda _: _set_decision(Decision.APPROVED))
            deny_btn.connect("clicked", lambda _: _set_decision(Decision.DENIED))
            modify_btn.connect("clicked", lambda _: _set_decision(Decision.MODIFY))
            explain_btn.connect("clicked", lambda _: _set_decision(Decision.EXPLAIN))

            button_box.append(approve_btn)
            button_box.append(deny_btn)
            button_box.append(modify_btn)
            button_box.append(explain_btn)
            box.append(button_box)

            window.set_child(box)

            key_controller = Gtk.EventControllerKey()

            def on_key_pressed(_ctrl: object, keyval: int, _keycode: int, _state: object) -> bool:
                key_name = self._GLib.unichar_to_utf8(keyval) if keyval < 128 else ""
                if keyval == 65307:  # Escape
                    _set_decision(Decision.DENIED, "esc")
                    return True
                if key_name:
                    from ..keymap import Action
                    action = self._keymap.lookup(key_name)
                    if action == Action.APPROVE:
                        _set_decision(Decision.APPROVED, "mnemonic")
                        return True
                    if action == Action.DENY:
                        _set_decision(Decision.DENIED, "mnemonic")
                        return True
                return False

            key_controller.connect("key-pressed", on_key_pressed)
            window.add_controller(key_controller)

            window.connect("close-request", lambda _: _set_decision(Decision.DENIED, "close"))
            window.present()

            GLib.timeout_add_seconds(timeout_seconds, lambda: _set_decision(Decision.TIMEOUT, "timeout"))

        app.connect("activate", on_activate)

        try:
            app.run(None)
        except Exception:
            if self._decision is None:
                self._decision = Decision.DENIED
                self._last_key_class = "error"
                self._decision_event.set()
