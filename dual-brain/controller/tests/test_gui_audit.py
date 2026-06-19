"""Tests for GUI audit fields and redaction (PR #26)."""

from __future__ import annotations

import pytest

from controller.audit import (
    AuditFields,
    Outcome,
    make_entry,
    sanitize_gui_field,
)


class TestGuiOutcomes:
    def test_gui_executed_outcome(self):
        assert Outcome.GUI_EXECUTED.value == "gui_executed"

    def test_gui_denied_outcome(self):
        assert Outcome.GUI_DENIED.value == "gui_denied"

    def test_gui_error_outcome(self):
        assert Outcome.GUI_ERROR.value == "gui_error"


class TestGuiAuditFields:
    def test_extra_gui_fields_in_entry(self):
        fields = AuditFields(
            session_id="s1", turn_index=0,
            intent_id="i1", action="gui.click",
            target="", tier=1, reason="user_requested",
            risk_level="low", outcome=Outcome.GUI_EXECUTED,
            duration_ms=50.0, backend="local", model="test",
            tokens_in=0, tokens_out=0, cost_estimate_usd=0.0,
            extra={
                "execution_tier": "gui",
                "element_role": "push-button",
                "element_name": "OK",
                "window_title": "Settings",
                "screenshot_before_hash": "abc123def456",
            },
        )
        entry = make_entry(fields)
        assert entry["execution_tier"] == "gui"
        assert entry["element_role"] == "push-button"
        assert entry["element_name"] == "OK"
        assert entry["window_title"] == "Settings"
        assert entry["screenshot_before_hash"] == "abc123def456"

    def test_gui_type_text_redacted(self):
        fields = AuditFields(
            session_id="s1", turn_index=0,
            intent_id="i1", action="gui.type",
            target="", tier=1, reason="user_requested",
            risk_level="low", outcome=Outcome.GUI_EXECUTED,
            duration_ms=50.0, backend="local", model="test",
            tokens_in=0, tokens_out=0, cost_estimate_usd=0.0,
            extra={
                "params": {"window": "App", "role": "text", "name": "input", "text": "hello"},
            },
        )
        entry = make_entry(fields)
        params = entry["params"]
        assert params["window"] == "App"
        assert params["role"] == "text"
        assert params["name"] == "input"


class TestSanitizeGuiField:
    def test_strips_ansi_escapes(self):
        assert sanitize_gui_field("\x1b[31mred\x1b[0m") == "red"

    def test_strips_control_chars(self):
        assert sanitize_gui_field("hello\x00world") == "helloworld"

    def test_replaces_newlines(self):
        assert sanitize_gui_field("line1\nline2") == "line1 line2"

    def test_truncates_long_strings(self):
        result = sanitize_gui_field("x" * 300)
        assert len(result) == 259  # 256 + "..."
        assert result.endswith("...")

    def test_non_string_coerced(self):
        assert sanitize_gui_field(42) == "42"


class TestGuiToolRedaction:
    def test_gui_click_params_allowlist(self):
        from controller.audit import _TOOL_FIELD_ALLOWLIST
        assert "gui.click" in _TOOL_FIELD_ALLOWLIST
        allowed = _TOOL_FIELD_ALLOWLIST["gui.click"]
        assert "window" in allowed
        assert "role" in allowed
        assert "name" in allowed

    def test_gui_type_params_allowlist(self):
        from controller.audit import _TOOL_FIELD_ALLOWLIST
        allowed = _TOOL_FIELD_ALLOWLIST["gui.type"]
        assert "window" in allowed
        assert "name" in allowed
