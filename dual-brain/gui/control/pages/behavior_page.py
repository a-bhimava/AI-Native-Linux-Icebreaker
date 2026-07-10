"""Behavior page — verifier retry, fallback chain, cost-breach handling.

**F-49 (2026-07-10)** — verifier retry chooser. The v6.61 UTM sweep showed
legitimate writes rejected as ``verifier call failed``. The right retry
strategy is workload-dependent, so we expose it as a runtime knob:

  off                 — never retry, first vote authoritative
  on_call_failed_only — retry only on "verifier call failed" (default)
  on_any_rejection    — retry once on any verified=false
  skip_tier_01        — bypass verifier for Tier 0/1 (fastest)

**v6.65 additions**:

* **Fallback backend chain** — checkboxes for each registered QB backend.
  If the primary QB fails after ``qb_max_retries``, the controller tries
  the checked ones in order. Fallback only fires on transport/API errors,
  NOT on schema-validation failures (those are prompt issues that retrying
  the same backend won't fix).
* **Auto-cancel on cost breach** — when on, hitting ``session_ceiling_usd``
  cancels the current turn cleanly. When off, warns but continues.

Config writes to ``~/.config/icebreaker/controller.toml`` (user-layered
override — takes precedence over ``/etc/icebreaker/controller.toml``).
Controller must be restarted for changes to take effect; the page's
"Apply & Restart" button calls ``pkexec systemctl restart``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ..config_io import (
    SYSTEM_CONFIG_PATH,
    USER_CONFIG_PATH,
    effective,
    read_toml,
    restart_controller,
    set_user_override,
    walk,
)


# ── Verifier retry modes ──────────────────────────────────────────────────

@dataclass(frozen=True)
class _VerifierMode:
    key: str
    label: str
    description: str


_MODES: list[_VerifierMode] = [
    _VerifierMode(
        "off",
        "Off — trust the first vote",
        "Never retry. If the verifier rejects an intent, the turn fails "
        "immediately. Fastest; correct if Gemini's rejections are reliable.",
    ),
    _VerifierMode(
        "on_call_failed_only",
        "On call-failed only (default)",
        "Retry once if the reason contains 'verifier call failed'. Treats "
        "the failure as a transient API/model flake but respects semantic "
        "rejections. Ships as the default.",
    ),
    _VerifierMode(
        "on_any_rejection",
        "On any rejection",
        "Retry once on any verified=false, whatever the reason. Most "
        "forgiving; catches legitimate intents that lost a single vote. "
        "Extra Gemini roundtrip on every rejection.",
    ),
    _VerifierMode(
        "skip_tier_01",
        "Skip for Tier 0/1 intents",
        "Bypass the verifier entirely for read-only + home-write intents. "
        "Saves ~2s per turn; reduces defense-in-depth on QB↔PB tool "
        "alignment for auto-execute tiers.",
    ),
]

# Registered QB backends. Hardcoded (rather than pulled from
# ``registered_backends()``) because the Control Center runs in a
# separate process — importing controller modules can trigger API-key
# discovery that we don't need for a UI.
_BACKEND_ORDER: list[tuple[str, str]] = [
    ("gemini",   "Gemini"),
    ("anthropic","Anthropic Claude"),
    ("openai",   "OpenAI"),
    ("local",    "Local (llama.cpp)"),
]


# ── The page ──────────────────────────────────────────────────────────────


class BehaviorPage(Adw.PreferencesPage):
    """Verifier retry + fallback chain + cost-breach handling."""

    def __init__(self) -> None:
        super().__init__(title="Behavior", icon_name="applications-system-symbolic")
        self.set_name("behavior")

        self._system = read_toml(SYSTEM_CONFIG_PATH)
        self._user = read_toml(USER_CONFIG_PATH)
        self._pending_mode: Optional[str] = None
        self._pending_fallback: Optional[list[str]] = None
        self._pending_autocancel: Optional[bool] = None

        self._add_verifier_group()
        self._add_fallback_group()
        self._add_cost_group()
        self._add_actions_group()

    # ── Verifier retry ──────────────────────────────────────────────────

    def _effective_mode(self) -> str:
        v = walk(self._user, ("verifier", "retry_mode"))
        if isinstance(v, str):
            return v
        v = walk(self._system, ("verifier", "retry_mode"))
        if isinstance(v, str):
            return v
        return "on_call_failed_only"

    def _add_verifier_group(self) -> None:
        active = self._effective_mode()

        group = Adw.PreferencesGroup(
            title="Verifier retry mode (F-49)",
            description=(
                "How aggressively to retry when qb_verifier rejects an intent. "
                "The v6.61 UTM sweep showed legitimate writes rejected as "
                "'verifier call failed'; the right mode is workload-dependent — "
                "A/B this to see which works best for you."
            ),
        )
        self.add(group)

        self._mode_buttons: dict[str, Gtk.Button] = {}
        for mode in _MODES:
            row = Adw.ActionRow(title=mode.label, subtitle=mode.description)
            btn = Gtk.Button(label="Active" if mode.key == active else "Use this")
            btn.set_valign(Gtk.Align.CENTER)
            btn.add_css_class("flat" if mode.key != active else "suggested-action")
            btn.set_sensitive(mode.key != active)
            btn.connect("clicked", self._on_pick_mode, mode.key)
            row.add_suffix(btn)
            group.add(row)
            self._mode_buttons[mode.key] = btn

    def _on_pick_mode(self, _button, key: str) -> None:
        try:
            set_user_override(("verifier",), "retry_mode", key)
        except Exception as exc:  # noqa: BLE001
            self._flash(f"Save failed: {exc}", warning=True)
            return
        self._pending_mode = key
        for mode in _MODES:
            btn = self._mode_buttons[mode.key]
            btn.remove_css_class("suggested-action")
            btn.remove_css_class("flat")
            if mode.key == key:
                btn.set_label("Active")
                btn.set_sensitive(False)
                btn.add_css_class("suggested-action")
            else:
                btn.set_label("Use this")
                btn.set_sensitive(True)
                btn.add_css_class("flat")
        self._flash(f"Selected '{key}'. Click 'Apply & Restart' to activate.")

    # ── Fallback chain ──────────────────────────────────────────────────

    def _effective_fallback(self) -> list[str]:
        v = walk(self._user, ("qb", "fallback_chain"))
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return list(v)
        v = walk(self._system, ("qb", "fallback_chain"))
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return list(v)
        return []

    def _add_fallback_group(self) -> None:
        current = set(self._effective_fallback())
        primary = str(effective(
            self._system, self._user, ("qb",), "backend", "gemini"
        ))

        group = Adw.PreferencesGroup(
            title="Fallback backend chain",
            description=(
                "If the primary QB backend fails after its retry budget, "
                "the controller falls back to these in the order shown. "
                "Fallback fires on network / API errors, NOT schema errors "
                "(those are prompt issues). Uncheck a backend to skip it. "
                "The primary backend (currently: "
                f"{primary}) is never in this list."
            ),
        )
        self.add(group)

        self._fallback_switches: dict[str, Gtk.Switch] = {}
        for key, label in _BACKEND_ORDER:
            row = Adw.ActionRow(
                title=label,
                subtitle=(
                    "Primary backend — configure in Models page" if key == primary
                    else "Fallback candidate"
                ),
            )
            sw = Gtk.Switch()
            sw.set_valign(Gtk.Align.CENTER)
            sw.set_active(key in current)
            sw.set_sensitive(key != primary)
            sw.connect("state-set", self._on_fallback_toggled, key)
            row.add_suffix(sw)
            row.set_activatable_widget(sw)
            group.add(row)
            self._fallback_switches[key] = sw

    def _on_fallback_toggled(self, _switch, _state: bool, _key: str) -> bool:
        chain: list[str] = []
        for key, _label in _BACKEND_ORDER:
            sw = self._fallback_switches.get(key)
            if sw is not None and sw.get_active():
                chain.append(key)
        self._pending_fallback = chain
        return False

    # ── Cost breach ─────────────────────────────────────────────────────

    def _effective_autocancel(self) -> bool:
        v = walk(self._user, ("cost", "auto_cancel_on_breach"))
        if isinstance(v, bool):
            return v
        v = walk(self._system, ("cost", "auto_cancel_on_breach"))
        if isinstance(v, bool):
            return v
        return True

    def _add_cost_group(self) -> None:
        group = Adw.PreferencesGroup(
            title="Cost breach handling",
            description=(
                "Behavior when the session's cost total crosses "
                "session_ceiling_usd (set in the Limits page)."
            ),
        )
        self.add(group)

        row = Adw.ActionRow(
            title="Auto-cancel on breach",
            subtitle=(
                "On: cancel the current turn cleanly when the ceiling is "
                "crossed, refuse new turns. Off: emit a warning but keep "
                "running (useful for headless overnight runs where a "
                "manual stop is fine)."
            ),
        )
        sw = Gtk.Switch()
        sw.set_active(self._effective_autocancel())
        sw.set_valign(Gtk.Align.CENTER)
        sw.connect("state-set", self._on_autocancel_toggled)
        row.add_suffix(sw)
        row.set_activatable_widget(sw)
        group.add(row)
        self._autocancel_switch = sw

    def _on_autocancel_toggled(self, _switch, state: bool) -> bool:
        self._pending_autocancel = bool(state)
        return False

    # ── Apply ────────────────────────────────────────────────────────────

    def _add_actions_group(self) -> None:
        group = Adw.PreferencesGroup(title="Apply changes")
        self.add(group)

        row = Adw.ActionRow(
            title="Restart Controller",
            subtitle="Applies pending changes. Requires your password (polkit).",
        )
        btn = Gtk.Button(label="Apply & Restart Controller")
        btn.set_valign(Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self._on_restart_clicked)
        row.add_suffix(btn)
        group.add(row)

        row2 = Adw.ActionRow(
            title="Config path",
            subtitle=f"User override lives at {USER_CONFIG_PATH}",
        )
        group.add(row2)

        status_group = Adw.PreferencesGroup()
        self._status_label = Gtk.Label()
        self._status_label.set_wrap(True)
        self._status_label.add_css_class("ib-muted-text")
        status_row = Adw.ActionRow()
        status_row.set_child(self._status_label)
        status_group.add(status_row)
        self.add(status_group)

    def _on_restart_clicked(self, _button) -> None:
        # Persist any pending non-verifier changes first (verifier writes
        # are already committed on the button click).
        try:
            if self._pending_fallback is not None:
                set_user_override(("qb",), "fallback_chain", self._pending_fallback)
            if self._pending_autocancel is not None:
                set_user_override(("cost",), "auto_cancel_on_breach",
                                  self._pending_autocancel)
        except Exception as exc:  # noqa: BLE001
            self._flash(f"Save failed: {exc}", warning=True)
            return

        ok, msg = restart_controller()
        if ok:
            self._pending_mode = None
            self._pending_fallback = None
            self._pending_autocancel = None
            self._flash(msg)
        else:
            self._flash(f"Restart failed: {msg}", warning=True)

    def _flash(self, message: str, warning: bool = False) -> None:
        self._status_label.remove_css_class("ib-muted-text")
        self._status_label.remove_css_class("ib-destructive")
        self._status_label.remove_css_class("ib-secondary")
        self._status_label.add_css_class(
            "ib-destructive" if warning else "ib-secondary"
        )
        self._status_label.set_label(message)
