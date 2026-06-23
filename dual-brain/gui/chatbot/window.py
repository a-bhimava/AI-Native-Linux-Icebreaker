"""ChatbotWindow — main NL conversation window.

Connects to the Controller daemon via GtkDaemonClient, sends user
messages as nl.turn requests, and renders streamed CoT + results.
"""

from __future__ import annotations

from typing import Any, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from ..daemon_client import GtkDaemonClient
from ..widgets import _load_widget_classes, _sanitize
from .input_bar import InputBar
from .message_list import MessageList
from .message_row import MessageRow


class ChatbotWindow(Adw.ApplicationWindow):
    """NL chatbot window with conversation history and daemon IPC."""

    def __init__(self, app: Adw.Application,
                 client: Optional[GtkDaemonClient] = None) -> None:
        super().__init__(application=app)
        self._client = client
        self._current_row: Optional[MessageRow] = None

        self.set_title("Icebreaker")
        self.set_default_size(800, 600)
        self.add_css_class("ib-window")

        _load_widget_classes()
        from ..widgets import StatusDot

        header = Adw.HeaderBar()

        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu = Gio.Menu()
        menu.append("New Conversation", "win.new-conversation")
        menu.append("About", "win.about")
        menu_btn.set_menu_model(menu)
        header.pack_start(menu_btn)

        self._status_dot = StatusDot(connected=client is not None)
        header.pack_end(self._status_dot)

        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text("Settings")
        settings_btn.connect("clicked", self._on_open_settings)
        header.pack_end(settings_btn)

        self._message_list = MessageList()
        self._input_bar = InputBar(on_send=self._on_send, on_clear=self._on_clear)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content_box.set_vexpand(True)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)

        content_box.append(self._message_list)
        content_box.append(sep)
        content_box.append(self._input_bar)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(content_box)
        self.set_content(toolbar)

        new_conv = Gio.SimpleAction.new("new-conversation", None)
        new_conv.connect("activate", lambda *_: self._on_clear())
        self.add_action(new_conv)

        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about)
        self.add_action(about)

        self._message_list.add_system(
            "Welcome to Icebreaker.\nType a natural-language command."
        )

        if client is not None:
            client.on("cot", self._on_cot)
            client.on("token", self._on_token)
            client.on("progress", self._on_progress)
            client.on("info", self._on_info)

        self._input_bar.focus()

    def _on_send(self, text: str) -> None:
        self._message_list.add_user(text)
        self._input_bar.set_busy(True)

        if self._client is None:
            self._message_list.add_system(
                "Not connected to daemon. Start the controller daemon first."
            )
            self._input_bar.set_busy(False)
            return

        self._current_row = self._message_list.add_assistant("")
        if self._current_row.cot is not None:
            self._current_row.cot.set_visible(True)

        try:
            self._client.request("nl.turn", {"text": text}, callback=self._on_turn_result)
        except Exception as exc:
            self._message_list.add_system(f"Error: {_sanitize(str(exc))}")
            self._input_bar.set_busy(False)
            self._current_row = None

    def _on_turn_result(self, result: dict) -> None:
        GLib.idle_add(self._handle_turn_result, result)

    def _handle_turn_result(self, result: dict) -> bool:
        self._input_bar.set_busy(False)

        text = result.get("summary", result.get("text", ""))
        tier = result.get("tier")
        backend = result.get("backend", "")
        latency_ms = result.get("latency_ms")
        cost = result.get("cost")

        if self._current_row and self._current_row.cot:
            self._current_row.cot.complete_last()

        self._message_list.add_assistant(
            text, tier=tier, backend=backend,
            latency_ms=latency_ms, cost=cost,
        )
        self._current_row = None
        return False

    def _on_cot(self, params: dict) -> None:
        if self._current_row and self._current_row.cot:
            step_text = params.get("step", params.get("text", ""))
            state = params.get("state", "pending")
            self._current_row.cot.add_step(step_text, state)

    def _on_token(self, params: dict) -> None:
        pass

    def _on_progress(self, params: dict) -> None:
        stage = params.get("stage", "")
        if self._current_row and self._current_row.cot:
            self._current_row.cot.add_step(stage, "pending")

    def _on_info(self, params: dict) -> None:
        msg = params.get("message", "")
        if msg:
            self._message_list.add_system(msg)

    def _on_clear(self) -> None:
        self._message_list.clear()
        self._current_row = None
        self._message_list.add_system(
            "Welcome to Icebreaker.\nType a natural-language command."
        )
        self._input_bar.set_busy(False)

    def _on_open_settings(self, _btn: Gtk.Button) -> None:
        from ..settings.window import SettingsWindow
        win = SettingsWindow(app=self.get_application())
        win.present()

    def _on_about(self, _action: Gio.SimpleAction, _param: Any) -> None:
        about = Adw.AboutDialog(
            application_name="Icebreaker",
            application_icon="icebreaker",
            version="0.1.0",
            comments="AI-Native OS — natural language to safe system operations",
            website="https://github.com/a-bhimava/icebreaker",
        )
        about.present(self)
