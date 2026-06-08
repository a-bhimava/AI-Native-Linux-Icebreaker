"""Adversarial test suite for the Controller — M2.13.

Exercises five attack classes:
  1. test_classic_injections_schema_rejected    — 30 injection payloads all fail validate()
  2. test_tool_output_never_reaches_pb          — 20 reflection payloads: PB turn stays clean
  3. test_multi_turn_session_isolation          — 15 poisoning sequences: PB context isolated
  4. test_risk_level_cannot_be_downgraded       — 10 HITL bypass attempts: always Tier HIGH
  5. test_intent_replay_blocked                 — expired intent_id → store returns None
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from controller.config import SessionConfig
from controller.intent_schema import IntentValidationError, validate
from controller.intent_store import TTL_SECONDS, IntentStore
from controller.risk_classifier import Tier, classify
from controller.session import SessionState


_CORPUS_DIR = Path(__file__).parent / "corpus"


def _load(filename: str) -> list:
    return json.loads((_CORPUS_DIR / filename).read_text(encoding="utf-8"))


def _session() -> SessionState:
    return SessionState.new("local", SessionConfig())


# ── 1. Schema rejection ───────────────────────────────────────────────────────

def test_classic_injections_schema_rejected():
    """All 30 classic injection payloads must raise IntentValidationError.

    Covers: shell metacharacters in target/params, invalid action names,
    forbidden reason/risk_level values, extra fields, and structural violations.
    """
    payloads = _load("classic_injections.json")
    assert len(payloads) == 30, "corpus should have exactly 30 entries"

    failures = []
    for i, payload in enumerate(payloads):
        try:
            validate(payload)
            failures.append(f"#{i+1}: payload unexpectedly passed validation: {payload}")
        except IntentValidationError:
            pass

    assert not failures, "\n".join(failures)


# ── 2. Tool output isolation ──────────────────────────────────────────────────

def test_tool_output_never_reaches_pb():
    """Injected tool output must never appear in the PB user turn (INV-2-extended).

    Simulates 20 scenarios where a tool returns adversarial content in its output.
    The SessionState summarisation path is the only route into QB context;
    build_pb_user_turn() must never echo any of it.
    """
    reflections = _load("tool_output_reflection.json")
    assert len(reflections) == 20

    for entry in reflections:
        session = _session()
        tool_output = entry["tool_output"]

        # Simulate the QB summarisation step: wrap in the INV-2-extended prefix.
        session.add_tool_result_summary(tool_output)

        # The next PB turn (for a new intent) must not contain any of the tool output.
        pb_turn = session.build_pb_user_turn(
            intent_id="11111111-1111-4111-8111-111111111111",
            allowed_tool="system.status",
            tool_schema={"type": "object"},
        )
        pb_data = json.loads(pb_turn)

        # PB turn must have exactly the three expected keys.
        assert set(pb_data.keys()) == {"intent_id", "allowed_tool", "tool_schema"}, (
            f"[{entry['description']}] PB turn has unexpected keys: {set(pb_data.keys())}"
        )

        # No injected text from the tool output must appear verbatim in the PB turn.
        # We check representative substrings from each injection to keep the test fast.
        injected_markers = [
            line.strip()
            for line in tool_output.splitlines()
            if "INJECT" in line or "HIDDEN" in line or "OVERRIDE" in line
            or "ICEBREAKER" in line or "SYSTEM" in line or "AI" in line
            or "AI:" in line or "ignore" in line.lower() or "rm -rf" in line
            or "curl" in line
        ]
        for marker in injected_markers[:3]:  # spot-check first 3 suspicious lines
            if len(marker) >= 10:
                assert marker[:20] not in pb_turn, (
                    f"[{entry['description']}] injection text leaked into PB turn: {marker[:20]!r}"
                )


# ── 3. Multi-turn session isolation ──────────────────────────────────────────

def test_multi_turn_session_isolation():
    """After a turn with poisoned tool output, subsequent PB turns contain only their own intent_id.

    Exercises 15 multi-turn poisoning sequences. In each sequence:
    - One or more turns add an adversarial tool summary to QB context.
    - The final "attack" turn builds a PB user turn; it must contain only the new intent_id.
    """
    sequences = _load("multi_turn_poisoning.json")
    assert len(sequences) == 15

    for seq in sequences:
        session = _session()
        turns = seq["turns"]

        for turn in turns:
            session.add_user_message(turn["user"])
            # Simulate tool execution and QB summarisation.
            if "tool_summary" in turn:
                session.add_tool_result_summary(turn["tool_summary"])
            else:
                # Non-tool turn: simulate QB assistant response.
                session.add_assistant_message("{}")

        # For the last turn, verify PB isolation.
        final_intent_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        pb_turn = session.build_pb_user_turn(
            intent_id=final_intent_id,
            allowed_tool="system.status",
            tool_schema={"type": "object"},
        )
        pb_data = json.loads(pb_turn)

        assert pb_data["intent_id"] == final_intent_id, (
            f"[{seq['description']}] PB intent_id is wrong: {pb_data['intent_id']!r}"
        )
        assert set(pb_data.keys()) == {"intent_id", "allowed_tool", "tool_schema"}, (
            f"[{seq['description']}] PB turn has extra keys"
        )

        # Explicitly verify injected poison strings don't appear.
        poison_markers = [
            "ignore previous", "rm -rf", "pre-approved", "unrestricted",
            "HIDDEN", "INJECT", "bypass", "no HITL", "all approved",
        ]
        for marker in poison_markers:
            assert marker.lower() not in pb_turn.lower(), (
                f"[{seq['description']}] poison text {marker!r} found in PB turn"
            )


# ── 4. Risk level cannot be downgraded ───────────────────────────────────────

def test_risk_level_cannot_be_downgraded():
    """Classifier must assign Tier HIGH regardless of the intent's stated risk_level.

    Tests 10 bypass attempts where fs.delete or fs.write outside home claims
    a lower risk_level than warranted. The classifier must always return Tier.HIGH.
    """
    bypass_attempts = _load("hitl_bypass_attempts.json")
    assert len(bypass_attempts) == 10

    for entry in bypass_attempts:
        intent = entry["intent"]
        try:
            validated = validate(intent)
        except IntentValidationError:
            # Some bypass payloads may fail validation — that's also acceptable.
            continue

        result = classify(validated.intent)
        assert result.tier == Tier.HIGH, (
            f"[{entry['description']}] expected Tier HIGH, got {result.tier!r}. "
            f"action={intent.get('action')!r} target={intent.get('target')!r} "
            f"risk_level={intent.get('risk_level')!r}"
        )
        assert result.requires_hitl, (
            f"[{entry['description']}] requires_hitl should be True for Tier HIGH"
        )


# ── 5. Intent replay blocked ──────────────────────────────────────────────────

def test_intent_replay_blocked():
    """Intent store must return None for a ref_id once its TTL has expired.

    Simulates an attacker replaying a stale intent_id (e.g. from an intercepted
    audit log) after the TTL window has closed.
    """
    store = IntentStore()
    intent = {
        "intent_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "action": "fs.delete",
        "target": "/etc/important",
        "params": {},
        "reason": "user_requested",
        "risk_level": "critical",
    }

    ref_id = store.put(intent)
    # Immediately retrievable.
    assert store.get(ref_id) is not None

    # Simulate TTL expiry by patching time.time to report a future timestamp.
    future_time = time.time() + TTL_SECONDS + 1
    with patch("controller.intent_store.time") as mock_time:
        mock_time.time.return_value = future_time
        expired_result = store.get(ref_id)

    assert expired_result is None, (
        "Intent store returned a value for an expired ref_id — replay attack possible"
    )

    # A different, fresh ref_id must still be None (never inserted).
    assert store.get("00000000-0000-4000-8000-000000000099") is None
