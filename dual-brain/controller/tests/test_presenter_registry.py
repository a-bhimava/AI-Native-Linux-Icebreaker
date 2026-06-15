"""Tests for ``controller.presenters.registry`` — registration, factory, errors."""

from __future__ import annotations

import pytest

from controller.hitl import HitlPresenter, Decision, HitlDisplayData
from controller.presenters.registry import (
    _reset_registry_for_tests,
    make_presenter,
    register_presenter,
    registered_presenters,
)
from controller.keymap import Keymap


# ── Fixtures ─────────────────────────────────────────────────────────────────

class _DummyPresenter(HitlPresenter):
    def __init__(self, *, keymap=None):
        self._keymap = keymap

    def show_prompt(self, data):
        pass

    def lockout(self, seconds):
        pass

    def read_decision(self, timeout_seconds):
        return Decision.DENIED


class _AnotherPresenter(HitlPresenter):
    def __init__(self, *, keymap=None):
        pass

    def show_prompt(self, data):
        pass

    def lockout(self, seconds):
        pass

    def read_decision(self, timeout_seconds):
        return Decision.DENIED


@pytest.fixture(autouse=True)
def _clean_registry():
    """Save + restore registry around each test."""
    from controller.presenters.registry import _REGISTRY
    saved = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(saved)


# ── Registration ─────────────────────────────────────────────────────────────


class TestRegistration:
    def test_register_and_retrieve(self):
        register_presenter("test_dummy")(_DummyPresenter)
        p = make_presenter("test_dummy")
        assert isinstance(p, _DummyPresenter)

    def test_duplicate_raises(self):
        register_presenter("test_dup")(_DummyPresenter)
        with pytest.raises(ValueError, match="already registered"):
            register_presenter("test_dup")(_AnotherPresenter)

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            register_presenter("")(_DummyPresenter)

    def test_non_presenter_raises(self):
        with pytest.raises(TypeError, match="HitlPresenter"):
            register_presenter("bad")(str)  # type: ignore

    def test_unknown_name_raises(self):
        with pytest.raises(ValueError, match="unknown presenter"):
            make_presenter("nonexistent_xyz")


# ── Factory ──────────────────────────────────────────────────────────────────


class TestFactory:
    def test_terminal_registered(self):
        assert "terminal" in registered_presenters()

    def test_screen_reader_registered(self):
        assert "screen_reader" in registered_presenters()

    def test_gtk_registered(self):
        assert "gtk" in registered_presenters()

    def test_make_terminal(self):
        from controller.presenters.terminal import TerminalPresenter
        p = make_presenter("terminal")
        assert isinstance(p, TerminalPresenter)

    def test_make_screen_reader(self):
        from controller.presenters.screen_reader import ScreenReaderPresenter
        p = make_presenter("screen_reader")
        assert isinstance(p, ScreenReaderPresenter)

    def test_make_with_keymap(self):
        km = Keymap()
        p = make_presenter("terminal", keymap=km)
        assert p._keymap is km

    def test_registered_presenters_sorted(self):
        names = registered_presenters()
        assert names == tuple(sorted(names))


# ── Reset helper ─────────────────────────────────────────────────────────────


class TestReset:
    def test_reset_clears(self):
        register_presenter("test_reset_x")(_DummyPresenter)
        assert "test_reset_x" in registered_presenters()
        _reset_registry_for_tests()
        assert "test_reset_x" not in registered_presenters()
