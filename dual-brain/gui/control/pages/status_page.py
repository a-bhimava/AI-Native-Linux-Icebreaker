"""Status dashboard — live services / sockets / API-key summary."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from ..status import Health, collect


_REFRESH_INTERVAL_SEC = 5


def _colored_dot(status: str) -> tuple[str, str, str]:
    """(glyph, css-class, help-text) tuple for a status label."""
    match status:
        case "active" | "responded" | "reachable" | True:
            return ("●", "ib-tier-0", "OK")
        case "refused":
            return ("!", "ib-tier-2", "socket exists but refused")
        case "missing" | "inactive" | False:
            return ("✗", "ib-tier-3", "not present")
        case _:
            return ("?", "ib-muted-text", str(status))


class StatusPage(Adw.PreferencesPage):
    """Live system status — refreshes every few seconds."""

    def __init__(self) -> None:
        super().__init__(title="Status", icon_name="applications-system-symbolic")
        self.set_name("status")

        self._services_group = Adw.PreferencesGroup(title="Services")
        self._sockets_group = Adw.PreferencesGroup(title="Sockets")
        self._keys_group = Adw.PreferencesGroup(title="API Keys")
        self._version_row = Adw.ActionRow(title="ISO Version")
        self._version_row.add_suffix(self._version_label())
        general = Adw.PreferencesGroup(title="Overview")
        general.add(self._version_row)

        self.add(general)
        self.add(self._services_group)
        self.add(self._sockets_group)
        self.add(self._keys_group)

        # Rows are recreated each refresh so we don't accumulate stale ones.
        self._service_rows: list[Adw.ActionRow] = []
        self._socket_rows: list[Adw.ActionRow] = []
        self._key_rows: list[Adw.ActionRow] = []

        self._refresh_once()
        GLib.timeout_add_seconds(_REFRESH_INTERVAL_SEC, self._refresh_once)

    # ── Widget factories ─────────────────────────────────────────────────
    def _version_label(self) -> Gtk.Label:
        self._version_widget = Gtk.Label(label="—")
        self._version_widget.add_css_class("ib-muted-text")
        return self._version_widget

    def _make_status_row(self, title: str, status_text: str, detail: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title)
        row.set_subtitle(detail)
        glyph, css, _ = _colored_dot(status_text)
        badge = Gtk.Label(label=f"  {glyph}  ")
        badge.add_css_class("ib-badge")
        badge.add_css_class(css)
        row.add_suffix(badge)
        return row

    # ── Refresh ──────────────────────────────────────────────────────────
    def _refresh_once(self) -> bool:
        health: Health = collect()
        self._version_widget.set_label(health.version or "unknown")

        # Rebuild each group's rows (small counts — cheap).
        for old in self._service_rows:
            self._services_group.remove(old)
        self._service_rows.clear()
        for svc in health.services:
            row = self._make_status_row(
                svc["label"],
                "active" if svc["active"] else "inactive",
                f"{svc['unit']}  ·  {svc['state']}",
            )
            self._services_group.add(row)
            self._service_rows.append(row)

        for old in self._socket_rows:
            self._sockets_group.remove(old)
        self._socket_rows.clear()
        for sock in health.sockets:
            row = self._make_status_row(sock["label"], sock["status"], sock["path"])
            self._sockets_group.add(row)
            self._socket_rows.append(row)

        for old in self._key_rows:
            self._keys_group.remove(old)
        self._key_rows.clear()
        for k in health.keys:
            row = self._make_status_row(
                k.label,
                "active" if k.configured else "missing",
                f"{k.env_var}  ·  {k.masked_value or 'not set'}",
            )
            self._keys_group.add(row)
            self._key_rows.append(row)

        return True  # keep GLib timer alive
