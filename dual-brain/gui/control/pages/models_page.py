"""Models page — QB backend picker, model preset + custom, tokens/timeouts.

v6.65 makes every hard-coded value in ``controller.toml`` a first-class
Control Center setting. The Models page covers:

* QB backend selection (Gemini / Anthropic / OpenAI / local)
* QB model preset per backend (with "Custom…" free-text override)
* QB max_tokens + timeout (raised ceilings: 1M tokens, 2h timeout)
* PB max_tokens + timeout

All values write to ``~/.config/icebreaker/controller.toml`` (user layered
override — takes precedence over ``/etc/icebreaker/controller.toml``).
Restart button applies via ``pkexec systemctl restart``.
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
)
from ..schema_reader import get_field_spec


def _safe_read_toml(path):
    """See errors_page._safe_read_toml — surface parse errors instead
    of silently regressing to defaults (F-53 Scope A.P2)."""
    try:
        return read_toml(path), None
    except ConfigReadError as exc:
        return {}, str(exc)


# ── Backend + model presets ───────────────────────────────────────────────

_BACKENDS: list[tuple[str, str]] = [
    ("gemini",   "Gemini (cloud)"),
    ("anthropic","Anthropic Claude (cloud)"),
    ("openai",   "OpenAI (cloud)"),
    ("local",    "Local (llama.cpp)"),
]

_MODEL_PRESETS: dict[str, list[str]] = {
    "gemini": [
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
    ],
    "anthropic": [
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
    ],
    "openai": [
        "gpt-5-mini",
        "gpt-5",
        "gpt-4o",
    ],
    "local": [
        "qwen-2.5-coder-1.5b-instruct-q4_k_m",
    ],
}

_CUSTOM_LABEL = "Custom…"


# ── Numeric knob spec ─────────────────────────────────────────────────────

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


_KNOBS_QB: list[_Knob] = [
    _Knob(("qb", "gemini"), "max_tokens", "QB max_tokens",
          "Cap on Gemini output tokens per call. Ceiling is 1M — raise "
          "if long-content writes truncate.",
          8192, 128, 1_000_000, 512, suffix="tokens"),
    _Knob(("qb", "gemini"), "timeout_seconds", "QB timeout",
          "How long to wait for Gemini per call.",
          60, 5, 7200, 5, suffix="s"),
]

_KNOBS_PB: list[_Knob] = [
    _Knob(("run",), "pb_max_tokens", "PB max_tokens",
          "Cap on local Privileged Brain output tokens per tool-call "
          "generation.",
          1024, 64, 1_000_000, 128, suffix="tokens"),
    _Knob(("run",), "pb_timeout_seconds", "PB timeout",
          "How long to wait for the local Privileged Brain per call. "
          "Emulated environments (Rosetta 2 / QEMU) need more headroom.",
          600, 5, 7200, 30, suffix="s"),
]


# ── The page ──────────────────────────────────────────────────────────────


class ModelsPage(Adw.PreferencesPage):
    """QB backend + model + token/timeout config."""

    def __init__(self) -> None:
        super().__init__(title="Models", icon_name="applications-science-symbolic")
        self.set_name("models")

        self._system, self._system_read_error = _safe_read_toml(
            SYSTEM_CONFIG_PATH,
        )
        self._user, self._user_read_error = _safe_read_toml(
            USER_CONFIG_PATH,
        )
        self._pending: dict[tuple, object] = {}
        # Phase 6 Scope B B3 race fix: track which backend the custom
        # text row currently corresponds to. When _on_backend_changed
        # fires we save the current custom text under the OLD backend's
        # `qb.<old>.model` key before clearing the entry.
        self._custom_row_backend: str | None = None

        self._add_backend_group()
        self._add_qb_group()
        self._add_pb_group()
        self._add_advanced_pb_group()
        self._add_actions_group()

    # ── Backend + model ──────────────────────────────────────────────────

    def _effective_backend(self) -> str:
        return str(effective(self._system, self._user,
                             ("qb",), "backend", "gemini"))

    def _effective_model(self, backend: str) -> str:
        default_by_backend = {
            "gemini": "gemini-2.5-flash",
            "anthropic": "claude-sonnet-4-6",
            "openai": "gpt-5-mini",
            "local": "qwen-2.5-coder-1.5b-instruct-q4_k_m",
        }
        return str(effective(self._system, self._user,
                             ("qb", backend), "model",
                             default_by_backend.get(backend, "")))

    def _add_backend_group(self) -> None:
        backend = self._effective_backend()
        model = self._effective_model(backend)

        group = Adw.PreferencesGroup(
            title="QB backend + model",
            description=(
                "The Quarantined Brain handles natural-language → structured "
                "intent. Swap models per workload: fast/cheap for simple ops, "
                "reasoning-heavy for creative content or planning."
            ),
        )
        self.add(group)

        # Backend picker
        backend_row = Adw.ActionRow(title="Backend")
        self._backend_combo = Gtk.DropDown()
        model_names = [label for _, label in _BACKENDS]
        self._backend_combo.set_model(Gtk.StringList.new(model_names))
        current_idx = next(
            (i for i, (k, _) in enumerate(_BACKENDS) if k == backend), 0
        )
        self._backend_combo.set_selected(current_idx)
        self._backend_combo.set_valign(Gtk.Align.CENTER)
        self._backend_combo.connect("notify::selected", self._on_backend_changed)
        backend_row.add_suffix(self._backend_combo)
        group.add(backend_row)

        # Model picker
        model_row = Adw.ActionRow(title="Model")
        self._model_combo = Gtk.DropDown()
        self._model_row = model_row
        self._refresh_model_combo(backend, model)
        self._model_combo.set_valign(Gtk.Align.CENTER)
        self._model_combo.connect("notify::selected", self._on_model_preset_changed)
        model_row.add_suffix(self._model_combo)
        group.add(model_row)

        # Free-text entry (only visible when Custom picked)
        self._custom_row = Adw.EntryRow(title="Custom model name")
        self._custom_row.set_text(
            model if model not in _MODEL_PRESETS.get(backend, []) else ""
        )
        self._custom_row.connect("changed", self._on_custom_changed)
        group.add(self._custom_row)
        self._custom_row.set_visible(
            model not in _MODEL_PRESETS.get(backend, [])
        )
        # Record which backend the custom text currently belongs to —
        # used by _on_backend_changed to save/restore per-backend text
        # (Phase 6 Scope B B3).
        self._custom_row_backend = backend

        # Per-backend cache of custom model text the user has typed but
        # not yet saved. Lets a user type Gemini custom → switch to
        # Anthropic → switch back → find their custom text restored.
        self._custom_text_by_backend: dict[str, str] = {}
        if self._custom_row.get_visible():
            self._custom_text_by_backend[backend] = self._custom_row.get_text()

        # Confirm-on-apply summary — surfaced next to the Apply button
        # so the user sees the exact `[qb].backend` + `[qb.<x>].model`
        # that will land in TOML before restart (BP-4 informed decision).
        self._confirm_summary: Optional[Gtk.Label] = None

    def _refresh_model_combo(self, backend: str, current_model: str) -> None:
        presets = _MODEL_PRESETS.get(backend, [])
        entries = presets + [_CUSTOM_LABEL]
        self._model_combo.set_model(Gtk.StringList.new(entries))
        if current_model in presets:
            self._model_combo.set_selected(presets.index(current_model))
        else:
            self._model_combo.set_selected(len(entries) - 1)  # Custom…

    def _on_backend_changed(self, combo, _pspec) -> None:
        idx = combo.get_selected()
        backend, _label = _BACKENDS[idx]

        # Phase 6 Scope B B3 fix: before we redirect the custom-model
        # text entry to the new backend, stash its current text under
        # the OLD backend's key. Otherwise a "typed Gemini custom →
        # switched to Anthropic → clicked Apply" workflow writes the
        # Gemini text under `[qb.anthropic].model`, silently corrupting
        # the Anthropic config.
        old_backend = self._custom_row_backend
        if (
            old_backend is not None
            and self._custom_row.get_visible()
        ):
            current_text = self._custom_row.get_text().strip()
            if current_text:
                # Remember it so switching BACK to old_backend restores
                # the user's typed value.
                self._custom_text_by_backend[old_backend] = current_text
                # Persist under the correct (old) backend key so a
                # subsequent Apply lands in the right stanza.
                self._pending[("qb", old_backend, "model")] = current_text

        self._pending[("qb", "backend")] = backend
        # Restore any custom text the user had previously typed for THIS
        # backend, otherwise clear the entry so stale text can't leak in.
        restored = self._custom_text_by_backend.get(backend, "")
        # Suppress the `changed` signal while we programmatically reset
        # the entry — otherwise handler_block_by_func would be cleaner,
        # but a simple flag matches the existing style.
        self._custom_row.set_text(restored)
        self._custom_row_backend = backend

        # Model combo now shows the new backend's presets; visibility of
        # the custom row depends on whether the effective model is a
        # preset or free text.
        effective_for_new = restored or self._effective_model(backend)
        self._refresh_model_combo(backend, effective_for_new)
        self._custom_row.set_visible(
            effective_for_new not in _MODEL_PRESETS.get(backend, [])
        )

        self._update_confirm_summary()
        self._flash(
            f"Backend set to '{backend}'. Set the model + API key next, "
            "then Apply & Restart."
        )

    def _on_model_preset_changed(self, combo, _pspec) -> None:
        idx = combo.get_selected()
        backend = self._current_backend()
        presets = _MODEL_PRESETS.get(backend, [])
        if idx < len(presets):
            model = presets[idx]
            self._pending[("qb", backend, "model")] = model
            self._custom_row.set_visible(False)
            # Preset picked — clear cached custom text for THIS backend
            # so switching backends and back doesn't restore stale text.
            self._custom_text_by_backend.pop(backend, None)
        else:
            # Custom picked — show entry
            self._custom_row.set_visible(True)
        self._update_confirm_summary()

    def _on_custom_changed(self, entry) -> None:
        text = entry.get_text().strip()
        backend = self._current_backend()
        if text:
            self._pending[("qb", backend, "model")] = text
            # Keep the per-backend cache in sync so the user's live
            # keystrokes survive a mid-flight backend swap-and-back.
            self._custom_text_by_backend[backend] = text
        self._update_confirm_summary()

    def _update_confirm_summary(self) -> None:
        """Render the "Will write" summary next to the Apply button so
        the user sees the exact TOML tuple before restart. BP-4 gate
        integrity: informed decision, no surprise on next boot."""
        if self._confirm_summary is None:
            return
        backend = self._current_backend()
        # Resolve the model the same way the save path will:
        pending_model = self._pending.get(("qb", backend, "model"))
        if isinstance(pending_model, str) and pending_model:
            model = pending_model
        else:
            model = self._effective_model(backend)
        self._confirm_summary.set_label(
            f"Will write [qb].backend = \"{backend}\" and "
            f"[qb.{backend}].model = \"{model}\"."
        )

    def _current_backend(self) -> str:
        pending = self._pending.get(("qb", "backend"))
        if isinstance(pending, str):
            return pending
        return self._effective_backend()

    # ── QB / PB numeric knobs ───────────────────────────────────────────

    def _add_qb_group(self) -> None:
        group = Adw.PreferencesGroup(
            title="QB tokens + timeout",
            description=(
                "Raise if you're hitting truncation on long-content writes "
                "or verifier responses. Ceiling: 1M tokens, 2h timeout."
            ),
        )
        self.add(group)
        for knob in _KNOBS_QB:
            self._add_knob(group, knob)

    def _add_pb_group(self) -> None:
        group = Adw.PreferencesGroup(
            title="PB tokens + timeout",
            description=(
                "PB emits tool-call JSON (rarely >200 tokens in practice), "
                "but the timeout matters under emulation. Raise if you see "
                "'PB not available' timeouts on Apple Silicon UTM."
            ),
        )
        self.add(group)
        for knob in _KNOBS_PB:
            self._add_knob(group, knob)

    def _add_advanced_pb_group(self) -> None:
        """Phase 6 Scope B B2 — pb_endpoint / pb_model_id / pb_transport
        are declared in TOML but were only editable by hand. Surfacing
        them here closes the "local-PB tuning requires shelling in"
        gap. Also hosts the client-side turn timeout (B1).

        Wrapped in an `Adw.ExpanderRow` collapsed by default so the
        common Gemini/Anthropic user isn't confronted with `unix://`
        socket paths on first open (Hick's law: keep the front page
        skimmable)."""
        group = Adw.PreferencesGroup(
            title="Advanced (local PB + timings)",
            description=(
                "Fields for users running a non-default local Privileged "
                "Brain or tuning client-side timeouts. Bundled ISOs use "
                "safe defaults; adjust only if you know why."
            ),
        )
        self.add(group)

        expander = Adw.ExpanderRow(
            title="Advanced PB endpoint + timings",
            subtitle="Expand to override defaults",
        )
        expander.set_expanded(False)
        group.add(expander)

        # pb_endpoint
        endpoint_row = Adw.EntryRow(title="PB endpoint")
        endpoint_row.set_text(str(effective(
            self._system, self._user,
            ("run",), "pb_endpoint",
            "http://127.0.0.1:8080",
        )))
        endpoint_row.connect("changed", self._on_pb_endpoint_changed)
        expander.add_row(endpoint_row)

        # pb_model_id
        model_id_row = Adw.EntryRow(title="PB model ID")
        model_id_row.set_text(str(effective(
            self._system, self._user,
            ("run",), "pb_model_id",
            "qwen-2.5-coder-1.5b-instruct-q4_k_m",
        )))
        model_id_row.connect("changed", self._on_pb_model_id_changed)
        expander.add_row(model_id_row)

        # pb_transport (http | unix)
        transport_row = Adw.ActionRow(
            title="PB transport",
            subtitle="'http' = TCP loopback; 'unix' = AF_UNIX socket.",
        )
        transport_combo = Gtk.DropDown()
        transport_combo.set_model(Gtk.StringList.new(["http", "unix"]))
        current_transport = str(effective(
            self._system, self._user,
            ("run",), "pb_transport", "http",
        ))
        transport_combo.set_selected(1 if current_transport == "unix" else 0)
        transport_combo.set_valign(Gtk.Align.CENTER)
        transport_combo.connect("notify::selected", self._on_pb_transport_changed)
        transport_row.add_suffix(transport_combo)
        expander.add_row(transport_row)

        # turn_timeout_seconds (Scope B B1 — was hardcoded 600.0 in client.py)
        turn_lo, turn_hi = 30.0, 7200.0
        turn_spec = get_field_spec(("run",), "turn_timeout_seconds")
        if turn_spec and turn_spec.minimum is not None:
            turn_lo = float(turn_spec.minimum)
        if turn_spec and turn_spec.maximum is not None:
            turn_hi = float(turn_spec.maximum)
        turn_row = Adw.ActionRow(
            title="Turn timeout",
            subtitle=(
                turn_spec.description if turn_spec and turn_spec.description
                else "Client-side timeout for a single turn.run RPC. "
                     "Raise for slow-emulation guests (Rosetta 2)."
            ),
        )
        current_turn = float(effective(
            self._system, self._user,
            ("run",), "turn_timeout_seconds", 600.0,
        ))
        turn_spin = Gtk.SpinButton()
        turn_spin.set_valign(Gtk.Align.CENTER)
        turn_spin.set_adjustment(Gtk.Adjustment(
            value=current_turn,
            lower=turn_lo, upper=turn_hi,
            step_increment=30, page_increment=300,
        ))
        turn_spin.set_digits(0)
        turn_spin.set_numeric(True)
        turn_spin.connect("value-changed", self._on_turn_timeout_changed)
        turn_row.add_suffix(turn_spin)
        suffix_lbl = Gtk.Label(label="s")
        suffix_lbl.add_css_class("dim-label")
        turn_row.add_suffix(suffix_lbl)
        # Restart-required badge
        badge = Gtk.Label(label="restart to apply")
        badge.add_css_class("dim-label")
        turn_row.add_suffix(badge)
        expander.add_row(turn_row)

    # ── Advanced PB / turn timeout handlers ─────────────────────────────

    def _on_pb_endpoint_changed(self, entry) -> None:
        text = entry.get_text().strip()
        if text:
            self._pending[("run", "pb_endpoint")] = text

    def _on_pb_model_id_changed(self, entry) -> None:
        text = entry.get_text().strip()
        if text:
            self._pending[("run", "pb_model_id")] = text

    def _on_pb_transport_changed(self, combo, _pspec) -> None:
        idx = combo.get_selected()
        value = "unix" if idx == 1 else "http"
        self._pending[("run", "pb_transport")] = value

    def _on_turn_timeout_changed(self, spin: Gtk.SpinButton) -> None:
        self._pending[("run", "turn_timeout_seconds")] = float(spin.get_value())

    def _add_knob(self, group: Adw.PreferencesGroup, knob: _Knob) -> None:
        row = Adw.ActionRow(title=knob.label, subtitle=knob.subtitle)
        current = effective(
            self._system, self._user, knob.section, knob.key, knob.default
        )
        spin = Gtk.SpinButton()
        spin.set_valign(Gtk.Align.CENTER)
        spin.set_adjustment(Gtk.Adjustment(
            value=float(current),
            lower=knob.lo, upper=knob.hi,
            step_increment=knob.step, page_increment=knob.step * 10,
        ))
        spin.set_digits(0 if knob.integer else 1)
        spin.set_numeric(True)
        spin.connect("value-changed", self._on_knob_changed, knob)
        row.add_suffix(spin)
        if knob.suffix:
            lbl = Gtk.Label(label=knob.suffix)
            lbl.add_css_class("dim-label")
            row.add_suffix(lbl)
        group.add(row)

    def _on_knob_changed(self, spin: Gtk.SpinButton, knob: _Knob) -> None:
        raw = spin.get_value()
        value = int(raw) if knob.integer else round(raw, 3)
        self._pending[knob.section + (knob.key,)] = value

    # ── Actions ─────────────────────────────────────────────────────────

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

        # Phase 6 Scope B B3: confirm-on-apply summary. Shows the exact
        # `[qb].backend` + `[qb.<x>].model` pair that Apply will write
        # so the user is never surprised at next boot. BP-4.
        confirm_row = Adw.ActionRow(title="On apply")
        confirm_label = Gtk.Label()
        confirm_label.set_wrap(True)
        confirm_label.add_css_class("dim-label")
        confirm_row.set_child(confirm_label)
        group.add(confirm_row)
        self._confirm_summary = confirm_label
        self._update_confirm_summary()

        row2 = Adw.ActionRow(
            title="Config file", subtitle=f"User override: {USER_CONFIG_PATH}"
        )
        group.add(row2)

        # Feedback strip
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
