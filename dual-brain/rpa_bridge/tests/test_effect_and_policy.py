"""Tests for screenshot policy (S3) and auto-wait wiring (R3).

Drives ``handle_request`` end-to-end on the no-Robot-Framework path with
a stubbed screenshot capture.
"""

from __future__ import annotations

import pytest

import time

from rpa_bridge.bridge import KeywordResult, RpaBridge

HASH_A = "a" * 64
HASH_B = "b" * 64


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


def _stub_captures(monkeypatch, bridge, hashes):
    seq = iter(hashes)
    calls = []

    def _capture():
        calls.append(True)
        return next(seq)

    monkeypatch.setattr(bridge, "_capture_step_screenshot", _capture)
    return calls


def _record(bridge, results, index, name, *, status="pass", policy="all"):
    now = time.monotonic()
    bridge._record_step(
        results,
        index=index, name=name, status=status, error="",
        kw_start=now, start_time=now,
        timeout_seconds=30.0, total=10,
        screenshot_policy=policy,
    )


class TestScreenshotPolicy:
    def test_policy_all_captures_every_step(self, bridge, monkeypatch):
        calls = _stub_captures(monkeypatch, bridge, [HASH_A, HASH_B])
        monkeypatch.setattr(bridge, "_emit_progress", lambda **kw: None)
        results: list[KeywordResult] = []

        _record(bridge, results, 0, "Click Element", policy="all")
        _record(bridge, results, 1, "Get Text", policy="all")

        assert len(calls) == 2
        assert results[1].screenshot_hash == HASH_B

    def test_policy_state_changing_skips_read_only(self, bridge, monkeypatch):
        calls = _stub_captures(monkeypatch, bridge, [HASH_A])
        monkeypatch.setattr(bridge, "_emit_progress", lambda **kw: None)
        results: list[KeywordResult] = []

        _record(bridge, results, 0, "Get Text", policy="state_changing")
        _record(bridge, results, 1, "Click Element", policy="state_changing")

        assert len(calls) == 1
        assert results[0].screenshot_hash == ""
        assert results[1].screenshot_hash == HASH_A

    def test_policy_none_never_captures(self, bridge, monkeypatch):
        calls = _stub_captures(monkeypatch, bridge, [])
        monkeypatch.setattr(bridge, "_emit_progress", lambda **kw: None)
        results: list[KeywordResult] = []

        _record(bridge, results, 0, "Click Element", policy="none")

        assert calls == []
        assert results[0].screenshot_hash == ""
