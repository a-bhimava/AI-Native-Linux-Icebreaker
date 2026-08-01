"""Tests for controller.mcp_gui_server — F-101 Fix M iceui MCP server.

Covers the JSON-RPC protocol surface (initialize, tools/list, tools/call,
error paths) plus the subprocess-dispatch delegation. Mirrors the pattern
established by `test_agentgraph_audit_and_cot.py::TestGuiRpaDispatchInAgentGraph`
— monkeypatch subprocess.run so the test doesn't need pyatspi/uinput on
the CI host.
"""

from __future__ import annotations

import json

import pytest

from controller import mcp_gui_server as mcps


# ── protocol handshake ────────────────────────────────────────────────


def test_initialize_returns_correct_capabilities():
    req = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    })
    resp = mcps._handle_line(req)
    assert resp is not None
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == mcps.PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "iceui"
    assert result["capabilities"]["tools"]["listChanged"] is False


def test_notifications_initialized_returns_no_response():
    """Per JSON-RPC 2.0, notifications (no `id`) get no response."""
    req = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    resp = mcps._handle_line(req)
    assert resp is None


def test_unknown_notification_silently_dropped():
    req = json.dumps({"jsonrpc": "2.0", "method": "notifications/some_future_thing"})
    resp = mcps._handle_line(req)
    assert resp is None


def test_tools_list_returns_expected_tool_count_with_correct_shape():
    """v6.14 shipped 12 tools; Fix V.4 (v6.15) adds 12 vision-grounded
    tools → 24 total. The _EXPECTED_TOOL_COUNT constant is the single
    source of truth so this test can't drift silently."""
    req = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    resp = mcps._handle_line(req)
    assert resp is not None
    tools = resp["result"]["tools"]
    assert len(tools) == mcps._EXPECTED_TOOL_COUNT, \
        f"expected {mcps._EXPECTED_TOOL_COUNT} tools, got {[t['name'] for t in tools]}"
    for t in tools:
        assert "name" in t
        assert "description" in t
        assert "inputSchema" in t
        assert t["name"].startswith("gui.") or t["name"].startswith("rpa."), \
            f"unexpected tool namespace: {t['name']!r}"
    names = {t["name"] for t in tools}
    # Sanity: known-critical tools are present (v6.14 baseline + v6.15 Fix V).
    for expected in ("gui.get_window_list", "gui.screenshot",
                     "rpa.execute_workflow", "rpa.list_workflows",
                     "gui.parse_screen", "gui.click_at_coords",
                     "gui.grounded_click", "gui.grounded_drag",
                     "gui.press_key", "gui.key_sequence"):
        assert expected in names, f"missing tool {expected}"


def test_every_tool_has_a_description():
    """All 24 tools listed by mcp_gui_server MUST have a real
    description string — no `"GUI helper"` / `"RPA helper"` fallbacks
    from _build_tools_list. A missing description reads like a broken
    ISO in the opencode UI."""
    req = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    resp = mcps._handle_line(req)
    tools = resp["result"]["tools"]
    for t in tools:
        assert t["description"] not in ("GUI helper", "RPA helper"), \
            f"tool {t['name']!r} has fallback description — missing from _TOOL_DESCRIPTIONS"
        assert len(t["description"]) > 20, \
            f"tool {t['name']!r} description too short: {t['description']!r}"


def test_all_v_new_tools_have_description():
    """Regression guard: every V.4a-added constant must have a
    _TOOL_DESCRIPTIONS entry. If someone adds a new tool via schema
    but forgets the description, this test catches it before smoke gate."""
    v_tools = {
        "gui.parse_screen",
        "gui.click_at_coords", "gui.type_at_coords",
        "gui.drag", "gui.scroll", "gui.hover",
        "gui.press_key", "gui.key_sequence",
        "gui.grounded_click", "gui.grounded_type",
        "gui.grounded_drag", "gui.grounded_scroll",
    }
    for name in v_tools:
        assert name in mcps._TOOL_DESCRIPTIONS, \
            f"Fix V tool {name!r} missing from _TOOL_DESCRIPTIONS"


def test_unknown_method_returns_32601():
    req = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    resp = mcps._handle_line(req)
    assert resp is not None
    assert resp["error"]["code"] == mcps._METHOD_NOT_FOUND


def test_malformed_json_returns_32700():
    resp = mcps._handle_line("{not valid json")
    assert resp is not None
    assert resp["error"]["code"] == mcps._PARSE_ERROR


def test_wrong_jsonrpc_version_returns_32600():
    req = json.dumps({"jsonrpc": "1.0", "id": 4, "method": "initialize"})
    resp = mcps._handle_line(req)
    assert resp is not None
    assert resp["error"]["code"] == mcps._INVALID_REQUEST


# ── tools/call dispatch ───────────────────────────────────────────────


def _fake_dispatch_success(*, kind, tool_call, cfg, timeout_s):
    """Stand-in for _dispatch_in_subprocess — returns success envelope."""
    return {"ok": True, "result": {"echoed_tool": tool_call["tool"],
                                    "echoed_kind": kind,
                                    "echoed_params": tool_call["params"]}}


def _fake_dispatch_failure(*, kind, tool_call, cfg, timeout_s):
    return {"ok": False, "error": f"stub failure for {tool_call['tool']}"}


def _fake_dispatch_raise(*, kind, tool_call, cfg, timeout_s):
    raise RuntimeError("dispatcher blew up")


def test_tools_call_gui_dispatches_with_kind_gui(monkeypatch):
    from controller import agent_graph_nodes
    monkeypatch.setattr(agent_graph_nodes, "_dispatch_in_subprocess", _fake_dispatch_success)

    req = json.dumps({
        "jsonrpc": "2.0", "id": 10, "method": "tools/call",
        "params": {"name": "gui.ping", "arguments": {}},
    })
    resp = mcps._handle_line(req)
    assert resp is not None
    result = resp["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["echoed_tool"] == "gui.ping"
    assert payload["echoed_kind"] == "gui"


def test_tools_call_rpa_dispatches_with_kind_rpa(monkeypatch):
    from controller import agent_graph_nodes
    monkeypatch.setattr(agent_graph_nodes, "_dispatch_in_subprocess", _fake_dispatch_success)

    req = json.dumps({
        "jsonrpc": "2.0", "id": 11, "method": "tools/call",
        "params": {"name": "rpa.list_workflows", "arguments": {}},
    })
    resp = mcps._handle_line(req)
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["echoed_kind"] == "rpa"


def test_tools_call_dispatch_failure_reports_isError_true(monkeypatch):
    from controller import agent_graph_nodes
    monkeypatch.setattr(agent_graph_nodes, "_dispatch_in_subprocess", _fake_dispatch_failure)

    req = json.dumps({
        "jsonrpc": "2.0", "id": 12, "method": "tools/call",
        "params": {"name": "gui.click", "arguments": {"window": "w", "role": "button", "name": "OK"}},
    })
    resp = mcps._handle_line(req)
    result = resp["result"]
    assert result["isError"] is True
    assert "stub failure" in result["content"][0]["text"]


def test_tools_call_unknown_tool_returns_isError_true():
    req = json.dumps({
        "jsonrpc": "2.0", "id": 13, "method": "tools/call",
        "params": {"name": "not.a.real.tool", "arguments": {}},
    })
    resp = mcps._handle_line(req)
    result = resp["result"]
    assert result["isError"] is True
    assert "unknown tool" in result["content"][0]["text"].lower()


def test_tools_call_dispatch_raise_reports_isError_true(monkeypatch):
    """F-101 BP-10: server MUST NOT propagate exceptions to the caller."""
    from controller import agent_graph_nodes
    monkeypatch.setattr(agent_graph_nodes, "_dispatch_in_subprocess", _fake_dispatch_raise)

    req = json.dumps({
        "jsonrpc": "2.0", "id": 14, "method": "tools/call",
        "params": {"name": "gui.ping", "arguments": {}},
    })
    resp = mcps._handle_line(req)
    assert resp is not None
    # Handler should catch → return -32603 internal (per _handle_line's
    # exception guard), NOT let RuntimeError kill the process.
    assert resp["error"]["code"] == mcps._INTERNAL_ERROR


# ── self-test entry point ─────────────────────────────────────────────


def test_self_test_returns_zero_on_success(capsys):
    """--self-test path is used by smoke-gate — ensure it exits 0 with
    the expected _EXPECTED_TOOL_COUNT tools."""
    rc = mcps._self_test()
    assert rc == 0
    captured = capsys.readouterr()
    assert f"{mcps._EXPECTED_TOOL_COUNT} tools" in captured.err
