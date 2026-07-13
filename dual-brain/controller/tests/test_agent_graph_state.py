"""v6.8 Task #146 — pure state schema tests (no langgraph dependency).

These run without langgraph installed. They lock in:
- last_write_wins reducer semantics
- make_initial_state() sets every field to a well-known unset value
- No mutable default corruption between two make_initial_state() calls
- GraphState round-trips through TypedDict copy
"""

from __future__ import annotations

from controller.agent_graph_state import (
    GraphState,
    last_write_wins,
    make_initial_state,
)


# ── last_write_wins reducer ───────────────────────────────────────────

def test_last_write_wins_overwrites_string():
    assert last_write_wins("old", "new") == "new"


def test_last_write_wins_overwrites_int():
    assert last_write_wins(3, 5) == 5


def test_last_write_wins_overwrites_none():
    assert last_write_wins("something", None) is None


def test_last_write_wins_overwrites_with_falsy():
    """Falsy incoming should still overwrite — the reducer is 'last
    write' not 'last truthy write'."""
    assert last_write_wins(True, False) is False
    assert last_write_wins(5, 0) == 0
    assert last_write_wins("x", "") == ""


# ── make_initial_state factory ────────────────────────────────────────

def test_make_initial_state_sets_required_fields():
    s = make_initial_state(session_id="s1", turn_id="t1", query="hello")
    assert s["session_id"] == "s1"
    assert s["turn_id"] == "t1"
    assert s["query"] == "hello"


def test_make_initial_state_sentinel_tier_is_negative():
    """tier=-1 is the 'unclassified' sentinel. RiskClassifier sets to
    0/1/2/3. Downstream nodes can assert tier >= 0 as a health check."""
    s = make_initial_state(session_id="s", turn_id="t", query="q")
    assert s["tier"] == -1


def test_make_initial_state_content_refs_empty():
    """All hash/id fields start empty. Nodes fill them as work
    progresses."""
    s = make_initial_state(session_id="s", turn_id="t", query="q")
    assert s["intent_id"] == ""
    assert s["tool_call_hash"] == ""
    assert s["mcpd_result_hash"] == ""


def test_make_initial_state_hitl_defaults():
    s = make_initial_state(session_id="s", turn_id="t", query="q")
    assert s["hitl_required"] is False
    assert s["hitl_decision"] is None


def test_make_initial_state_error_none():
    s = make_initial_state(session_id="s", turn_id="t", query="q")
    assert s["error_kind"] is None
    assert s["error_reason"] is None


def test_make_initial_state_not_completed():
    s = make_initial_state(session_id="s", turn_id="t", query="q")
    assert s["completed"] is False


def test_make_initial_state_streaming_seq_zero():
    s = make_initial_state(session_id="s", turn_id="t", query="q")
    assert s["last_event_seq"] == 0


# ── Isolation: two separate calls do not share mutable state ─────────

def test_two_make_initial_state_calls_are_independent():
    """Regression lock against the classic 'field: list = []' pitfall.
    Even though GraphState has no list fields today, this test proves
    the factory does not accidentally share any dict/list references."""
    a = make_initial_state(session_id="a", turn_id="ta", query="qa")
    b = make_initial_state(session_id="b", turn_id="tb", query="qb")
    assert a is not b
    a["query"] = "MUTATED"
    assert b["query"] == "qb"  # b unaffected


# ── TypedDict round-trip ──────────────────────────────────────────────

def test_graph_state_dict_copy_preserves_all_fields():
    """A LangGraph checkpointer serializes state via dict.copy semantics.
    Verify that a round-trip does not drop any field."""
    original = make_initial_state(session_id="s1", turn_id="t1", query="hi")
    original["tier"] = 2
    original["intent_id"] = "uuid-123"
    original["hitl_required"] = True

    copy = dict(original)
    assert copy == original
    assert copy["tier"] == 2
    assert copy["intent_id"] == "uuid-123"
    assert copy["hitl_required"] is True


# ── Partial-update semantics ──────────────────────────────────────────

def test_partial_update_pattern():
    """A node returns a partial dict; LangGraph's reducer merges it
    into the current state. Verify our reducer semantics do the right
    thing when applied by hand (simulates what LangGraph does)."""
    current = make_initial_state(session_id="s", turn_id="t", query="q")

    # A node returns just the fields it changed.
    planner_update = {"intent_id": "uuid-xyz", "intent_valid": True, "tier": 1}

    merged = dict(current)
    for k, v in planner_update.items():
        merged[k] = last_write_wins(merged.get(k), v)

    assert merged["intent_id"] == "uuid-xyz"
    assert merged["intent_valid"] is True
    assert merged["tier"] == 1
    # Other fields untouched
    assert merged["query"] == "q"
    assert merged["hitl_required"] is False
