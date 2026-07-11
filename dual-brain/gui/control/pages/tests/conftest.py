"""Phase 6 Scope D — shared pytest fixtures for Control Center page tests.

Every page test needs:
  * `gi` / `gi.repository` stubbed with Adw + Gtk classes that
    subclass cleanly (so `class Page(Adw.PreferencesPage):` works at
    import time on the headless Mac dev host that has no libadwaita).
  * A hermetic tempdir + env vars pointing config_io there so writes
    don't leak to `~/.config`.

Shared here rather than duplicated per test to keep the fixture
surface consistent — every page must see the same shim shape.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


# ── Adw + Gtk shim classes ───────────────────────────────────────────────


class _PageStub:
    """Stand-in for Adw.PreferencesPage — trivially inheritable."""
    def __init__(self, *args, **kwargs) -> None:
        pass
    def set_name(self, *args, **kwargs) -> None:
        pass
    def add(self, *args, **kwargs) -> None:
        pass


class _RowStub:
    """Stand-in for Adw.ActionRow / Adw.EntryRow / Adw.ExpanderRow /
    Adw.PreferencesGroup / Adw.Banner / Gtk.Widget generally.

    Unknown method calls return a no-op via ``__getattr__`` so we
    don't have to enumerate every possible GTK API surface. Explicit
    setters/getters are kept for the small set of state fields the
    backend-swap / validator tests actually inspect."""

    def __init__(self, *args, **kwargs) -> None:
        self._is_visible = True
        self._text = ""
        self._label = ""

    # Explicit state-tracking methods (existing tests assert on these).
    def set_visible(self, visible: bool) -> None:
        self._is_visible = bool(visible)
    def get_visible(self) -> bool:
        return self._is_visible
    def set_text(self, text: str) -> None:
        self._text = text
    def get_text(self) -> str:
        return self._text
    def set_label(self, label: str) -> None:
        self._label = label
    def get_label(self) -> str:
        return self._label

    def __getattr__(self, name: str):
        """Any unknown attribute (add, add_suffix, connect,
        set_activatable_widget, set_valign, ...) returns a no-op
        callable. Attribute access itself is safe — GTK APIs are all
        method calls, not attribute reads."""
        def _noop(*args, **kwargs):
            return None
        return _noop


_adw_mock = MagicMock()
_adw_mock.PreferencesPage = _PageStub
_adw_mock.PreferencesGroup = _RowStub
_adw_mock.ActionRow = _RowStub
_adw_mock.EntryRow = _RowStub
_adw_mock.ExpanderRow = _RowStub
_adw_mock.Banner = _RowStub
_adw_mock.Toast = _RowStub
_adw_mock.SpinRow = _RowStub

_gi_mock = MagicMock()
_gi_mock.repository = MagicMock()
_gi_mock.repository.Adw = _adw_mock
_gi_mock.repository.Gtk = MagicMock()
_gi_mock.repository.GLib = MagicMock()
_gi_mock.repository.Gdk = MagicMock()
_gi_mock.repository.Gio = MagicMock()

# Install shims *before* any Control Center page imports pull in
# `gi.repository`. Overwrite unconditionally — a plain MagicMock from
# another test file's `setdefault` doesn't have our _RowStub/_PageStub
# concrete classes and would cause "MagicMock is not a valid base
# class" errors on `class Page(Adw.PreferencesPage):`.
sys.modules["gi"] = _gi_mock
sys.modules["gi.repository"] = _gi_mock.repository
# Also stash each submodule so `from gi.repository import Adw` resolves
# to our shim rather than a fresh MagicMock.
sys.modules["gi.repository.Adw"] = _adw_mock
sys.modules["gi.repository.Gtk"] = _gi_mock.repository.Gtk
sys.modules["gi.repository.GLib"] = _gi_mock.repository.GLib
sys.modules["gi.repository.Gdk"] = _gi_mock.repository.Gdk
sys.modules["gi.repository.Gio"] = _gi_mock.repository.Gio


# ── Hermetic config paths ────────────────────────────────────────────────


_PAGE_MODULES = (
    "gui.control.pages.behavior_page",
    "gui.control.pages.limits_page",
    "gui.control.pages.models_page",
    "gui.control.pages.errors_page",
    "gui.control.pages.status_page",
    "gui.control.pages.tools_page",
    "gui.control.pages.keys_page",
    "gui.control.pages.theme_page",
)


def _propagate_paths_to_page_modules() -> None:
    """After `_refresh_paths_from_env`, propagate the new
    USER_CONFIG_PATH / SYSTEM_CONFIG_PATH values into every page
    module that previously did `from ..config_io import USER_CONFIG_PATH`.
    Import-time `from X import Y` binds `Y` in the importing module's
    globals; updating `config_io.USER_CONFIG_PATH` doesn't retroactively
    update those bindings, so we walk each page module and rebind."""
    import sys
    from gui.control import config_io as _cfg
    for mod_name in _PAGE_MODULES:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        if hasattr(mod, "USER_CONFIG_PATH"):
            mod.USER_CONFIG_PATH = _cfg.USER_CONFIG_PATH
        if hasattr(mod, "SYSTEM_CONFIG_PATH"):
            mod.SYSTEM_CONFIG_PATH = _cfg.SYSTEM_CONFIG_PATH


@pytest.fixture
def hermetic_config_paths(tmp_path, monkeypatch):
    """Point config_io at a per-test tempdir so pages that call
    `set_user_override` don't touch ~/.config."""
    from gui.control import config_io as _config_io
    user_path = tmp_path / "user" / "controller.toml"
    system_path = tmp_path / "etc" / "controller.toml"
    monkeypatch.setenv("ICEBREAKER_USER_CONFIG_PATH", str(user_path))
    monkeypatch.setenv("ICEBREAKER_SYSTEM_CONFIG_PATH", str(system_path))
    monkeypatch.setenv("ICEBREAKER_DRY_RUN", "1")  # never invoke pkexec
    _config_io._refresh_paths_from_env()
    _propagate_paths_to_page_modules()
    yield user_path, system_path
    monkeypatch.delenv("ICEBREAKER_USER_CONFIG_PATH", raising=False)
    monkeypatch.delenv("ICEBREAKER_SYSTEM_CONFIG_PATH", raising=False)
    monkeypatch.delenv("ICEBREAKER_DRY_RUN", raising=False)
    _config_io._refresh_paths_from_env()
    _propagate_paths_to_page_modules()


@pytest.fixture
def _RowStubCls():
    """Export the row stub so tests can `isinstance` against it."""
    return _RowStub


@pytest.fixture
def _PageStubCls():
    return _PageStub
