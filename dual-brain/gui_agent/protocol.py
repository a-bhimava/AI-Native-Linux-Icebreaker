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

# Fix V (v6.15): vision-grounded UI automation tools.
# All 12 land here so `mcp_gui_server.py::_build_tools_list()` and
# `gen-oc-config.sh` (which harvest `_PARAM_SCHEMAS`) auto-pick them up
# with no other file needing to change.
GUI_PARSE_SCREEN = "gui.parse_screen"
GUI_CLICK_AT_COORDS = "gui.click_at_coords"
GUI_TYPE_AT_COORDS = "gui.type_at_coords"
GUI_DRAG = "gui.drag"
GUI_SCROLL = "gui.scroll"
GUI_HOVER = "gui.hover"
GUI_PRESS_KEY = "gui.press_key"
GUI_KEY_SEQUENCE = "gui.key_sequence"
GUI_GROUNDED_CLICK = "gui.grounded_click"
GUI_GROUNDED_TYPE = "gui.grounded_type"
GUI_GROUNDED_DRAG = "gui.grounded_drag"
GUI_GROUNDED_SCROLL = "gui.grounded_scroll"

GUI_READONLY_METHODS = frozenset({
    GUI_PING, GUI_SCREENSHOT, GUI_FIND_ELEMENT,
    GUI_GET_WINDOW_LIST, GUI_GET_ELEMENT_TREE,
    # Fix V: parse_screen is read-only (screenshot + VLM parse, no
    # input synthesis); hover moves the cursor but produces no
    # persistent effect and is often needed for pre-inspection.
    GUI_PARSE_SCREEN, GUI_HOVER,
})

GUI_WRITE_METHODS = frozenset({
    GUI_CLICK, GUI_TYPE, GUI_SELECT,
    # Fix V write tools — all synthesize keyboard/mouse events.
    GUI_CLICK_AT_COORDS, GUI_TYPE_AT_COORDS, GUI_DRAG, GUI_SCROLL,
    GUI_PRESS_KEY, GUI_KEY_SEQUENCE,
    GUI_GROUNDED_CLICK, GUI_GROUNDED_TYPE,
    GUI_GROUNDED_DRAG, GUI_GROUNDED_SCROLL,
})

ALL_GUI_METHODS = GUI_READONLY_METHODS | GUI_WRITE_METHODS

# F-101.1 (2026-07-28): the original strict pattern banned shell
# metachars `; & | \` $ < >` on the assumption that these strings might
# reach a shell. In practice, GUI window/element names route through
# D-Bus (AT-SPI) — never a shell. The strict ban broke legitimate
# window titles like "OC | System uptime check" (opencode's own
# auto-generated titles use `|`) and menu items like "File & Edit",
# causing every screenshot of an opencode window to fail with a
# validation error. Loosening for UI strings — bans only C0 control
# characters (which would corrupt terminal / keystroke output). The
# strict pattern is preserved for filesystem path inputs
# (see _FS_SAFE_STRING_SCHEMA in rpa_bridge.protocol).
_SAFE_STRING_PATTERN = r"^[^\x00-\x1f]*$"

_SAFE_STRING_SCHEMA = {
    "type": "string",
    "pattern": _SAFE_STRING_PATTERN,
    "maxLength": 256,
}

# Fix V: shared schemas for coord-based tools. Coords are physical
# pixels as reported by the VLM; downstream input_synth.py translates
# through MonitorLayout before handing to xdotool.
_COORD_SCHEMA = {"type": "integer", "minimum": 0, "maximum": 32768}

_BUTTON_SCHEMA = {"type": "string", "enum": ["left", "right", "middle"]}

_SCROLL_DIRECTION_SCHEMA = {
    "type": "string",
    "enum": ["up", "down", "left", "right"],
}

# Key combo — matches _validate_key_combo() in input_synth.py. Kept
# permissive at schema layer (input_synth does the allowlist check);
# schema just caps length to prevent DoS-shaped input.
_KEY_COMBO_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 64,
                     "pattern": r"^[A-Za-z0-9+]+$"}

# Grounded-action prompt — natural language sent to the VLM to pick
# which parsed element the user meant. Same character allowlist as
# GUI selector strings.
_GROUNDED_PROMPT_SCHEMA = {**_SAFE_STRING_SCHEMA, "maxLength": 512}

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
    # ── Fix V (v6.15) — vision-grounded UI automation ──
    GUI_PARSE_SCREEN: {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "window": {**_SAFE_STRING_SCHEMA,
                       "description": "Window title to screenshot (empty = full screen)"},
            "prompt_hint": {**_GROUNDED_PROMPT_SCHEMA,
                            "description": "Optional hint to the VLM about what to look for"},
        },
    },
    GUI_CLICK_AT_COORDS: {
        "type": "object",
        "required": ["x", "y"],
        "additionalProperties": False,
        "properties": {
            "x": _COORD_SCHEMA,
            "y": _COORD_SCHEMA,
            "button": _BUTTON_SCHEMA,
            "count": {"type": "integer", "enum": [1, 2, 3]},
        },
    },
    GUI_TYPE_AT_COORDS: {
        "type": "object",
        "required": ["x", "y", "text"],
        "additionalProperties": False,
        "properties": {
            "x": _COORD_SCHEMA,
            "y": _COORD_SCHEMA,
            "text": {"type": "string", "maxLength": 4096},
            "delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
        },
    },
    GUI_DRAG: {
        "type": "object",
        "required": ["x1", "y1", "x2", "y2"],
        "additionalProperties": False,
        "properties": {
            "x1": _COORD_SCHEMA,
            "y1": _COORD_SCHEMA,
            "x2": _COORD_SCHEMA,
            "y2": _COORD_SCHEMA,
            "button": _BUTTON_SCHEMA,
            "hold_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
            "waypoints": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "array",
                    "minItems": 2, "maxItems": 2,
                    "items": _COORD_SCHEMA,
                },
            },
        },
    },
    GUI_SCROLL: {
        "type": "object",
        "required": ["x", "y", "direction", "amount"],
        "additionalProperties": False,
        "properties": {
            "x": _COORD_SCHEMA,
            "y": _COORD_SCHEMA,
            "direction": _SCROLL_DIRECTION_SCHEMA,
            "amount": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },
    GUI_HOVER: {
        "type": "object",
        "required": ["x", "y"],
        "additionalProperties": False,
        "properties": {
            "x": _COORD_SCHEMA,
            "y": _COORD_SCHEMA,
        },
    },
    GUI_PRESS_KEY: {
        "type": "object",
        "required": ["combo"],
        "additionalProperties": False,
        "properties": {
            "combo": _KEY_COMBO_SCHEMA,
        },
    },
    GUI_KEY_SEQUENCE: {
        "type": "object",
        "required": ["items"],
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 32,
                "items": {
                    # Each item is either a plain string OR a dict
                    # with {type: key|text, ...} — matches
                    # input_synth.key_sequence() flexible input.
                    "oneOf": [
                        {"type": "string", "minLength": 1, "maxLength": 4096},
                        {
                            "type": "object",
                            "required": ["type"],
                            "additionalProperties": False,
                            "properties": {
                                "type": {"type": "string", "enum": ["key", "text"]},
                                "combo": _KEY_COMBO_SCHEMA,
                                "text": {"type": "string", "maxLength": 4096},
                            },
                        },
                    ],
                },
            },
        },
    },
    GUI_GROUNDED_CLICK: {
        "type": "object",
        "required": ["window", "prompt"],
        "additionalProperties": False,
        "properties": {
            "window": _SAFE_STRING_SCHEMA,
            "prompt": _GROUNDED_PROMPT_SCHEMA,
            "button": _BUTTON_SCHEMA,
        },
    },
    GUI_GROUNDED_TYPE: {
        "type": "object",
        "required": ["window", "prompt", "text"],
        "additionalProperties": False,
        "properties": {
            "window": _SAFE_STRING_SCHEMA,
            "prompt": _GROUNDED_PROMPT_SCHEMA,
            "text": {"type": "string", "maxLength": 4096},
        },
    },
    GUI_GROUNDED_DRAG: {
        "type": "object",
        "required": ["window", "source_prompt", "target_prompt"],
        "additionalProperties": False,
        "properties": {
            "window": _SAFE_STRING_SCHEMA,
            "source_prompt": _GROUNDED_PROMPT_SCHEMA,
            "target_prompt": _GROUNDED_PROMPT_SCHEMA,
        },
    },
    GUI_GROUNDED_SCROLL: {
        "type": "object",
        "required": ["window", "prompt", "direction"],
        "additionalProperties": False,
        "properties": {
            "window": _SAFE_STRING_SCHEMA,
            "prompt": _GROUNDED_PROMPT_SCHEMA,
            "direction": _SCROLL_DIRECTION_SCHEMA,
            "amount": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },
}


def validate_gui_params(method: str, params: dict[str, Any]) -> None:
    """Validate GUI method params against schema. Raises ``jsonschema.ValidationError``."""
    schema = _PARAM_SCHEMAS.get(method)
    if schema is None:
        raise ValueError(f"unknown GUI method: {method!r}")
    jsonschema.validate(params, schema)
