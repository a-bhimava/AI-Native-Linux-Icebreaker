"""
intent_schema.py — JSON-Schema validator for the Intent Object (INV-2).

The Intent Object is the ONLY thing the Quarantined Brain produces that
crosses toward the Privileged Brain path. The PB itself never sees the
Intent Object — it sees only the opaque UUID returned by intent_store.put().
But every Intent Object MUST pass this validator before classification,
storage, or any other downstream step.

Two layers protect against malformed intents reaching the security gates:

  1. (local QB backend only) GBNF grammar-constrains the QB's token
     sampling so invalid output is near-mathematically impossible.
  2. (this module — applies to ALL backends, including API providers)
     `jsonschema` validation against `schemas/intent.json`. This is the
     belt-and-suspenders gate and the safety floor when GBNF is not
     available (Anthropic / Gemini API backends — INV-2-pluggable).

The schema (`schemas/intent.json`) enforces:
  - 6 required fields: intent_id, action, target, params, reason, risk_level
  - 2 optional: schema_version, timestamp
  - additionalProperties: false (no smuggled fields)
  - action: must match `^[a-z][a-z0-9_]*\\.[a-z][a-z0-9_]*$` (module.method)
  - target: must NOT contain shell metacharacters or C0 control chars
  - params: values restricted to {string-with-metachar-denylist, number,
    bool, null}; nested objects and arrays are rejected
  - reason: enum of {user_requested, ai_autonomous, scheduled}
  - risk_level: enum of {read_only, low, medium, high, critical}
  - intent_id: uuid format

Failures surface as IntentValidationError, never bare jsonschema errors
or other exceptions. The orchestration layer (M2.12) routes a rejection
through audit (M2.3) with `outcome="schema_rejected"` and aborts the turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import jsonschema
from jsonschema import Draft7Validator, FormatChecker


# Truncation cap for `received` values in the error envelope.
# Adversarial payloads can be megabytes; keep audit-log lines bounded.
_RECEIVED_MAX_LEN = 200


# ── Exceptions ──────────────────────────────────────────────────────────────

class IntentValidationError(Exception):
    """A candidate intent failed schema validation.

    Attributes:
        field_path: JSON-pointer-ish path to the failing field
            (e.g. "/target", "/params/api_key"). "/" for whole-object failures.
        error_type: which schema validator failed ("required", "pattern",
            "type", "enum", "additionalProperties", "maxLength",
            "oneOf", "additionalProperties (params)", "not_a_dict",
            "internal").
        message: human-readable summary suitable for HITL display.
        received: the offending value, truncated to _RECEIVED_MAX_LEN
            characters. May be None if the failure was structural
            (e.g. candidate wasn't a dict).
    """

    def __init__(
        self,
        message: str,
        *,
        field_path: str = "/",
        error_type: str = "validation",
        received: Optional[Any] = None,
    ):
        super().__init__(message)
        self.message = message
        self.field_path = field_path
        self.error_type = error_type
        self.received = received

    def as_audit_fields(self) -> dict:
        """Compact form suitable for embedding in an audit entry."""
        return {
            "rejection_field": self.field_path,
            "rejection_type": self.error_type,
            "rejection_message": self.message,
        }


# ── Validated wrapper ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class ValidatedIntent:
    """An Intent Object that has passed schema validation.

    The orchestration layer should treat any direct dict access to the
    underlying intent dict as a smell — most downstream code (classifier,
    intent_store) takes the dict directly today, but new consumers should
    accept ValidatedIntent and read from it. Future refactors can tighten
    this without breaking the wire format.
    """

    intent: dict
    schema_version: str


# ── Validator ──────────────────────────────────────────────────────────────

class IntentSchema:
    """Loads `schemas/intent.json` once and validates candidate intents.

    Typical use:

        validator = IntentSchema()
        try:
            v = validator.validate(candidate_dict)
        except IntentValidationError as e:
            audit.write(... outcome="schema_rejected", **e.as_audit_fields() ...)
            return  # abort turn

    A module-level singleton `_default` is exposed via the module-level
    `validate()` function for callers that don't need a custom path.
    """

    # Default path: schemas/intent.json next to this module.
    DEFAULT_SCHEMA_PATH = Path(__file__).parent / "schemas" / "intent.json"

    # Intent Object schema version. The schemas/intent.json file's $id
    # references "v1"; we stamp matching mcpd schema_version semver here
    # so audit log + ValidatedIntent both report a consistent string.
    SCHEMA_VERSION = "1.0.0"

    def __init__(self, schema_path: Optional[Path] = None):
        self._path = (schema_path or self.DEFAULT_SCHEMA_PATH).resolve()
        with open(self._path, "r", encoding="utf-8") as f:
            self._raw_schema = json.load(f)
        # Draft7Validator matches the schema's "$schema" declaration.
        # Compile once, reuse — iter_errors is the hot path.
        #
        # format_checker is REQUIRED: by default Draft7Validator treats
        # "format" as documentation and skips it, which would let a
        # malformed intent_id slip through (the schema declares
        # format: "uuid"). The opaque-UUID flow that protects PB
        # context (INV-1) depends on this being enforced.
        self._validator = Draft7Validator(
            self._raw_schema,
            format_checker=FormatChecker(formats=["uuid"]),
        )

    @property
    def schema_path(self) -> Path:
        return self._path

    @property
    def raw_schema(self) -> dict:
        return self._raw_schema

    def validate(self, candidate: Any) -> ValidatedIntent:
        """Validate a candidate intent.

        Returns:
            ValidatedIntent wrapping the candidate dict and the schema
            version this validator was compiled against.

        Raises:
            IntentValidationError on any validation failure. NEVER raises
            another exception — the boundary defends against fuzz inputs
            that might trigger pathological jsonschema behaviour.
        """
        # Boundary: candidate MUST be a dict. JSON parsers can return
        # lists, strings, numbers, bools, None; we treat anything that's
        # not an object as a structural failure.
        if not isinstance(candidate, dict):
            raise IntentValidationError(
                f"Intent must be a JSON object, got {type(candidate).__name__}",
                field_path="/",
                error_type="not_a_dict",
                received=_truncate_for_log(candidate),
            )

        try:
            errors = sorted(
                self._validator.iter_errors(candidate),
                key=lambda e: (list(e.absolute_path), e.validator or ""),
            )
        except Exception as e:  # noqa: BLE001 — defend against jsonschema raising on weird input
            # jsonschema is well-behaved on JSON-decoded values, but the
            # 10 k hypothesis fuzz can produce edge cases (e.g. extreme
            # recursion via custom dict subclasses). Convert to our typed
            # error so the orchestration layer's handling stays uniform.
            raise IntentValidationError(
                f"validator raised unexpectedly: {type(e).__name__}: {e}",
                field_path="/",
                error_type="internal",
                received=_truncate_for_log(candidate),
            ) from e

        if errors:
            first = errors[0]
            raise IntentValidationError(
                first.message,
                field_path=_format_pointer(list(first.absolute_path)),
                error_type=str(first.validator or "validation"),
                received=_truncate_for_log(first.instance),
            )

        return ValidatedIntent(intent=candidate, schema_version=self.SCHEMA_VERSION)


# ── Module-level convenience ────────────────────────────────────────────────

_default: Optional[IntentSchema] = None


def _get_default() -> IntentSchema:
    global _default
    if _default is None:
        _default = IntentSchema()
    return _default


def validate(candidate: Any) -> ValidatedIntent:
    """Validate against the default schema (schemas/intent.json)."""
    return _get_default().validate(candidate)


# ── Helpers ────────────────────────────────────────────────────────────────

def _format_pointer(path_parts: list) -> str:
    """Build a JSON-pointer-ish path from jsonschema's absolute_path deque.

    Examples:
        []                 -> "/"
        ["target"]         -> "/target"
        ["params", "key"]  -> "/params/key"
    """
    if not path_parts:
        return "/"
    return "/" + "/".join(str(p) for p in path_parts)


def _truncate_for_log(value: Any) -> str:
    """Render `value` as a string, truncated for safe audit-log inclusion.

    Why: adversarial inputs can be megabytes (multi-paragraph injection
    payloads, base64'd binaries). We MUST NOT write the whole thing to
    the audit log — it would blow up disk and make grepping unusable.
    """
    try:
        s = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        s = repr(value)
    if len(s) > _RECEIVED_MAX_LEN:
        return s[:_RECEIVED_MAX_LEN] + f"…(+{len(s) - _RECEIVED_MAX_LEN} chars truncated)"
    return s
