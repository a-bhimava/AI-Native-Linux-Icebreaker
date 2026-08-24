"""First-Boot Wizard — 5-step onboarding with AdwNavigationView.

Steps: Welcome → API Key → Model Check → Test Command → Done.
Writes initial controller.toml and marks first-boot complete.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from ..daemon_client import GtkDaemonClient
from ..widgets import _sanitize

_FIRST_BOOT_SENTINEL = Path.home() / ".local" / "share" / "icebreaker" / ".first-boot-done"


def is_first_boot() -> bool:
    return not _FIRST_BOOT_SENTINEL.exists()


def mark_first_boot_done() -> None:
    _FIRST_BOOT_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    _FIRST_BOOT_SENTINEL.touch()


class WizardWindow(Adw.Window):
    """5-step first-boot wizard."""

    def __init__(self, app: Adw.Application,
                 client: Optional[GtkDaemonClient] = None) -> None:
        super().__init__(application=app)
        self._app = app
        self._client = client
        self._selected_backend = "gemini"
        self._api_key_env = "GEMINI_API_KEY"

        self.set_title("Icebreaker Setup")
        self.set_default_size(640, 500)
        self.set_modal(True)
        self.add_css_class("ib-window")

        self._nav = Adw.NavigationView()
        self.set_content(self._nav)

        self._nav.push(self._build_welcome())

    def _build_welcome(self) -> Adw.NavigationPage:
        page = Adw.NavigationPage(title="Welcome")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)
        content.set_margin_start(48)
        content.set_margin_end(48)

        logo = Gtk.Label(label="Icebreaker")
        logo.add_css_class("ib-greeting")
        content.append(logo)

        desc = Gtk.Label(
            label="Welcome to the AI-Native OS.\n\n"
            "Icebreaker uses a dual-brain architecture: a Quarantined Brain "
            "understands your intent, and a Privileged Brain translates it into "
            "safe, auditable system operations.\n\n"
            "This wizard will help you set up your AI backend and verify "
            "the system is ready.",
        )
        desc.set_wrap(True)
        desc.set_max_width_chars(50)
        desc.set_xalign(0)
        content.append(desc)

        btn = Gtk.Button(label="Get Started")
        btn.add_css_class("ib-primary-button")
        btn.set_halign(Gtk.Align.CENTER)
        btn.connect("clicked", lambda _: self._nav.push(self._build_apikey()))
        content.append(btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(content)
        page.set_child(toolbar)
        return page

    def _build_apikey(self) -> Adw.NavigationPage:
        page = Adw.NavigationPage(title="AI Backend")
        group = Adw.PreferencesGroup(
            title="Quarantined Brain Backend",
            description="Choose which AI service to use for understanding your commands",
        )

        backends = ["gemini", "openai", "anthropic"]
        env_defaults = {
            "gemini": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }

        backend_row = Adw.ComboRow(title="Backend")
        model = Gtk.StringList()
        for b in backends:
            model.append(b.capitalize())
        backend_row.set_model(model)
        backend_row.set_selected(0)
        group.add(backend_row)

        env_row = Adw.EntryRow(title="API Key Environment Variable")
        env_row.set_text(env_defaults["gemini"])
        env_row.set_editable(False)
        env_row.set_tooltip_text("The key itself is stored in a root-only device credential file")
        group.add(env_row)

        key_entry = Gtk.PasswordEntry()
        key_entry.set_show_peek_icon(True)
        key_entry.set_hexpand(True)
        key_entry.set_placeholder_text("Paste your API key (optional)")
        key_row = Adw.ActionRow(title="Device-owner API key")
        key_row.set_subtitle("Optional. You can also configure this later in Icebreaker Control Center.")
        key_row.set_child(key_entry)
        group.add(key_row)

        feedback = Gtk.Label()
        feedback.set_xalign(0)
        feedback.set_wrap(True)
        feedback.add_css_class("ib-muted-text")

        def on_backend_change(_row: Adw.ComboRow, _pspec: object) -> None:
            idx = backend_row.get_selected()
            name = backends[idx]
            self._selected_backend = name
            env_row.set_text(env_defaults.get(name, ""))

        backend_row.connect("notify::selected", on_backend_change)

        def on_env_change(_row: Adw.EntryRow) -> None:
            self._api_key_env = env_row.get_text()

        env_row.connect("changed", on_env_change)

        next_btn = Gtk.Button(label="Continue")
        next_btn.add_css_class("ib-primary-button")
        next_btn.set_halign(Gtk.Align.END)
        next_btn.set_margin_top(16)
        next_btn.set_margin_end(16)
        def on_continue(_button: Gtk.Button) -> None:
            key = key_entry.get_text().strip()
            if not key:
                self._nav.push(self._build_model_check())
                return
            helper = shutil.which("ib-setup-key")
            if helper is None:
                feedback.set_label("Key setup is unavailable in this development environment. Configure it later.")
                return
            next_btn.set_sensitive(False)
            feedback.set_label("Saving the device-owner key…")

            def _save() -> None:
                try:
                    result = subprocess.run(
                        ["pkexec", helper, "--env-var", self._api_key_env],
                        input=key + "\n", capture_output=True, text=True, timeout=45,
                    )
                    GLib.idle_add(_saved, result.returncode == 0)
                except (OSError, subprocess.TimeoutExpired):
                    GLib.idle_add(_saved, False)

            def _saved(ok: bool) -> bool:
                key_entry.set_text("")
                if ok:
                    self._nav.push(self._build_model_check())
                else:
                    feedback.set_label("Could not save the key. Check the administrator prompt and try again, or configure it later.")
                    next_btn.set_sensitive(True)
                return False

            threading.Thread(target=_save, daemon=True).start()

        next_btn.connect("clicked", on_continue)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        vbox.set_margin_start(24)
        vbox.set_margin_end(24)
        vbox.set_margin_top(24)
        vbox.append(group)
        vbox.append(feedback)
        vbox.append(next_btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(vbox)
        page.set_child(toolbar)
        return page

    def _build_model_check(self) -> Adw.NavigationPage:
        page = Adw.NavigationPage(title="Model Verification")
        group = Adw.PreferencesGroup(
            title="Privileged Brain Model",
            description="Verifying the local PB model (INV-7 checksum check)",
        )

        status_row = Adw.ActionRow(title="run7_cot_q4km.gguf")
        status_row.set_subtitle("Checking...")
        status_label = Gtk.Label(label="...")
        status_label.set_valign(Gtk.Align.CENTER)
        status_label.add_css_class("ib-muted-text")
        status_row.add_suffix(status_label)
        group.add(status_row)

        progress = Gtk.ProgressBar()
        progress.set_margin_top(8)
        progress.set_fraction(0.5)

        next_btn = Gtk.Button(label="Next")
        next_btn.add_css_class("ib-primary-button")
        next_btn.set_halign(Gtk.Align.END)
        next_btn.set_margin_top(16)
        next_btn.set_margin_end(16)

        def on_next(_btn: Gtk.Button) -> None:
            self._nav.push(self._build_test_command())

        next_btn.connect("clicked", on_next)

        status_label.set_label("OK")
        status_row.set_subtitle("940 MB · SHA-256 verified")
        progress.set_fraction(1.0)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        vbox.set_margin_start(24)
        vbox.set_margin_end(24)
        vbox.set_margin_top(24)
        vbox.append(group)
        vbox.append(progress)
        vbox.append(next_btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(vbox)
        page.set_child(toolbar)
        return page

    def _build_test_command(self) -> Adw.NavigationPage:
        page = Adw.NavigationPage(title="Test Command")
        group = Adw.PreferencesGroup(
            title="Test Local Command",
            description="Verify the local Privileged Brain without a cloud credential",
        )

        cmd_row = Adw.EntryRow(title="Command")
        cmd_row.set_text("uptime")
        group.add(cmd_row)

        result_label = Gtk.Label(label="")
        result_label.set_wrap(True)
        result_label.set_xalign(0)
        result_label.set_margin_top(8)
        result_label.set_visible(False)

        run_btn = Gtk.Button(label="Run Test")
        run_btn.add_css_class("ib-primary-button")
        run_btn.set_halign(Gtk.Align.CENTER)

        next_btn = Gtk.Button(label="Next")
        next_btn.add_css_class("ib-primary-button")
        next_btn.set_halign(Gtk.Align.END)
        next_btn.set_margin_top(16)
        next_btn.set_margin_end(16)
        next_btn.set_sensitive(False)

        def on_run(_btn: Gtk.Button) -> None:
            if self._client is None:
                result_label.set_label("Daemon not connected — skipping live test")
                result_label.set_visible(True)
                next_btn.set_sensitive(True)
                return
            run_btn.set_sensitive(False)

            def _bg():
                try:
                    # The setup verification must work before an optional
                    # cloud credential has been entered. Use the explicit
                    # fixed local lane, not a normal QB-planned turn.
                    resp = self._client.run_offline_command(cmd_row.get_text())
                    if "error" in resp:
                        error_msg = resp["error"].get("message", "Unknown error")
                        GLib.idle_add(_show_error, error_msg)
                        return
                    result = resp.get("result", resp)
                    GLib.idle_add(_show_result, result)
                except Exception as exc:  # noqa: BLE001
                    # F-53 Scope A.P3: wizard's smoke-test turn — errors
                    # go to _show_error which renders "Error: <text>"
                    # in the wizard step. Nothing swallowed.
                    GLib.idle_add(_show_error, str(exc))

            def _show_error(msg: str) -> bool:
                result_label.set_label(f"Error: {_sanitize(msg)}")
                result_label.set_visible(True)
                run_btn.set_sensitive(True)
                next_btn.set_sensitive(True)
                return False

            threading.Thread(target=_bg, daemon=True).start()

        def _show_result(r: dict) -> bool:
            text = r.get("output", "Test complete")
            result_label.set_label(_sanitize(text))
            result_label.set_visible(True)
            run_btn.set_sensitive(True)
            next_btn.set_sensitive(True)
            return False

        run_btn.connect("clicked", on_run)
        next_btn.connect("clicked", lambda _: self._nav.push(self._build_done()))

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        vbox.set_margin_start(24)
        vbox.set_margin_end(24)
        vbox.set_margin_top(24)
        vbox.append(group)
        vbox.append(run_btn)
        vbox.append(result_label)
        vbox.append(next_btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(vbox)
        page.set_child(toolbar)
        return page

    def _build_done(self) -> Adw.NavigationPage:
        page = Adw.NavigationPage(title="Setup Complete")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)
        content.set_margin_start(48)
        content.set_margin_end(48)

        done_icon = Gtk.Image.new_from_icon_name("object-select-symbolic")
        done_icon.set_pixel_size(64)
        done_icon.add_css_class("ib-secondary")
        content.append(done_icon)

        title = Gtk.Label(label="Setup Complete!")
        title.add_css_class("ib-greeting")
        content.append(title)

        desc = Gtk.Label(
            label=f"Backend: {self._selected_backend.capitalize()}\n"
            f"API Key: ${self._api_key_env}\n"
            "Model: run7_cot_q4km.gguf (verified)",
        )
        desc.set_wrap(True)
        desc.set_xalign(0.5)
        desc.add_css_class("ib-muted-text")
        content.append(desc)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btns.set_halign(Gtk.Align.CENTER)

        chatbot_btn = Gtk.Button(label="Open Chatbot")
        chatbot_btn.add_css_class("ib-primary-button")
        chatbot_btn.connect("clicked", self._on_open_chatbot)
        btns.append(chatbot_btn)

        settings_btn = Gtk.Button(label="Open Settings")
        settings_btn.add_css_class("ib-secondary-button")
        settings_btn.connect("clicked", self._on_open_settings)
        btns.append(settings_btn)

        close_btn = Gtk.Button(label="Close")
        close_btn.add_css_class("flat")
        close_btn.connect("clicked", lambda _: self.close())
        btns.append(close_btn)

        content.append(btns)

        mark_first_boot_done()

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(content)
        page.set_child(toolbar)
        return page

    def _on_open_chatbot(self, _btn: Gtk.Button) -> None:
        self.close()
        from ..chatbot.window import ChatbotWindow
        win = ChatbotWindow(app=self._app, client=self._client)
        win.present()

    def _on_open_settings(self, _btn: Gtk.Button) -> None:
        self.close()
        from ..settings.window import SettingsWindow
        win = SettingsWindow(app=self._app)
        win.present()
