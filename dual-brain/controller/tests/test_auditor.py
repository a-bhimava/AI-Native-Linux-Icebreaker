"""Tests for ``backends.auditor`` — outbound payload G3 defense (D17 stage 2).

The auditor scans every outbound LLM payload immediately before SDK
transmission. Any tool/function/grounding key at any depth → raise.
"""

from __future__ import annotations

import pytest

from controller.backends import BrainSecurityError, OutboundPayloadAuditor


# ── 17: top-level tool key blocked ───────────────────────────────────────────


def test_auditor_blocks_top_level_tools_key():
    a = OutboundPayloadAuditor()
    with pytest.raises(BrainSecurityError, match="tools"):
        a.intercept({"messages": [{"role": "user", "content": "x"}], "tools": []})


# ── 18: nested tool key blocked ──────────────────────────────────────────────


def test_auditor_blocks_nested_tools_key():
    a = OutboundPayloadAuditor()
    payload = {
        "messages": [{"role": "user", "content": [{"tool_calls": []}]}],
    }
    with pytest.raises(BrainSecurityError, match="tool_calls"):
        a.intercept(payload)


# ── 19: Gemini grounding blocked ─────────────────────────────────────────────


def test_auditor_blocks_gemini_grounding():
    a = OutboundPayloadAuditor()
    payload = {
        "contents": [{"parts": [{"text": "hi"}]}],
        "tools": [{"google_search_retrieval": {}}],
    }
    with pytest.raises(BrainSecurityError):
        a.intercept(payload)


def test_auditor_blocks_bare_google_search_key():
    a = OutboundPayloadAuditor()
    with pytest.raises(BrainSecurityError, match="google_search"):
        a.intercept({"contents": [], "google_search": True})


# ── 20: Anthropic tool_use blocked ───────────────────────────────────────────


def test_auditor_blocks_anthropic_tool_use():
    a = OutboundPayloadAuditor()
    payload = {
        "model": "claude-haiku-4-5",
        "messages": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "x"}]},
        ],
    }
    # tool_use appears as a value of "type" → that's fine; only the KEY matters.
    # But if there's a "tool_use" key, that's the bad shape:
    a.intercept(payload)  # should succeed: tool_use is a value here

    with pytest.raises(BrainSecurityError):
        a.intercept({"tool_use": {"id": "x"}})


# ── 21: clean payload allowed ────────────────────────────────────────────────


def test_auditor_allows_clean_payload():
    a = OutboundPayloadAuditor()
    payload = {
        "model": "claude-haiku-4-5",
        "max_tokens": 1024,
        "system": "you are a helper",
        "messages": [{"role": "user", "content": "hello"}],
    }
    a.intercept(payload)  # no raise
    assert a.call_count == 1


# ── 22: call_count increments ────────────────────────────────────────────────


def test_auditor_call_count_increments():
    a = OutboundPayloadAuditor()
    a.intercept({"x": 1})
    a.intercept({"y": 2})
    a.intercept({"z": 3})
    assert a.call_count == 3


# ── 23: probe pattern in base class (mirrored here for unit isolation) ──────


def test_auditor_call_count_starts_at_zero():
    a = OutboundPayloadAuditor()
    assert a.call_count == 0


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_auditor_handles_empty_payload():
    a = OutboundPayloadAuditor()
    a.intercept({})
    assert a.call_count == 1


def test_auditor_handles_lists_at_root():
    a = OutboundPayloadAuditor()
    a.intercept([{"x": 1}, {"y": 2}])


def test_auditor_blocks_function_call_field():
    a = OutboundPayloadAuditor()
    with pytest.raises(BrainSecurityError, match="function_call"):
        a.intercept({"messages": [{"role": "user", "function_call": {}}]})


def test_auditor_blocks_retrieval_key():
    a = OutboundPayloadAuditor()
    with pytest.raises(BrainSecurityError, match="retrieval"):
        a.intercept({"config": {"retrieval": True}})


def test_auditor_blocks_code_interpreter():
    a = OutboundPayloadAuditor()
    with pytest.raises(BrainSecurityError, match="code_interpreter"):
        a.intercept({"tools_config": {"code_interpreter": {"enabled": True}}})


def test_auditor_path_reports_depth():
    a = OutboundPayloadAuditor()
    try:
        a.intercept({"a": {"b": {"c": {"tools": []}}}})
    except BrainSecurityError as e:
        assert "/a/b/c" in str(e)
