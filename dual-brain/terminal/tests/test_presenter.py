"""Tests for AiTerminalPresenter (PR #24).

Covers:
  1.  Presenter is registered as "ai_terminal" in the registry
  2.  pre_check returns None (always allow — TUI is interactive)
  3.  show_prompt calls the show callback
  4.  lockout calls the lockout callback
  5.  read_decision blocks until submit_decision is called
  6.  read_decision times out and returns TIMEOUT
  7.  submit_decision unblocks read_decision with correct value
"""

from __future__ import annotations

import threading
import time

import pytest

from controller.hitl import Decision, HitlDisplayData
from controller.presenters.registry import make_presenter, registered_presenters
from controller.risk_classifier import Tier
from terminal.presenter import AiTerminalPresenter


@pytest.fixture
def presenter():
    return AiTerminalPresenter()


def _make_display_data(**overrides):
    defaults = dict(
        action="fs.read",
        target="/etc/hostname",
        tier=Tier(0),
        risk_level="low",
        reversible=True,
        backend="local",
        reason="read a file",
        blocked_pattern=None,
        cow_summary=None,
    )
    defaults.update(overrides)
    return HitlDisplayData(**defaults)


class TestRegistration:
    def test_registered_in_registry(self):
        assert "ai_terminal" in registered_presenters()

    def test_make_presenter_returns_instance(self):
        p = make_presenter("ai_terminal")
        assert isinstance(p, AiTerminalPresenter)


class TestPreCheck:
    def test_pre_check_returns_none(self, presenter):
        assert presenter.pre_check() is None


class TestCallbacks:
    def test_show_prompt_calls_callback(self, presenter):
        received = []
        presenter.set_callbacks(
            show_cb=lambda d: received.append(d),
            lockout_cb=lambda s: None,
        )
        data = _make_display_data()
        presenter.show_prompt(data)
        assert len(received) == 1
        assert received[0] is data

    def test_lockout_calls_callback(self, presenter):
        received = []
        presenter.set_callbacks(
            show_cb=lambda d: None,
            lockout_cb=lambda s: received.append(s),
        )
        presenter.lockout(3)
        assert received == [3]


class TestDecision:
    def test_submit_unblocks_read(self, presenter):
        result = [None]

        def reader():
            result[0] = presenter.read_decision(timeout_seconds=5)

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)
        presenter.submit_decision(Decision.APPROVED)
        t.join(timeout=2)
        assert result[0] == Decision.APPROVED

    def test_timeout_returns_timeout(self, presenter):
        decision = presenter.read_decision(timeout_seconds=0.1)
        assert decision == Decision.TIMEOUT
        assert presenter.last_key_class == "timeout"

    def test_deny_decision(self, presenter):
        result = [None]

        def reader():
            result[0] = presenter.read_decision(timeout_seconds=5)

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)
        presenter.submit_decision(Decision.DENIED)
        t.join(timeout=2)
        assert result[0] == Decision.DENIED
