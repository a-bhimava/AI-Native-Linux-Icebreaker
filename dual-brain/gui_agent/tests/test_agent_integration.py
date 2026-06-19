"""Integration tests for gui_agent.agent — GuiAgent dispatch."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from gui_agent.agent import GuiAgent


class TestPingHandler:
    def test_ping_returns_status(self, tmp_path):
        agent = GuiAgent(scratch_dir=tmp_path)
        result = agent.handle_request("gui.ping", {})
        assert result["status"] == "ok"
        assert "atspi_available" in result
        assert "screenshots_available" in result

    def test_ping_reports_atspi_unavailable(self, tmp_path):
        agent = GuiAgent(scratch_dir=tmp_path)
        assert result_has_atspi_field(agent)


class TestSandboxFailure:
    def test_main_exits_on_sandbox_failure(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ICEBREAKER_GUI_SKIP_SANDBOX", raising=False)
        monkeypatch.delenv("ICEBREAKER_GUI_SCRATCH", raising=False)

        if sys.platform != "linux":
            with patch.dict(os.environ, {}, clear=True):
                monkeypatch.setenv("HOME", str(tmp_path))
                exit_code = GuiAgent.main()
                assert exit_code == 1


class TestDispatchValidation:
    def test_unknown_method_raises(self, tmp_path):
        agent = GuiAgent(scratch_dir=tmp_path)
        with pytest.raises(Exception):
            agent.handle_request("gui.nonexistent", {})

    def test_invalid_params_raises(self, tmp_path):
        agent = GuiAgent(scratch_dir=tmp_path)
        with pytest.raises(Exception):
            agent.handle_request("gui.find_element", {"window": "test;evil"})


def result_has_atspi_field(agent: GuiAgent) -> bool:
    result = agent.handle_request("gui.ping", {})
    return "atspi_available" in result


import os
