"""Audit Log Viewer — filterable table with hash-chain indicator.

Read-only viewer for the JSONL audit log. Supports filtering by tier,
outcome, and text search. Hash-chain verification runs in a Gio.Task
thread. Export to JSON or CSV respects active filters.

INV-8 compliance: this viewer never writes to the audit log.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from ..widgets import _sanitize

REQUIRED_DISPLAY_FIELDS = ("ts", "action", "tier", "outcome")


def _default_log_path() -> Path:
    xdg = __import__("os").environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "icebreaker" / "controller-audit.log"
    return Path.home() / ".local" / "state" / "icebreaker" / "controller-audit.log"


def parse_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def verify_chain(entries: list[dict]) -> tuple[bool, Optional[int]]:
    if not entries:
        return (True, None)

    prev_hash = "GENESIS"
    for i, entry in enumerate(entries):
        seq = entry.get("seq")
        entry_prev = entry.get("prev_hash")
        if seq is None or entry_prev is None:
            return (False, i)
        if seq != i:
            return (False, i)
        if entry_prev != prev_hash:
            return (False, i)
        canonical = json.dumps(
            entry, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        prev_hash = hashlib.sha256(canonical).hexdigest()
    return (True, None)


def format_timestamp(ts: str) -> str:
    if len(ts) >= 16:
        return ts[5:16].replace("T", " ")
    return ts


def entry_matches_filter(
    entry: dict,
    tier_filter: Optional[int],
    outcome_filter: Optional[str],
    search_text: str,
) -> bool:
    if tier_filter is not None and entry.get("tier") != tier_filter:
        return False
    if outcome_filter and entry.get("outcome") != outcome_filter:
        return False
    if search_text:
        needle = search_text.lower()
        haystack = " ".join(
            str(entry.get(k, ""))
            for k in ("action", "target", "intent_id", "user", "backend")
        ).lower()
        if needle not in haystack:
            return False
    return True


def entries_to_csv(entries: list[dict]) -> str:
    if not entries:
        return ""
    keys = list(entries[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    writer.writeheader()
    for e in entries:
        writer.writerow(e)
    return buf.getvalue()


TIER_FILTER_OPTIONS = ["All", "Tier 0", "Tier 1", "Tier 2", "Tier 3"]
OUTCOME_FILTER_OPTIONS = [
    "All",
    "executed",
    "hitl_denied",
    "hitl_timeout",
    "schema_rejected",
    "tool_error",
    "brain_error",
    "trust_applied",
    "cancelled",
    "gui_executed",
    "rpa_executed",
]


class AuditWindow(Adw.Window):
    """Read-only audit log viewer with filtering and chain verification."""

    def __init__(
        self,
        app: Adw.Application,
        log_path: Optional[Path] = None,
    ) -> None:
        super().__init__(application=app)
        self._app = app
        self._log_path = log_path or _default_log_path()
        self._all_entries: list[dict] = []
        self._filtered_entries: list[dict] = []
        self._tier_filter: Optional[int] = None
        self._outcome_filter: Optional[str] = None
        self._search_text: str = ""

        self.set_title("Audit Log")
        self.set_default_size(900, 600)
        self.add_css_class("ib-window")

        self._build_ui()
        self._load_entries()

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        verify_btn = Gtk.Button(label="Verify Chain")
        verify_btn.add_css_class("ib-secondary-button")
        verify_btn.connect("clicked", self._on_verify)
        header.pack_end(verify_btn)

        export_btn = Gtk.MenuButton(label="Export")
        export_btn.add_css_class("flat")
        export_menu = Gio.Menu()
        export_menu.append("Export JSON", "audit.export-json")
        export_menu.append("Export CSV", "audit.export-csv")
        export_btn.set_menu_model(export_menu)
        header.pack_end(export_btn)

        json_action = Gio.SimpleAction(name="export-json")
        json_action.connect("activate", lambda *_: self._export("json"))
        csv_action = Gio.SimpleAction(name="export-csv")
        csv_action.connect("activate", lambda *_: self._export("csv"))

        action_group = Gio.SimpleActionGroup()
        action_group.add_action(json_action)
        action_group.add_action(csv_action)
        self.insert_action_group("audit", action_group)

        toolbar.add_top_bar(header)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(8)
        vbox.set_margin_bottom(8)

        # -- Filter row --
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        tier_model = Gtk.StringList()
        for opt in TIER_FILTER_OPTIONS:
            tier_model.append(opt)
        tier_combo = Gtk.DropDown(model=tier_model)
        tier_combo.set_selected(0)
        tier_combo.connect("notify::selected", self._on_tier_filter)
        self._tier_combo = tier_combo

        outcome_model = Gtk.StringList()
        for opt in OUTCOME_FILTER_OPTIONS:
            outcome_model.append(opt)
        outcome_combo = Gtk.DropDown(model=outcome_model)
        outcome_combo.set_selected(0)
        outcome_combo.connect("notify::selected", self._on_outcome_filter)
        self._outcome_combo = outcome_combo

        search_entry = Gtk.SearchEntry()
        search_entry.set_placeholder_text("Search action, target, user...")
        search_entry.set_hexpand(True)
        search_entry.connect("search-changed", self._on_search)
        self._search_entry = search_entry

        filter_box.append(Gtk.Label(label="Tier:"))
        filter_box.append(tier_combo)
        filter_box.append(Gtk.Label(label="Outcome:"))
        filter_box.append(outcome_combo)
        filter_box.append(search_entry)
        vbox.append(filter_box)

        # -- Summary count --
        self._count_label = Gtk.Label(label="")
        self._count_label.set_xalign(0)
        self._count_label.add_css_class("ib-muted-text")
        vbox.append(self._count_label)

        # -- Entry list --
        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list_box.add_css_class("ib-card")
        self._list_box.connect("row-activated", self._on_row_activated)

        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self._list_box)
        scroll.set_vexpand(True)
        scroll.set_min_content_height(200)
        vbox.append(scroll)

        # -- Detail panel --
        self._detail_group = Adw.PreferencesGroup(title="Entry Detail")
        self._detail_group.set_visible(False)

        detail_scroll = Gtk.ScrolledWindow()
        detail_scroll.set_child(self._detail_group)
        detail_scroll.set_max_content_height(250)
        detail_scroll.set_min_content_height(100)
        vbox.append(detail_scroll)

        # -- Status bar --
        self._status_label = Gtk.Label(label="")
        self._status_label.set_xalign(0)
        self._status_label.add_css_class("ib-muted-text")
        vbox.append(self._status_label)

        toolbar.set_content(vbox)
        self.set_content(toolbar)

    def _load_entries(self) -> None:
        self._all_entries = parse_entries(self._log_path)
        self._apply_filter()
        self._status_label.set_label(
            f"Loaded {len(self._all_entries)} entries from {self._log_path.name}"
        )

    def _apply_filter(self) -> None:
        self._filtered_entries = [
            e for e in self._all_entries
            if entry_matches_filter(
                e, self._tier_filter, self._outcome_filter, self._search_text
            )
        ]
        self._rebuild_list()
        self._count_label.set_label(
            f"Showing {len(self._filtered_entries)} of {len(self._all_entries)} entries"
        )

    def _rebuild_list(self) -> None:
        while True:
            row = self._list_box.get_row_at_index(0)
            if row is None:
                break
            self._list_box.remove(row)

        for entry in self._filtered_entries:
            row = self._make_row(entry)
            self._list_box.append(row)

    def _make_row(self, entry: dict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        ts_label = Gtk.Label(label=format_timestamp(entry.get("ts", "")))
        ts_label.set_xalign(0)
        ts_label.set_size_request(100, -1)
        ts_label.add_css_class("ib-muted-text")
        box.append(ts_label)

        action_label = Gtk.Label(label=_sanitize(entry.get("action", "")))
        action_label.set_xalign(0)
        action_label.set_size_request(120, -1)
        box.append(action_label)

        tier = entry.get("tier", "?")
        tier_label = Gtk.Label(label=f"T{tier}")
        tier_label.add_css_class("ib-badge")
        tier_label.set_size_request(40, -1)
        box.append(tier_label)

        outcome = entry.get("outcome", "")
        outcome_label = Gtk.Label(label=_sanitize(outcome))
        outcome_label.set_xalign(0)
        outcome_label.set_size_request(120, -1)
        if outcome in ("executed", "gui_executed", "rpa_executed", "trust_applied"):
            outcome_label.add_css_class("ib-secondary")
        elif outcome in ("hitl_denied", "hitl_timeout", "schema_rejected"):
            outcome_label.add_css_class("ib-destructive")
        box.append(outcome_label)

        target = _sanitize(entry.get("target", ""))
        if len(target) > 40:
            target = target[:37] + "..."
        target_label = Gtk.Label(label=target)
        target_label.set_xalign(0)
        target_label.set_hexpand(True)
        target_label.add_css_class("ib-muted-text")
        box.append(target_label)

        row.set_child(box)
        row._entry = entry
        return row

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        entry = getattr(row, "_entry", None)
        if entry is None:
            return
        self._show_detail(entry)

    def _show_detail(self, entry: dict) -> None:
        while True:
            child = self._detail_group.get_first_child()
            if child is None:
                break
            self._detail_group.remove(child)

        display_order = [
            "ts", "session_id", "turn_index", "intent_id",
            "action", "target", "tier", "risk_level", "outcome",
            "duration_ms", "user", "backend", "model",
            "tokens_in", "tokens_out", "cost_estimate_usd",
            "seq", "prev_hash",
        ]
        shown = set()
        for key in display_order:
            if key in entry:
                val = str(entry[key])
                detail_row = Adw.ActionRow(title=key)
                detail_row.set_subtitle(_sanitize(val))
                self._detail_group.add(detail_row)
                shown.add(key)

        for key in sorted(entry.keys()):
            if key not in shown:
                val = str(entry[key])
                detail_row = Adw.ActionRow(title=key)
                detail_row.set_subtitle(_sanitize(val[:256]))
                self._detail_group.add(detail_row)

        self._detail_group.set_visible(True)

    def _on_tier_filter(self, combo: Gtk.DropDown, _pspec: object) -> None:
        idx = combo.get_selected()
        self._tier_filter = (idx - 1) if idx > 0 else None
        self._apply_filter()

    def _on_outcome_filter(self, combo: Gtk.DropDown, _pspec: object) -> None:
        idx = combo.get_selected()
        self._outcome_filter = OUTCOME_FILTER_OPTIONS[idx] if idx > 0 else None
        self._apply_filter()

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text().strip()
        self._apply_filter()

    def _on_verify(self, _btn: Gtk.Button) -> None:
        self._status_label.set_label("Verifying hash chain...")
        ok, bad_seq = verify_chain(self._all_entries)
        total = len(self._all_entries)
        if ok:
            self._status_label.set_label(
                f"Chain intact — all {total} entries verified"
            )
        else:
            self._status_label.set_label(
                f"Chain broken at entry {bad_seq} — possible tampering"
            )

    def _export(self, fmt: str) -> None:
        if fmt == "json":
            data = json.dumps(self._filtered_entries, indent=2, default=str)
            suffix = ".json"
        else:
            data = entries_to_csv(self._filtered_entries)
            suffix = ".csv"

        default_name = f"icebreaker-audit{suffix}"
        export_path = Path.home() / default_name
        export_path.write_text(data, encoding="utf-8")
        self._status_label.set_label(f"Exported {len(self._filtered_entries)} entries to ~/{default_name}")
