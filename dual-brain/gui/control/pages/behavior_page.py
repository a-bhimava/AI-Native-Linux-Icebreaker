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
    ConfigReadError,
    SYSTEM_CONFIG_PATH,
    USER_CONFIG_PATH,
    effective,
    read_toml,
    restart_controller,
    set_user_override,
    walk,
)
from ..schema_reader import get_field_spec


def _safe_read_toml(path):
    """See errors_page._safe_read_toml — surface parse errors instead
    of silently regressing to defaults (F-53 Scope A.P2)."""
    try:
        return read_toml(path), None
    except ConfigReadError as exc:
        return {}, str(exc)


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

        self._system, self._system_read_error = _safe_read_toml(
            SYSTEM_CONFIG_PATH,
        )
        self._user, self._user_read_error = _safe_read_toml(
            USER_CONFIG_PATH,
        )
        self._pending_mode: Optional[str] = None
        self._pending_fallback: Optional[list[str]] = None
        self._pending_autocancel: Optional[bool] = None
        # Phase 6 Scope B — verifier voting (declared-but-unsurfaced
        # fields) + qb_max_retries. Live pending values so the
        # cross-field validator can decide whether to enable Apply.
        self._pending_votes: Optional[int] = None
        self._pending_require: Optional[int] = None
        self._pending_parallel: Optional[bool] = None
        self._pending_qb_max_retries: Optional[int] = None
        # Widgets referenced from the validator + on-save path.
        self._votes_spin: Optional[Gtk.SpinButton] = None
        self._require_spin: Optional[Gtk.SpinButton] = None
        self._parallel_switch: Optional[Gtk.Switch] = None
        self._qb_retries_spin: Optional[Gtk.SpinButton] = None
        self._vote_validator_row: Optional[Adw.ActionRow] = None

        self._add_verifier_voting_group()
        self._add_verifier_group()
        self._add_fallback_group()
        self._add_cost_group()
        self._add_debug_group()
        self._add_actions_group()

    # ── Verifier voting (Phase 6 Scope B) ───────────────────────────────

    def _add_verifier_voting_group(self) -> None:
        """Surface `verifier.votes`, `verifier.require`, `verifier.parallel`,
        `run.qb_max_retries` — declared in TOML but not editable in GUI
        pre-Scope B. Bounds come from the JSON Schema (single source of
        truth). Cross-field validator: `require > votes` (or
        `require == votes` with parallel=false) is a config that always
        rejects; Apply is disabled while the warning is live (BP-11
        validate-on-input)."""

        group = Adw.PreferencesGroup(
            title="Verifier voting",
            description=(
                "Multi-vote qb_verifier: run N independent calls and "
                "accept if at least K vote 'verified'. Default (votes=1) "
                "preserves single-call behavior. `require = 0` = majority."
            ),
        )
        self.add(group)

        current_votes = int(
            effective(self._system, self._user, ("verifier",), "votes", 1) or 1
        )
        current_require = int(
            effective(self._system, self._user, ("verifier",), "require", 0) or 0
        )
        current_parallel = bool(
            effective(self._system, self._user, ("verifier",), "parallel", True)
        )
        current_qb_retries = int(
            effective(self._system, self._user, ("run",), "qb_max_retries", 3) or 3
        )

        # ── votes row (schema-bounded) ──
        votes_lo, votes_hi = 1, 9
        votes_spec = get_field_spec(("verifier",), "votes")
        if votes_spec and votes_spec.minimum is not None:
            votes_lo = int(votes_spec.minimum)
        if votes_spec and votes_spec.maximum is not None:
            votes_hi = int(votes_spec.maximum)
        votes_row = Adw.ActionRow(
            title="Votes",
            subtitle=(
                votes_spec.description if votes_spec and votes_spec.description
                else "Number of independent verifier calls per turn. "
                     "1 = single-call (default)."
            ),
        )
        votes_spin = Gtk.SpinButton()
        votes_spin.set_valign(Gtk.Align.CENTER)
        votes_spin.set_adjustment(Gtk.Adjustment(
            value=float(current_votes),
            lower=votes_lo, upper=votes_hi,
            step_increment=1, page_increment=1,
        ))
        votes_spin.set_digits(0)
        votes_spin.set_numeric(True)
        votes_spin.connect("value-changed", self._on_votes_changed)
        votes_row.add_suffix(votes_spin)
        self._votes_spin = votes_spin
        group.add(votes_row)

        # ── require row (schema-bounded) ──
        req_lo, req_hi = 0, 9
        req_spec = get_field_spec(("verifier",), "require")
        if req_spec and req_spec.minimum is not None:
            req_lo = int(req_spec.minimum)
        if req_spec and req_spec.maximum is not None:
            req_hi = int(req_spec.maximum)
        require_row = Adw.ActionRow(
            title="Require",
            subtitle=(
                req_spec.description if req_spec and req_spec.description
                else "Votes needed to accept. 0 = majority of `votes`."
            ),
        )
        require_spin = Gtk.SpinButton()
        require_spin.set_valign(Gtk.Align.CENTER)
        require_spin.set_adjustment(Gtk.Adjustment(
            value=float(current_require),
            lower=req_lo, upper=req_hi,
            step_increment=1, page_increment=1,
        ))
        require_spin.set_digits(0)
        require_spin.set_numeric(True)
        require_spin.connect("value-changed", self._on_require_changed)
        require_row.add_suffix(require_spin)
        self._require_spin = require_spin
        group.add(require_row)

        # ── parallel row (bool) ──
        parallel_row = Adw.ActionRow(
            title="Parallel",
            subtitle="Run votes in parallel (ThreadPoolExecutor). Off = "
                     "sequential (slower but easier to debug).",
        )
        parallel_switch = Gtk.Switch()
        parallel_switch.set_valign(Gtk.Align.CENTER)
        parallel_switch.set_active(current_parallel)
        parallel_switch.connect("state-set", self._on_parallel_changed)
        parallel_row.add_suffix(parallel_switch)
        self._parallel_switch = parallel_switch
        group.add(parallel_row)

        # ── validator row (initially hidden) ──
        validator_row = Adw.ActionRow(
            title="⚠ Configuration will always reject",
            subtitle=(
                "Votes ≤ require means the verifier can never accept. "
                "Lower `require` or raise `votes`."
            ),
        )
        validator_row.add_css_class("error")
        group.add(validator_row)
        validator_row.set_visible(False)
        self._vote_validator_row = validator_row

        # ── qb_max_retries row ──
        retries_lo, retries_hi = 0, 10
        retries_spec = get_field_spec(("run",), "qb_max_retries")
        if retries_spec and retries_spec.minimum is not None:
            retries_lo = int(retries_spec.minimum)
        if retries_spec and retries_spec.maximum is not None:
            retries_hi = int(retries_spec.maximum)
        retries_row = Adw.ActionRow(
            title="QB max retries",
            subtitle=(
                retries_spec.description if retries_spec and retries_spec.description
                else "Times to retry a rejected QB response before falling "
                     "through to the fallback chain."
            ),
        )
        retries_spin = Gtk.SpinButton()
        retries_spin.set_valign(Gtk.Align.CENTER)
        retries_spin.set_adjustment(Gtk.Adjustment(
            value=float(current_qb_retries),
            lower=retries_lo, upper=retries_hi,
            step_increment=1, page_increment=1,
        ))
        retries_spin.set_digits(0)
        retries_spin.set_numeric(True)
        retries_spin.connect("value-changed", self._on_qb_retries_changed)
        retries_row.add_suffix(retries_spin)
        self._qb_retries_spin = retries_spin
        group.add(retries_row)

        # Pre-populate pending so Apply always sees the current values
        # even if the user didn't touch them (idempotent save).
        self._pending_votes = current_votes
        self._pending_require = current_require
        self._pending_parallel = current_parallel
        self._pending_qb_max_retries = current_qb_retries
        # Run initial validator pass so an already-broken config on
        # disk shows the warning immediately.
        self._revalidate_votes()

    def _on_votes_changed(self, spin: Gtk.SpinButton) -> None:
        self._pending_votes = int(spin.get_value())
        self._revalidate_votes()

    def _on_require_changed(self, spin: Gtk.SpinButton) -> None:
        self._pending_require = int(spin.get_value())
        self._revalidate_votes()

    def _on_parallel_changed(self, _switch, active: bool) -> bool:
        self._pending_parallel = bool(active)
        self._revalidate_votes()
        return False  # let Gtk update the switch state

    def _on_qb_retries_changed(self, spin: Gtk.SpinButton) -> None:
        self._pending_qb_max_retries = int(spin.get_value())

    def _revalidate_votes(self) -> None:
        """Cross-field validator. Reason: `require > votes` is always
        false; `require == votes` on sequential (parallel=false) means
        the FIRST 'verified: false' short-circuits the loop and forces
        rejection, so equal votes/require on sequential is also useless.
        Show warning + drive Apply-button sensitivity."""
        if self._vote_validator_row is None:
            return
        votes = self._pending_votes or 1
        require = self._pending_require or 0
        parallel = self._pending_parallel if self._pending_parallel is not None else True
        # `require = 0` means "majority", never rejecting-by-config.
        broken = False
        if require > votes:
            broken = True
        elif require > 0 and require == votes and not parallel:
            broken = True
        self._vote_validator_row.set_visible(broken)
        # Signal Apply-button state — the Actions group reads this
        # attribute when constructing the button; the callback below
        # keeps it in sync live.
        self._vote_config_broken = broken
        apply_btn = getattr(self, "_apply_button", None)
        if apply_btn is not None:
            apply_btn.set_sensitive(not broken)

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

    # ── Debug mode (Phase 6 Scope D/E) ──────────────────────────────────

    def _add_debug_group(self) -> None:
        """Surface `[debug].enabled` + a "Clear log" affordance.

        Debug mode is off by default (BP-2). When on, key call sites
        (fallback fire, preset verify, config write, restart) emit
        structured JSONL to `$XDG_STATE_HOME/icebreaker/debug.jsonl`.
        Secrets (api_key / token / password) are redacted before write.
        """
        group = Adw.PreferencesGroup(
            title="Debug mode",
            description=(
                "Off by default. When on, the controller records "
                "structured diagnostics to "
                "$XDG_STATE_HOME/icebreaker/debug.jsonl for later "
                "triage. API keys and tokens are automatically "
                "redacted. Restart required to apply."
            ),
        )
        self.add(group)

        current_enabled = bool(effective(
            self._system, self._user, ("debug",), "enabled", False,
        ))

        toggle_row = Adw.ActionRow(
            title="Enable debug logging",
            subtitle="Restart controller to apply. Off = zero overhead.",
        )
        toggle_switch = Gtk.Switch()
        toggle_switch.set_valign(Gtk.Align.CENTER)
        toggle_switch.set_active(current_enabled)
        toggle_switch.connect("state-set", self._on_debug_enabled_changed)
        toggle_row.add_suffix(toggle_switch)
        group.add(toggle_row)
        self._debug_toggle = toggle_switch

        # Clear-log affordance. Doesn't need a restart; acts on the
        # log file directly.
        clear_row = Adw.ActionRow(
            title="Clear debug log",
            subtitle="Truncate the current debug.jsonl file.",
        )
        clear_btn = Gtk.Button(label="Clear now")
        clear_btn.set_valign(Gtk.Align.CENTER)
        clear_btn.connect("clicked", self._on_debug_clear)
        clear_row.add_suffix(clear_btn)
        group.add(clear_row)

    def _on_debug_enabled_changed(self, _switch, active: bool) -> bool:
        """Persist the toggle to `~/.config/icebreaker/controller.toml`
        immediately (single-checkbox interactions don't need an Apply
        button). Restart-required semantics are conveyed via the row
        subtitle so users know the effect is on next restart."""
        try:
            set_user_override(("debug",), "enabled", bool(active))
            self._flash(
                "Debug mode "
                + ("enabled" if active else "disabled")
                + " — click Apply & Restart to load."
            )
        except Exception as exc:  # noqa: BLE001
            self._flash(f"Save failed: {exc}", warning=True)
        return False  # let Gtk update the switch visually

    def _on_debug_clear(self, _button) -> None:
        """Truncate the debug log via `controller.debug_log.clear()`.
        Lazy-import so the button remains functional even if the
        controller module can't be loaded from this GUI process."""
        try:
            from controller import debug_log
            ok, msg = debug_log.clear()
            if ok:
                self._flash(msg)
            else:
                self._flash(msg, warning=True)
        except Exception as exc:  # noqa: BLE001
            self._flash(f"Clear failed: {exc}", warning=True)

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
        # Phase 6 Scope B: Apply button is disabled when the verifier
        # voting validator says the current pending config would always
        # reject. Store the reference so _revalidate_votes() can toggle
        # `sensitive` live.
        self._apply_button = btn
        # Re-run validator now that _apply_button exists — the initial
        # call in _add_verifier_voting_group happened before the button
        # was created (see BP-4: gate integrity).
        self._revalidate_votes()

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
        # Phase 6 Scope B: guard against saving a config the validator
        # flagged as always-rejecting. Belt-and-braces: the button is
        # already disabled but callers can programmatically click it.
        if getattr(self, "_vote_config_broken", False):
            self._flash(
                "Verifier voting config would always reject — fix "
                "before saving.", warning=True,
            )
            return
        # Persist any pending non-verifier changes first (verifier writes
        # are already committed on the button click).
        try:
            if self._pending_fallback is not None:
                set_user_override(("qb",), "fallback_chain", self._pending_fallback)
            if self._pending_autocancel is not None:
                set_user_override(("cost",), "auto_cancel_on_breach",
                                  self._pending_autocancel)
            # Phase 6 Scope B: persist the new verifier voting + qb
            # retry knobs. Values are always populated (initialised
            # from effective config) so writing them is idempotent.
            if self._pending_votes is not None:
                set_user_override(("verifier",), "votes",
                                  self._pending_votes)
            if self._pending_require is not None:
                set_user_override(("verifier",), "require",
                                  self._pending_require)
            if self._pending_parallel is not None:
                set_user_override(("verifier",), "parallel",
                                  self._pending_parallel)
            if self._pending_qb_max_retries is not None:
                set_user_override(("run",), "qb_max_retries",
                                  self._pending_qb_max_retries)
        except Exception as exc:  # noqa: BLE001
            self._flash(f"Save failed: {exc}", warning=True)
            return

        ok, msg = restart_controller()
        if ok:
            self._pending_mode = None
            self._pending_fallback = None
            self._pending_autocancel = None
            # Phase 6 Scope B: clear pending after successful restart so
            # subsequent edits are diff'd against the freshly-written
            # values.
            self._pending_votes = None
            self._pending_require = None
            self._pending_parallel = None
            self._pending_qb_max_retries = None
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
