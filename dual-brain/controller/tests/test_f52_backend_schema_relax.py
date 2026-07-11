"""Regression tests for F-52.

Failure log entry this file locks in
─────────────────────────────────────

**F-52 (2026-07-10).** V6.63 UTM: ``# whats in my home directory`` produced
``Schema validation failure at ['intent_id']: 'DO_NOT_EMIT' is not a
'uuid'``. F-43 (unconditional server-side ``intent_id`` regeneration) was
in place — but Gemini's ``response_schema`` was rejecting the model's
placeholder BEFORE the daemon-side normalise ran.

The failure sequence:

1. Backend hands Gemini the full intent schema (with ``format=uuid`` +
   ``required``).
2. Model emits ``intent_id="DO_NOT_EMIT"`` — a value it memorised from
   training data.
3. Gemini's response_schema validator rejects.
4. Backend retries. Same prompt, same placeholder comes back.
5. After ``max_retries``, backend gives up and raises — BEFORE
   ``_normalize_server_owned_fields`` could rewrite the id.

Solution: hand the backend a RELAXED copy of the schema. The relaxed
schema drops ``required`` + ``format`` + ``pattern`` for the three
server-owned fields. The model can now legally emit whatever it likes
(or omit them entirely); ``_normalize_server_owned_fields`` overwrites
with fresh values; the FULL (strict) validator runs AFTER normalization
and catches any actually-invalid intent.

Fix shape and what this file guards
────────────────────────────────────

``controller/main.py:191`` defines ``_relax_server_owned_for_backend`` which
returns a deep-copy of the intent schema with:

* ``required`` array no longer containing ``intent_id`` / ``schema_version``
  / ``timestamp``.
* ``properties.intent_id`` and ``properties.schema_version`` with
  ``format`` and ``pattern`` stripped.
* Every OTHER field (``action``, ``target``, ``params``, ``content``,
  ``reason``, ``risk_level``) unchanged.

The regression lock covers four properties:

1. Direct: the three server-owned fields are removed from ``required`` and
   their format/pattern constraints are dropped.
2. Non-relaxation: the FULL model-facing constraints on non-server fields
   survive. Weakening ``action`` or ``target`` would let a hostile QB emit
   shell metacharacters.
3. Deep-copy: mutating the returned schema does NOT mutate the input.
4. Validator invariant: the STRICT ``validate()`` (Controller-side) still
   rejects intents missing server-owned fields. The relaxation is
   backend-facing only; the invariant enforcement is downstream.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from controller.intent_schema import IntentValidationError, validate
from controller.main import (
    _SERVER_OWNED_INTENT_FIELDS,
    _relax_server_owned_for_backend,
)


_SCHEMA_PATH = (
    Path(__file__).parent.parent / "schemas" / "intent.json"
)


@pytest.fixture(scope="module")
def intent_schema() -> dict:
    """The strict intent schema loaded from disk. Used both as input to the
    relax helper AND as the reference the controller-side validator uses."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


# ── Direct: required + format/pattern stripped from server-owned fields ──


def test_server_owned_fields_dropped_from_required(intent_schema: dict) -> None:
    """The relaxed schema must NOT ``require`` any server-owned field.
    Otherwise Gemini re-triggers the retry loop that F-52 flagged."""
    relaxed = _relax_server_owned_for_backend(intent_schema)
    assert "required" in relaxed
    for field in _SERVER_OWNED_INTENT_FIELDS:
        assert field not in relaxed["required"], (
            f"{field!r} still in `required` — backend will loop on "
            "placeholder values (F-52 regression)"
        )


def test_intent_id_format_and_pattern_stripped(intent_schema: dict) -> None:
    """The F-43 root case: Gemini echoed ``DO_NOT_EMIT``, backend rejected
    on ``format=uuid``. Relaxed schema must have neither ``format`` nor
    ``pattern`` on ``intent_id``."""
    relaxed = _relax_server_owned_for_backend(intent_schema)
    entry = relaxed["properties"]["intent_id"]
    assert "format" not in entry, (
        "format=uuid survives on intent_id — backend will retry-loop"
    )
    assert "pattern" not in entry, "pattern survives on intent_id"


def test_schema_version_pattern_stripped(intent_schema: dict) -> None:
    """Gemini's placeholder was ``"1.0"`` — fails the semver pattern.
    Relaxed schema must drop the pattern so the backend doesn't reject."""
    relaxed = _relax_server_owned_for_backend(intent_schema)
    entry = relaxed["properties"]["schema_version"]
    assert "pattern" not in entry, (
        "semver pattern survives on schema_version — backend will "
        "reject placeholder '1.0' (F-52 regression)"
    )


def test_timestamp_relaxed(intent_schema: dict) -> None:
    """Timestamp had no format/pattern in the original schema — relaxation
    should be a no-op, but the field must still be present in properties
    so backends that inspect it don't crash."""
    relaxed = _relax_server_owned_for_backend(intent_schema)
    assert "timestamp" in relaxed["properties"]
    entry = relaxed["properties"]["timestamp"]
    assert "format" not in entry
    assert "pattern" not in entry


# ── Non-relaxation: EVERYTHING ELSE survives strict ──────────────────────


def test_action_pattern_preserved(intent_schema: dict) -> None:
    """``action`` carries the dotted-module pattern (INV-2 relevant). A
    relaxation that touched this would let QB emit arbitrary strings and
    the backend wouldn't reject them at response time — MCP dispatch
    could accept nonsense actions before the Controller catches them."""
    relaxed = _relax_server_owned_for_backend(intent_schema)
    original_action = intent_schema["properties"]["action"]
    relaxed_action = relaxed["properties"]["action"]
    assert original_action["pattern"] == relaxed_action["pattern"]
    assert original_action.get("maxLength") == relaxed_action.get("maxLength")


def test_target_pattern_preserved(intent_schema: dict) -> None:
    """``target`` pattern forbids shell metacharacters (INV-2 explicit
    constraint). Relaxation must not weaken this — the whole shell-safety
    story depends on backends rejecting metachar targets at parse time."""
    relaxed = _relax_server_owned_for_backend(intent_schema)
    original = intent_schema["properties"]["target"]
    relaxed_target = relaxed["properties"]["target"]
    assert relaxed_target["pattern"] == original["pattern"]
    # Sanity: the pattern actually blocks metachars.
    assert ";" in original["pattern"] or "&" in original["pattern"]


def test_content_pattern_preserved(intent_schema: dict) -> None:
    """``content`` (F-41 field) forbids NUL bytes. Relaxation must preserve."""
    relaxed = _relax_server_owned_for_backend(intent_schema)
    if "content" in intent_schema["properties"]:
        original = intent_schema["properties"]["content"]
        relaxed_c = relaxed["properties"]["content"]
        assert relaxed_c.get("pattern") == original.get("pattern")


def test_required_still_contains_non_server_fields(intent_schema: dict) -> None:
    """After relaxation, the non-server-owned required fields must still
    be required. A relaxed ``required`` list of just ``["action"]`` would
    be a bug — backend would happily return intents missing ``target``,
    ``params``, ``reason``, ``risk_level`` — the validator would then
    reject downstream but the retry loop would have wasted budget."""
    relaxed = _relax_server_owned_for_backend(intent_schema)
    expected_present = ["action", "target", "params", "reason", "risk_level"]
    for field in expected_present:
        assert field in relaxed["required"], (
            f"{field!r} dropped from `required` — backend won't retry "
            "when the model omits it"
        )


# ── Deep copy: input schema is NOT mutated ────────────────────────────────


def test_input_schema_not_mutated(intent_schema: dict) -> None:
    """The controller loads the strict schema once at construction and
    reuses it for validate(). If _relax_server_owned_for_backend mutated
    the input, every subsequent validate() call would operate on the
    relaxed schema and F-52's whole point (strict validation AFTER
    normalize) would break."""
    before = copy.deepcopy(intent_schema)
    _ = _relax_server_owned_for_backend(intent_schema)
    assert intent_schema == before, (
        "_relax_server_owned_for_backend mutated its input — strict "
        "validate() would silently permit malformed intents"
    )


def test_relaxation_is_idempotent(intent_schema: dict) -> None:
    """Two relaxations produce equal output. Nested calls (which shouldn't
    happen, but defence in depth) must not weaken further."""
    once = _relax_server_owned_for_backend(intent_schema)
    twice = _relax_server_owned_for_backend(once)
    assert once == twice


# ── Validator invariant: STRICT validate() still catches missing fields ──


def test_strict_validator_still_rejects_missing_intent_id(intent_schema: dict) -> None:
    """The relaxation is backend-facing. The Controller's ``validate()`` is
    the invariant enforcer and MUST still reject an intent that lacks
    ``intent_id`` — that would break INV-1's opaque-reference contract.

    (In production ``_normalize_server_owned_fields`` runs first and
    guarantees ``intent_id`` is set; this test proves what happens if
    normalization ever gets skipped.)"""
    intent_without_id = {
        "action": "fs.list",
        "target": "/home/icebreaker",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
        "schema_version": "1.0.0",
        "timestamp": 1720000000.0,
    }
    with pytest.raises(IntentValidationError):
        validate(intent_without_id)


def test_strict_validator_still_rejects_do_not_emit_intent_id() -> None:
    """The FULL schema's ``format=uuid`` on ``intent_id`` must still fire.
    The relaxed schema is backend-only; the invariant is enforced by
    the Controller-side ``validate()`` after ``_normalize_...`` runs."""
    intent_with_placeholder = {
        "intent_id": "DO_NOT_EMIT",
        "action": "fs.list",
        "target": "/home/icebreaker",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
        "schema_version": "1.0.0",
        "timestamp": 1720000000.0,
    }
    with pytest.raises(IntentValidationError):
        validate(intent_with_placeholder)


# ── Contract: the constant naming the fields is the single source ────────


def test_server_owned_fields_tuple_matches_relaxation_behavior(
    intent_schema: dict,
) -> None:
    """The exact set of fields that get relaxed must be exactly
    ``_SERVER_OWNED_INTENT_FIELDS``. If someone extends the tuple, the
    relaxation logic auto-picks it up (property-based check). If someone
    diverges the tuple from the relaxation, this test catches it."""
    relaxed = _relax_server_owned_for_backend(intent_schema)
    for field in _SERVER_OWNED_INTENT_FIELDS:
        assert field not in relaxed["required"]
        entry = relaxed["properties"].get(field, {})
        assert "format" not in entry
        assert "pattern" not in entry


# ── Contract: the call site in Controller.__init__ still wires it up ─────


_MAIN_PY = Path(__file__).parent.parent / "main.py"


def test_controller_init_still_calls_relax_helper() -> None:
    """The F-52 fix wired ``_relax_server_owned_for_backend`` into
    ``Controller.__init__`` so the backend gets the relaxed schema. If
    someone accidentally reverts to ``self._intent_schema`` for the
    backend, the retry-loop reappears."""
    src = _MAIN_PY.read_text(encoding="utf-8")
    assert (
        "_backend_intent_schema" in src
        and "_relax_server_owned_for_backend(" in src
    ), (
        "F-52 regression: Controller no longer relaxes the backend-facing "
        "schema. Gemini will start retry-looping on placeholder UUIDs."
    )
