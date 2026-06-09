"""Tests for ``backends._api_common.transform_schema_for_provider``.

The transformer strips JSON Schema keywords that Anthropic / Gemini
reject from their native structured-output grammars. The local
``jsonschema.Draft7Validator`` in ``base.complete()`` still enforces
the ORIGINAL schema — these tests pin which keywords the wire schema
loses, not which the validator enforces.

Covers tests 1-12 from the M2.6/M2.7 plan.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from controller.backends._api_common import transform_schema_for_provider


# ── Helpers ──────────────────────────────────────────────────────────────────


def _collect_keys(node: object) -> set[str]:
    """Recursively collect every dict key in a schema tree."""
    keys: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            keys.add(k)
            keys.update(_collect_keys(v))
    elif isinstance(node, list):
        for item in node:
            keys.update(_collect_keys(item))
    return keys


def _intent_schema() -> dict:
    path = Path(__file__).parent.parent / "schemas" / "intent.json"
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


# ── 1: strip maxLength ──────────────────────────────────────────────────────


def test_strip_max_length():
    schema = {"type": "string", "maxLength": 1024}
    out = transform_schema_for_provider(schema)
    assert "maxLength" not in out
    assert out["type"] == "string"


def test_strip_max_length_at_depth():
    schema = {
        "type": "object",
        "properties": {
            "params": {
                "type": "object",
                "additionalProperties": {"type": "string", "maxLength": 1024},
            }
        },
    }
    out = transform_schema_for_provider(schema)
    assert "maxLength" not in _collect_keys(out)


# ── 2: strip minLength ──────────────────────────────────────────────────────


def test_strip_min_length():
    schema = {"type": "string", "minLength": 1, "minimum": 0}
    out = transform_schema_for_provider(schema)
    assert "minLength" not in out
    assert "minimum" not in out


# ── 3: strip numeric bounds ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"],
)
def test_strip_numeric_bounds(key):
    schema = {"type": "number", key: 10}
    out = transform_schema_for_provider(schema)
    assert key not in out


# ── 4: strip array bounds ────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["minItems", "maxItems", "uniqueItems"])
def test_strip_array_bounds(key):
    schema = {"type": "array", "items": {"type": "string"}, key: 5}
    out = transform_schema_for_provider(schema)
    assert key not in out
    assert out["items"] == {"type": "string"}


# ── 5: strip object bounds ───────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["minProperties", "maxProperties"])
def test_strip_object_bounds(key):
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, key: 1}
    out = transform_schema_for_provider(schema)
    assert key not in out
    assert "properties" in out


# ── 6: oneOf -> anyOf ────────────────────────────────────────────────────────


def test_oneof_to_anyof():
    schema = {
        "oneOf": [
            {"type": "string"},
            {"type": "number"},
            {"type": "boolean"},
            {"type": "null"},
        ]
    }
    out = transform_schema_for_provider(schema)
    assert "oneOf" not in out
    assert "anyOf" in out
    assert len(out["anyOf"]) == 4
    assert {"type": "string"} in out["anyOf"]


def test_oneof_with_inner_stripping():
    """``oneOf`` branches are themselves transformed."""
    schema = {
        "oneOf": [
            {"type": "string", "maxLength": 100},
            {"type": "integer", "minimum": 0},
        ]
    }
    out = transform_schema_for_provider(schema)
    assert "anyOf" in out
    for branch in out["anyOf"]:
        assert "maxLength" not in branch
        assert "minimum" not in branch


# ── 7: oneOf unchanged when conversion is disabled ──────────────────────────


def test_oneof_unchanged_when_disabled():
    schema = {"oneOf": [{"type": "string"}, {"type": "number"}]}
    out = transform_schema_for_provider(schema, convert_oneof_to_anyof=False)
    assert "oneOf" in out
    assert "anyOf" not in out
    assert out["oneOf"] == schema["oneOf"]


# ── 8: format stripped when requested ───────────────────────────────────────


def test_format_stripped_when_requested():
    schema = {"type": "string", "format": "uuid"}
    out = transform_schema_for_provider(schema, strip_format=True)
    assert "format" not in out
    assert out["type"] == "string"


def test_format_stripped_at_depth():
    schema = {
        "type": "object",
        "properties": {"id": {"type": "string", "format": "uuid"}},
    }
    out = transform_schema_for_provider(schema, strip_format=True)
    assert "format" not in _collect_keys(out)


# ── 9: format preserved by default ──────────────────────────────────────────


def test_format_preserved_by_default():
    schema = {"type": "string", "format": "uuid"}
    out = transform_schema_for_provider(schema)
    assert out["format"] == "uuid"


# ── 10: nested transformation ───────────────────────────────────────────────


def test_nested_transformation_through_lists_and_dicts():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "maxLength": 50},
                        "value": {"type": "number", "minimum": 0, "maximum": 100},
                    },
                },
            },
        },
        "minProperties": 1,
    }
    out = transform_schema_for_provider(schema)
    all_keys = _collect_keys(out)
    forbidden = {
        "minItems",
        "maxLength",
        "minimum",
        "maximum",
        "minProperties",
    }
    assert not (all_keys & forbidden), (
        f"forbidden keys leaked through transform: {all_keys & forbidden}"
    )
    # Structure preserved
    assert "properties" in out
    assert "items" in out["properties"]
    assert "name" in out["properties"]["items"]["items"]["properties"]


def test_transform_is_pure_does_not_mutate_input():
    schema = {"type": "string", "maxLength": 1024}
    original = copy.deepcopy(schema)
    _ = transform_schema_for_provider(schema)
    assert schema == original, "transform_schema_for_provider mutated input"


# ── 11: intent.json round-trip for Anthropic ────────────────────────────────


def test_intent_schema_round_trip_for_anthropic():
    """Real intent.json transforms cleanly for Anthropic native JSON mode."""
    schema = _intent_schema()
    out = transform_schema_for_provider(
        schema, convert_oneof_to_anyof=True, strip_format=False
    )
    keys = _collect_keys(out)
    forbidden = {
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "minItems",
        "maxItems",
        "oneOf",
    }
    leaked = keys & forbidden
    assert not leaked, f"forbidden keys leaked: {leaked}"
    # Anthropic supports format=uuid; it must survive.
    assert "format" in keys
    # Top-level required intent fields preserved.
    assert "properties" in out
    props = out["properties"]
    for field in (
        "intent_id",
        "action",
        "target",
        "params",
        "reason",
        "risk_level",
    ):
        assert field in props, f"required intent field {field!r} dropped"


# ── 12: intent.json round-trip for Gemini ───────────────────────────────────


def test_intent_schema_round_trip_for_gemini():
    """Real intent.json transforms cleanly for Gemini response_schema."""
    schema = _intent_schema()
    out = transform_schema_for_provider(
        schema, convert_oneof_to_anyof=True, strip_format=True
    )
    keys = _collect_keys(out)
    forbidden = {
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "minItems",
        "maxItems",
        "oneOf",
        "format",  # extra: Gemini doesn't support format=uuid
    }
    leaked = keys & forbidden
    assert not leaked, f"forbidden keys leaked: {leaked}"
    assert "properties" in out
    props = out["properties"]
    for field in (
        "intent_id",
        "action",
        "target",
        "params",
        "reason",
        "risk_level",
    ):
        assert field in props
