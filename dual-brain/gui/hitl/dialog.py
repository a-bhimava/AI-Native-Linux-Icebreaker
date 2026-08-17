"""LibAdwaita HITL approval dialog — visual lockout, COW diff, tier coloring.

Upgrades the Phase 5 GTK presenter scaffold with:
  - LibAdwaita styling (AdwWindow, PreferencesGroup)
  - Visual lockout countdown with progress bar (INV-6)
  - COW diff preview with syntax-highlighted add/remove lines
  - Tier-colored header bar
  - Keyboard shortcut hints on buttons
  - RPA keyword preview for GUI Agent workflows

Registered as ``"libadwaita"`` in the presenter registry.
The GTK main loop runs on a separate thread; the caller blocks on
``threading.Event`` until the user decides.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from controller.backends.base import BrainConfigError
from controller.hitl import Decision, HitlDisplayData, HitlPresenter
from controller.keymap import Action, Keymap
from controller.risk_classifier import Tier
from controller.presenters.registry import register_presenter

from ..widgets import _sanitize

_TIER_CSS = {
    Tier.READ_ONLY: "ib-tier-0",
    Tier.LOW: "ib-tier-1",
    Tier.MEDIUM: "ib-tier-2",
    Tier.HIGH: "ib-tier-3",
}

_TIER_LABELS = {
    Tier.READ_ONLY: "Tier 0 — Read Only",
    Tier.LOW: "Tier 1 — Low Risk",
    Tier.MEDIUM: "Tier 2 — Medium Risk",
    Tier.HIGH: "Tier 3 — High Risk",
}


@register_presenter("libadwaita")
class LibAdwaitaHitlPresenter(HitlPresenter):
    """LibAdwaita HITL dialog with visual lockout and COW diff preview."""

    def __init__(self, *, keymap: Optional[Keymap] = None) -> None:
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            gi.require_version("Adw", "1")
            from gi.repository import Adw, GLib, Gtk
            self._Adw = Adw
            self._Gtk = Gtk
            self._GLib = GLib
        except (ImportError, ValueError) as exc:
            raise BrainConfigError(
                f"LibAdwaita presenter requires PyGObject + libadwaita: {exc}. "
                "Install: apt install python3-gi gir1.2-adw-1"
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

    def _add_extra_content(self, vbox: object, data: HitlDisplayData) -> None:
        """Subclass hook — inject widgets after Operation Details and
        before RPA/COW/lockout sections. Base implementation is a no-op
        so the standard `libadwaita` presenter behavior is unchanged.
        Called from within the GTK main thread (safe to touch widgets).
        """
        return None

    def _set_decision(self, decision: Decision, key_class: str, app: object) -> None:
        self._decision = decision
        self._last_key_class = key_class
        self._decision_event.set()
        app.quit()

    def _run_dialog(self, data: HitlDisplayData, timeout_seconds: int) -> None:
        Gtk = self._Gtk
        GLib = self._GLib
        Adw = self._Adw

        app = Adw.Application(application_id="org.icebreaker.hitl")

        def on_activate(app: object) -> None:
            window = Adw.Window(application=app)
            window.set_title("Action Approval Required")
            window.set_default_size(560, 520)
            window.set_modal(True)
            window.add_css_class("ib-window")
            window.add_css_class("ib-security-surface")
            window.add_css_class("ib-hitl-surface")

            toolbar = Adw.ToolbarView()
            header = Adw.HeaderBar()

            tier_badge = Gtk.Label(label=_TIER_LABELS.get(data.tier, f"Tier {data.tier}"))
            tier_css = _TIER_CSS.get(data.tier, "ib-tier-2")
            tier_badge.add_css_class(tier_css)
            tier_badge.add_css_class("ib-badge")
            header.pack_end(tier_badge)

            toolbar.add_top_bar(header)

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            vbox.set_margin_start(24)
            vbox.set_margin_end(24)
            vbox.set_margin_top(16)
            vbox.set_margin_bottom(24)

            # -- Detail fields --
            details_group = Adw.PreferencesGroup(
                title="Operation Details",
            )

            action_row = Adw.ActionRow(title="Action")
            action_row.set_subtitle(_sanitize(data.action))
            details_group.add(action_row)

            target_row = Adw.ActionRow(title="Target")
            target_row.set_subtitle(_sanitize(data.target or "none"))
            details_group.add(target_row)

            risk_row = Adw.ActionRow(title="Risk")
            risk_row.set_subtitle(_sanitize(data.risk_level))
            details_group.add(risk_row)

            reversible_row = Adw.ActionRow(title="Reversible")
            reversible_row.set_subtitle("Yes (COW snapshot)" if data.reversible else "No")
            details_group.add(reversible_row)

            if data.backend:
                backend_row = Adw.ActionRow(title="Backend")
                backend_row.set_subtitle(_sanitize(data.backend))
                details_group.add(backend_row)

            if data.reason:
                reason_row = Adw.ActionRow(title="Reason")
                reason_row.set_subtitle(_sanitize(data.reason))
                reason_row.set_subtitle_lines(3)
                details_group.add(reason_row)

            if data.blocked_pattern:
                blocked_row = Adw.ActionRow(title="Blocked Pattern")
                blocked_row.set_subtitle(_sanitize(data.blocked_pattern))
                blocked_row.add_css_class("error")
                details_group.add(blocked_row)

            vbox.append(details_group)

            # V.5b hook (2026-08-02): subclasses can inject additional
            # widgets AFTER the Operation Details group but BEFORE the
            # RPA / COW / lockout / buttons sections. Base implementation
            # is a no-op — behavior for the standard `libadwaita`
            # presenter is unchanged. See annotated_dialog.py for the
            # V.5b `annotated_screenshot` subclass that overrides this
            # to render an inline Gtk.Picture preview.
            self._add_extra_content(vbox, data)

            # -- RPA keyword preview --
            if data.rpa_keyword_preview:
                rpa_group = Adw.PreferencesGroup(title="RPA Workflow Steps")
                for i, kw in enumerate(data.rpa_keyword_preview, 1):
                    kw_row = Adw.ActionRow(title=f"Step {i}")
                    kw_row.set_subtitle(_sanitize(kw))
                    rpa_group.add(kw_row)
                vbox.append(rpa_group)

            # -- COW diff preview --
            if data.cow_summary:
                diff_group = Adw.PreferencesGroup(title="Dry-Run Preview")

                diff_view = Gtk.TextView()
                diff_view.set_editable(False)
                diff_view.set_cursor_visible(False)
                diff_view.set_monospace(True)
                diff_view.add_css_class("ib-card")
                diff_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)

                buf = diff_view.get_buffer()
                add_tag = buf.create_tag("add", foreground="#5e8787")
                rm_tag = buf.create_tag("remove", foreground="#f87171")
                ctx_tag = buf.create_tag("context", foreground="#888888")

                for line in _sanitize(data.cow_summary).split("\n"):
                    end = buf.get_end_iter()
                    if line.startswith("+"):
                        buf.insert_with_tags(end, line + "\n", add_tag)
                    elif line.startswith("-"):
                        buf.insert_with_tags(end, line + "\n", rm_tag)
                    else:
                        buf.insert_with_tags(end, line + "\n", ctx_tag)

                scroll = Gtk.ScrolledWindow()
                scroll.set_min_content_height(100)
                scroll.set_max_content_height(200)
                scroll.set_child(diff_view)

                diff_frame = Gtk.Frame()
                diff_frame.set_child(scroll)
                diff_frame.add_css_class("ib-card")

                diff_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                diff_box.append(diff_frame)
                diff_group.add(diff_box)
                vbox.append(diff_group)

            # -- Lockout progress bar --
            lockout_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            lockout_box.set_margin_top(8)

            progress = Gtk.ProgressBar()
            progress.set_fraction(0.0)
            progress.add_css_class("ib-primary-button")

            countdown_label = Gtk.Label(label="")
            countdown_label.add_css_class("ib-muted-text")
            countdown_label.set_halign(Gtk.Align.CENTER)

            lockout_box.append(progress)
            lockout_box.append(countdown_label)
            vbox.append(lockout_box)

            # -- Buttons with keyboard hints --
            btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn_box.set_halign(Gtk.Align.CENTER)
            btn_box.set_margin_top(8)

            approve_keys = self._keymap.bindings.get(Action.APPROVE, ("a",))
            deny_keys = self._keymap.bindings.get(Action.DENY, ("d",))
            modify_keys = self._keymap.bindings.get(Action.MODIFY, ("m",))
            explain_keys = self._keymap.bindings.get(Action.EXPLAIN, ("e",))

            approve_hint = approve_keys[0] if approve_keys else "a"
            deny_hint = deny_keys[0] if deny_keys else "d"
            modify_hint = modify_keys[0] if modify_keys else "m"
            explain_hint = explain_keys[0] if explain_keys else "e"

            approve_btn = Gtk.Button(label=f"Approve ({approve_hint})")
            approve_btn.add_css_class("ib-primary-button")
            approve_btn.set_sensitive(False)

            deny_btn = Gtk.Button(label=f"Deny ({deny_hint})")
            deny_btn.add_css_class("destructive-action")

            modify_btn = Gtk.Button(label=f"Modify ({modify_hint})")
            modify_btn.add_css_class("ib-secondary-button")

            explain_btn = Gtk.Button(label=f"Explain ({explain_hint})")
            explain_btn.add_css_class("flat")

            help_btn = Gtk.Button(label="?")
            help_btn.add_css_class("flat")
            help_btn.set_tooltip_text("Show keyboard shortcuts")

            def _decide(decision: Decision, key_class: str = "gui") -> None:
                self._set_decision(decision, key_class, app)

            approve_btn.connect("clicked", lambda _: _decide(Decision.APPROVED))
            deny_btn.connect("clicked", lambda _: _decide(Decision.DENIED))
            modify_btn.connect("clicked", lambda _: _decide(Decision.MODIFY))
            explain_btn.connect("clicked", lambda _: _decide(Decision.EXPLAIN))

            def _show_help(_btn: object) -> None:
                legend = self._keymap.legend(include_trust=True)
                dialog = Adw.AlertDialog(heading="Keyboard Shortcuts", body=legend)
                dialog.add_response("ok", "OK")
                dialog.present(window)

            help_btn.connect("clicked", _show_help)

            btn_box.append(approve_btn)
            btn_box.append(deny_btn)
            btn_box.append(modify_btn)
            btn_box.append(explain_btn)
            btn_box.append(help_btn)
            vbox.append(btn_box)

            # -- Lockout countdown animation --
            lockout_start = time.monotonic()
            lockout_seconds = 3

            def _lockout_tick() -> bool:
                elapsed = time.monotonic() - lockout_start
                fraction = min(elapsed / lockout_seconds, 1.0)
                progress.set_fraction(fraction)
                remaining = max(0, lockout_seconds - elapsed)
                if elapsed >= lockout_seconds:
                    approve_btn.set_sensitive(True)
                    countdown_label.set_text("")
                    progress.set_visible(False)
                    return False
                countdown_label.set_text(f"{int(remaining) + 1}s remaining")
                return True

            GLib.timeout_add(100, _lockout_tick)

            # -- Keyboard shortcuts --
            key_controller = Gtk.EventControllerKey()

            def on_key_pressed(_ctrl: object, keyval: int, _keycode: int, _state: object) -> bool:
                key_name = GLib.unichar_to_utf8(keyval) if keyval < 128 else ""
                if keyval == 65307:  # Escape
                    _decide(Decision.DENIED, "esc")
                    return True
                if key_name:
                    action = self._keymap.lookup(key_name)
                    if action == Action.APPROVE and approve_btn.get_sensitive():
                        _decide(Decision.APPROVED, "mnemonic")
                        return True
                    if action == Action.DENY:
                        _decide(Decision.DENIED, "mnemonic")
                        return True
                    if action == Action.MODIFY:
                        _decide(Decision.MODIFY, "mnemonic")
                        return True
                    if action == Action.EXPLAIN:
                        _decide(Decision.EXPLAIN, "mnemonic")
                        return True
                    if action == Action.TRUST:
                        _decide(Decision.TRUST, "mnemonic")
                        return True
                return False

            key_controller.connect("key-pressed", on_key_pressed)
            window.add_controller(key_controller)

            window.connect("close-request", lambda _: _decide(Decision.DENIED, "close"))

            scrolled = Gtk.ScrolledWindow()
            scrolled.set_child(vbox)
            scrolled.set_vexpand(True)
            toolbar.set_content(scrolled)

            window.set_content(toolbar)
            window.present()

            GLib.timeout_add_seconds(
                timeout_seconds,
                lambda: _decide(Decision.TIMEOUT, "timeout"),
            )

        app.connect("activate", on_activate)

        try:
            app.run(None)
        except Exception:
            if self._decision is None:
                self._decision = Decision.DENIED
                self._last_key_class = "error"
                self._decision_event.set()
