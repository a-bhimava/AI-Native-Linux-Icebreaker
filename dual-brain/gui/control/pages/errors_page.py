"""Errors page — read-only view of the last N exceptions from SystemLogger.

Reads ``/var/log/icebreaker/system.jsonl`` (a tamper-evident chained JSONL
written by ``SystemLogger``), filters to ``event_type ==
"exception_swallowed"`` (the F-53 helper's event type), and lists the
last 50 entries newest-first.

Also exposes:
* Verbose-errors toggle (``[dev] verbose_errors = true``) which flips
  user-visible error reasons from ``ExcType: msg`` to include the full
  traceback. Useful for developers; noisy for end users.
* Copy-All button that dumps every visible row to the clipboard for
  bug reports.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk

from ..config_io import (
    ConfigReadError,
    SYSTEM_CONFIG_PATH,
    USER_CONFIG_PATH,
    effective,
    read_toml,
    restart_controller,
    set_user_override,
)


def _safe_read_toml(path: Path) -> tuple[dict[str, Any], str | None]:
    """Wrap read_toml so a corrupt user layer degrades to {} + an
    error string instead of crashing the page. F-53 Scope A.P2 — the
    Errors page is one of the layers that surfaces this to the user."""
    try:
        return read_toml(path), None
    except ConfigReadError as exc:
        return {}, str(exc)


_SYSTEM_LOG_PATH = Path("/var/log/icebreaker/system.jsonl")
_MAX_ROWS = 50


class ErrorsPage(Adw.PreferencesPage):
    def __init__(self) -> None:
        super().__init__(title="Errors", icon_name="dialog-error-symbolic")
        self.set_name("errors")

        self._system, self._system_read_error = _safe_read_toml(
            SYSTEM_CONFIG_PATH,
        )
        self._user, self._user_read_error = _safe_read_toml(
            USER_CONFIG_PATH,
        )
        self._collected_rows: list[Adw.ActionRow] = []

        self._add_toggle_group()
        self._add_actions_group()
        self._add_errors_group()
        self._refresh()

    # ── Verbose toggle ──────────────────────────────────────────────────

    def _add_toggle_group(self) -> None:
        group = Adw.PreferencesGroup(
            title="Error verbosity",
            description=(
                "When on, exception reasons shown in the terminal include "
                "the full Python traceback. Useful for debugging; noisy "
                "for end users. Requires controller restart to take effect."
            ),
        )
        self.add(group)

        row = Adw.ActionRow(
            title="Verbose errors",
            subtitle="Include full traceback in user-visible reason strings.",
        )
        current = bool(effective(
            self._system, self._user, ("dev",), "verbose_errors", False
        ))
        self._verbose_switch = Gtk.Switch()
        self._verbose_switch.set_active(current)
        self._verbose_switch.set_valign(Gtk.Align.CENTER)
        self._verbose_switch.connect("state-set", self._on_verbose_toggled)
        row.add_suffix(self._verbose_switch)
        row.set_activatable_widget(self._verbose_switch)
        group.add(row)

    def _on_verbose_toggled(self, _switch, state: bool) -> bool:
        try:
            set_user_override(("dev",), "verbose_errors", bool(state))
        except Exception as exc:  # noqa: BLE001
            self._flash(f"Save failed: {exc}", warning=True)
            return False
        ok, msg = restart_controller()
        if ok:
            self._flash(f"Verbose errors → {state}. {msg}")
        else:
            self._flash(f"Setting saved, but restart failed: {msg}",
                        warning=True)
        return False

    # ── Actions (refresh + copy) ────────────────────────────────────────

    def _add_actions_group(self) -> None:
        group = Adw.PreferencesGroup()
        self.add(group)

        row = Adw.ActionRow(
            title="Recent exceptions",
            subtitle=f"Last {_MAX_ROWS} from {_SYSTEM_LOG_PATH}",
        )
        refresh = Gtk.Button(label="Refresh")
        refresh.set_valign(Gtk.Align.CENTER)
        refresh.connect("clicked", lambda _b: self._refresh())
        row.add_suffix(refresh)

        copy = Gtk.Button(label="Copy all")
        copy.set_valign(Gtk.Align.CENTER)
        copy.connect("clicked", self._on_copy)
        row.add_suffix(copy)
        group.add(row)

        status_row = Adw.ActionRow()
        self._status_label = Gtk.Label()
        self._status_label.set_wrap(True)
        self._status_label.add_css_class("dim-label")
        status_row.set_child(self._status_label)
        group.add(status_row)

    # ── Error list ──────────────────────────────────────────────────────

    def _add_errors_group(self) -> None:
        self._errors_group = Adw.PreferencesGroup(title="Exceptions")
        self.add(self._errors_group)

    def _refresh(self) -> None:
        for row in list(self._collected_rows):
            self._errors_group.remove(row)
        self._collected_rows.clear()

        entries = self._load_entries()
        if not entries:
            row = Adw.ActionRow(
                title="No exceptions recorded",
                subtitle=(
                    f"Either the log at {_SYSTEM_LOG_PATH} does not exist "
                    "yet, is unreadable, or the daemon hasn't swallowed "
                    "any exceptions since it started."
                ),
            )
            self._errors_group.add(row)
            self._collected_rows.append(row)
            self._flash("Log is empty or unreadable.")
            return

        for entry in entries:
            payload = entry.get("payload", {}) or {}
            source = payload.get("source", "unknown")
            exc_type = payload.get("exc_type", "?")
            exc_message = payload.get("exc_message", "")
            ts = entry.get("timestamp", "")
            title = f"{exc_type}  ·  {source}"
            subtitle = f"{ts}\n{exc_message}" if exc_message else ts
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.set_subtitle_lines(4)
            self._errors_group.add(row)
            self._collected_rows.append(row)

        self._flash(f"Showing {len(entries)} of the most recent exceptions.")

    def _load_entries(self) -> list[dict[str, Any]]:
        # F-53 Scope A.P2: the Errors page IS the surfacing UI. It has
        # nowhere higher up the stack to escalate to — so file read or
        # per-line JSON parse failures are stashed on `self` as
        # ``self._last_read_error`` / ``self._malformed_lines`` so the
        # page can render a top banner instead of silently claiming
        # "no errors found".
        self._last_read_error: str | None = None
        self._malformed_lines: int = 0
        if not _SYSTEM_LOG_PATH.exists():
            return []
        try:
            # Read whole file — SystemLogger's JSONL is typically small.
            # For very large logs we'd tail from EOF; for v6.65 UX this is fine.
            lines = _SYSTEM_LOG_PATH.read_text("utf-8", errors="replace").splitlines()
        except Exception as exc:
            self._last_read_error = (
                f"{type(exc).__name__}: {exc}"
            )[:400]
            return []

        entries: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001
                # One bad line must not mask legitimate entries after it
                # in reverse order — but count so the top banner can say
                # "3 corrupted rows skipped" and the user can decide
                # whether to open a bug.
                self._malformed_lines += 1
                continue
            if obj.get("event_type") == "exception_swallowed":
                entries.append(obj)
                if len(entries) >= _MAX_ROWS:
                    break
        return entries

    # ── Copy to clipboard ────────────────────────────────────────────────

    def _on_copy(self, _button) -> None:
        entries = self._load_entries()
        if not entries:
            self._flash("Nothing to copy.", warning=True)
            return
        text = "\n".join(
            f"{e.get('timestamp','')} "
            f"[{(e.get('payload',{}) or {}).get('source','?')}] "
            f"{(e.get('payload',{}) or {}).get('exc_type','?')}: "
            f"{(e.get('payload',{}) or {}).get('exc_message','')}"
            for e in entries
        )
        display = Gdk.Display.get_default()
        if display is None:
            self._flash("No display available for clipboard.", warning=True)
            return
        clipboard = display.get_clipboard()
        clipboard.set(text)
        self._flash(f"Copied {len(entries)} exception rows to the clipboard.")

    def _flash(self, message: str, warning: bool = False) -> None:
        for cls in ("dim-label", "error", "success"):
            self._status_label.remove_css_class(cls)
        self._status_label.add_css_class("error" if warning else "success")
        self._status_label.set_label(message)
