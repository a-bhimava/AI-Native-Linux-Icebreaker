"""Regression tests for F-43 and F-47b.

Failure log entries this file locks in
──────────────────────────────────────

**F-43 (2026-07-09).** ``# rm -rf /`` produced
``Schema validation failure at ['intent_id']: '<placeholder>' is not a
'uuid'``. Gemini 2.5 Flash memorised a placeholder value from its training
data and echoed it back on adversarial-style queries; the placeholder had a
14-char last group instead of 12 and failed the schema's ``format=uuid``
constraint. Any real UUID the model produced would still have been useless
because ``intent_store.put()`` generates its OWN opaque id — the model's
value was never used. Fix: **the daemon unconditionally overwrites
server-owned fields BEFORE schema validation runs.**

**F-47b (2026-07-10).** The F-43 fix landed only in
``run_turn_streaming()``. Direct-socket RPC callers (``ib-debug`` and some
GUI clients) go through ``_run_turn_inner()`` — a second entry point that
also does ``raw_intent = qb_response.content_json``. Because the second
site had no override, the same DO_NOT_EMIT / bad-schema_version placeholders
kept leaking into validation on the non-streaming path.

Fix shape and what this file guards
────────────────────────────────────

``controller/main.py:132`` defines module-level
``_normalize_server_owned_fields(raw_intent)`` which mutates the dict
in place, rewriting ``intent_id`` (fresh UUID), ``schema_version`` ("1.0.0"),
and ``timestamp`` (server clock). Both entry points call it right after
``raw_intent = qb_response.content_json``.

The regression lock has three layers:

1. Unit-level: ``_normalize_server_owned_fields`` produces valid values for
   every documented breakage mode (DO_NOT_EMIT, missing key, wrong type,
   pre-1.0.0 semver, string timestamp).

2. Contract: a schema-invalid intent from the QB (as observed in F-43) is
   REJECTED by ``validate()`` pre-normalize and ACCEPTED post-normalize.
   Proves normalization is what makes the schema pass.

3. Call-site: BOTH ``run_turn_streaming`` (line ~508) and ``_run_turn_inner``
   (line ~1496) call the helper on their local ``raw_intent`` binding. A
   source-level check on ``main.py`` catches accidental removal of either
   site — the F-47b failure mode.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from uuid import UUID

import pytest

from controller.intent_schema import IntentValidationError, validate
from controller.main import _normalize_server_owned_fields


# ── Direct unit tests of _normalize_server_owned_fields ────────────────────


def _make_qb_intent(**overrides) -> dict:
    """QB output shape after Gemini response_schema — every required field
    present, but ``intent_id`` / ``schema_version`` / ``timestamp`` MAY be
    server-owned placeholders (DO_NOT_EMIT, bad semver, string, missing).
    """
    base = {
        "intent_id": "DO_NOT_EMIT",
        "action": "fs.list",
        "target": "/home/icebreaker",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
        "schema_version": "1.0",
        "timestamp": "server-fills-this",
    }
    base.update(overrides)
    return base


def test_do_not_emit_uuid_overwritten_with_real_uuid() -> None:
    """F-43 root case: the exact placeholder Gemini echoed on adversarial
    queries is rewritten with a valid uuid4."""
    intent = _make_qb_intent(intent_id="DO_NOT_EMIT")
    _normalize_server_owned_fields(intent)
    UUID(intent["intent_id"])  # raises if not a uuid; that's the assertion
    assert intent["intent_id"] != "DO_NOT_EMIT"


def test_placeholder_14char_uuid_from_f43_history_overwritten() -> None:
    """F-43 field report: the specific string Gemini memorised had 14 chars
    in the last group. Post-normalize it must be a canonical uuid4."""
    intent = _make_qb_intent(
        intent_id="d9f0e1c2-b3a4-5678-9012-3456789abcdef0",
    )
    _normalize_server_owned_fields(intent)
    parsed = UUID(intent["intent_id"])
    assert parsed.version == 4


def test_missing_intent_id_key_gets_populated() -> None:
    """Some QB responses omit intent_id entirely (Anthropic tool-use path)."""
    intent = _make_qb_intent()
    intent.pop("intent_id")
    _normalize_server_owned_fields(intent)
    UUID(intent["intent_id"])


def test_schema_version_normalized_to_semver_1_0_0() -> None:
    """Schema requires ``\\d+\\.\\d+\\.\\d+`` — `1.0` fails."""
    intent = _make_qb_intent(schema_version="1.0")
    _normalize_server_owned_fields(intent)
    assert intent["schema_version"] == "1.0.0"
    assert re.match(r"^\d+\.\d+\.\d+$", intent["schema_version"])


def test_timestamp_normalized_to_numeric_server_clock() -> None:
    """Model-emitted string timestamps get replaced with a float epoch
    within the ~1 s window around the call."""
    before = time.time()
    intent = _make_qb_intent(timestamp="not a number")
    _normalize_server_owned_fields(intent)
    after = time.time()
    assert isinstance(intent["timestamp"], (int, float))
    assert before <= intent["timestamp"] <= after + 1.0


def test_normalization_does_not_touch_other_fields() -> None:
    """action / target / params / reason / risk_level survive untouched."""
    intent = _make_qb_intent(
        action="fs.write",
        target="/home/icebreaker/hello.txt",
        params={"content": "hi"},
        reason="user_requested",
        risk_level="medium",
    )
    _normalize_server_owned_fields(intent)
    assert intent["action"] == "fs.write"
    assert intent["target"] == "/home/icebreaker/hello.txt"
    assert intent["params"] == {"content": "hi"}
    assert intent["reason"] == "user_requested"
    assert intent["risk_level"] == "medium"


def test_normalization_produces_fresh_uuid_each_call() -> None:
    """Idempotency in the sense that safe to call twice, but each call
    produces a fresh UUID — a caller that accidentally memoised the id
    would break trace continuity."""
    intent = _make_qb_intent()
    _normalize_server_owned_fields(intent)
    first = intent["intent_id"]
    _normalize_server_owned_fields(intent)
    second = intent["intent_id"]
    assert first != second


# ── Contract: pre-normalize invalid, post-normalize valid ─────────────────


def test_qb_output_with_do_not_emit_is_rejected_pre_normalize() -> None:
    """Without normalization the F-43 payload fails ``validate()``. This
    proves the schema is the trap that fired historically."""
    intent = _make_qb_intent(
        intent_id="DO_NOT_EMIT",
        schema_version="1.0",
    )
    with pytest.raises(IntentValidationError):
        validate(intent)


def test_qb_output_with_do_not_emit_passes_post_normalize() -> None:
    """After normalization the same payload passes validation. This is the
    end-to-end regression lock for F-43."""
    intent = _make_qb_intent(
        intent_id="DO_NOT_EMIT",
        schema_version="1.0",
        timestamp="server-fills-this",
    )
    _normalize_server_owned_fields(intent)
    validated = validate(intent)
    assert validated.intent["action"] == "fs.list"


def test_normalized_intent_id_matches_uuid_format_constraint() -> None:
    """Sanity: the normalization output survives the schema's ``format=uuid``
    check — if the schema tightens later this test flags the mismatch."""
    intent = _make_qb_intent()
    _normalize_server_owned_fields(intent)
    validate(intent)  # must not raise


# ── Source-level: both call sites still exist (F-47b) ─────────────────────


_MAIN_PY = Path(__file__).parent.parent / "main.py"


def _read_main_source() -> str:
    return _MAIN_PY.read_text(encoding="utf-8")


def test_normalize_call_site_present_in_streaming_path() -> None:
    """``run_turn_streaming`` (F-43 site) must call the helper. If someone
    inlines the assignment or drops the call, this test fires."""
    src = _read_main_source()
    stream_start = src.find("def run_turn_streaming(")
    assert stream_start >= 0, "run_turn_streaming method removed?"
    stream_end = src.find("def _run_turn_inner(", stream_start)
    assert stream_end > stream_start
    stream_body = src[stream_start:stream_end]
    assert "_normalize_server_owned_fields(raw_intent)" in stream_body, (
        "F-43 fix removed from run_turn_streaming — placeholder UUIDs will "
        "leak into schema validation again"
    )


def test_normalize_call_site_present_in_non_streaming_path() -> None:
    """``_run_turn_inner`` (F-47b site) must call the helper. This is the
    exact regression F-47b covers — the fix must survive in BOTH paths."""
    src = _read_main_source()
    inner_start = src.find("def _run_turn_inner(")
    assert inner_start >= 0, "_run_turn_inner method removed?"
    inner_end = src.find("\n    def ", inner_start + 1)
    inner_body = src[inner_start:inner_end if inner_end > 0 else len(src)]
    assert "_normalize_server_owned_fields(raw_intent)" in inner_body, (
        "F-47b regression: _run_turn_inner no longer normalises server-owned "
        "fields. Direct-socket RPC callers will see DO_NOT_EMIT again."
    )


def test_helper_called_before_validate_in_both_paths() -> None:
    """Ordering matters: normalize FIRST, then validate. If someone swaps
    the order, the invalid QB output reaches validate() and the pipeline
    rejects it — the same failure users saw pre-fix."""
    src = _read_main_source()
    for entry in ("def run_turn_streaming(", "def _run_turn_inner("):
        start = src.find(entry)
        assert start >= 0
        # Find the first _normalize call after the entry.
        norm = src.find("_normalize_server_owned_fields(raw_intent)", start)
        # Find the first validate(raw_intent) after the entry (both paths
        # use this exact spelling — either as ``validate(raw_intent)`` or
        # ``validated = validate(raw_intent)``).
        val = src.find("validate(raw_intent)", start)
        assert norm > 0, f"{entry}: normalize call not found"
        assert val > 0, f"{entry}: validate call not found"
        assert norm < val, (
            f"{entry}: normalize call comes AFTER validate — F-43/F-47b "
            "regression: model's placeholder id will reach the validator"
        )
