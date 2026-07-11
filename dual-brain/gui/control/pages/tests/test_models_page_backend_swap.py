"""Phase 6 Scope B B3 — models_page backend-swap race regression.

Root cause pre-Scope-B: when the user typed a custom model ID (e.g.
`gemini-2.5-flash-thinking`) then switched backends via the dropdown,
the custom text field silently retained its old contents. If they then
clicked Apply, the OLD backend's custom text was written under the NEW
backend's `qb.<new>.model` key, silently corrupting the new backend's
config.

The fix (in models_page.ModelsPage._on_backend_changed):
  1. Before swapping, if the custom row is visible, save the current
     text under the OLD backend's `("qb", old_backend, "model")` key.
  2. Cache the old text in `_custom_text_by_backend[old_backend]` so
     switching back restores it.
  3. Clear/restore the entry to reflect the NEW backend's state.

We exercise the handler directly because ModelsPage inherits from
Adw.PreferencesPage — instantiating one requires a live GTK context.
Instead we build a bare object with only the attributes the handler
touches and drive `_on_backend_changed` through fake objects.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


# `class ModelsPage(Adw.PreferencesPage)` runs at import time and needs
# a real subclass-able base. A plain MagicMock() breaks isinstance /
# __new__ checks. Build a minimal Adw namespace with concrete class
# stubs, then mock the rest of gi.
class _PageStub:
    """Stand-in for Adw.PreferencesPage — inheritable + trivially
    constructible so ``ModelsPage.__new__`` succeeds."""
    def __init__(self, *args, **kwargs) -> None:
        pass
    def set_name(self, *args, **kwargs) -> None:
        pass
    def add(self, *args, **kwargs) -> None:
        pass


class _RowStub:
    """Stand-in for Adw.ActionRow / Adw.EntryRow / Adw.ExpanderRow."""
    def __init__(self, *args, **kwargs) -> None:
        pass
    def add_suffix(self, *args, **kwargs) -> None:
        pass
    def add_row(self, *args, **kwargs) -> None:
        pass
    def set_child(self, *args, **kwargs) -> None:
        pass
    def set_visible(self, *args, **kwargs) -> None:
        pass
    def set_expanded(self, *args, **kwargs) -> None:
        pass


_adw_mock = MagicMock()
_adw_mock.PreferencesPage = _PageStub
_adw_mock.PreferencesGroup = _RowStub
_adw_mock.ActionRow = _RowStub
_adw_mock.EntryRow = _RowStub
_adw_mock.ExpanderRow = _RowStub

_gi_mock = MagicMock()
_gi_mock.repository = MagicMock()
_gi_mock.repository.Adw = _adw_mock
_gi_mock.repository.Gtk = MagicMock()

sys.modules.setdefault("gi", _gi_mock)
sys.modules.setdefault("gi.repository", _gi_mock.repository)

# Import AFTER the mocks are installed so the module-level `from
# gi.repository import Gtk, Adw` picks up the stubbed replacements.
from gui.control.pages import models_page as mp


# ── Helpers ──────────────────────────────────────────────────────────────


# Phase 6 Scope C: the page reads presets from `preset_registry`, no
# longer from module-level `_BACKENDS` / `_MODEL_PRESETS`. Build a
# minimal in-memory registry so the swap tests stay isolated from
# whatever `catalogue.toml` ships.
from controller.preset_registry import Provider, Preset

_STUB_PROVIDERS: tuple[Provider, ...] = (
    Provider(
        key="gemini", display_name="Gemini (cloud)",
        presets=(
            Preset(
                id="gemini-2.5-flash", provider="gemini",
                display_name="Gemini 2.5 Flash", context_window=1_048_576,
                output_token_limit=None, status="recommended",
                capabilities=(), notes="", verified_at=None,
            ),
        ),
    ),
    Provider(
        key="anthropic", display_name="Anthropic Claude (cloud)",
        presets=(
            Preset(
                id="claude-sonnet-4-6", provider="anthropic",
                display_name="Claude Sonnet 4.6", context_window=200_000,
                output_token_limit=None, status="recommended",
                capabilities=(), notes="", verified_at=None,
            ),
        ),
    ),
    Provider(
        key="openai", display_name="OpenAI (cloud)",
        presets=(
            Preset(
                id="gpt-5-mini", provider="openai",
                display_name="GPT-5 Mini", context_window=400_000,
                output_token_limit=None, status="unverified",
                capabilities=(), notes="", verified_at=None,
            ),
        ),
    ),
    Provider(
        key="local", display_name="Local (llama.cpp)",
        presets=(
            Preset(
                id="qwen-2.5-coder-1.5b-instruct-q4_k_m", provider="local",
                display_name="Qwen 2.5 Coder 1.5B (local)",
                context_window=32_768,
                output_token_limit=None, status="recommended",
                capabilities=(), notes="", verified_at=None,
            ),
        ),
    ),
)


def _make_page(effective_backend: str = "gemini",
               effective_model_by_backend: dict[str, str] | None = None):
    """Build a bare object with just the attributes needed by
    `_on_backend_changed`. Faster than a real page and lets us inspect
    _pending / _custom_text_by_backend directly."""
    page = mp.ModelsPage.__new__(mp.ModelsPage)
    page._system = {}
    page._user = {}
    page._pending = {}
    page._custom_text_by_backend = {}
    page._custom_row_backend = effective_backend
    # Phase 6 Scope C: inject the stub registry so the handler
    # doesn't try to read the real `catalogue.toml` (which changes as
    # the catalogue evolves and would make these tests brittle).
    page._providers = _STUB_PROVIDERS
    page._preset_ids_by_backend = {
        p.key: [preset.id for preset in p.presets] for p in _STUB_PROVIDERS
    }

    # Fake widgets — track calls / state via MagicMock.
    page._custom_row = MagicMock()
    page._custom_row.get_text.return_value = ""
    page._custom_row.get_visible.return_value = False
    page._custom_row._visible = False

    # `_refresh_model_combo` builds a new StringList from Gtk (mocked).
    page._model_combo = MagicMock()

    # `_confirm_summary` may or may not be set — models_page only
    # updates it if not None.
    page._confirm_summary = None

    # Store effective values so `_effective_model` can return them.
    _by_backend = effective_model_by_backend or {
        "gemini": "gemini-2.5-flash",
        "anthropic": "claude-sonnet-4-6",
        "openai": "gpt-5-mini",
        "local": "qwen-2.5-coder-1.5b-instruct-q4_k_m",
    }
    page._effective_model_by_backend = _by_backend

    # Override the two page methods that read from self._user/_system
    # for the effective values.
    def _effective_backend():
        return effective_backend
    def _effective_model(backend: str):
        return _by_backend.get(backend, "")

    page._effective_backend = _effective_backend  # type: ignore[assignment]
    page._effective_model = _effective_model  # type: ignore[assignment]

    # Silence _flash — it writes to a Gtk label we don't have.
    page._flash = MagicMock()

    return page


def _backend_index(key: str) -> int:
    """Look up a backend's index in the stub provider order — replaces
    the pre-Scope-C `mp._BACKENDS.index()` pattern."""
    for i, provider in enumerate(_STUB_PROVIDERS):
        if provider.key == key:
            return i
    raise KeyError(key)


def _fake_backend_combo(new_idx: int) -> MagicMock:
    """Fake Gtk.DropDown whose `.get_selected()` returns `new_idx`."""
    combo = MagicMock()
    combo.get_selected.return_value = new_idx
    return combo


# ── The regression ──────────────────────────────────────────────────────


def test_backend_swap_saves_old_custom_text_under_old_key() -> None:
    """User typed `my-custom-gemini`, then switches Gemini → Anthropic.
    The custom text must land under `("qb", "gemini", "model")`, NOT
    under Anthropic's key. This is the exact race that Scope B B3 fixes.
    """
    page = _make_page(effective_backend="gemini")
    # Simulate: custom row is visible with the user's typed text.
    page._custom_row.get_visible.return_value = True
    page._custom_row.get_text.return_value = "my-custom-gemini-thinking"

    # Anthropic is index 1 in _BACKENDS.
    idx_anthropic = _backend_index("anthropic")
    combo = _fake_backend_combo(idx_anthropic)

    page._on_backend_changed(combo, None)

    # The old backend's key holds the old text — NOT anthropic's key.
    assert page._pending.get(
        ("qb", "gemini", "model")
    ) == "my-custom-gemini-thinking"
    assert ("qb", "anthropic", "model") not in page._pending

    # The new backend is pending.
    assert page._pending.get(("qb", "backend")) == "anthropic"

    # And the old text is cached so switching back restores it.
    assert page._custom_text_by_backend.get(
        "gemini"
    ) == "my-custom-gemini-thinking"

    # The custom row was reset to what Anthropic's effective model is —
    # since we didn't cache any prior text for Anthropic, that's empty.
    page._custom_row.set_text.assert_called_with("")


def test_backend_swap_restores_previously_cached_text() -> None:
    """Switch A → B → A: the custom text typed for A must be
    restored when we come back."""
    page = _make_page(effective_backend="gemini")
    # Pretend we already switched Gemini → Anthropic once and cached
    # gemini's text.
    page._custom_text_by_backend["gemini"] = "my-cached-gemini"
    # Now we're on Anthropic and have no custom text open.
    page._custom_row_backend = "anthropic"
    page._custom_row.get_visible.return_value = False
    page._custom_row.get_text.return_value = ""

    idx_gemini = _backend_index("gemini")
    page._on_backend_changed(_fake_backend_combo(idx_gemini), None)

    # The row was reset to the cached gemini text.
    page._custom_row.set_text.assert_called_with("my-cached-gemini")
    # Pending backend swapped.
    assert page._pending.get(("qb", "backend")) == "gemini"


def test_no_stale_text_leaks_to_new_backend_when_switching_from_hidden() -> None:
    """If the custom row was NOT visible on the old backend (user was
    on a preset), swapping backends must not fabricate a save under
    the old backend's key from stale entry text."""
    page = _make_page(effective_backend="gemini")
    # Custom row hidden — user was on a preset.
    page._custom_row.get_visible.return_value = False
    # Even if the entry buffer has stale text from a much earlier
    # interaction, we should not save it.
    page._custom_row.get_text.return_value = "stale-text-from-before"

    idx_openai = _backend_index("openai")
    page._on_backend_changed(_fake_backend_combo(idx_openai), None)

    # No save under old backend key.
    assert ("qb", "gemini", "model") not in page._pending
    # Only the backend swap.
    assert page._pending.get(("qb", "backend")) == "openai"


def test_current_backend_reflects_pending_swap() -> None:
    """After a swap, `_current_backend` must return the new backend so
    the next `_on_custom_changed` writes to the right stanza."""
    page = _make_page(effective_backend="gemini")

    idx_local = _backend_index("local")
    page._on_backend_changed(_fake_backend_combo(idx_local), None)

    assert page._current_backend() == "local"
