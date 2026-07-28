"""JSON-RPC protocol definitions for RPA Bridge communication.

Method constants, parameter schemas (JSON Schema Draft 7), and
validation function. Reuses ``controller.protocol`` classes for
message framing — this module defines only RPA-specific semantics.

Security: all keyword names and arguments are validated against a
metacharacter denylist (INV-2 extended to RPA operations).
"""

from __future__ import annotations

import re
from typing import Any

import jsonschema

RPA_PING = "rpa.ping"
RPA_EXECUTE_WORKFLOW = "rpa.execute_workflow"
RPA_FIND_BY_IMAGE = "rpa.find_by_image"
RPA_LIST_WORKFLOWS = "rpa.list_workflows"

RPA_READONLY_METHODS = frozenset({
    RPA_PING, RPA_LIST_WORKFLOWS, RPA_FIND_BY_IMAGE,
})

RPA_WRITE_METHODS = frozenset({
    RPA_EXECUTE_WORKFLOW,
})

ALL_RPA_METHODS = RPA_READONLY_METHODS | RPA_WRITE_METHODS

# F-101.1 (2026-07-28): loosen the default pattern to bans-only-controls
# because RPA UI content (Robot Framework keyword args, workflow_name)
# is passed to Robot's parser, not a shell. Kept the strict pattern for
# `template_path` (filesystem path) below via _FS_SAFE_STRING_SCHEMA.
# Matches the same-day loosening in gui_agent.protocol — see comment
# there for the failure mode this fixes (opencode session titles
# containing `|` etc. rejected by validation).
_SAFE_STRING_PATTERN = r"^[^\x00-\x1f]*$"

# Strict pattern retained for filesystem-path-shaped inputs where
# metachars could shell-interpolate if ever piped somewhere unexpected.
_FS_SAFE_STRING_PATTERN = r"^[^;&|`$<>\x00-\x1f]*$"

_SAFE_STRING_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": _SAFE_STRING_PATTERN,
    "maxLength": 256,
}

_FS_SAFE_STRING_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": _FS_SAFE_STRING_PATTERN,
    "maxLength": 256,
}

_KEYWORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name"],
    "additionalProperties": False,
    "properties": {
        "name": _SAFE_STRING_SCHEMA,
        "args": {
            "type": "array",
            "items": {"type": "string", "maxLength": 1024},
            "maxItems": 10,
        },
    },
}

_PARAM_SCHEMAS: dict[str, dict] = {
    RPA_PING: {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
    RPA_EXECUTE_WORKFLOW: {
        "type": "object",
        "required": ["keywords"],
        "additionalProperties": False,
        "properties": {
            "keywords": {
                "type": "array",
                "items": _KEYWORD_SCHEMA,
                "minItems": 1,
                "maxItems": 20,
            },
            "workflow_name": {
                **_SAFE_STRING_SCHEMA,
                "maxLength": 64,
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 5,
                "maximum": 120,
            },
            # Reliability: insert "Wait Until Element Is Visible <locator> <N>s"
            # before locator interactions (workflow_gen.insert_auto_waits).
            # 0 (the default) disables insertion — BP-2 safe/current path.
            "auto_wait_seconds": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 30.0,
            },
            # Screenshot capture policy per keyword step. "all" (default,
            # current behavior) captures after every keyword;
            # "state_changing" skips READ_ONLY_KEYWORDS; "none" disables.
            "screenshot_policy": {
                "type": "string",
                "enum": ["all", "state_changing", "none"],
            },
        },
    },
    RPA_FIND_BY_IMAGE: {
        "type": "object",
        "required": ["template_path"],
        "additionalProperties": False,
        "properties": {
            "template_path": _FS_SAFE_STRING_SCHEMA,
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "region": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "x": {"type": "integer", "minimum": 0},
                    "y": {"type": "integer", "minimum": 0},
                    "width": {"type": "integer", "minimum": 1},
                    "height": {"type": "integer", "minimum": 1},
                },
                "required": ["x", "y", "width", "height"],
            },
        },
    },
    RPA_LIST_WORKFLOWS: {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
}


def validate_rpa_params(method: str, params: dict[str, Any]) -> None:
    """Validate RPA method params against schema. Raises ``jsonschema.ValidationError``."""
    schema = _PARAM_SCHEMAS.get(method)
    if schema is None:
        raise ValueError(f"unknown RPA method: {method!r}")
    jsonschema.validate(params, schema)
