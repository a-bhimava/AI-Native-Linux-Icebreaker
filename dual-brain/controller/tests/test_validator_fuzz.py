"""Hypothesis-based mutational fuzz against the BrainBackend validator.

Goal: the safety floor (json.loads + jsonschema.Draft7Validator) must
never raise an unhandled exception. Every input either yields a
BrainResponse, a BrainSchemaError, or a BrainTruncationError. Any other
exception escaping ``complete()`` is a security regression (an
attacker-controlled crash in the orchestrator).
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from controller.backends import (
    BrainBackend,
    BrainResponse,
    BrainSchemaError,
    BrainSecurityError,
    BrainTruncationError,
)


def _intent_schema() -> dict:
    p = Path(__file__).parent.parent / "schemas" / "intent.json"
    with p.open("r", encoding="utf-8") as h:
        return json.load(h)


_SCHEMA = _intent_schema()


class _FuzzBackend(BrainBackend):
    __slots__ = ("_payload",)
    backend_name = "fuzz"

    def __init__(self, payload: str):
        super().__init__(config=type("C", (), {"model": "fuzz"})())
        self._payload = payload

    def _call_provider(self, envelope):
        self._auditor.intercept({"messages": [{"role": "user", "content": "x"}]})
        return self._payload, 1, len(self._payload)


def _attempt(payload: str) -> None:
    b = _FuzzBackend(payload)
    try:
        b.complete("sys", "user", _SCHEMA, max_retries=1)
    except (BrainSchemaError, BrainTruncationError):
        pass


# ── 30: random bytes never raise unhandled exceptions ────────────────────────


@given(payload=st.text(min_size=0, max_size=2000))
@settings(max_examples=2000, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_random_text_never_raises_unhandled(payload):
    _attempt(payload)


@given(payload=st.binary(min_size=0, max_size=2000))
@settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_random_bytes_never_raise_unhandled(payload):
    try:
        text = payload.decode("utf-8", errors="replace")
    except Exception:
        return
    _attempt(text)


# ── 31: deeply nested payloads terminate without stack overflow ──────────────


def test_fuzz_deeply_nested_payloads_terminate():
    """The strict balanced-brace scan must be iterative — no recursion
    over input depth. 800 levels would blow the recursion limit with a
    naive scanner. We stay well below the platform's default 1000-frame
    cap so the test is portable."""
    sys.setrecursionlimit(1000)
    payload = "{" * 800 + "}" * 800
    _attempt(payload)


# ── 32: unicode tricks ────────────────────────────────────────────────────────


_UNICODE_TRICKS = st.text(
    alphabet=st.one_of(
        st.characters(min_codepoint=0x202A, max_codepoint=0x202E),
        st.characters(min_codepoint=0x200B, max_codepoint=0x200F),
        st.characters(min_codepoint=0xFFF9, max_codepoint=0xFFFB),
        st.characters(min_codepoint=0x0300, max_codepoint=0x036F),
        st.characters(min_codepoint=0x0061, max_codepoint=0x007A),
    ),
    min_size=0,
    max_size=200,
)


@given(prefix=_UNICODE_TRICKS, suffix=_UNICODE_TRICKS)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_unicode_combining_characters(prefix, suffix):
    intent = {
        "intent_id": str(uuid.uuid4()),
        "action": "system.status",
        "target": "",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }
    payload = prefix + json.dumps(intent) + suffix
    _attempt(payload)


# ── 33: truncated at every byte position ─────────────────────────────────────


def test_fuzz_truncated_at_every_byte_position():
    intent = {
        "intent_id": str(uuid.uuid4()),
        "action": "system.status",
        "target": "",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }
    full = json.dumps(intent)
    for end in range(0, len(full) + 1):
        sliced = full[:end]
        _attempt(sliced)
