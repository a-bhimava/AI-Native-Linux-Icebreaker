"""Grammar / schema parity tests for `qb_intent.gbnf` (M2.8).

Contract: GBNF accepts ⊆ jsonschema(intent.json) accepts.
If the grammar emits a string, the schema validator MUST accept it
(possibly with additional checks like maxLength that the grammar
can't enforce; those are the validator's responsibility, not the
grammar's).

Methodology: a Python regex (``_GRAMMAR_REGEX``) mirrors the GBNF
grammar exactly. The regex IS the shadow of the grammar — any change
to ``qb_intent.gbnf`` must also update the regex below; CI catches
the rest via the differential fuzz.

A future iteration should layer ``gbnf-validator`` (from llama.cpp) on
top of this to catch regex/GBNF semantic drift, but for now the regex
shadow + 10K hypothesis fuzz is the safety floor.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import jsonschema
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st


# ─── Schema loader ───────────────────────────────────────────────────────


def _intent_schema() -> dict:
    p = Path(__file__).parent.parent / "schemas" / "intent.json"
    with p.open("r", encoding="utf-8") as h:
        return json.load(h)


_SCHEMA = _intent_schema()
_VALIDATOR = jsonschema.Draft7Validator(
    _SCHEMA,
    format_checker=jsonschema.FormatChecker(formats=["uuid"]),
)


def _schema_accepts(s: str) -> bool:
    try:
        _VALIDATOR.validate(json.loads(s))
        return True
    except (json.JSONDecodeError, jsonschema.ValidationError):
        return False


# ─── GBNF shadow regex ───────────────────────────────────────────────────
#
# MUST mirror dual-brain/controller/grammars/qb_intent.gbnf.
# Any grammar change requires an equivalent change here.

_HEX = r"[0-9a-fA-F]"
_UUID_STR = rf'"{_HEX}{{8}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{12}}"'

_ACTION_SEG = r"[a-z][a-z0-9_]*"
_ACTION = rf'"{_ACTION_SEG}(?:\.{_ACTION_SEG})+"'

# target-char := safe-char | escape
# safe-char   := [^"\\;&|`$<>\x00-\x1f]
# escape      := \\ ( ["\\/bfnrt] | "u" hex{4} )
_SAFE_CHAR = r'[^"\\;&|`$<>\x00-\x1f]'
_ESCAPE = rf'\\(?:["\\/bfnrt]|u{_HEX}{{4}})'
_TARGET_CHAR = rf'(?:{_SAFE_CHAR}|{_ESCAPE})'
_TARGET = rf'"{_TARGET_CHAR}*"'
_SAFE_STRING = _TARGET

_PARAM_KEY = r"[a-z][a-z0-9_]*"
_NUMBER = r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?"
# v6.9 Bug D (2026-07-17): mirror GBNF `scalar-array` branch — flat
# array of string | number | boolean (no null, no nesting). Keep in sync
# with qb_intent.gbnf::scalar-array + intent.json params oneOf array item.
_ARRAY_SCALAR = rf"(?:{_SAFE_STRING}|{_NUMBER}|true|false)"
_SCALAR_ARRAY = (
    rf"\[\s*\]|"
    rf"\[\s*{_ARRAY_SCALAR}(?:\s*,\s*{_ARRAY_SCALAR})*\s*\]"
)
_PARAM_VALUE = rf"(?:{_SAFE_STRING}|{_NUMBER}|true|false|null|(?:{_SCALAR_ARRAY}))"
_PARAMS_PAIR = rf'"{_PARAM_KEY}"\s*:\s*{_PARAM_VALUE}'
_PARAMS = (
    rf"\{{\s*\}}|"
    rf"\{{\s*{_PARAMS_PAIR}(?:\s*,\s*{_PARAMS_PAIR})*\s*\}}"
)

_REASON = r'"(?:user_requested|ai_autonomous|scheduled)"'
_RISK = r'"(?:read_only|low|medium|high|critical)"'
_SCHEMA_VERSION = r'"[0-9]+\.[0-9]+\.[0-9]+"'

_OPTIONAL_TAIL = (
    rf'(?:\s*,\s*"schema_version"\s*:\s*{_SCHEMA_VERSION})?'
    rf"(?:\s*,\s*\"timestamp\"\s*:\s*{_NUMBER})?"
)

_INTENT_RE = re.compile(
    rf"^\{{\s*"
    rf'"intent_id"\s*:\s*{_UUID_STR}\s*,\s*'
    rf'"action"\s*:\s*{_ACTION}\s*,\s*'
    rf'"target"\s*:\s*{_TARGET}\s*,\s*'
    rf'"params"\s*:\s*(?:{_PARAMS})\s*,\s*'
    rf'"reason"\s*:\s*{_REASON}\s*,\s*'
    rf'"risk_level"\s*:\s*{_RISK}'
    rf"{_OPTIONAL_TAIL}\s*\}}$",
    re.DOTALL,
)


def grammar_accepts(s: str) -> bool:
    """True iff the GBNF grammar (mirrored by _INTENT_RE) accepts s."""
    return _INTENT_RE.match(s) is not None


# ─── Builders ────────────────────────────────────────────────────────────


def _valid_intent_json(
    *,
    action: str = "system.disk",
    target: str = "",
    params: str = "{}",
    reason: str = "user_requested",
    risk_level: str = "read_only",
) -> str:
    return (
        f'{{"intent_id":"{uuid.uuid4()}","action":"{action}",'
        f'"target":"{target}","params":{params},"reason":"{reason}",'
        f'"risk_level":"{risk_level}"}}'
    )


# ─── 1: seed corpus of valid intents ─────────────────────────────────────


VALID_CORPUS = [
    _valid_intent_json(action="system.disk", target="", params="{}"),
    _valid_intent_json(action="system.uptime", target=""),
    _valid_intent_json(action="fs.read", target="/etc/hostname"),
    _valid_intent_json(action="fs.list", target="/var/log"),
    _valid_intent_json(action="process.list", target=""),
    _valid_intent_json(
        action="service.restart",
        target="nginx",
        params='{"unit":"nginx"}',
        risk_level="medium",
    ),
    _valid_intent_json(
        action="package.install",
        target="htop",
        params='{"package":"htop"}',
        risk_level="medium",
    ),
    _valid_intent_json(
        action="fs.write",
        target="/tmp/x.txt",
        params='{"content":"hello"}',
        risk_level="low",
    ),
    _valid_intent_json(
        action="fs.delete",
        target="/tmp/oldfile",
        risk_level="high",
    ),
    _valid_intent_json(action="network.dns.read", target="example.com"),
    _valid_intent_json(action="service.status", target="sshd"),
    _valid_intent_json(action="system.cpu", target=""),
    _valid_intent_json(action="system.memory", target=""),
    _valid_intent_json(action="package.query", target="bash"),
    _valid_intent_json(
        action="package.upgrade",
        target="curl",
        risk_level="medium",
    ),
    _valid_intent_json(
        action="fs.stat",
        target="/var/log/syslog",
    ),
    _valid_intent_json(
        action="process.inspect",
        target="",
        params='{"pid":1234}',
    ),
    _valid_intent_json(action="network.status", target=""),
    _valid_intent_json(action="network.interfaces", target=""),
    _valid_intent_json(
        action="service.logs",
        target="nginx",
        params='{"lines":100}',
    ),
    _valid_intent_json(
        action="system.status", target="", params='{"verbose":true}'
    ),
    _valid_intent_json(
        action="system.status", target="", params='{"json":false}'
    ),
    _valid_intent_json(
        action="system.status", target="", params='{"count":null}'
    ),
    _valid_intent_json(reason="ai_autonomous"),
    _valid_intent_json(reason="scheduled"),
    _valid_intent_json(risk_level="critical"),
    _valid_intent_json(risk_level="low"),
]


@pytest.mark.parametrize("payload", VALID_CORPUS)
def test_seed_corpus_valid_intents_accepted_by_both(payload):
    """Every hand-crafted valid intent must pass BOTH the grammar
    shadow regex AND the jsonschema validator."""
    assert grammar_accepts(payload), f"grammar rejected: {payload!r}"
    assert _schema_accepts(payload), f"schema rejected: {payload!r}"


# ─── 2: malformed corpus ────────────────────────────────────────────────


MALFORMED_CORPUS = [
    # missing required field
    '{"intent_id":"a4f6b8e1-2c3d-4e5f-9a8b-1c2d3e4f5a6b","action":"system.disk","target":"","params":{},"reason":"user_requested"}',
    # extra field
    '{"intent_id":"a4f6b8e1-2c3d-4e5f-9a8b-1c2d3e4f5a6b","action":"system.disk","target":"","params":{},"reason":"user_requested","risk_level":"read_only","stowaway":"x"}',
    # invalid risk_level
    _valid_intent_json(risk_level="extreme"),
    # invalid reason
    _valid_intent_json(reason="curiosity"),
    # action with shell metachar
    _valid_intent_json(action="system.disk;ls"),
    # action not lowercase
    _valid_intent_json(action="System.Disk"),
    # action no dot
    _valid_intent_json(action="systemdisk"),
    # target with shell metachar
    _valid_intent_json(target="; rm -rf /"),
    # target with backtick
    _valid_intent_json(target="`whoami`"),
    # target with C0 control (tab)
    _valid_intent_json(target="line1\tline2"),
    # intent_id not UUID-shaped
    '{"intent_id":"not-a-uuid","action":"system.disk","target":"","params":{},"reason":"user_requested","risk_level":"read_only"}',
    # params has nested object
    _valid_intent_json(params='{"x":{"nested":1}}'),
    # v6.9 Bug D (2026-07-17): flat scalar arrays MOVED to VALID corpus.
    # Both grammar (param-value gains scalar-array branch) and schema
    # (intent.json params.additionalProperties.oneOf gains array) accept
    # them. Nested arrays and array-of-objects still rejected below.
    # params has array of arrays (nested)
    _valid_intent_json(params='{"x":[[1,2],[3,4]]}'),
    # params has array of objects
    _valid_intent_json(params='{"x":[{"k":"v"}]}'),
]


@pytest.mark.parametrize("payload", MALFORMED_CORPUS)
def test_seed_corpus_malformed_rejected_by_both(payload):
    """Every hand-crafted malformed intent must be rejected by BOTH the
    grammar shadow regex AND the jsonschema validator."""
    schema_ok = _schema_accepts(payload)
    grammar_ok = grammar_accepts(payload)
    # Both should reject. (We allow grammar to reject more than schema,
    # but never the other way around — that's the parity gate.)
    assert not schema_ok, f"schema unexpectedly accepted: {payload!r}"
    assert not grammar_ok, f"grammar unexpectedly accepted: {payload!r}"


# ─── 3: action pattern — metachars blocked ──────────────────────────────


@pytest.mark.parametrize(
    "bad_action",
    [
        "system.disk;",
        "system.disk|ls",
        "system.`backtick`",
        "system.$dollar",
        "system.disk\x00",
        "system.disk space",
        "0system.disk",
    ],
)
def test_action_pattern_metachar_blocked(bad_action):
    payload = _valid_intent_json(action=bad_action)
    assert not grammar_accepts(payload)
    assert not _schema_accepts(payload)


# ─── 4: target — C0 controls blocked ────────────────────────────────────


@pytest.mark.parametrize("c0_byte", [chr(c) for c in range(0, 32)])
def test_target_pattern_C0_blocked(c0_byte):
    """C0 control characters \\x00..\\x1f are forbidden in target."""
    # We must construct the JSON with the actual raw C0 byte in the string.
    # JSON-encode the target so the validator sees the same input the
    # grammar would. The grammar disallows raw C0 AND disallows escaped
    # C0 (no \\u00XX shortcut for control chars in the grammar's safe-char).
    if c0_byte == '"' or c0_byte == "\\":
        return  # already covered by JSON's own escaping requirements
    raw_payload = (
        '{"intent_id":"a4f6b8e1-2c3d-4e5f-9a8b-1c2d3e4f5a6b",'
        '"action":"system.disk",'
        f'"target":"abc{c0_byte}def",'
        '"params":{},"reason":"user_requested","risk_level":"read_only"}'
    )
    assert not grammar_accepts(raw_payload)
    assert not _schema_accepts(raw_payload)


# ─── 5: target — shell metachars blocked ────────────────────────────────


@pytest.mark.parametrize("ch", [";", "&", "|", "`", "$", "<", ">"])
def test_target_pattern_shell_metachar_blocked(ch):
    payload = _valid_intent_json(target=f"file{ch}name")
    assert not grammar_accepts(payload)
    assert not _schema_accepts(payload)


# ─── 6: risk_level enum exhaustive ──────────────────────────────────────


@pytest.mark.parametrize(
    "level",
    ["read_only", "low", "medium", "high", "critical"],
)
def test_risk_level_enum_accepts_all_five(level):
    payload = _valid_intent_json(risk_level=level)
    assert grammar_accepts(payload)
    assert _schema_accepts(payload)


@pytest.mark.parametrize(
    "level",
    ["READ_ONLY", "Read_Only", "danger", "trivial", "", "high "],
)
def test_risk_level_enum_rejects_others(level):
    payload = _valid_intent_json(risk_level=level)
    assert not grammar_accepts(payload)
    assert not _schema_accepts(payload)


# ─── 7: reason enum exhaustive ──────────────────────────────────────────


@pytest.mark.parametrize(
    "r",
    ["user_requested", "ai_autonomous", "scheduled"],
)
def test_reason_enum_accepts_all_three(r):
    payload = _valid_intent_json(reason=r)
    assert grammar_accepts(payload)
    assert _schema_accepts(payload)


@pytest.mark.parametrize(
    "r",
    ["user", "manual", "user_request", "AI_AUTONOMOUS", ""],
)
def test_reason_enum_rejects_others(r):
    payload = _valid_intent_json(reason=r)
    assert not grammar_accepts(payload)
    assert not _schema_accepts(payload)


# ─── 8: params value types ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "params_json",
    [
        "{}",
        '{"k":"v"}',
        '{"n":1}',
        '{"n":-1.5}',
        '{"f":true}',
        '{"f":false}',
        '{"z":null}',
        '{"a":1,"b":2,"c":"three"}',
        # v6.9 Bug D (2026-07-17): flat scalar arrays accepted by both.
        '{"alternative_actions":["fs.list","system.status"]}',
        '{"tags":[1,2,3]}',
        '{"flags":[true,false]}',
        '{"empty":[]}',
    ],
)
def test_params_value_types_accepted(params_json):
    payload = _valid_intent_json(params=params_json)
    assert grammar_accepts(payload), f"grammar rejected: {payload!r}"
    assert _schema_accepts(payload), f"schema rejected: {payload!r}"


@pytest.mark.parametrize(
    "params_json",
    [
        '{"k":{"nested":1}}',
        # v6.9 Bug D: flat scalar arrays moved to accepted; nested arrays
        # and array-of-object still refused.
        '{"k":[[1,2]]}',
        '{"k":[{"nested":"v"}]}',
    ],
)
def test_params_nested_types_rejected(params_json):
    payload = _valid_intent_json(params=params_json)
    assert not grammar_accepts(payload)
    assert not _schema_accepts(payload)


# ─── 9: differential fuzz — grammar ⊆ schema ────────────────────────────


@given(payload=st.text(min_size=0, max_size=2000))
@settings(
    max_examples=10000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    deadline=None,
)
def test_grammar_subset_schema_fuzz_text(payload):
    """Random text: if the grammar accepts, the schema must too.
    The converse is not required — the schema is stricter on length.
    """
    if grammar_accepts(payload):
        assert _schema_accepts(payload), (
            f"PARITY VIOLATION: grammar accepts but schema rejects:\n"
            f"  {payload!r}"
        )


@given(payload=st.binary(min_size=0, max_size=2000))
@settings(
    max_examples=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    deadline=None,
)
def test_grammar_subset_schema_fuzz_bytes(payload):
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return
    if grammar_accepts(text):
        assert _schema_accepts(text)


# ─── 10: round-trip UUID format check ───────────────────────────────────


def test_grammar_accepts_real_uuids():
    """Every str(uuid.uuid4()) should pass the grammar's UUID rule."""
    for _ in range(100):
        u = str(uuid.uuid4())
        payload = (
            f'{{"intent_id":"{u}","action":"system.disk","target":"",'
            f'"params":{{}},"reason":"user_requested","risk_level":"read_only"}}'
        )
        assert grammar_accepts(payload)
        assert _schema_accepts(payload)


def test_grammar_rejects_malformed_uuid_shapes():
    """A few obvious bad UUIDs."""
    bad = [
        "00000000-0000-0000-0000-00000000000",   # too short
        "00000000-0000-0000-0000-0000000000000", # too long
        "00000000_0000_0000_0000_000000000000",  # wrong separator
        "zzzzzzzz-0000-0000-0000-000000000000",  # invalid hex
    ]
    for u in bad:
        payload = (
            f'{{"intent_id":"{u}","action":"system.disk","target":"",'
            f'"params":{{}},"reason":"user_requested","risk_level":"read_only"}}'
        )
        assert not grammar_accepts(payload), f"grammar accepted bad uuid: {u}"


# ─── 11: optional fields ────────────────────────────────────────────────


def test_grammar_accepts_with_optional_schema_version():
    payload = (
        '{"intent_id":"a4f6b8e1-2c3d-4e5f-9a8b-1c2d3e4f5a6b",'
        '"action":"system.disk","target":"","params":{},'
        '"reason":"user_requested","risk_level":"read_only",'
        '"schema_version":"1.0.0"}'
    )
    assert grammar_accepts(payload)
    assert _schema_accepts(payload)


def test_grammar_accepts_with_optional_timestamp():
    payload = (
        '{"intent_id":"a4f6b8e1-2c3d-4e5f-9a8b-1c2d3e4f5a6b",'
        '"action":"system.disk","target":"","params":{},'
        '"reason":"user_requested","risk_level":"read_only",'
        '"timestamp":1717800000.123}'
    )
    assert grammar_accepts(payload)
    assert _schema_accepts(payload)


def test_grammar_accepts_with_both_optionals():
    payload = (
        '{"intent_id":"a4f6b8e1-2c3d-4e5f-9a8b-1c2d3e4f5a6b",'
        '"action":"system.disk","target":"","params":{},'
        '"reason":"user_requested","risk_level":"read_only",'
        '"schema_version":"1.0.0","timestamp":1717800000}'
    )
    assert grammar_accepts(payload)
    assert _schema_accepts(payload)
