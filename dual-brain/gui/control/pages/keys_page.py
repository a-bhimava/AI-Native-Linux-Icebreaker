"""API Keys page — set Gemini / Anthropic / OpenAI keys via pkexec.

The keys live in ``/etc/icebreaker/locations.env`` (0640 root:icebreaker-users).
That file is only writable by root, so saves go through ``pkexec`` which
prompts the user for their password via polkit and then invokes the same
``ib-setup-key`` helper the CLI uses.

Fallback for local dev (no ``ib-setup-key`` binary present): write to
``~/.config/icebreaker/keys.env`` so the developer can iterate on the UI
without root.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from ..status import KeyState, collect


_LOCATIONS_ENV = Path("/etc/icebreaker/locations.env")
_USER_FALLBACK = Path.home() / ".config" / "icebreaker" / "keys.env"


def _find_helper() -> Optional[str]:
    """Return path to ib-setup-key (root-elevating helper) if installed."""
    return shutil.which("ib-setup-key")


class KeyRow(Adw.ExpanderRow):
    """One expandable row per provider.

    Collapsed state shows the status glyph + masked value.
    Expanded state exposes the entry + Save button + Reveal toggle.
    """

    __slots__ = ("_env_var", "_entry", "_reveal_btn", "_save_btn", "_status_label", "_on_saved")

    def __init__(self, key_state: KeyState, on_saved) -> None:
        super().__init__()
        self._env_var = key_state.env_var
        self._on_saved = on_saved

        self.set_title(key_state.label)
        self._status_label = Gtk.Label()
        self._status_label.add_css_class("ib-muted-text")
        self.add_suffix(self._status_label)
        self._update_summary(key_state)

        # Password-style entry (hidden by default).
        self._entry = Gtk.PasswordEntry()
        self._entry.set_show_peek_icon(True)
        self._entry.set_hexpand(True)
        self._entry.set_property("placeholder-text", f"Paste your {key_state.label} key…")
        entry_row = Adw.ActionRow()
        entry_row.set_child(self._entry)
        self.add_row(entry_row)

        # Buttons row.
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_margin_top(6)
        actions.set_margin_bottom(6)
        actions.set_margin_start(12)
        actions.set_margin_end(12)
        actions.set_halign(Gtk.Align.END)

        self._save_btn = Gtk.Button(label="Save & Restart Controller")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.add_css_class("ib-primary-button")
        self._save_btn.connect("clicked", self._on_save)
        actions.append(self._save_btn)

        actions_row = Adw.ActionRow()
        actions_row.set_child(actions)
        self.add_row(actions_row)

    def _update_summary(self, key_state: KeyState) -> None:
        if key_state.configured:
            self._status_label.set_label(f"✓  {key_state.masked_value}")
        else:
            self._status_label.set_label("not set")

    # ── Save ────────────────────────────────────────────────────────────
    def _on_save(self, _button) -> None:
        key = self._entry.get_text().strip()
        if not key:
            self._flash("Enter a value first.", warning=True)
            return

        helper = _find_helper()
        if helper is not None:
            self._save_with_helper(helper, key)
        else:
            self._save_to_user_fallback(key)

    def _save_with_helper(self, helper_path: str, key: str) -> None:
        # ``ib-setup-key`` currently prompts interactively; the GUI passes the
        # env-var name + value via a small wrapper that pipes on stdin. For
        # V6.6 shipping we accept the polkit password prompt as the elevation
        # step.
        try:
            proc = subprocess.run(
                ["pkexec", helper_path, "--env-var", self._env_var],
                input=key + "\n",
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                self._flash("Saved. Controller restarting…")
                self._entry.set_text("")
                self._on_saved()
            else:
                self._flash(f"Save failed: {proc.stderr.strip() or 'unknown error'}", warning=True)
        except subprocess.TimeoutExpired:
            self._flash("Timed out — polkit prompt cancelled?", warning=True)
        except FileNotFoundError:
            self._flash("pkexec not available — install polkit or run as root.", warning=True)

    def _save_to_user_fallback(self, key: str) -> None:
        try:
            _USER_FALLBACK.parent.mkdir(parents=True, exist_ok=True)
            lines: list[str] = []
            if _USER_FALLBACK.exists():
                for line in _USER_FALLBACK.read_text().splitlines():
                    if not line.startswith(f"{self._env_var}="):
                        lines.append(line)
            lines.append(f"{self._env_var}={key}")
            _USER_FALLBACK.write_text("\n".join(lines) + "\n")
            os.chmod(_USER_FALLBACK, 0o600)
            self._flash(f"Saved to {_USER_FALLBACK} (dev fallback)")
            self._entry.set_text("")
            self._on_saved()
        except Exception as exc:
            self._flash(f"Fallback save failed: {exc}", warning=True)

    def _flash(self, message: str, warning: bool = False) -> None:
        # Simple visual feedback in the status label. Overwritten by next
        # global refresh in ~5s.
        self._status_label.remove_css_class("ib-muted-text")
        self._status_label.remove_css_class("ib-destructive")
        self._status_label.add_css_class("ib-destructive" if warning else "ib-secondary")
        self._status_label.set_label(message)


class KeysPage(Adw.PreferencesPage):
    def __init__(self) -> None:
        super().__init__(title="API Keys", icon_name="dialog-password-symbolic")
        self.set_name("keys")

        info = Adw.PreferencesGroup(
            title="Where keys are stored",
            description=(
                "Keys are written to /etc/icebreaker/locations.env (owner "
                "root:icebreaker-users, mode 0640). Saving prompts for your "
                "password via polkit. Existing keys are shown masked; the "
                "clear-text value is never displayed once saved."
            ),
        )
        self.add(info)

        self._group = Adw.PreferencesGroup(title="Configured providers")
        self.add(self._group)

        self._rows: list[KeyRow] = []
        self.refresh()

    def refresh(self) -> None:
        for row in self._rows:
            self._group.remove(row)
        self._rows.clear()

        for k in collect().keys:
            row = KeyRow(k, on_saved=self.refresh)
            self._group.add(row)
            self._rows.append(row)
