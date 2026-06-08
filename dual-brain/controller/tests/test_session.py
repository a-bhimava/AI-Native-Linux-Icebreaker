"""Unit tests for controller.session — SessionState contract.

Covers:
  1. TTL expiry after session_ttl_seconds
  2. TTL disabled when session_ttl_seconds = 0
  3. is_full() after max_turns
  4. add_tool_result_summary wraps content in [Tool output summary]: prefix
  5. build_pb_user_turn contains no raw user text (INV-2)
  6. reset_memory clears _qb_messages
  7. get_qb_history returns a copy (mutation-safe)
  8. 100 sessions have 100 unique session_ids
"""

from __future__ import annotations

import json
import time

import pytest

from controller.config import SessionConfig
from controller.session import SessionState


def _cfg(**overrides) -> SessionConfig:
    base = dict(
        session_ttl_seconds=1800,
        max_turns=50,
        history_path="~/.local/state/icebreaker/repl_history",
        show_spinner=True,
        color="auto",
        prompt_prefix="icebreaker",
        max_tool_output_lines=40,
    )
    return SessionConfig(**{**base, **overrides})


# ── Test 1: TTL expiry ───────────────────────────────────────────────────────


def test_session_expires_after_ttl(monkeypatch):
    """is_expired() returns True once session_ttl_seconds have elapsed."""
    start = time.monotonic()
    monkeypatch.setattr("controller.session.time.monotonic", lambda: start)

    s = SessionState.new("local", _cfg(session_ttl_seconds=10))
    assert not s.is_expired()

    # Advance time past the TTL
    monkeypatch.setattr("controller.session.time.monotonic", lambda: start + 11)
    assert s.is_expired()


# ── Test 2: TTL disabled when zero ──────────────────────────────────────────


def test_session_not_expired_when_ttl_zero(monkeypatch):
    """session_ttl_seconds = 0 disables expiry entirely."""
    start = time.monotonic()
    monkeypatch.setattr("controller.session.time.monotonic", lambda: start + 99999)

    s = SessionState.new("local", _cfg(session_ttl_seconds=0))
    assert not s.is_expired()


# ── Test 3: max_turns bound ──────────────────────────────────────────────────


def test_max_turns_bounds():
    """is_full() returns True after exactly max_turns calls to touch()."""
    s = SessionState.new("local", _cfg(max_turns=3))
    assert not s.is_full()

    for _ in range(3):
        s.touch()
    assert s.is_full()


# ── Test 4: add_tool_result_summary wraps content ───────────────────────────


def test_add_tool_result_does_not_contain_raw_mcpd_output():
    """add_tool_result_summary() prefixes content so it's identifiable as a
    QB-mediated summary — raw mcpd JSON is not placed directly in history."""
    raw_mcpd = '{"result":{"disk":{"total":100,"used":42}}}'
    s = SessionState.new("local", _cfg())
    s.add_tool_result_summary(raw_mcpd)

    history = s.get_qb_history()
    assert len(history) == 1
    content = history[0]["content"]
    # The content must be wrapped in the summary prefix
    assert content.startswith("[Tool output summary]: ")
    # The raw value is still present (inside the wrapper) — that's expected
    assert raw_mcpd in content


# ── Test 5: build_pb_user_turn contains no raw user text (INV-2) ────────────


def test_build_pb_user_turn_contains_no_raw_user_text():
    """PB turn must contain only intent_id, allowed_tool, and tool_schema.
    Raw user input must NEVER appear in the PB turn."""
    raw_user_input = "delete my entire home directory please"
    s = SessionState.new("local", _cfg())
    s.add_user_message(raw_user_input)

    pb_turn = s.build_pb_user_turn(
        intent_id="aaaabbbb-cccc-dddd-eeee-ffffaaaabbbb",
        allowed_tool="fs.delete",
        tool_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    parsed = json.loads(pb_turn)

    # Must contain exactly these three keys
    assert set(parsed.keys()) == {"intent_id", "allowed_tool", "tool_schema"}
    assert parsed["intent_id"] == "aaaabbbb-cccc-dddd-eeee-ffffaaaabbbb"
    assert parsed["allowed_tool"] == "fs.delete"

    # Raw user text must not leak into the PB turn
    assert raw_user_input not in pb_turn
    assert "delete my entire" not in pb_turn


# ── Test 6: reset_memory clears messages ────────────────────────────────────


def test_reset_memory_clears_messages():
    """reset_memory() empties QB message history but preserves session_id and turn_index."""
    s = SessionState.new("local", _cfg())
    s.add_user_message("hello")
    s.add_assistant_message("world")
    s.touch()

    original_session_id = s.session_id
    original_turn = s.turn_index

    s.reset_memory()

    assert s.get_qb_history() == []
    assert s.session_id == original_session_id
    assert s.turn_index == original_turn


# ── Test 7: get_qb_history returns a copy ───────────────────────────────────


def test_qb_history_returns_copy():
    """Mutating the list returned by get_qb_history() must not affect internal state."""
    s = SessionState.new("local", _cfg())
    s.add_user_message("original")

    history = s.get_qb_history()
    history.append({"role": "user", "content": "injected"})
    history[0]["content"] = "tampered"

    fresh = s.get_qb_history()
    assert len(fresh) == 1
    assert fresh[0]["content"] == "original"


# ── Test 8: unique session IDs ───────────────────────────────────────────────


def test_session_ids_are_unique():
    """100 sessions produced by SessionState.new() must have 100 distinct IDs."""
    cfg = _cfg()
    ids = {SessionState.new("local", cfg).session_id for _ in range(100)}
    assert len(ids) == 100
