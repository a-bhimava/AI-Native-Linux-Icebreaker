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
    SYSTEM_CONFIG_PATH,
    USER_CONFIG_PATH,
    effective,
    read_toml,
    restart_controller,
    set_user_override,
)


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

        self._system = read_toml(SYSTEM_CONFIG_PATH)
        self._user = read_toml(USER_CONFIG_PATH)
        self._pending: dict[tuple, object] = {}

        self._add_backend_group()
        self._add_qb_group()
        self._add_pb_group()
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
        self._pending[("qb", "backend")] = backend
        self._refresh_model_combo(backend, self._effective_model(backend))
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
        else:
            # Custom picked — show entry
            self._custom_row.set_visible(True)

    def _on_custom_changed(self, entry) -> None:
        text = entry.get_text().strip()
        backend = self._current_backend()
        if text:
            self._pending[("qb", backend, "model")] = text

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
