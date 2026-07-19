"""Tests for auto-wait execution wiring (R3) via ``rpa.execute_workflow``.

Drives ``handle_request`` end-to-end on the no-Robot-Framework path with
a stubbed screenshot capture.
"""

from __future__ import annotations

import pytest

from rpa_bridge.bridge import RpaBridge

HASH_A = "a" * 64


@pytest.fixture
def bridge(tmp_path):
    return RpaBridge(scratch_dir=tmp_path)


class TestExecuteWorkflowEndToEnd:
    """rpa.execute_workflow through handle_request (no-RF no-op path)."""

    def test_auto_wait_expands_executed_keywords(self, bridge, monkeypatch):
        monkeypatch.setattr(bridge, "_capture_step_screenshot", lambda: HASH_A)
        monkeypatch.setattr(bridge, "_emit_progress", lambda **kw: None)

        result = bridge.handle_request("rpa.execute_workflow", {
            "keywords": [{"name": "Click Element", "args": ["id=go"]}],
            "workflow_name": "wf_auto_wait",
            "auto_wait_seconds": 5.0,
        })

        assert result["success"] is True
        assert result["keywords_total"] == 2
        names = [kr["name"] for kr in result["keyword_results"]]
        assert names == ["Wait Until Element Is Visible", "Click Element"]

    def test_default_has_no_auto_wait(self, bridge, monkeypatch):
        monkeypatch.setattr(bridge, "_capture_step_screenshot", lambda: HASH_A)
        monkeypatch.setattr(bridge, "_emit_progress", lambda **kw: None)

        result = bridge.handle_request("rpa.execute_workflow", {
            "keywords": [{"name": "Click Element", "args": ["id=go"]}],
            "workflow_name": "wf_plain",
        })

        assert result["keywords_total"] == 1

    def test_robot_audit_file_contains_inserted_waits(self, bridge, monkeypatch, tmp_path):
        monkeypatch.setattr(bridge, "_capture_step_screenshot", lambda: HASH_A)
        monkeypatch.setattr(bridge, "_emit_progress", lambda **kw: None)

        bridge.handle_request("rpa.execute_workflow", {
            "keywords": [{"name": "Click Element", "args": ["id=go"]}],
            "workflow_name": "wf_audit",
            "auto_wait_seconds": 5.0,
        })

        robot_file = tmp_path / "wf_audit.robot"
        content = robot_file.read_text()
        assert content.startswith("# auto-wait: inserted 1")
        assert "Wait Until Element Is Visible    id=go    5s" in content
