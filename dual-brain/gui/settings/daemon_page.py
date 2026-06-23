"""Daemon page — system-level config: daemon, HITL, PB, cost, limits.

Groups the "plumbing" settings that most users won't touch often:
daemon socket/PID paths, HITL lockout/timeout/presenter, PB endpoint,
cost ceiling, and rate limits.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk


class DaemonPage(Adw.PreferencesPage):
    """Daemon, HITL, PB, cost, and limits configuration."""

    def __init__(self, raw: dict) -> None:
        super().__init__()
        self.set_title("System")
        self.set_icon_name("emblem-system-symbolic")

        self._dirty_cb: Optional[Callable[[], None]] = None
        self._raw_hitl = raw.get("hitl", {})
        self._raw_daemon = raw.get("daemon", {})
        self._raw_run = raw.get("run", {})
        self._raw_cost = raw.get("cost", {})
        self._raw_limits = raw.get("limits", {})

        hitl_group = Adw.PreferencesGroup(
            title="Human-in-the-Loop",
            description="HITL approval gate configuration (INV-6)",
        )
        self.add(hitl_group)

        self._lockout_adj = Gtk.Adjustment(
            value=self._raw_hitl.get("lockout_seconds", 3),
            lower=1, upper=30, step_increment=1,
        )
        self._lockout_row = Adw.SpinRow(
            title="Lockout (s)",
            adjustment=self._lockout_adj,
        )
        self._lockout_row.set_subtitle("Approve button disabled for this many seconds")
        self._lockout_row.connect("notify::value", self._signal_dirty)
        hitl_group.add(self._lockout_row)

        self._timeout_adj = Gtk.Adjustment(
            value=self._raw_hitl.get("timeout_seconds", 30),
            lower=5, upper=300, step_increment=5,
        )
        self._timeout_row = Adw.SpinRow(
            title="Decision Timeout (s)",
            adjustment=self._timeout_adj,
        )
        self._timeout_row.set_subtitle("Auto-deny after this many seconds")
        self._timeout_row.connect("notify::value", self._signal_dirty)
        hitl_group.add(self._timeout_row)

        self._presenter_row = Adw.ComboRow(title="Presenter")
        presenter_model = Gtk.StringList()
        _PRESENTERS = ["terminal", "screen_reader", "gtk", "ai_terminal"]
        for p in _PRESENTERS:
            presenter_model.append(p)
        self._presenter_row.set_model(presenter_model)
        current = self._raw_hitl.get("presenter", "terminal")
        try:
            idx = _PRESENTERS.index(current)
        except ValueError:
            idx = 0
        self._presenter_row.set_selected(idx)
        self._presenter_row.connect("notify::selected", self._signal_dirty)
        hitl_group.add(self._presenter_row)

        self._trust_adj = Gtk.Adjustment(
            value=self._raw_hitl.get("trust_ttl_seconds", 0),
            lower=0, upper=3600, step_increment=30,
        )
        self._trust_row = Adw.SpinRow(
            title="Trust TTL (s)",
            adjustment=self._trust_adj,
        )
        self._trust_row.set_subtitle("0 = disabled; >0 = grant lifetime in seconds")
        self._trust_row.connect("notify::value", self._signal_dirty)
        hitl_group.add(self._trust_row)

        daemon_group = Adw.PreferencesGroup(title="Daemon")
        self.add(daemon_group)

        self._socket_row = Adw.EntryRow(title="Socket Path")
        self._socket_row.set_text(
            self._raw_daemon.get("socket_path", "~/.local/state/icebreaker/controller.sock")
        )
        self._socket_row.connect("changed", self._signal_dirty)
        daemon_group.add(self._socket_row)

        self._max_conn_adj = Gtk.Adjustment(
            value=self._raw_daemon.get("max_connections", 1),
            lower=1, upper=16, step_increment=1,
        )
        self._max_conn_row = Adw.SpinRow(
            title="Max Connections",
            adjustment=self._max_conn_adj,
        )
        self._max_conn_row.connect("notify::value", self._signal_dirty)
        daemon_group.add(self._max_conn_row)

        pb_group = Adw.PreferencesGroup(title="Privileged Brain")
        self.add(pb_group)

        self._pb_endpoint_row = Adw.EntryRow(title="PB Endpoint")
        self._pb_endpoint_row.set_text(
            self._raw_run.get("pb_endpoint", "http://127.0.0.1:8080")
        )
        self._pb_endpoint_row.connect("changed", self._signal_dirty)
        pb_group.add(self._pb_endpoint_row)

        self._pb_transport_row = Adw.ComboRow(title="PB Transport")
        transport_model = Gtk.StringList()
        for t in ("http", "unix"):
            transport_model.append(t)
        self._pb_transport_row.set_model(transport_model)
        current_t = self._raw_run.get("pb_transport", "http")
        self._pb_transport_row.set_selected(0 if current_t == "http" else 1)
        self._pb_transport_row.connect("notify::selected", self._signal_dirty)
        pb_group.add(self._pb_transport_row)

        self._pb_timeout_adj = Gtk.Adjustment(
            value=self._raw_run.get("pb_timeout_seconds", 10),
            lower=1, upper=300, step_increment=1,
        )
        self._pb_timeout_row = Adw.SpinRow(
            title="PB Timeout (s)",
            adjustment=self._pb_timeout_adj,
        )
        self._pb_timeout_row.connect("notify::value", self._signal_dirty)
        pb_group.add(self._pb_timeout_row)

        cost_group = Adw.PreferencesGroup(title="Cost & Limits")
        self.add(cost_group)

        self._ceiling_adj = Gtk.Adjustment(
            value=self._raw_cost.get("session_ceiling_usd", 0.0),
            lower=0.0, upper=100.0, step_increment=0.1,
        )
        self._ceiling_row = Adw.SpinRow(
            title="Session Ceiling (USD)",
            adjustment=self._ceiling_adj,
            digits=2,
        )
        self._ceiling_row.set_subtitle("0 = no limit")
        self._ceiling_row.connect("notify::value", self._signal_dirty)
        cost_group.add(self._ceiling_row)

        self._max_input_adj = Gtk.Adjustment(
            value=self._raw_limits.get("max_input_chars", 4000),
            lower=0, upper=100000, step_increment=100,
        )
        self._max_input_row = Adw.SpinRow(
            title="Max Input Chars",
            adjustment=self._max_input_adj,
        )
        self._max_input_row.set_subtitle("0 = no limit")
        self._max_input_row.connect("notify::value", self._signal_dirty)
        cost_group.add(self._max_input_row)

        self._rate_adj = Gtk.Adjustment(
            value=self._raw_limits.get("max_turns_per_min", 30),
            lower=0, upper=1000, step_increment=1,
        )
        self._rate_row = Adw.SpinRow(
            title="Max Turns/Min",
            adjustment=self._rate_adj,
        )
        self._rate_row.set_subtitle("0 = unlimited")
        self._rate_row.connect("notify::value", self._signal_dirty)
        cost_group.add(self._rate_row)

    def connect_dirty(self, cb: Callable[[], None]) -> None:
        self._dirty_cb = cb

    def _signal_dirty(self, *_args: object) -> None:
        if self._dirty_cb:
            self._dirty_cb()

    def collect_into(self, data: dict) -> None:
        _PRESENTERS = ["terminal", "screen_reader", "gtk", "ai_terminal"]

        data["hitl"] = {
            "lockout_seconds": int(self._lockout_row.get_value()),
            "timeout_seconds": int(self._timeout_row.get_value()),
            "presenter": _PRESENTERS[self._presenter_row.get_selected()],
            "trust_ttl_seconds": int(self._trust_row.get_value()),
        }

        data["daemon"] = {
            "socket_path": self._socket_row.get_text(),
            "max_connections": int(self._max_conn_row.get_value()),
        }

        run = data.get("run", {})
        run["pb_endpoint"] = self._pb_endpoint_row.get_text()
        run["pb_transport"] = ["http", "unix"][self._pb_transport_row.get_selected()]
        run["pb_timeout_seconds"] = int(self._pb_timeout_row.get_value())
        data["run"] = run

        data["cost"] = {
            "session_ceiling_usd": round(self._ceiling_row.get_value(), 2),
        }

        data["limits"] = {
            "max_input_chars": int(self._max_input_row.get_value()),
            "max_turns_per_min": int(self._rate_row.get_value()),
        }
