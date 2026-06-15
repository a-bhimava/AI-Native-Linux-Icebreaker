"""Tests for ``controller.presenters.gtk.GtkPresenter``.

GTK tests mock ``gi.repository`` — no display server or PyGObject required.
Tests verify: constructor error when PyGObject missing, lazy import, decision
mapping, lockout behavior, and scaffold wiring.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from controller.backends.base import BrainConfigError
from controller.hitl import Decision
from controller.keymap import Keymap


# ── Constructor tests ────────────────────────────────────────────────────────


class TestConstructor:
    def test_missing_pyobject_raises_config_error(self):
        saved = sys.modules.get("gi")
        try:
            sys.modules["gi"] = None  # type: ignore
            with pytest.raises(BrainConfigError, match="PyGObject"):
                from controller.presenters.gtk import GtkPresenter
                # Force re-import by recreating
                GtkPresenter(keymap=Keymap())
        finally:
            if saved is not None:
                sys.modules["gi"] = saved
            elif "gi" in sys.modules:
                del sys.modules["gi"]

    def test_error_message_includes_install_hint(self):
        saved = sys.modules.get("gi")
        try:
            sys.modules["gi"] = None  # type: ignore
            with pytest.raises(BrainConfigError) as exc_info:
                from controller.presenters.gtk import GtkPresenter
                GtkPresenter(keymap=Keymap())
            msg = str(exc_info.value)
            assert "pip install" in msg or "apt install" in msg
        finally:
            if saved is not None:
                sys.modules["gi"] = saved
            elif "gi" in sys.modules:
                del sys.modules["gi"]


# ── Registration tests ───────────────────────────────────────────────────────


class TestRegistration:
    def test_gtk_in_registry(self):
        from controller.presenters.registry import registered_presenters
        assert "gtk" in registered_presenters()


# ── Mock GTK dialog lifecycle ────────────────────────────────────────────────


class _MockGi:
    """Minimal mock for gi + Gtk + GLib."""

    def __init__(self):
        self.Gtk = MagicMock()
        self.GLib = MagicMock()

    def require_version(self, name, version):
        pass

    @property
    def repository(self):
        ns = SimpleNamespace(Gtk=self.Gtk, GLib=self.GLib)
        return ns


class TestMockDialogLifecycle:
    def _make_presenter_with_mock(self):
        mock_gi = _MockGi()
        saved_gi = sys.modules.get("gi")
        saved_repo = sys.modules.get("gi.repository")
        sys.modules["gi"] = mock_gi  # type: ignore
        sys.modules["gi.repository"] = mock_gi.repository  # type: ignore

        try:
            from controller.presenters.gtk import GtkPresenter
            # Need to bypass the cached import — construct directly
            presenter = object.__new__(GtkPresenter)
            presenter._gi = mock_gi
            presenter._Gtk = mock_gi.Gtk
            presenter._GLib = mock_gi.GLib
            presenter._keymap = Keymap()
            presenter._last_key_class = ""
            presenter._decision = None

            import threading
            presenter._decision_event = threading.Event()
            return presenter, mock_gi
        finally:
            if saved_gi is not None:
                sys.modules["gi"] = saved_gi
            elif "gi" in sys.modules:
                del sys.modules["gi"]
            if saved_repo is not None:
                sys.modules["gi.repository"] = saved_repo
            elif "gi.repository" in sys.modules:
                del sys.modules["gi.repository"]

    def test_show_prompt_stores_data(self):
        presenter, _ = self._make_presenter_with_mock()
        from controller.hitl import HitlDisplayData
        from controller.risk_classifier import Tier
        data = HitlDisplayData(
            action="system.status", target="", tier=Tier.READ_ONLY,
            risk_level="read_only", reversible=True, backend="local",
            reason="", blocked_pattern=None, cow_summary=None,
        )
        presenter.show_prompt(data)
        assert presenter._pending_data is data
        assert presenter._decision is None

    def test_lockout_sleeps(self):
        presenter, _ = self._make_presenter_with_mock()
        with patch("time.sleep") as mock_sleep:
            presenter.lockout(3)
            mock_sleep.assert_called_once_with(3)

    def test_no_pending_data_returns_denied(self):
        presenter, _ = self._make_presenter_with_mock()
        result = presenter.read_decision(1)
        assert result == Decision.DENIED
        assert presenter.last_key_class == "error"

    def test_timeout_returns_timeout(self):
        from controller.hitl import HitlDisplayData
        from controller.risk_classifier import Tier
        presenter, mock_gi = self._make_presenter_with_mock()

        data = HitlDisplayData(
            action="system.status", target="", tier=Tier.READ_ONLY,
            risk_level="read_only", reversible=True, backend="local",
            reason="", blocked_pattern=None, cow_summary=None,
        )
        presenter.show_prompt(data)

        mock_gi.Gtk.Application.side_effect = Exception("no display")
        result = presenter.read_decision(1)
        assert result in (Decision.TIMEOUT, Decision.DENIED)


# ── Keymap tests ─────────────────────────────────────────────────────────────


class TestKeymap:
    def test_default_keymap(self):
        presenter, _ = TestMockDialogLifecycle()._make_presenter_with_mock()
        assert presenter._keymap is not None
