"""Tests for screenshot policy (S3) and action-effect verification (R2).

Drives ``RpaBridge._record_step`` / ``_classify_effect`` directly with a
stubbed screenshot capture, plus ``rpa.execute_workflow`` end-to-end on
the no-Robot-Framework path.
"""

from __future__ import annotations

import time

import pytest

from rpa_bridge.bridge import KeywordResult, RpaBridge

HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture
def bridge(tmp_path):
    return RpaBridge(scratch_dir=tmp_path)


def _stub_captures(monkeypatch, bridge, hashes):
    seq = iter(hashes)
    calls = []

    def _capture():
        calls.append(True)
        return next(seq)

    monkeypatch.setattr(bridge, "_capture_step_screenshot", _capture)
    return calls


def _collect_notifications(monkeypatch, bridge):
    notes = []
    monkeypatch.setattr(
        bridge, "_emit_notification",
        lambda method, params: notes.append((method, params)),
    )
    return notes


def _record(bridge, results, index, name, *, status="pass", policy="all"):
    now = time.monotonic()
    bridge._record_step(
        results,
        index=index, name=name, status=status, error="",
        kw_start=now, start_time=now,
        timeout_seconds=30.0, total=10,
        screenshot_policy=policy,
    )


class TestClassifyEffect:
    def test_first_step_is_unknown_then_changed(self, bridge, monkeypatch):
        _stub_captures(monkeypatch, bridge, [HASH_A, HASH_B])
        _collect_notifications(monkeypatch, bridge)
        results: list[KeywordResult] = []

        _record(bridge, results, 0, "Click Element")
        _record(bridge, results, 1, "Click Element")

        assert results[0].effect == "unknown"  # no previous hash yet
        assert results[1].effect == "changed"

    def test_unchanged_hash_flags_none_and_notifies(self, bridge, monkeypatch):
        _stub_captures(monkeypatch, bridge, [HASH_A, HASH_A])
        notes = _collect_notifications(monkeypatch, bridge)
        results: list[KeywordResult] = []

        _record(bridge, results, 0, "Click Element")
        _record(bridge, results, 1, "Click Element")

        assert results[1].effect == "none"
        no_effect = [n for n in notes if n[0] == "rpa.no_effect"]
        assert len(no_effect) == 1
        assert no_effect[0][1]["keyword_index"] == 1
        assert no_effect[0][1]["keyword_name"] == "Click Element"

    def test_read_only_keyword_is_unknown(self, bridge, monkeypatch):
        _stub_captures(monkeypatch, bridge, [HASH_A, HASH_A])
        notes = _collect_notifications(monkeypatch, bridge)
        results: list[KeywordResult] = []

        _record(bridge, results, 0, "Click Element")
        _record(bridge, results, 1, "Get Text")

        assert results[1].effect == "unknown"
        assert not [n for n in notes if n[0] == "rpa.no_effect"]

    def test_failed_keyword_is_unknown(self, bridge, monkeypatch):
        _stub_captures(monkeypatch, bridge, [HASH_A, HASH_A])
        _collect_notifications(monkeypatch, bridge)
        results: list[KeywordResult] = []

        _record(bridge, results, 0, "Click Element")
        _record(bridge, results, 1, "Click Element", status="fail")

        assert results[1].effect == "unknown"

    def test_effect_never_alters_success(self, bridge, monkeypatch):
        """Advisory-only guard: a no-effect step still counts as pass."""
        _stub_captures(monkeypatch, bridge, [HASH_A, HASH_A])
        _collect_notifications(monkeypatch, bridge)
        results: list[KeywordResult] = []

        _record(bridge, results, 0, "Click Element")
        _record(bridge, results, 1, "Click Element")

        assert results[1].effect == "none"
        assert results[1].status == "pass"


class TestScreenshotPolicy:
    def test_policy_all_captures_every_step(self, bridge, monkeypatch):
        calls = _stub_captures(monkeypatch, bridge, [HASH_A, HASH_B])
        _collect_notifications(monkeypatch, bridge)
        results: list[KeywordResult] = []

        _record(bridge, results, 0, "Click Element", policy="all")
        _record(bridge, results, 1, "Get Text", policy="all")

        assert len(calls) == 2
        assert results[1].screenshot_hash == HASH_B

    def test_policy_state_changing_skips_read_only(self, bridge, monkeypatch):
        calls = _stub_captures(monkeypatch, bridge, [HASH_A])
        _collect_notifications(monkeypatch, bridge)
        results: list[KeywordResult] = []

        _record(bridge, results, 0, "Get Text", policy="state_changing")
        _record(bridge, results, 1, "Click Element", policy="state_changing")

        assert len(calls) == 1
        assert results[0].screenshot_hash == ""
        assert results[1].screenshot_hash == HASH_A

    def test_policy_none_never_captures(self, bridge, monkeypatch):
        calls = _stub_captures(monkeypatch, bridge, [])
        _collect_notifications(monkeypatch, bridge)
        results: list[KeywordResult] = []

        _record(bridge, results, 0, "Click Element", policy="none")

        assert calls == []
        assert results[0].screenshot_hash == ""
        assert results[0].effect == "unknown"


class TestExecuteWorkflowEndToEnd:
    """rpa.execute_workflow through handle_request (no-RF no-op path)."""

    def test_auto_wait_expands_executed_keywords(self, bridge, monkeypatch):
        monkeypatch.setattr(bridge, "_capture_step_screenshot", lambda: HASH_A)
        monkeypatch.setattr(bridge, "_emit_notification", lambda m, p: None)

        result = bridge.handle_request("rpa.execute_workflow", {
            "keywords": [{"name": "Click Element", "args": ["id=go"]}],
            "workflow_name": "wf_auto_wait",
            "auto_wait_seconds": 5.0,
        })

        assert result["success"] is True
        assert result["keywords_total"] == 2
        names = [kr["name"] for kr in result["keyword_results"]]
        assert names == ["Wait Until Element Is Visible", "Click Element"]
        assert all("effect" in kr for kr in result["keyword_results"])

    def test_default_has_no_auto_wait(self, bridge, monkeypatch):
        monkeypatch.setattr(bridge, "_capture_step_screenshot", lambda: HASH_A)
        monkeypatch.setattr(bridge, "_emit_notification", lambda m, p: None)

        result = bridge.handle_request("rpa.execute_workflow", {
            "keywords": [{"name": "Click Element", "args": ["id=go"]}],
            "workflow_name": "wf_plain",
        })

        assert result["keywords_total"] == 1

    def test_robot_audit_file_contains_inserted_waits(self, bridge, monkeypatch, tmp_path):
        monkeypatch.setattr(bridge, "_capture_step_screenshot", lambda: HASH_A)
        monkeypatch.setattr(bridge, "_emit_notification", lambda m, p: None)

        bridge.handle_request("rpa.execute_workflow", {
            "keywords": [{"name": "Click Element", "args": ["id=go"]}],
            "workflow_name": "wf_audit",
            "auto_wait_seconds": 5.0,
        })

        robot_file = tmp_path / "wf_audit.robot"
        content = robot_file.read_text()
        assert content.startswith("# auto-wait: inserted 1")
        assert "Wait Until Element Is Visible    id=go    5s" in content
