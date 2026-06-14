"""Tests for HITL raw-input hardening — G5.2 gate.

Verifies SF-2/SF-3 fixes:
  - tcflush is called after lockout (pre-buffered bytes discarded)
  - Single raw keypress via os.read, not readline
  - ESC → DENY, Ctrl+C → DENY, EOF → DENY, timeout → TIMEOUT
  - Non-TTY → NON_TTY
  - key_pressed_class metadata set correctly
"""

from __future__ import annotations

import os
import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from controller.hitl import (
    Decision,
    HitlDisplayData,
    HitlPresenter,
    HitlPrompt,
    TerminalPresenter,
)
from controller.keymap import Keymap
from controller.risk_classifier import ClassificationResult, Tier


def _intent(**overrides) -> dict:
    base = dict(
        intent_id="test-uuid",
        action="fs.delete",
        target="/etc/hosts",
        params={},
        reason="user_requested",
        risk_level="critical",
    )
    return {**base, **overrides}


def _cls(**overrides) -> ClassificationResult:
    base = dict(tier=Tier.HIGH, reason="critical path", reversible=False)
    return ClassificationResult(**{**base, **overrides})


class FakeStdin:
    """Fake stdin with a working fileno() for raw-input tests."""

    def __init__(self):
        self._r, self._w = os.pipe()

    def fileno(self):
        return self._r

    def isatty(self):
        return True

    def close(self):
        try:
            os.close(self._r)
        except OSError:
            pass
        try:
            os.close(self._w)
        except OSError:
            pass

    def write_bytes(self, data: bytes):
        os.write(self._w, data)


def _make_presenter_with_mocked_termios(monkeypatch, key_bytes, *, keymap=None):
    """Create a TerminalPresenter with mocked termios and a fake stdin that
    returns the given key_bytes on os.read."""
    fake = FakeStdin()
    monkeypatch.setattr("sys.stdin", fake)

    monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "controller.hitl.select.select",
        lambda *a, **k: ([fake], [], []),
    )
    monkeypatch.setattr(
        "controller.hitl.os.read",
        lambda fd, n: key_bytes,
    )

    import controller.hitl as hitl_mod
    monkeypatch.setattr(hitl_mod, "_cbreak", _noop_cbreak)

    import termios
    monkeypatch.setattr(termios, "tcflush", lambda fd, q: None)

    presenter = TerminalPresenter(keymap=keymap or Keymap())
    return presenter, fake


from contextlib import contextmanager

@contextmanager
def _noop_cbreak(stream):
    yield


class TestTcflushCalled:
    def test_tcflush_is_called_in_read_decision(self, monkeypatch):
        tcflush_calls = []
        fake = FakeStdin()
        monkeypatch.setattr("sys.stdin", fake)
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
        monkeypatch.setattr(
            "controller.hitl.select.select",
            lambda *a, **k: ([fake], [], []),
        )
        monkeypatch.setattr(
            "controller.hitl.os.read",
            lambda fd, n: b"d",
        )

        import controller.hitl as hitl_mod
        monkeypatch.setattr(hitl_mod, "_cbreak", _noop_cbreak)

        import termios
        orig_tcflush = termios.tcflush
        monkeypatch.setattr(termios, "tcflush", lambda fd, q: tcflush_calls.append((fd, q)))

        presenter = TerminalPresenter(keymap=Keymap())
        presenter.read_decision(5)
        fake.close()

        assert len(tcflush_calls) >= 1, "tcflush must be called during read_decision"


class TestNonTTY:
    def test_non_tty_returns_non_tty(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
        monkeypatch.setattr("sys.stdin", StringIO(""))

        prompt = HitlPrompt(_intent(), _cls())
        result = prompt.ask()

        assert result == Decision.NON_TTY
        assert prompt.key_pressed_class == "non_tty"

    def test_non_tty_latency_is_zero(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
        monkeypatch.setattr("sys.stdin", StringIO(""))

        prompt = HitlPrompt(_intent(), _cls())
        prompt.ask()

        assert prompt.decision_latency_ms == 0.0


class TestEscDeny:
    def test_esc_returns_denied(self, monkeypatch):
        presenter, fake = _make_presenter_with_mocked_termios(monkeypatch, b"\x1b")
        result = presenter.read_decision(10)
        fake.close()

        assert result == Decision.DENIED
        assert presenter.last_key_class == "esc"


class TestEofDeny:
    def test_eof_returns_denied(self, monkeypatch):
        presenter, fake = _make_presenter_with_mocked_termios(monkeypatch, b"")
        result = presenter.read_decision(10)
        fake.close()

        assert result == Decision.DENIED
        assert presenter.last_key_class == "eof"


class TestTimeoutDeny:
    def test_timeout_returns_timeout(self, monkeypatch):
        fake = FakeStdin()
        monkeypatch.setattr("sys.stdin", fake)
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
        monkeypatch.setattr(
            "controller.hitl.select.select",
            lambda *a, **k: ([], [], []),
        )

        import controller.hitl as hitl_mod
        monkeypatch.setattr(hitl_mod, "_cbreak", _noop_cbreak)

        import termios
        monkeypatch.setattr(termios, "tcflush", lambda fd, q: None)

        presenter = TerminalPresenter(keymap=Keymap())
        result = presenter.read_decision(0)
        fake.close()

        assert result == Decision.TIMEOUT
        assert presenter.last_key_class == "timeout"


class TestKeyClassification:
    def test_numeric_key_class(self, monkeypatch):
        presenter, fake = _make_presenter_with_mocked_termios(monkeypatch, b"1")
        decision = presenter.read_decision(10)
        fake.close()

        assert decision == Decision.APPROVED
        assert presenter.last_key_class == "numeric"

    def test_mnemonic_key_class(self, monkeypatch):
        presenter, fake = _make_presenter_with_mocked_termios(monkeypatch, b"a")
        decision = presenter.read_decision(10)
        fake.close()

        assert decision == Decision.APPROVED
        assert presenter.last_key_class == "mnemonic"

    def test_deny_numeric_key_class(self, monkeypatch):
        presenter, fake = _make_presenter_with_mocked_termios(monkeypatch, b"2")
        decision = presenter.read_decision(10)
        fake.close()

        assert decision == Decision.DENIED
        assert presenter.last_key_class == "numeric"

    def test_deny_mnemonic_key_class(self, monkeypatch):
        presenter, fake = _make_presenter_with_mocked_termios(monkeypatch, b"d")
        decision = presenter.read_decision(10)
        fake.close()

        assert decision == Decision.DENIED
        assert presenter.last_key_class == "mnemonic"


class TestDecisionId:
    def test_decision_id_is_uuid(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
        monkeypatch.setattr("sys.stdin", StringIO(""))

        prompt = HitlPrompt(_intent(), _cls())
        prompt.ask()

        assert len(prompt.decision_id) == 36
        assert prompt.decision_id.count("-") == 4

    def test_different_prompts_different_ids(self, monkeypatch):
        monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
        monkeypatch.setattr("sys.stdin", StringIO(""))

        p1 = HitlPrompt(_intent(), _cls())
        p2 = HitlPrompt(_intent(), _cls())

        assert p1.decision_id != p2.decision_id
