"""Unit tests for controller.hitl — M2.9 HITL terminal gate.

Covers exit gate G8: 3-second lockout verified by call-order test.
All tests complete in < 1 s (sleep and select are mocked out).
"""

from __future__ import annotations

import sys
from io import StringIO

import pytest

from controller import hitl as hitl_module
from controller.hitl import (
    Decision,
    HitlDisplayData,
    HitlPresenter,
    HitlPrompt,
    TerminalPresenter,
)
from controller.risk_classifier import ClassificationResult, Tier


# ── Helpers ──────────────────────────────────────────────────────────────────


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
    """Fake stdin that yields pre-canned lines and reports isatty() = True."""

    def __init__(self, *lines: str) -> None:
        self._lines = iter(lines)

    def readline(self) -> str:
        return next(self._lines, "")

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return sys.stdin.fileno()


# ── Test 1: non-TTY path ─────────────────────────────────────────────────────


def test_non_tty_returns_non_tty(monkeypatch):
    sleep_called = []
    monkeypatch.setattr("controller.hitl.time.sleep", lambda s: sleep_called.append(s))

    # Patch sys.stdin.isatty at the module level so TerminalPresenter.pre_check sees it
    monkeypatch.setattr("sys.stdin", StringIO(""))  # StringIO.isatty() returns False

    result = HitlPrompt(_intent(), _cls()).ask()

    assert result == Decision.NON_TTY
    assert sleep_called == [], "sleep must not be called on non-TTY path"


# ── Test 2: G8 — lockout sleep happens before select.select ─────────────────


def test_lockout_sleep_before_select(monkeypatch):
    call_order: list[str] = []
    monkeypatch.setattr(
        "controller.hitl.time.sleep", lambda s: call_order.append("sleep")
    )
    monkeypatch.setattr(
        "controller.hitl.select.select",
        lambda *a, **k: (call_order.append("select"), ([sys.stdin], [], []))[1],
    )
    fake = FakeStdin("A\n")
    monkeypatch.setattr("sys.stdin", fake)

    HitlPrompt(_intent(), _cls()).ask()

    assert "sleep" in call_order, "sleep must be called (lockout)"
    assert "select" in call_order, "select must be called (input loop)"
    assert call_order.index("sleep") < call_order.index("select"), (
        "G8: lockout sleep must precede any stdin select"
    )


# ── Test 3: [A] key → APPROVED ───────────────────────────────────────────────


def test_approve_key_returns_approved(monkeypatch):
    monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "controller.hitl.select.select",
        lambda *a, **k: ([sys.stdin], [], []),
    )
    fake = FakeStdin("A\n")
    monkeypatch.setattr("sys.stdin", fake)

    result = HitlPrompt(_intent(), _cls()).ask()
    assert result == Decision.APPROVED


# ── Test 4: [D] key → DENIED ─────────────────────────────────────────────────


def test_deny_key_returns_denied(monkeypatch):
    monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "controller.hitl.select.select",
        lambda *a, **k: ([sys.stdin], [], []),
    )
    fake = FakeStdin("D\n")
    monkeypatch.setattr("sys.stdin", fake)

    result = HitlPrompt(_intent(), _cls()).ask()
    assert result == Decision.DENIED


# ── Test 5: timeout → TIMEOUT ────────────────────────────────────────────────


def test_timeout_returns_timeout(monkeypatch):
    monkeypatch.setattr(hitl_module.HitlPrompt, "LOCKOUT_SECONDS", 0)
    monkeypatch.setattr(hitl_module.HitlPrompt, "TIMEOUT_SECONDS", 0)
    monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "controller.hitl.select.select", lambda *a, **k: ([], [], [])
    )
    fake = FakeStdin()
    monkeypatch.setattr("sys.stdin", fake)

    result = HitlPrompt(_intent(), _cls()).ask()
    assert result == Decision.TIMEOUT


# ── Test 6: [E]/[M] stubs re-prompt, then [D] → DENIED ──────────────────────


def test_stub_keys_reprompt_then_deny(monkeypatch, capsys):
    monkeypatch.setattr("controller.hitl.time.sleep", lambda s: None)
    call_count = 0

    def _fake_select(*a, **k):
        return ([sys.stdin], [], [])

    monkeypatch.setattr("controller.hitl.select.select", _fake_select)
    fake = FakeStdin("E\n", "M\n", "D\n")
    monkeypatch.setattr("sys.stdin", fake)

    result = HitlPrompt(_intent(), _cls()).ask()

    assert result == Decision.DENIED
    captured = capsys.readouterr()
    assert "Explain more" in captured.out
    assert "Modify command" in captured.out
    assert "Phase 5" in captured.out


# ── Test 7: _render contains key fields ──────────────────────────────────────


def test_render_contains_key_fields():
    prompt = HitlPrompt(
        _intent(action="service.stop", target="/etc/nginx.conf"),
        _cls(tier=Tier.HIGH),
    )
    rendered = prompt._render()
    assert "service.stop" in rendered
    assert "/etc/nginx.conf" in rendered
    # Risk label (color stripped in non-TTY context since stdout is not a TTY)
    assert "CRITICAL" in rendered or "HIGH" in rendered


# ── Test 8: _render includes cow_summary ─────────────────────────────────────


def test_render_cow_summary_shown():
    summary = "would delete 3 files"
    prompt = HitlPrompt(_intent(), _cls(), cow_summary=summary)
    rendered = prompt._render()
    assert summary in rendered


# ── Test 9: lockout_seconds constructor kwarg controls sleep count ───────────


def test_lockout_seconds_override(monkeypatch):
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "controller.hitl.time.sleep", lambda s: sleep_calls.append(s)
    )
    monkeypatch.setattr(
        "controller.hitl.select.select",
        lambda *a, **k: ([sys.stdin], [], []),
    )
    fake = FakeStdin("D\n")
    monkeypatch.setattr("sys.stdin", fake)

    HitlPrompt(_intent(), _cls(), lockout_seconds=5).ask()

    assert len(sleep_calls) == 5, (
        f"expected 5 sleep calls for lockout_seconds=5, got {len(sleep_calls)}"
    )


# ── Test 10: custom presenter is invoked ────────────────────────────────────


def test_custom_presenter_invoked():
    class SpyPresenter(HitlPresenter):
        def __init__(self):
            self.calls: list[str] = []

        def show_prompt(self, data: HitlDisplayData) -> None:
            self.calls.append("show")

        def lockout(self, seconds: int) -> None:
            self.calls.append("lockout")

        def read_decision(self, timeout_seconds: int) -> Decision:
            self.calls.append("read")
            return Decision.APPROVED

    spy = SpyPresenter()
    result = HitlPrompt(_intent(), _cls(), presenter=spy).ask()

    assert result == Decision.APPROVED
    assert spy.calls == ["show", "lockout", "read"]
