"""ChatbotWindow — Copilot-style NL conversation window.

Two-state layout:
  - Welcome state: centered greeting, input bar, suggestion chips
  - Active state: scrolling message list, bottom-pinned input bar

Left sidebar with icon navigation to Settings, Audit, etc.
"""

from __future__ import annotations

import os
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

_SUGGESTIONS = [
    "Show disk usage",
    "List running services",
    "Check system updates",
    "Show network status",
    "View recent logs",
    "Check CPU temperature",
]


class ChatbotWindow(Adw.ApplicationWindow):
    """Copilot-style NL chatbot with welcome state and sidebar."""

    def __init__(self, app: Adw.Application,
                 client: Optional[GtkDaemonClient] = None) -> None:
        super().__init__(application=app)
        self._client = client
        self._current_row: Optional[MessageRow] = None
        self._in_welcome = True

        self.set_title("Icebreaker")
        self.set_default_size(900, 650)
        self.add_css_class("ib-window")

        _load_widget_classes()

        root_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        sidebar = self._build_sidebar()
        root_box.append(sidebar)

        self._main_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._main_area.set_hexpand(True)
        self._main_area.set_vexpand(True)
        root_box.append(self._main_area)

        self._message_list = MessageList()
        self._input_bar = InputBar(
            on_send=self._on_send,
            on_clear=self._on_clear,
            backend="Gemini",
        )

        self._welcome_view = self._build_welcome()
        self._chat_view = self._build_chat()
        self._chat_view.set_visible(False)

        self._main_area.append(self._welcome_view)
        self._main_area.append(self._chat_view)

        self.set_content(root_box)

        new_conv = Gio.SimpleAction.new("new-conversation", None)
        new_conv.connect("activate", lambda *_: self._on_clear())
        self.add_action(new_conv)

        if client is not None:
            client.on("cot", self._on_cot)
            client.on("token", self._on_token)
            client.on("progress", self._on_progress)
            client.on("info", self._on_info)

    def _build_sidebar(self) -> Gtk.Box:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sidebar.add_css_class("ib-sidebar")
        sidebar.set_size_request(48, -1)
        sidebar.set_margin_top(8)
        sidebar.set_margin_bottom(8)

        logo_btn = Gtk.Button(icon_name="starred-symbolic")
        logo_btn.add_css_class("flat")
        logo_btn.set_tooltip_text("Icebreaker")
        logo_btn.connect("clicked", lambda _: self._on_clear())
        sidebar.append(logo_btn)

        sep1 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep1.set_margin_top(4)
        sep1.set_margin_bottom(4)
        sep1.set_margin_start(8)
        sep1.set_margin_end(8)
        sidebar.append(sep1)

        chat_btn = Gtk.Button(icon_name="user-available-symbolic")
        chat_btn.add_css_class("flat")
        chat_btn.set_tooltip_text("New Chat")
        chat_btn.connect("clicked", lambda _: self._on_clear())
        sidebar.append(chat_btn)

        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.add_css_class("flat")
        settings_btn.set_tooltip_text("Settings")
        settings_btn.connect("clicked", self._on_open_settings)
        sidebar.append(settings_btn)

        audit_btn = Gtk.Button(icon_name="document-open-recent-symbolic")
        audit_btn.add_css_class("flat")
        audit_btn.set_tooltip_text("Audit Log")
        sidebar.append(audit_btn)

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        sidebar.append(spacer)

        from ..widgets import StatusDot
        self._status_dot = StatusDot(connected=self._client is not None)
        self._status_dot.set_halign(Gtk.Align.CENTER)
        self._status_dot.set_margin_bottom(8)
        sidebar.append(self._status_dot)

        return sidebar

    def _build_welcome(self) -> Gtk.Box:
        view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        view.set_vexpand(True)
        view.set_hexpand(True)

        top_spacer = Gtk.Box()
        top_spacer.set_vexpand(True)
        view.append(top_spacer)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        center.set_halign(Gtk.Align.CENTER)
        center.set_valign(Gtk.Align.CENTER)

        user = os.environ.get("USER", "")
        greeting_text = f"What should we do, {user}?" if user else "What should we do today?"
        greeting = Gtk.Label(label=greeting_text)
        greeting.add_css_class("ib-greeting")
        center.append(greeting)

        center.append(self._input_bar)

        chips_flow = Gtk.FlowBox()
        chips_flow.set_max_children_per_line(4)
        chips_flow.set_min_children_per_line(2)
        chips_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        chips_flow.set_column_spacing(8)
        chips_flow.set_row_spacing(8)
        chips_flow.set_halign(Gtk.Align.CENTER)
        chips_flow.set_homogeneous(False)

        for suggestion in _SUGGESTIONS:
            chip = Gtk.Button(label=suggestion)
            chip.add_css_class("ib-chip")
            chip.connect("clicked", self._on_chip_clicked, suggestion)
            chips_flow.append(chip)

        center.append(chips_flow)
        view.append(center)

        bottom_spacer = Gtk.Box()
        bottom_spacer.set_vexpand(True)
        view.append(bottom_spacer)

        return view

    def _build_chat(self) -> Gtk.Box:
        view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        view.set_vexpand(True)
        view.set_hexpand(True)

        view.append(self._message_list)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        view.append(sep)

        input_bar_chat = InputBar(
            on_send=self._on_send,
            on_clear=self._on_clear,
            backend=self._input_bar.backend,
        )
        self._chat_input_bar = input_bar_chat
        view.append(input_bar_chat)

        return view

    def _switch_to_chat(self) -> None:
        if not self._in_welcome:
            return
        self._in_welcome = False
        self._welcome_view.set_visible(False)
        self._chat_view.set_visible(True)

    def _on_chip_clicked(self, _btn: Gtk.Button, text: str) -> None:
        self._on_send(text)

    def _on_send(self, text: str) -> None:
        self._switch_to_chat()

        self._message_list.add_user(text)
        self._chat_input_bar.set_busy(True)
        self._input_bar.set_busy(True)

        if self._client is None:
            self._message_list.add_system(
                "Not connected to daemon. Start the controller daemon first."
            )
            self._chat_input_bar.set_busy(False)
            self._input_bar.set_busy(False)
            return

        self._current_row = self._message_list.add_assistant("")
        if self._current_row.cot is not None:
            self._current_row.cot.set_visible(True)

        try:
            self._client.request("nl.turn", {"text": text}, callback=self._on_turn_result)
        except Exception as exc:
            self._message_list.add_system(f"Error: {_sanitize(str(exc))}")
            self._chat_input_bar.set_busy(False)
            self._input_bar.set_busy(False)
            self._current_row = None

    def _on_turn_result(self, result: dict) -> None:
        GLib.idle_add(self._handle_turn_result, result)

    def _handle_turn_result(self, result: dict) -> bool:
        self._chat_input_bar.set_busy(False)
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
        self._chat_input_bar.set_busy(False)
        self._input_bar.set_busy(False)

        self._in_welcome = True
        self._chat_view.set_visible(False)
        self._welcome_view.set_visible(True)
        self._input_bar.focus()

    def _on_open_settings(self, _btn: Gtk.Button) -> None:
        from ..settings.window import SettingsWindow
        win = SettingsWindow(app=self.get_application())
        win.present()
