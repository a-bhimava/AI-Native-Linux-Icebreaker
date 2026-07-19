"""Tests for GUI tool risk classification (PR #26)."""

from __future__ import annotations

import os

import pytest

from controller.risk_classifier import Tier, classify


class TestGuiReadonlyTier0:
    @pytest.mark.parametrize("action", [
        "gui.ping",
        "gui.screenshot",
        "gui.find_element",
        "gui.get_window_list",
        "gui.get_element_tree",
    ])
    def test_gui_readonly_is_tier0(self, action):
        result = classify({"action": action, "target": "", "risk_level": "read_only"})
        assert result.tier == Tier.READ_ONLY
        assert result.reversible is True
        assert not result.requires_hitl

    @pytest.mark.parametrize("action", [
        "gui.ping",
        "gui.screenshot",
    ])
    def test_gui_readonly_auto_execute(self, action):
        result = classify({"action": action, "target": "", "risk_level": "read_only"})
        assert result.auto_execute is True


class TestGuiWriteTier:
    @pytest.mark.parametrize("action", ["gui.click", "gui.type", "gui.select"])
    def test_gui_write_user_home_is_tier1(self, action, monkeypatch):
        # Pin HOME: under root, expanduser("~") is /root — a protected
        # path the classifier correctly tiers HIGH. Hermetic tests must
        # not depend on the invoking user.
        monkeypatch.setenv("HOME", "/home/testuser")
        result = classify({
            "action": action,
            "target": "/home/testuser/doc.txt",
            "risk_level": "low",
        })
        assert result.tier == Tier.LOW

    @pytest.mark.parametrize("action", ["gui.click", "gui.type", "gui.select"])
    def test_gui_write_system_is_tier2(self, action):
        result = classify({"action": action, "target": "/usr/share/app", "risk_level": "medium"})
        assert result.tier == Tier.MEDIUM

    def test_gui_write_critical_path_is_tier3(self):
        result = classify({"action": "gui.click", "target": "/boot/grub", "risk_level": "high"})
        assert result.tier == Tier.HIGH
        assert result.requires_hitl

    def test_explicit_critical_overrides_gui(self):
        result = classify({
            "action": "gui.screenshot",
            "target": "",
            "risk_level": "critical",
        })
        assert result.tier == Tier.HIGH


class TestGuiToolSets:
    def test_gui_tools_not_in_mcpd_all_tools(self):
        from controller._mcpd_tools import ALL_TOOLS, ALL_GUI_TOOLS
        assert ALL_TOOLS & ALL_GUI_TOOLS == frozenset()

    def test_gui_readonly_and_write_disjoint(self):
        from controller._mcpd_tools import GUI_READONLY_TOOLS, GUI_WRITE_TOOLS
        assert GUI_READONLY_TOOLS & GUI_WRITE_TOOLS == frozenset()

    def test_all_gui_tools_union(self):
        from controller._mcpd_tools import ALL_GUI_TOOLS, GUI_READONLY_TOOLS, GUI_WRITE_TOOLS
        assert ALL_GUI_TOOLS == GUI_READONLY_TOOLS | GUI_WRITE_TOOLS
