"""Tests for PR #29 — RPA escalation, QB-monitored execution, config, audit."""

from __future__ import annotations

import typing

import pytest

from controller._mcpd_tools import (
    ALL_RPA_TOOLS,
    RPA_READONLY_TOOLS,
    RPA_WRITE_TOOLS,
)
from controller.audit import Outcome
from controller.config import RpaConfig
from controller.hitl import HitlDisplayData
from controller.main import Controller
from controller.risk_classifier import Tier, classify
from controller.trust_store import TrustStore
from controller.turn_events import RpaEvent, TurnEvent


# ── Layer 1: Data + constants ─────────────────────────────────────────────


class TestRpaToolConstants:
    def test_rpa_readonly_tools_are_frozenset(self):
        assert isinstance(RPA_READONLY_TOOLS, frozenset)
        assert "rpa.ping" in RPA_READONLY_TOOLS
        assert "rpa.find_by_image" in RPA_READONLY_TOOLS
        assert "rpa.list_workflows" in RPA_READONLY_TOOLS

    def test_rpa_write_tools_are_frozenset(self):
        assert isinstance(RPA_WRITE_TOOLS, frozenset)
        assert "rpa.execute_workflow" in RPA_WRITE_TOOLS

    def test_all_rpa_tools_is_union(self):
        assert ALL_RPA_TOOLS == RPA_READONLY_TOOLS | RPA_WRITE_TOOLS


class TestRpaEvent:
    def test_valid_phases(self):
        for phase in ("preview", "executing", "step", "paused", "complete"):
            event = RpaEvent(phase=phase, workflow_name="test")
            assert event.phase == phase

    def test_invalid_phase_raises(self):
        with pytest.raises(ValueError, match="phase must be one of"):
            RpaEvent(phase="bad_phase", workflow_name="test")

    def test_rpa_event_in_turn_event_union(self):
        args = typing.get_args(TurnEvent)
        assert RpaEvent in args

    def test_defaults(self):
        event = RpaEvent(phase="preview", workflow_name="wf")
        assert event.keyword_index == 0
        assert event.keyword_total == 0
        assert event.qb_on_track is True
        assert event.qb_concern == ""
        assert event.error == ""


class TestRpaConfig:
    def test_defaults(self):
        cfg = RpaConfig()
        # v6.10 P2 (F-69): flipped to True. See test_config.py
        # test_rpa_config_default_enabled for the anti-hide guard +
        # rationale. Sandbox is safe-by-design so shipping off-by-default
        # hid the whole Phase 6T RPA milestone with no security benefit.
        assert cfg.enabled is True
        assert cfg.timeout_seconds == 30
        assert cfg.max_keywords_per_workflow == 20
        assert cfg.screenshot_every_step is True

    def test_custom_values(self):
        cfg = RpaConfig(enabled=True, timeout_seconds=60, max_keywords_per_workflow=10)
        assert cfg.enabled is True
        assert cfg.timeout_seconds == 60
        assert cfg.max_keywords_per_workflow == 10


# ── Layer 2: Security gates ──────────────────────────────────────────────


class TestRpaRiskClassification:
    def test_rpa_execute_classified_high(self):
        result = classify({"action": "rpa.execute_workflow", "target": "", "risk_level": ""})
        assert result.tier == Tier.HIGH
        assert result.requires_hitl is True
        assert result.reversible is False

    def test_rpa_ping_classified_readonly(self):
        result = classify({"action": "rpa.ping", "target": "", "risk_level": ""})
        assert result.tier == Tier.READ_ONLY

    def test_rpa_find_by_image_classified_readonly(self):
        result = classify({"action": "rpa.find_by_image", "target": "", "risk_level": ""})
        assert result.tier == Tier.READ_ONLY

    def test_rpa_list_workflows_classified_readonly(self):
        result = classify({"action": "rpa.list_workflows", "target": "", "risk_level": ""})
        assert result.tier == Tier.READ_ONLY


class TestRpaTrustStore:
    def test_trust_store_rejects_rpa_write(self):
        store = TrustStore()
        with pytest.raises(ValueError, match="RPA write operation"):
            store.grant(
                action="rpa.execute_workflow",
                target_prefix="",
                max_tier=Tier.LOW,
                session_id="test-session",
                ttl_seconds=300,
            )

    def test_trust_store_allows_rpa_readonly(self):
        store = TrustStore()
        grant = store.grant(
            action="rpa.ping",
            target_prefix="",
            max_tier=Tier.READ_ONLY,
            session_id="test-session",
            ttl_seconds=300,
        )
        assert grant.action == "rpa.ping"


class TestRpaAuditOutcomes:
    def test_rpa_outcomes_exist(self):
        assert Outcome.RPA_EXECUTED == "rpa_executed"
        assert Outcome.RPA_DENIED == "rpa_denied"
        assert Outcome.RPA_ERROR == "rpa_error"
        assert Outcome.RPA_TIMEOUT == "rpa_timeout"
        assert Outcome.RPA_QB_PAUSED == "rpa_qb_paused"

    def test_rpa_outcomes_are_strings(self):
        for outcome in (
            Outcome.RPA_EXECUTED, Outcome.RPA_DENIED,
            Outcome.RPA_ERROR, Outcome.RPA_TIMEOUT,
            Outcome.RPA_QB_PAUSED,
        ):
            assert isinstance(outcome, str)


# ── Layer 3: HITL display ────────────────────────────────────────────────


class TestHitlRpaPreview:
    def test_hitl_display_data_accepts_rpa_preview(self):
        data = HitlDisplayData(
            action="rpa.execute_workflow",
            target="",
            tier=Tier.HIGH,
            risk_level="high",
            reversible=False,
            backend="local",
            reason="RPA workflow",
            blocked_pattern=None,
            cow_summary=None,
            rpa_keyword_preview=(
                "Click Element  #submit-btn",
                "Input Text  #name  Alice",
                "Click Element  #save",
            ),
        )
        assert len(data.rpa_keyword_preview) == 3
        assert "Click Element" in data.rpa_keyword_preview[0]

    def test_hitl_display_data_defaults_empty_preview(self):
        data = HitlDisplayData(
            action="fs.read",
            target="/tmp/test",
            tier=Tier.READ_ONLY,
            risk_level="read_only",
            reversible=True,
            backend="local",
            reason="test",
            blocked_pattern=None,
            cow_summary=None,
        )
        assert data.rpa_keyword_preview == ()

    def test_rpa_preview_sanitized(self):
        data = HitlDisplayData(
            action="rpa.execute_workflow",
            target="",
            tier=Tier.HIGH,
            risk_level="high",
            reversible=False,
            backend="local",
            reason="test",
            blocked_pattern=None,
            cow_summary=None,
            rpa_keyword_preview=(
                "Click Element  \x1b[31m#evil\x1b[0m",
            ),
        )
        assert "\x1b" not in data.rpa_keyword_preview[0]


# ── Gap fixes: escalation, translator, streaming ─────────────────────────


class TestGuiActionToRpaKeywords:
    def test_click_produces_click_element(self):
        kws = Controller._gui_action_to_rpa_keywords("gui.click", {
            "window": "Firefox", "role": "push button", "name": "Submit",
        })
        names = [kw["name"] for kw in kws]
        assert "Click Element" in names
        assert any("Submit" in " ".join(kw.get("args", [])) for kw in kws)

    def test_type_produces_input_text(self):
        kws = Controller._gui_action_to_rpa_keywords("gui.type", {
            "window": "Firefox", "role": "text", "name": "username",
            "text": "alice",
        })
        names = [kw["name"] for kw in kws]
        assert "Input Text" in names
        input_kw = next(kw for kw in kws if kw["name"] == "Input Text")
        assert "alice" in input_kw["args"]

    def test_select_produces_select_from_list(self):
        kws = Controller._gui_action_to_rpa_keywords("gui.select", {
            "window": "App", "role": "combo box", "name": "country",
            "value": "US",
        })
        names = [kw["name"] for kw in kws]
        assert "Select From List By Value" in names

    def test_includes_wait_when_window_specified(self):
        kws = Controller._gui_action_to_rpa_keywords("gui.click", {
            "window": "Firefox", "role": "button", "name": "OK",
        })
        assert kws[0]["name"] == "Wait Until Page Contains Element"

    def test_no_wait_when_no_window(self):
        kws = Controller._gui_action_to_rpa_keywords("gui.click", {
            "role": "button", "name": "OK",
        })
        assert kws[0]["name"] == "Click Element"

    def test_unknown_action_falls_back_to_click(self):
        kws = Controller._gui_action_to_rpa_keywords("gui.unknown", {
            "name": "element",
        })
        assert kws[0]["name"] == "Click Element"

    def test_locator_uses_name_when_available(self):
        kws = Controller._gui_action_to_rpa_keywords("gui.click", {
            "name": "Submit",
        })
        assert kws[0]["args"][0] == "name=Submit"

    def test_locator_falls_back_to_role(self):
        kws = Controller._gui_action_to_rpa_keywords("gui.click", {
            "role": "push button",
        })
        assert kws[0]["args"][0] == "role=push button"


class TestGuiAgentReasonCodes:
    """Verify GUI Agent handlers return reason codes for escalation."""

    def test_click_handler_error_format(self):
        from gui_agent.agent import GuiAgent
        from unittest.mock import MagicMock, patch
        from gui_agent.atspi import ElementNotFoundError

        agent = GuiAgent.__new__(GuiAgent)
        agent._prefer_app_api = False
        agent._atspi = MagicMock()
        agent._atspi.find_element.side_effect = ElementNotFoundError(
            "no a11y tree", reason="no_a11y_tree",
        )
        result = agent._handle_click({
            "window": "LegacyApp", "role": "button", "name": "Save",
        })
        assert result["success"] is False
        assert result["reason"] == "no_a11y_tree"

    def test_type_handler_error_format(self):
        from gui_agent.agent import GuiAgent
        from unittest.mock import MagicMock
        from gui_agent.atspi import AtSpiUnavailableError

        agent = GuiAgent.__new__(GuiAgent)
        agent._prefer_app_api = False
        agent._atspi = MagicMock()
        agent._atspi.find_element.side_effect = AtSpiUnavailableError("no AT-SPI")
        result = agent._handle_type({
            "window": "App", "role": "text", "name": "field", "text": "hello",
        })
        assert result["success"] is False
        assert result["reason"] == "atspi_unavailable"


class TestEscalatableReasons:
    """Verify the escalation reason set matches what GUI Agent can return."""

    def test_escalatable_reasons_are_subset_of_gui_reasons(self):
        escalatable = {"no_a11y_tree", "atspi_unavailable", "element_too_small"}
        gui_reasons = {
            "no_a11y_tree", "atspi_unavailable", "window_not_found",
            "wrong_role", "wrong_name", "multiple_matches", "element_too_small",
        }
        assert escalatable.issubset(gui_reasons)

    def test_window_not_found_is_not_escalatable(self):
        escalatable = {"no_a11y_tree", "atspi_unavailable", "element_too_small"}
        assert "window_not_found" not in escalatable
