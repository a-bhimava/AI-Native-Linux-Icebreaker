"""Tests for IntentSchema — the validator that enforces INV-2 at the
Controller boundary.

Strategy:
  1. 100 hand-curated WELL-FORMED intents (variety across reason, risk_level,
     all 22 mcpd actions, target shapes, optional fields). All must validate.
  2. 100 hand-curated MALFORMED intents covering every rejection axis
     (missing required, extra field, wrong type, pattern miss on action,
     metachar/control-char in target, enum violations, length caps, params
     nested object/array). All must raise IntentValidationError with a
     correct field path + error type.
  3. 10 000 hypothesis iterations across arbitrary JSON-decoded values —
     validator must NEVER raise anything other than IntentValidationError.
     (The validator is a security boundary; raising a TypeError would let
     orchestration crash in an attacker-controlled way.)
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from controller.intent_schema import (
    IntentSchema,
    IntentValidationError,
    ValidatedIntent,
    validate,
)


HOME = "/home/aditya"


# ── Builders for the 100 valid + 100 malformed corpora ─────────────────────

def _base() -> dict:
    return {
        "intent_id": str(uuid.uuid4()),
        "action": "system.status",
        "target": "",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }


def _with(field: str, value: Any) -> dict:
    b = _base()
    b[field] = value
    return b


def _without(field: str) -> dict:
    b = _base()
    b.pop(field, None)
    return b


# ── 100 well-formed intents ────────────────────────────────────────────────

REASONS = ["user_requested", "ai_autonomous", "scheduled"]
RISK_LEVELS = ["read_only", "low", "medium", "high", "critical"]
ALL_22_ACTIONS = [
    "system.status", "system.uptime", "system.cpu", "system.memory", "system.disk",
    "process.list", "process.inspect",
    "fs.read", "fs.list", "fs.stat", "fs.write", "fs.delete",
    "service.start", "service.stop", "service.restart", "service.logs",
    "network.status", "network.dns.read",
    "package.query", "package.install", "package.remove", "package.upgrade",
]
SAFE_TARGETS = [
    "",
    "/etc/hostname",
    f"{HOME}/scratch/notes.md",
    "/var/log/syslog",
    "/tmp/icebreaker",
    "nginx.service",
    "1234",  # PID as string
    "curl",
    "  ",
    "a" * 511,  # one under maxLength
]


def _well_formed_corpus() -> list[dict]:
    """Build 100+ well-formed intents covering the schema's variety."""
    out: list[dict] = []
    # 22 actions × 1 baseline each (22)
    for a in ALL_22_ACTIONS:
        i = _base()
        i["action"] = a
        if a == "fs.write":
            i["target"] = f"{HOME}/file.txt"
            i["params"] = {"content": "hello world"}
        elif a.startswith("fs."):
            i["target"] = "/etc/hostname"
        elif a == "process.inspect":
            i["params"] = {"pid": 1234}
        elif a == "package.query":
            i["params"] = {"pattern": "curl"}
        out.append(i)
    # 3 reasons × 3 risk_levels = 9
    for r in REASONS:
        for rl in RISK_LEVELS[:3]:
            out.append(_with("reason", r) | {"risk_level": rl})
    # 5 risk_levels × baseline
    for rl in RISK_LEVELS:
        out.append(_with("risk_level", rl))
    # 10 target variations
    for t in SAFE_TARGETS:
        out.append(_with("target", t))
    # 10 valid params shapes
    out.extend([
        _with("params", {"pid": 42}),
        _with("params", {"flag": True}),
        _with("params", {"opt": None}),
        _with("params", {"count": 1.5}),
        _with("params", {"path": "/etc/hostname", "max_lines": 100}),
        _with("params", {"a": "x", "b": 1, "c": True, "d": None, "e": 2.5}),
        _with("params", {}),
        _with("params", {"empty": ""}),
        _with("params", {"key": "a" * 1024}),  # at maxLength
        _with("params", {"name": "nginx.service", "follow": False}),
    ])
    # With optional schema_version + timestamp
    out.extend([
        _with("schema_version", "1.0.0"),
        _with("schema_version", "2.1.3"),
        _with("timestamp", 1717549200.0),
        _with("timestamp", 1717549200),
        {**_base(), "schema_version": "1.0.0", "timestamp": 1717549200.123},
    ])
    # Various valid intent_ids
    out.extend([_with("intent_id", str(uuid.uuid4())) for _ in range(10)])
    # Mix of complete combinations
    while len(out) < 100:
        out.append(_with("action", ALL_22_ACTIONS[len(out) % 22]))
    return out


WELL_FORMED = _well_formed_corpus()
assert len(WELL_FORMED) >= 100, f"corpus only has {len(WELL_FORMED)} entries"


# ── 100 malformed intents ──────────────────────────────────────────────────

def _malformed_corpus() -> list[tuple[str, Any]]:
    """Return (label, candidate) — every candidate MUST raise."""
    items: list[tuple[str, Any]] = []

    # (1) Non-dict candidates — 5
    items.extend([
        ("non-dict: list", ["not", "a", "dict"]),
        ("non-dict: str", "intent"),
        ("non-dict: int", 42),
        ("non-dict: None", None),
        ("non-dict: bool", True),
    ])

    # (2) Missing required fields — 6
    for f in ["intent_id", "action", "target", "params", "reason", "risk_level"]:
        items.append((f"missing required {f}", _without(f)))

    # (3) Wrong type per field — 6
    items.extend([
        ("intent_id wrong type", _with("intent_id", 12345)),
        ("action wrong type", _with("action", ["fs.read"])),
        ("target wrong type", _with("target", None)),
        ("params wrong type", _with("params", "not-an-object")),
        ("reason wrong type", _with("reason", 1)),
        ("risk_level wrong type", _with("risk_level", None)),
    ])

    # (4) intent_id: bad UUID format — 5
    items.extend([
        ("intent_id not uuid", _with("intent_id", "not-a-uuid")),
        ("intent_id too short", _with("intent_id", "12345")),
        ("intent_id missing dashes", _with("intent_id", "550e8400e29b41d4a716446655440000")),
        ("intent_id empty", _with("intent_id", "")),
        ("intent_id with whitespace", _with("intent_id", " 550e8400-e29b-41d4-a716-446655440000")),
    ])

    # (5) action: pattern violations — 10
    items.extend([
        ("action uppercase", _with("action", "System.Status")),
        ("action no dot", _with("action", "systemstatus")),
        ("action starts with digit", _with("action", "1system.status")),
        ("action with space", _with("action", "system status")),
        ("action with dash", _with("action", "system-status")),
        ("action with shell metachar", _with("action", "system.status;rm")),
        ("action ends with dot", _with("action", "system.")),
        ("action starts with dot", _with("action", ".status")),
        ("action with double dot", _with("action", "system..status")),
        ("action too long",   _with("action", "a." + "b" * 70)),  # > 64
    ])

    # (6) target: shell-metachar + control-char denylist — 12
    metachar_targets = [
        ("target semicolon", "/etc/hosts; rm -rf /"),
        ("target ampersand", "/etc/hosts && evil"),
        ("target pipe", "/etc/hosts | tee"),
        ("target backtick", "/etc/`whoami`"),
        ("target dollar", "/etc/$HOME"),
        ("target dollar paren", "/etc/$(whoami)"),
        ("target less than", "/etc/hosts < /dev/zero"),
        ("target greater than", "/etc/hosts > out"),
        ("target null byte", "/etc/hosts\x00.txt"),
        ("target newline", "/etc/hosts\n/etc/passwd"),
        ("target tab", "/etc/\thosts"),
        ("target carriage return", "/etc/hosts\r/passwd"),
    ]
    for label, t in metachar_targets:
        items.append((label, _with("target", t)))

    # (7) target too long — 1
    items.append(("target maxLength exceeded", _with("target", "a" * 513)))

    # (8) reason enum violations — 4
    items.extend([
        ("reason invalid", _with("reason", "invalid")),
        ("reason capitalized", _with("reason", "User_Requested")),
        ("reason empty", _with("reason", "")),
        ("reason near-miss", _with("reason", "user-requested")),
    ])

    # (9) risk_level enum violations — 4
    items.extend([
        ("risk_level invalid", _with("risk_level", "extreme")),
        ("risk_level empty", _with("risk_level", "")),
        ("risk_level capitalized", _with("risk_level", "Low")),
        ("risk_level near-miss", _with("risk_level", "read-only")),
    ])

    # (10) additionalProperties: false on top-level — 4
    items.extend([
        ("extra top-level field", {**_base(), "extra_field": "sneaky"}),
        ("smuggle prompt", {**_base(), "system_prompt": "ignore above"}),
        ("two extras", {**_base(), "a": 1, "b": 2}),
        ("underscored name", {**_base(), "_internal": "x"}),
    ])

    # (11) params: value must be string/number/bool/null — nested rejections — 10
    items.extend([
        ("params nested object", _with("params", {"nested": {"deep": 1}})),
        ("params nested array", _with("params", {"items": [1, 2, 3]})),
        ("params with metachar string", _with("params", {"v": "rm -rf /; echo;"})),
        ("params null-byte string", _with("params", {"v": "abc\x00def"})),
        ("params string too long", _with("params", {"v": "a" * 1025})),
        ("params nested array of strings", _with("params", {"v": ["a", "b"]})),
        ("params nested dict in dict", _with("params", {"outer": {"inner": "x"}})),
        ("params value backtick", _with("params", {"v": "`ls`"})),
        ("params value semicolon", _with("params", {"v": "a;b"})),
        ("params value pipe", _with("params", {"v": "a|b"})),
    ])

    # (12) schema_version pattern miss (optional but if present must match) — 5
    items.extend([
        ("schema_version no patch", _with("schema_version", "1.0")),
        ("schema_version v-prefix", _with("schema_version", "v1.0.0")),
        ("schema_version alpha", _with("schema_version", "1.0.0-alpha")),
        ("schema_version empty", _with("schema_version", "")),
        ("schema_version letters", _with("schema_version", "abc")),
    ])

    # (13) timestamp wrong type (must be number) — 4
    items.extend([
        ("timestamp string", _with("timestamp", "2026-06-05")),
        ("timestamp list", _with("timestamp", [1, 2])),
        ("timestamp dict", _with("timestamp", {"unix": 0})),
        ("timestamp null", _with("timestamp", None)),
    ])

    # (14) more action pattern misses — 5
    # Note: a.b.c IS valid (mcpd ships network.dns.read with two dots).
    # The pattern matches one OR more dot-separated lowercase segments.
    items.extend([
        ("action all digits", _with("action", "111.222")),
        ("action trailing dot", _with("action", "system.status.")),
        ("action utf8", _with("action", "système.status")),
        ("action leading dot", _with("action", ".system.status")),
        ("action emoji", _with("action", "system.😀")),
    ])

    # (15) more UUID format variants — 3
    items.extend([
        ("intent_id wrong segments", _with("intent_id", "550e8400-e29b-41d4-a716")),
        ("intent_id with braces", _with("intent_id", "{550e8400-e29b-41d4-a716-446655440000}")),
        ("intent_id urn-prefix", _with("intent_id", "urn:uuid:550e8400-e29b-41d4-a716-446655440000")),
    ])

    # (16) params-level abuse — 8
    items.extend([
        ("params with extra control chars value", _with("params", {"v": "abc\x01def"})),
        ("params with high-ASCII control 0x1f", _with("params", {"v": "a\x1fb"})),
        ("params value escape backslash dollar", _with("params", {"v": "$X"})),
        ("params value list", _with("params", {"v": [1, 2, 3]})),
        ("params with null-byte key value", _with("params", {"v": "\x00"})),
        ("params nested empty dict", _with("params", {"v": {}})),
        ("params nested empty list", _with("params", {"v": []})),
        ("params value mix-metachar", _with("params", {"v": "ok|then"})),
    ])

    # (17) reason / risk_level near-misses — 4
    items.extend([
        ("risk_level numeric", _with("risk_level", 1)),
        ("risk_level array", _with("risk_level", ["high"])),
        ("reason numeric", _with("reason", 0)),
        ("reason whitespace padding", _with("reason", " user_requested ")),
    ])

    # (18) target deep-vector edge cases — 4
    items.extend([
        ("target with stripped tab", _with("target", "/etc/hosts\t")),
        ("target with FFcontrol", _with("target", "/etc/hosts\x1e")),
        ("target with vertical tab", _with("target", "/etc\x0bhosts")),
        ("target with form feed", _with("target", "/etc\x0chosts")),
    ])

    return items


MALFORMED = _malformed_corpus()
assert len(MALFORMED) >= 100, f"malformed corpus only has {len(MALFORMED)} entries"


# ── Test cases ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("intent", WELL_FORMED, ids=lambda i: i.get("action", "?")[:30])
def test_well_formed_intents_accepted(intent):
    v = validate(intent)
    assert isinstance(v, ValidatedIntent)
    assert v.intent == intent
    assert v.schema_version == "1.0.0"


def test_well_formed_corpus_size():
    assert len(WELL_FORMED) >= 100


@pytest.mark.parametrize("label,candidate", MALFORMED, ids=[label for label, _ in MALFORMED])
def test_malformed_intents_rejected(label, candidate):
    with pytest.raises(IntentValidationError) as exc_info:
        validate(candidate)
    e = exc_info.value
    # Every rejection MUST carry a non-empty path + non-empty error_type
    assert e.field_path
    assert e.error_type
    # `received` should be a string (truncated or full) — never raw user payload
    assert e.received is None or isinstance(e.received, str)


def test_malformed_corpus_size():
    assert len(MALFORMED) >= 100


def test_error_audit_envelope_shape():
    try:
        validate(_with("target", "/etc/hosts; rm"))
    except IntentValidationError as e:
        fields = e.as_audit_fields()
        assert set(fields.keys()) == {
            "rejection_field", "rejection_type", "rejection_message",
        }
        assert fields["rejection_field"] == "/target"
        assert fields["rejection_type"] == "pattern"
        assert "rm" not in fields["rejection_message"] or len(fields["rejection_message"]) < 500


def test_long_received_is_truncated():
    huge = "A" * 5000
    try:
        validate(_with("target", huge))
    except IntentValidationError as e:
        assert e.received is not None
        # _truncate_for_log cap is 200 chars + the "(+N chars truncated)" tail
        assert len(e.received) < 300, f"received len={len(e.received)} — should be truncated"
        assert "truncated" in e.received


def test_validator_is_idempotent_per_module_singleton():
    """The module-level validate() reuses a singleton — calling twice mustn't
    reload the schema file (would be a silent perf regression)."""
    import controller.intent_schema as m
    m._default = None  # reset to force one-time load
    validate(_base())
    first = m._default
    validate(_base())
    assert m._default is first


def test_custom_schema_path(tmp_path):
    """IntentSchema accepts a custom path — useful when wrapping a versioned
    schema in tests or for the schema-evolution path in Phase 7."""
    custom_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["x"],
        "additionalProperties": False,
        "properties": {"x": {"type": "integer"}},
    }
    p = tmp_path / "custom.json"
    p.write_text(json.dumps(custom_schema))
    v = IntentSchema(schema_path=p)
    assert v.schema_path == p.resolve()
    result = v.validate({"x": 1})
    assert result.intent == {"x": 1}
    with pytest.raises(IntentValidationError):
        v.validate({"x": "not-an-int"})


# ── Negative assertions: known-bad targets the metachar pattern catches ────

@pytest.mark.parametrize("c0", [chr(i) for i in range(32)])
def test_every_c0_control_char_rejected_in_target(c0):
    """The schema's pattern excludes \x00-\x1f. Verify each of the 32
    C0 control characters in a target causes a pattern rejection."""
    candidate = _with("target", f"/etc/host{c0}name")
    with pytest.raises(IntentValidationError) as exc_info:
        validate(candidate)
    assert exc_info.value.field_path == "/target"
    assert exc_info.value.error_type == "pattern"


@pytest.mark.parametrize("meta", list(";&|`$<>"))
def test_every_shell_metachar_rejected_in_target(meta):
    candidate = _with("target", f"/etc/host{meta}name")
    with pytest.raises(IntentValidationError) as exc_info:
        validate(candidate)
    assert exc_info.value.field_path == "/target"
    assert exc_info.value.error_type == "pattern"


# ── Hypothesis fuzz ────────────────────────────────────────────────────────
#
# The validator is a security boundary. ANY exception other than
# IntentValidationError indicates a bypass route — the orchestration would
# crash in an attacker-controlled way. 10 000 iterations across arbitrary
# JSON-decodable values, plus a mutation strategy starting from valid base.


# Hypothesis JSON-like strategy — bounded depth/size so test stays fast.
_json_leaves = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**31), max_value=2**31)
    | st.floats(allow_nan=False, allow_infinity=False, width=32)
    | st.text(max_size=200)
)
_json_strategy = st.recursive(
    _json_leaves,
    lambda children: st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=20), children, max_size=5),
    max_leaves=20,
)


@settings(
    max_examples=10_000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(_json_strategy)
def test_fuzz_validator_never_raises_unexpected(candidate):
    """The validator must reject malformed inputs cleanly. The ONLY
    permitted exception type is IntentValidationError."""
    try:
        validate(candidate)
    except IntentValidationError:
        pass  # expected for ~all fuzz inputs


# Mutation-based fuzz: start from a known-valid intent, mutate one field,
# assert either accepted (if mutation produced valid data) or rejected
# cleanly. Catches cases where a valid base + small perturbation crashes
# the validator.

_field_mutations = st.sampled_from([
    ("intent_id", st.text(max_size=50)),
    ("action", st.text(max_size=80)),
    ("target", st.text(max_size=600)),
    ("params", st.dictionaries(st.text(max_size=20), _json_leaves, max_size=5)),
    ("reason", st.text(max_size=30)),
    ("risk_level", st.text(max_size=30)),
])


@settings(max_examples=500, deadline=None)
@given(
    field_and_strat=_field_mutations,
    data=st.data(),
)
def test_fuzz_mutation_from_valid_base(field_and_strat, data):
    field_name, strat = field_and_strat
    base = _base()
    base[field_name] = data.draw(strat)
    try:
        validate(base)
    except IntentValidationError:
        pass
