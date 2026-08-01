"""iceui — Model Context Protocol server exposing gui.* + rpa.* to opencode.

v6.14 F-101 (2026-07-27): OC edition previously had no way to reach GUI
enumeration, screenshots, click/type automation, or Robot-Framework
workflows because mcpd (Rust) has no gui.*/rpa.* tools and opencode
bypasses the Python controller entirely. This module ships those
capabilities as a SECOND stdio MCP server that opencode connects to
alongside `icebreaker` (mcpd). Tools appear namespaced under `iceui_`
(e.g. `iceui_gui.get_window_list`, `iceui_rpa.execute_workflow`).

## Design

- **Hand-rolled MCP protocol**: no `mcp` Python package in the venv;
  JSON-RPC 2.0 over stdin/stdout is ~150 LOC, mirroring
  `src/mcpd/src/server.rs`. Methods implemented: `initialize`,
  `notifications/initialized` (no-op accept), `tools/list`, `tools/call`.
- **Schema reuse**: tool inputSchema comes verbatim from
  `gui_agent.protocol._PARAM_SCHEMAS` +
  `rpa_bridge.protocol._PARAM_SCHEMAS`. No duplication.
- **Subprocess isolation**: `tools/call` delegates via
  `agent_graph_nodes._dispatch_in_subprocess` — pyatspi's GLib mainloop
  requirement (F-88) is satisfied by spawning a fresh Python interpreter
  per call.
- **Error mapping**: mirrors mcpd's codes — -32700 parse, -32601
  method-not-found, -32602 invalid-params (schema fail), -32603 internal.
  Tool errors return `{"content":[...], "isError": true}` NOT a JSON-RPC
  error — that's the MCP tools/call convention.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import Any

# Reuse the existing schemas — no rewriting.
from gui_agent.protocol import (
    _PARAM_SCHEMAS as _GUI_SCHEMAS,
    ALL_GUI_METHODS,
)
from rpa_bridge.protocol import _PARAM_SCHEMAS as _RPA_SCHEMAS

_log = logging.getLogger("controller.mcp_gui_server")

SERVER_NAME = "iceui"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"  # matches mcpd's advertised version

# JSON-RPC error codes (JSON-RPC 2.0 spec + mcpd conventions).
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

# GUI tool prefix (a bare `gui.` is unambiguous — the RpaBridge doesn't
# expose any `gui.*` methods and vice versa).
_ALL_TOOL_NAMES = tuple(sorted(_GUI_SCHEMAS.keys())) + tuple(sorted(_RPA_SCHEMAS.keys()))


def _tool_kind(name: str) -> str:
    if name.startswith("gui."):
        return "gui"
    if name.startswith("rpa."):
        return "rpa"
    return ""


def _build_tools_list() -> list[dict]:
    """Materialize the tools/list response payload from the schemas.

    Each entry: {name, description, inputSchema}. Descriptions come from
    a small hand-authored table below since gui_agent/rpa_bridge don't
    export prose descriptions — opencode displays these in its tool
    catalog UI so they should be useful.
    """
    tools: list[dict] = []
    for name in sorted(_GUI_SCHEMAS.keys()):
        tools.append({
            "name": name,
            "description": _TOOL_DESCRIPTIONS.get(name, "GUI helper"),
            "inputSchema": _GUI_SCHEMAS[name],
        })
    for name in sorted(_RPA_SCHEMAS.keys()):
        tools.append({
            "name": name,
            "description": _TOOL_DESCRIPTIONS.get(name, "RPA helper"),
            "inputSchema": _RPA_SCHEMAS[name],
        })
    return tools


_TOOL_DESCRIPTIONS: dict[str, str] = {
    "gui.ping": "Returns AT-SPI + screenshot subsystem availability.",
    "gui.screenshot": "Capture a PNG screenshot of a window; returns path + hash + dimensions.",
    "gui.find_element": "Find a single AT-SPI element by role + accessible name inside a window.",
    "gui.get_window_list": "Enumerate open windows: title, app name, PID, geometry.",
    "gui.get_element_tree": "Return the AT-SPI accessibility tree for a window (bounded depth).",
    "gui.click": "Click an AT-SPI element identified by (window, role, name).",
    "gui.type": "Type text into a focused AT-SPI element (max 4096 chars).",
    "gui.select": "Set the value of a selectable AT-SPI element (dropdown, checkbox, radio).",
    # ── Fix V (v6.15) vision-grounded UI automation ──
    "gui.parse_screen": (
        "Screenshot a window and call the vision model to return a "
        "structured list of every clickable element (buttons, inputs, "
        "links, icons) with pixel boxes + captions + kind + confidence. "
        "Writes an annotated preview PNG and fires a desktop notification "
        "so the user sees it before any grounded action."
    ),
    "gui.click_at_coords": (
        "Raw pixel click at (x, y). Button ∈ {left, right, middle}, "
        "count ∈ {1, 2, 3}. HiDPI-aware — coordinates are physical pixels "
        "as returned by the vision model."
    ),
    "gui.type_at_coords": (
        "Focus-click at (x, y) then type text (≤4096 chars) into the "
        "focused element. Combines a click and a type into one atomic call."
    ),
    "gui.drag": (
        "Mouse drag from (x1, y1) to (x2, y2) with optional intermediate "
        "waypoints for gesture-style drags. Configurable button and hold "
        "duration."
    ),
    "gui.scroll": (
        "Scroll ‘amount’ wheel clicks at (x, y). Direction ∈ "
        "{up, down, left, right}."
    ),
    "gui.hover": (
        "Move the mouse cursor to (x, y) without clicking. Useful for "
        "revealing tooltips or hover-state UI before deciding to click."
    ),
    "gui.press_key": (
        "Press one key or key combo, e.g. 'ctrl+s', 'escape', 'F12'. "
        "Combos are '+'-separated; each token is checked against an "
        "allowlist so shell-injection patterns are rejected structurally."
    ),
    "gui.key_sequence": (
        "Execute a mixed sequence of key combos and typed text atomically. "
        "Accepts a list of strings (heuristically classified) or explicit "
        "{type: 'key'|'text', ...} dicts. Stops at the first failure."
    ),
    "gui.grounded_click": (
        "Natural-language click: given a window and a description like "
        "'the Send button', screenshot + parse the window with the vision "
        "model, ask the language model to pick which element matches, "
        "then click its centroid. Emits an annotated preview PNG with the "
        "target highlighted before firing."
    ),
    "gui.grounded_type": (
        "Natural-language type: like grounded_click but after picking the "
        "element, click-to-focus and then type the given text into it. "
        "Best for form fields ('the URL bar', 'the search input')."
    ),
    "gui.grounded_drag": (
        "Natural-language drag: given a window plus source_prompt and "
        "target_prompt, parse once, pick BOTH endpoints in a single LLM "
        "call, then drag the source element's centroid to the target's."
    ),
    "gui.grounded_scroll": (
        "Natural-language scroll: pick a scroll region by description, "
        "then scroll it in the given direction by the given amount "
        "(default 3)."
    ),
    "rpa.ping": "Returns Robot Framework + /dev/uinput availability.",
    "rpa.execute_workflow": (
        "Run a Robot Framework workflow of allowlisted keywords; "
        "returns per-step timing + screenshots + effect verification."
    ),
    "rpa.find_by_image": "Locate a UI element by template image match; returns bbox + confidence.",
    "rpa.list_workflows": "List Robot Framework workflow files present in the scratch dir.",
}


def _make_error_response(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _make_result_response(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _handle_initialize(msg_id: Any, _params: dict) -> dict:
    return _make_result_response(msg_id, {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    })


def _handle_tools_list(msg_id: Any, _params: dict) -> dict:
    return _make_result_response(msg_id, {"tools": _build_tools_list()})


def _handle_tools_call(msg_id: Any, params: dict) -> dict:
    tool_name = params.get("name") or ""
    arguments = params.get("arguments") or {}

    kind = _tool_kind(tool_name)
    if kind not in ("gui", "rpa"):
        return _make_result_response(msg_id, {
            "content": [{"type": "text", "text": f"unknown tool: {tool_name!r}"}],
            "isError": True,
        })

    # Delegate to the existing subprocess dispatcher — same code path
    # the current-edition controller uses. Handles timeout, spawn
    # failure, non-zero exit, parse error. Never raises.
    from .agent_graph_nodes import _dispatch_in_subprocess
    envelope = _dispatch_in_subprocess(
        kind=kind,
        tool_call={"tool": tool_name, "params": arguments},
        cfg=None,  # no config threading for MVP; both agents accept None
        timeout_s=15.0,  # slightly longer than dispatcher's 10s to survive
                        # slow AT-SPI enumeration on cold Firefox windows
    )

    if envelope.get("ok"):
        payload_json = json.dumps(envelope.get("result", {}), separators=(",", ":"))
        return _make_result_response(msg_id, {
            "content": [{"type": "text", "text": payload_json}],
            "isError": False,
        })
    return _make_result_response(msg_id, {
        "content": [{"type": "text", "text": envelope.get("error", "unknown dispatch failure")}],
        "isError": True,
    })


_METHOD_HANDLERS = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
}


def _handle_line(line: str) -> dict | None:
    """Parse + route one JSON-RPC line. Returns response dict or None
    (for notifications that don't require a response)."""
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return _make_error_response(None, _PARSE_ERROR, "invalid JSON")
    if not isinstance(msg, dict):
        return _make_error_response(None, _INVALID_REQUEST, "message must be an object")
    if msg.get("jsonrpc") != "2.0":
        return _make_error_response(msg.get("id"), _INVALID_REQUEST, "jsonrpc must be '2.0'")

    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    # Notifications have no `id` — spec says the server MUST NOT respond.
    # `notifications/initialized` is the canonical one from the MCP
    # handshake; accept-and-ignore per the protocol.
    is_notification = "id" not in msg
    if is_notification:
        if method == "notifications/initialized":
            return None
        # Unknown notification: silently drop (per JSON-RPC 2.0 spec).
        return None

    handler = _METHOD_HANDLERS.get(method)
    if handler is None:
        return _make_error_response(msg_id, _METHOD_NOT_FOUND, f"method not found: {method!r}")

    try:
        return handler(msg_id, params)
    except Exception as exc:  # noqa: BLE001 — server MUST NOT crash on handler failure
        _log.exception("mcp_gui_server: handler %s raised", method)
        return _make_error_response(
            msg_id, _INTERNAL_ERROR,
            f"internal error: {type(exc).__name__}: {exc}",
        )


def _write_response(resp: dict) -> None:
    line = json.dumps(resp, separators=(",", ":")) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


_EXPECTED_TOOL_COUNT = 24
# v6.14 F-101 (Fix M) shipped 12 tools (8 gui.* AT-SPI + 4 rpa.*).
# v6.15 Fix V.4 adds 12 vision-grounded tools (parse_screen +
# click/type/drag/scroll/hover + press_key/key_sequence +
# grounded_click/type/drag/scroll) — total 24. Smoke-gate L3
# asserts this count; drift in either direction means a schema was
# added/removed without the count being updated.


def _self_test() -> int:
    """--self-test: verify the handshake round-trips + tools/list returns
    the expected 24 tools. Used by smoke-gate L3 assertion."""
    init_req = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "smoke-gate", "version": "0.0"},
        },
    })
    init_resp = _handle_line(init_req)
    if not init_resp or "result" not in init_resp:
        print(f"self-test FAILED: initialize returned {init_resp!r}", file=sys.stderr)
        return 1

    tl_req = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tl_resp = _handle_line(tl_req)
    if not tl_resp or "result" not in tl_resp:
        print(f"self-test FAILED: tools/list returned {tl_resp!r}", file=sys.stderr)
        return 1
    tools = tl_resp["result"].get("tools") or []
    if len(tools) != _EXPECTED_TOOL_COUNT:
        print(
            f"self-test FAILED: expected {_EXPECTED_TOOL_COUNT} tools, "
            f"got {len(tools)}: "
            f"{[t['name'] for t in tools]}",
            file=sys.stderr,
        )
        return 1
    print(f"self-test OK: initialize + tools/list ({len(tools)} tools)", file=sys.stderr)
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()

    # Blocking stdin read loop, one JSON message per line. opencode
    # keeps the connection open across many tool calls, so `for line in
    # sys.stdin` is the natural shape (blocks on EOF from opencode).
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            resp = _handle_line(line)
            if resp is not None:
                _write_response(resp)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    except Exception as exc:  # noqa: BLE001 — never propagate; log + exit clean
        _log.exception("mcp_gui_server: unhandled exception in main loop: %s", exc)
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
