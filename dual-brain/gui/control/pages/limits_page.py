"""Limits page — cost ceiling, rate limits, per-turn caps, HITL timings.

Every runaway-risk knob in one place. All settings write to
``~/.config/icebreaker/controller.toml`` (user-layered override).
"""

from __future__ import annotations

from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ..config_io import (
    ConfigReadError,
    SYSTEM_CONFIG_PATH,
    USER_CONFIG_PATH,
    effective,
    read_toml,
    restart_controller,
    set_user_override,
)
from ..schema_reader import get_field_spec


def _safe_read_toml(path):
    """See errors_page._safe_read_toml — surface parse errors instead
    of silently regressing to defaults (F-53 Scope A.P2)."""
    try:
        return read_toml(path), None
    except ConfigReadError as exc:
        return {}, str(exc)


@dataclass(frozen=True)
class _Knob:
    section: tuple[str, ...]
    key: str
    label: str
    subtitle: str
    default: float
    lo: float
    hi: float
    step: float
    integer: bool = True
    suffix: str = ""
    # Phase 6 Scope B: when True, override lo/hi with the schema's
    # minimum/maximum at render time. Lets a schema bump automatically
    # widen the GUI without touching this file.
    use_schema_bounds: bool = False
    # Phase 6 Scope B: "hot" = takes effect on next turn, "restart" =
    # needs `systemctl restart icebreaker-controller`. Rendered as a
    # dim-label suffix so users know why a change didn't stick.
    reload: str = "restart"


_KNOBS: list[_Knob] = [
    # Cost budget ─────────────────────────────────────────────────────────
    _Knob(("cost",), "session_ceiling_usd",
          "Session cost ceiling (USD)",
          "Hard budget across all Gemini/Anthropic/OpenAI calls this session. "
          "0 = unlimited. When exceeded, in-flight turns can auto-cancel "
          "(Behavior → Auto-cancel on cost breach).",
          0.0, 0.0, 100.0, 0.05, integer=False, suffix="$"),
    _Knob(("cost",), "warn_fraction",
          "Warn fraction",
          "Emit a warning at this fraction of the ceiling. "
          "0.8 warns at 80%.",
          0.8, 0.0, 1.0, 0.05, integer=False),
    # Turn budget ─────────────────────────────────────────────────────────
    _Knob(("session",), "max_turns_per_session",
          "Max turns per session",
          "Hard cap on the number of natural-language turns before the "
          "session refuses new input.",
          100, 1, 10000, 1),
    _Knob(("session",), "max_turns_per_minute",
          "Max turns per minute (rate limit)",
          "0 = unlimited. Prevents runaway automation loops from "
          "burning the cost ceiling in seconds.",
          0, 0, 120, 1),
    _Knob(("session",), "max_input_chars",
          "Max input characters per turn",
          "0 = unlimited. Truncation is opt-in; the daemon rejects "
          "over-cap input rather than silently truncating.",
          0, 0, 1_000_000, 1024),
    _Knob(("session",), "max_tool_output_lines",
          "Max tool output lines shown per step",
          "Beyond this, output is summarised for the CoT panel.",
          100, 1, 10000, 10),
    # HITL timings — INV-6 floor is 3s for lockout ────────────────────────
    _Knob(("hitl",), "lockout_seconds",
          "HITL lockout (INV-6 floor: 3s)",
          "Approve button stays disabled for this long after the "
          "dry-run report renders. Floor cannot go below 3s per INV-6.",
          3, 3, 30, 1, suffix="s"),
    _Knob(("hitl",), "timeout_seconds",
          "HITL timeout",
          "How long the HITL gate waits for a human decision "
          "before defaulting to DENY.",
          30, 5, 3600, 5, suffix="s"),
    # Phase 6 Scope B — shell context caps. Bounds live in the schema;
    # the fallback lo/hi here only kicks in if the schema can't be read.
    _Knob(("session",), "max_shell_context_chars",
          "Max shell context characters",
          "Per-field cap on `cwd`, `active_window`, and recent commands "
          "rendered into the <context> preamble QB sees. Smaller = "
          "tighter INV-2 blast radius on prompt-injection through "
          "untrusted shell state; larger = QB sees richer environment.",
          512, 128, 8192, 64,
          use_schema_bounds=True, reload="hot"),
    _Knob(("session",), "max_recent_commands",
          "Max recent shell commands in context",
          "Number of most-recent shell commands included in <context>. "
          "QB uses these to resolve references like 'the file I was "
          "editing'.",
          5, 1, 50, 1,
          use_schema_bounds=True, reload="hot"),
]


class LimitsPage(Adw.PreferencesPage):
    def __init__(self) -> None:
        super().__init__(title="Limits", icon_name="emblem-important-symbolic")
        self.set_name("limits")

        self._system, self._system_read_error = _safe_read_toml(
            SYSTEM_CONFIG_PATH,
        )
        self._user, self._user_read_error = _safe_read_toml(
            USER_CONFIG_PATH,
        )
        self._pending: dict[tuple, object] = {}

        self._add_cost_group()
        self._add_turn_group()
        self._add_hitl_group()
        self._add_shell_context_group()
        self._add_actions_group()

    # ── Groups ──────────────────────────────────────────────────────────

    def _add_cost_group(self) -> None:
        group = Adw.PreferencesGroup(
            title="Cost budget",
            description=(
                "Cloud-backend spend cap. Applies to Gemini / Anthropic / "
                "OpenAI cumulative token cost across the session."
            ),
        )
        self.add(group)
        for k in _KNOBS[:2]:
            self._add_knob(group, k)

    def _add_turn_group(self) -> None:
        group = Adw.PreferencesGroup(
            title="Turn + input limits",
            description=(
                "Runaway-automation guards. A stuck loop should hit a "
                "limit, not the credit card."
            ),
        )
        self.add(group)
        for k in _KNOBS[2:6]:
            self._add_knob(group, k)

    def _add_hitl_group(self) -> None:
        group = Adw.PreferencesGroup(
            title="Human-in-the-loop timings",
            description=(
                "INV-6 requires a minimum 3-second lockout on the Approve "
                "button so a pre-buffered Enter key can't rubber-stamp a "
                "destructive operation. Raise for even more deliberation."
            ),
        )
        self.add(group)
        # Only the 2 HITL knobs (lockout + timeout) belong here.
        for k in [
            knob for knob in _KNOBS
            if knob.section == ("hitl",)
        ]:
            self._add_knob(group, k)

    def _add_shell_context_group(self) -> None:
        # Phase 6 Scope B — the two session.max_* knobs previously
        # hardcoded as `_MAX_CONTEXT_LEN` / `_MAX_RECENT` module
        # constants in session.py. Bounds pulled from the JSON Schema.
        group = Adw.PreferencesGroup(
            title="Shell context (into QB)",
            description=(
                "Caps on the environmental context the terminal ships "
                "with every turn. Tighter = smaller INV-2 surface if the "
                "shell state is somehow untrusted; looser = QB can "
                "resolve richer references like 'the file I was editing'."
            ),
        )
        self.add(group)
        for k in [
            knob for knob in _KNOBS
            if knob.section == ("session",)
            and knob.key in ("max_shell_context_chars", "max_recent_commands")
        ]:
            self._add_knob(group, k)

    def _add_knob(self, group: Adw.PreferencesGroup, knob: _Knob) -> None:
        # Phase 6 Scope B: when use_schema_bounds is set, override the
        # in-file lo/hi with the JSON Schema's minimum/maximum. Schema
        # is the source of truth — daemon validates against it on load,
        # so any UI value the schema rejects is a UI bug.
        lo = knob.lo
        hi = knob.hi
        subtitle = knob.subtitle
        reload_tag = knob.reload
        if knob.use_schema_bounds:
            spec = get_field_spec(knob.section, knob.key)
            if spec is not None:
                if spec.minimum is not None:
                    lo = float(spec.minimum)
                if spec.maximum is not None:
                    hi = float(spec.maximum)
                # Schema description wins when present (single source of
                # truth avoids drift between code and schema wording).
                if spec.description:
                    subtitle = spec.description
                if spec.reload != "unspecified":
                    reload_tag = spec.reload
        row = Adw.ActionRow(title=knob.label, subtitle=subtitle)
        current = effective(
            self._system, self._user, knob.section, knob.key, knob.default
        )
        spin = Gtk.SpinButton()
        spin.set_valign(Gtk.Align.CENTER)
        spin.set_adjustment(Gtk.Adjustment(
            value=float(current),
            lower=lo, upper=hi,
            step_increment=knob.step, page_increment=max(knob.step * 10, 1),
        ))
        spin.set_digits(0 if knob.integer else 2)
        spin.set_numeric(True)
        spin.connect("value-changed", self._on_knob_changed, knob)
        row.add_suffix(spin)
        if knob.suffix:
            lbl = Gtk.Label(label=knob.suffix)
            lbl.add_css_class("dim-label")
            row.add_suffix(lbl)
        # Phase 6 Scope B: dim-label reload badge so the user knows
        # whether their edit takes effect immediately or needs a
        # `systemctl restart icebreaker-controller`.
        if reload_tag in ("hot", "restart"):
            badge = Gtk.Label(label=(
                "hot-reload" if reload_tag == "hot" else "restart to apply"
            ))
            badge.add_css_class("dim-label")
            row.add_suffix(badge)
        group.add(row)

    def _on_knob_changed(self, spin: Gtk.SpinButton, knob: _Knob) -> None:
        raw = spin.get_value()
        value = int(raw) if knob.integer else round(raw, 3)
        self._pending[knob.section + (knob.key,)] = value

    # ── Apply ────────────────────────────────────────────────────────────

    def _add_actions_group(self) -> None:
        group = Adw.PreferencesGroup(title="Apply changes")
        self.add(group)

        row = Adw.ActionRow(
            title="Restart Controller",
            subtitle="Applies all pending changes on this page. "
                     "Requires your password (polkit).",
        )
        btn = Gtk.Button(label="Apply & Restart Controller")
        btn.set_valign(Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self._on_apply)
        row.add_suffix(btn)
        group.add(row)

        status_group = Adw.PreferencesGroup()
        self._status_label = Gtk.Label()
        self._status_label.set_wrap(True)
        self._status_label.add_css_class("dim-label")
        status_row = Adw.ActionRow()
        status_row.set_child(self._status_label)
        status_group.add(status_row)
        self.add(status_group)

    def _on_apply(self, _button) -> None:
        try:
            for keypath, value in self._pending.items():
                *section_parts, key = keypath
                set_user_override(tuple(section_parts), key, value)
        except Exception as exc:  # noqa: BLE001
            self._flash(f"Save failed: {exc}", warning=True)
            return
        ok, msg = restart_controller()
        if ok:
            self._pending.clear()
            self._flash(msg)
        else:
            self._flash(f"Save wrote OK, but restart failed: {msg}", warning=True)

    def _flash(self, message: str, warning: bool = False) -> None:
        for cls in ("dim-label", "error", "success"):
            self._status_label.remove_css_class(cls)
        self._status_label.add_css_class("error" if warning else "success")
        self._status_label.set_label(message)
