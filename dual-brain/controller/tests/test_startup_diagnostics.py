"""Regression locks for controller daemon startup diagnostics (F-120)."""

from __future__ import annotations

import json

import controller.__main__ as controller_main


def test_startup_breadcrumb_records_stage_without_exception_message(
    tmp_path, monkeypatch,
):
    """BP-8: diagnostics name the failed stage but never copy secret text."""
    status = tmp_path / "controller-startup.json"
    monkeypatch.setattr(controller_main, "_DAEMON_STARTUP_STATUS", status)

    controller_main._record_daemon_startup(
        "spawn_mcpd", RuntimeError("provider token is definitely-not-a-secret"),
    )

    data = json.loads(status.read_text())
    assert data["stage"] == "spawn_mcpd"
    assert data["error_type"] == "RuntimeError"
    assert "definitely-not-a-secret" not in status.read_text()
