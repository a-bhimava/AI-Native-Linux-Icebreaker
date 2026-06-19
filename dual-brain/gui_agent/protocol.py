"""JSON-RPC protocol definitions for GUI Agent communication.

Method constants, parameter schemas (JSON Schema Draft 7), and
validation function. Reuses ``controller.protocol`` classes for
message framing — this module defines only GUI-specific semantics.

Security: all element selector fields are validated against a
metacharacter denylist (INV-2 extended to GUI operations).
"""

from __future__ import annotations

import re
from typing import Any

import jsonschema

GUI_PING = "gui.ping"
GUI_SCREENSHOT = "gui.screenshot"
GUI_FIND_ELEMENT = "gui.find_element"
GUI_GET_WINDOW_LIST = "gui.get_window_list"
GUI_GET_ELEMENT_TREE = "gui.get_element_tree"
GUI_CLICK = "gui.click"
GUI_TYPE = "gui.type"
GUI_SELECT = "gui.select"

GUI_READONLY_METHODS = frozenset({
    GUI_PING, GUI_SCREENSHOT, GUI_FIND_ELEMENT,
    GUI_GET_WINDOW_LIST, GUI_GET_ELEMENT_TREE,
})

GUI_WRITE_METHODS = frozenset({
    GUI_CLICK, GUI_TYPE, GUI_SELECT,
})

ALL_GUI_METHODS = GUI_READONLY_METHODS | GUI_WRITE_METHODS

_SAFE_STRING_PATTERN = r"^[^;&|`$<>\x00-\x1f]*$"

_SAFE_STRING_SCHEMA = {
    "type": "string",
    "pattern": _SAFE_STRING_PATTERN,
    "maxLength": 256,
}

_PARAM_SCHEMAS: dict[str, dict] = {
    GUI_PING: {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
    GUI_SCREENSHOT: {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "window": {**_SAFE_STRING_SCHEMA, "description": "Window title (empty = full screen)"},
        },
    },
    GUI_FIND_ELEMENT: {
        "type": "object",
        "required": ["window", "role", "name"],
        "additionalProperties": False,
        "properties": {
            "window": _SAFE_STRING_SCHEMA,
            "role": _SAFE_STRING_SCHEMA,
            "name": _SAFE_STRING_SCHEMA,
        },
    },
    GUI_GET_WINDOW_LIST: {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
    GUI_GET_ELEMENT_TREE: {
        "type": "object",
        "required": ["window"],
        "additionalProperties": False,
        "properties": {
            "window": _SAFE_STRING_SCHEMA,
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 10},
        },
    },
    GUI_CLICK: {
        "type": "object",
        "required": ["window", "role", "name"],
        "additionalProperties": False,
        "properties": {
            "window": _SAFE_STRING_SCHEMA,
            "role": _SAFE_STRING_SCHEMA,
            "name": _SAFE_STRING_SCHEMA,
        },
    },
    GUI_TYPE: {
        "type": "object",
        "required": ["window", "role", "name", "text"],
        "additionalProperties": False,
        "properties": {
            "window": _SAFE_STRING_SCHEMA,
            "role": _SAFE_STRING_SCHEMA,
            "name": _SAFE_STRING_SCHEMA,
            "text": {"type": "string", "maxLength": 4096},
        },
    },
    GUI_SELECT: {
        "type": "object",
        "required": ["window", "role", "name", "value"],
        "additionalProperties": False,
        "properties": {
            "window": _SAFE_STRING_SCHEMA,
            "role": _SAFE_STRING_SCHEMA,
            "name": _SAFE_STRING_SCHEMA,
            "value": _SAFE_STRING_SCHEMA,
        },
    },
}


def validate_gui_params(method: str, params: dict[str, Any]) -> None:
    """Validate GUI method params against schema. Raises ``jsonschema.ValidationError``."""
    schema = _PARAM_SCHEMAS.get(method)
    if schema is None:
        raise ValueError(f"unknown GUI method: {method!r}")
    jsonschema.validate(params, schema)
