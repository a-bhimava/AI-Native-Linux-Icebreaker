"""v6.10 Task #152 / F-61 — HitlModal + AI Terminal wiring tests.

Locks in:
  1. HitlModal parses the daemon-forwarded prompt payload without
     raising on empty / missing / oversized fields (defensive
     input handling — the daemon sanitises but the modal must not
     crash on legacy shapes).
  2. Lockout window: APPROVE dispatches are ignored while
     ``time.monotonic() < locked_until``; DENY works immediately.
  3. Decision actions dismiss with the correct decision strings
     matching ``controller.hitl.Decision.value``.
  4. AiTerminalApp wires ``hitl_prompt`` + ``hitl_lockout`` events
     to the DaemonClient (so the daemon-forwarded notifications are
     not silently dropped — the F-61 root cause).
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from terminal.hitl_modal import HitlModal


# ── Payload parsing ──────────────────────────────────────────────────


def test_hitl_modal_constructs_with_full_payload():
    m = HitlModal({
        "action": "fs.delete",
        "target": "/etc/passwd",
        "tier": 3,
        "risk_level": "critical",
        "reversible": False,
        "backend": "gemini",
        "reason": "user_requested",
        "blocked_pattern": None,
        "cow_summary": "Removed: /etc/passwd (backup at /var/backups/…)",
    })
    assert m._params["action"] == "fs.delete"
    assert m._params["target"] == "/etc/passwd"
    assert m._params["tier"] == 3


def test_hitl_modal_constructs_with_empty_payload():
    """Defensive — daemon may send a stripped-down payload during
    schema evolution (e.g. blocked_pattern removed). Modal must not
    raise; missing fields render as empty strings."""
    m = HitlModal({})
    assert m._params == {}


def test_hitl_modal_default_lockout_is_three_seconds():
    """Matches INV-6 default. Daemon side sets this via the
    HitlConfig.lockout_seconds field."""
    m = HitlModal({"action": "x", "tier": 2})
    remaining = m._locked_until - time.monotonic()
    assert 2.9 < remaining <= 3.0


def test_hitl_modal_zero_lockout_flag_starts_unlocked():
    """Config override lockout_seconds=0 → APPROVE ready immediately.
    Used in tests + local admin overrides."""
    m = HitlModal({"action": "x", "tier": 2}, lockout_seconds=0)
    remaining = m._locked_until - time.monotonic()
    assert remaining <= 0.001


# ── Actions ──────────────────────────────────────────────────────────


def test_deny_action_dismisses_with_denied_string():
    """Matches Decision.DENIED.value; daemon parses this back."""
    m = HitlModal({"action": "x", "tier": 3}, lockout_seconds=0)
    m.dismiss = MagicMock()
    m.action_deny()
    m.dismiss.assert_called_once_with("denied")


def test_approve_dismisses_after_lockout_expires():
    m = HitlModal({"action": "x", "tier": 2}, lockout_seconds=0)
    m.dismiss = MagicMock()
    m.action_approve_if_unlocked()
    m.dismiss.assert_called_once_with("approved")


def test_approve_is_ignored_during_lockout():
    """INV-6: pressing approve during the lockout window is a no-op.
    Daemon still sees no decision → keeps waiting."""
    m = HitlModal({"action": "x", "tier": 3}, lockout_seconds=5)
    m.dismiss = MagicMock()
    m.action_approve_if_unlocked()
    m.dismiss.assert_not_called()


def test_modify_action_dismisses_with_modify():
    m = HitlModal({"action": "x", "tier": 2}, lockout_seconds=0)
    m.dismiss = MagicMock()
    m.action_modify()
    m.dismiss.assert_called_once_with("modify")


def test_explain_action_dismisses_with_explain():
    m = HitlModal({"action": "x", "tier": 2}, lockout_seconds=0)
    m.dismiss = MagicMock()
    m.action_explain()
    m.dismiss.assert_called_once_with("explain")


def test_trust_action_dismisses_with_trust():
    m = HitlModal({"action": "x", "tier": 2}, lockout_seconds=0)
    m.dismiss = MagicMock()
    m.action_trust()
    m.dismiss.assert_called_once_with("trust")


# ── Countdown label ──────────────────────────────────────────────────


def test_countdown_text_reflects_time_remaining():
    m = HitlModal({"action": "x", "tier": 3}, lockout_seconds=3)
    txt = m._countdown_text()
    assert "Approve unlocks in" in txt


def test_countdown_text_shows_ready_when_unlocked():
    m = HitlModal({"action": "x", "tier": 2}, lockout_seconds=0)
    txt = m._countdown_text()
    assert "ready" in txt.lower()


# ── App wiring (F-61 root cause regression guard) ────────────────────


def test_app_wires_hitl_events_to_daemon_client():
    """The F-61 bug was that AiTerminalApp did NOT subscribe to
    ``hitl_prompt`` or ``hitl_lockout`` on the daemon client — the
    daemon forwarded the prompt, the client received it, fired the
    event, and nothing listened. The user saw ``awaiting human
    decision`` in the CoT panel but no overlay, then a 30 s timeout
    and ``Operation denied``.

    This test constructs the app with a mock daemon client and calls
    the internal ``_wire_daemon_callbacks`` to verify both events are
    subscribed. If the ``client.on("hitl_prompt", ...)`` line ever
    gets dropped in a refactor, this test fails with a clear name."""
    from terminal.app import AiTerminalApp

    client = MagicMock()
    app = AiTerminalApp(daemon_client=client)
    app._wire_daemon_callbacks()

    subscribed_events = {call.args[0] for call in client.on.call_args_list}
    assert "hitl_prompt" in subscribed_events, (
        "F-61 regression: AiTerminalApp must subscribe to 'hitl_prompt' "
        "so daemon-forwarded HITL prompts render the modal. Without this, "
        "every Tier ≥ 1 intent silently times out."
    )
    assert "hitl_lockout" in subscribed_events, (
        "AiTerminalApp must subscribe to 'hitl_lockout' so the INV-6 "
        "lockout window can be surfaced (currently a no-op logger, "
        "but the wire needs to be there for future UI feedback)."
    )
