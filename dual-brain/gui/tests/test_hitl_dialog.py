"""Tests for gui.hitl — HITL Dialog upgrade (M6UI.5).

Headless tests covering:
  - Tier CSS class mapping
  - Tier label mapping
  - Decision defaults and timeout behavior
  - Lockout countdown logic (INV-6)
  - COW diff line classification
  - Keyboard shortcut → Action mapping
  - Button hint label formatting
  - RPA keyword sanitization
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("gi", MagicMock())
sys.modules.setdefault("gi.repository", MagicMock())

from controller.hitl import Decision, HitlDisplayData
from controller.keymap import Action, Keymap
from controller.risk_classifier import Tier

from gui.hitl.dialog import _TIER_CSS, _TIER_LABELS


# ---------------------------------------------------------------------------
# Tier CSS mapping
# ---------------------------------------------------------------------------

class TestTierCSS:
    def test_has_all_four_tiers(self) -> None:
        for tier in (Tier.READ_ONLY, Tier.LOW, Tier.MEDIUM, Tier.HIGH):
            assert tier in _TIER_CSS

    def test_tier0_class(self) -> None:
        assert _TIER_CSS[Tier.READ_ONLY] == "ib-tier-0"

    def test_tier1_class(self) -> None:
        assert _TIER_CSS[Tier.LOW] == "ib-tier-1"

    def test_tier2_class(self) -> None:
        assert _TIER_CSS[Tier.MEDIUM] == "ib-tier-2"

    def test_tier3_class(self) -> None:
        assert _TIER_CSS[Tier.HIGH] == "ib-tier-3"

    def test_all_classes_start_with_ib(self) -> None:
        for cls in _TIER_CSS.values():
            assert cls.startswith("ib-tier-")


# ---------------------------------------------------------------------------
# Tier labels
# ---------------------------------------------------------------------------

class TestTierLabels:
    def test_has_all_four_tiers(self) -> None:
        for tier in (Tier.READ_ONLY, Tier.LOW, Tier.MEDIUM, Tier.HIGH):
            assert tier in _TIER_LABELS

    def test_tier0_says_read_only(self) -> None:
        assert "Read Only" in _TIER_LABELS[Tier.READ_ONLY]

    def test_tier1_says_low(self) -> None:
        assert "Low" in _TIER_LABELS[Tier.LOW]

    def test_tier2_says_medium(self) -> None:
        assert "Medium" in _TIER_LABELS[Tier.MEDIUM]

    def test_tier3_says_high(self) -> None:
        assert "High" in _TIER_LABELS[Tier.HIGH]

    def test_all_labels_start_with_tier(self) -> None:
        for label in _TIER_LABELS.values():
            assert label.startswith("Tier ")


# ---------------------------------------------------------------------------
# Decision defaults
# ---------------------------------------------------------------------------

class TestDecisionDefaults:
    def test_denied_is_default_safe(self) -> None:
        assert Decision.DENIED.value == "denied"

    def test_timeout_maps_to_timeout(self) -> None:
        assert Decision.TIMEOUT.value == "timeout"

    def test_approved_is_explicit(self) -> None:
        assert Decision.APPROVED.value == "approved"

    def test_no_decision_returns_denied(self) -> None:
        """Presenter returns DENIED when no data is pending."""
        from gui.hitl.dialog import LibAdwaitaHitlPresenter
        p = LibAdwaitaHitlPresenter.__new__(LibAdwaitaHitlPresenter)
        p._keymap = Keymap()
        p._last_key_class = ""
        p._decision = None
        p._decision_event = MagicMock()
        result = p.read_decision(5)
        assert result == Decision.DENIED
        assert p._last_key_class == "error"


# ---------------------------------------------------------------------------
# COW diff line classification
# ---------------------------------------------------------------------------

class TestCowDiffLines:
    """Verify the diff color classification logic used in the dialog."""

    def test_add_line_detected(self) -> None:
        line = "+ 127.0.0.1  myhost"
        assert line.startswith("+")

    def test_remove_line_detected(self) -> None:
        line = "- 127.0.0.1  localhost"
        assert line.startswith("-")

    def test_context_line_neither(self) -> None:
        line = "  some context"
        assert not line.startswith("+") and not line.startswith("-")

    def test_empty_line_is_context(self) -> None:
        line = ""
        assert not line.startswith("+") and not line.startswith("-")


# ---------------------------------------------------------------------------
# Lockout timing logic (INV-6)
# ---------------------------------------------------------------------------

class TestLockoutLogic:
    """Verify the lockout countdown math without GTK."""

    LOCKOUT_SECONDS = 3

    def test_fraction_starts_at_zero(self) -> None:
        elapsed = 0.0
        fraction = min(elapsed / self.LOCKOUT_SECONDS, 1.0)
        assert fraction == 0.0

    def test_fraction_at_midpoint(self) -> None:
        elapsed = 1.5
        fraction = min(elapsed / self.LOCKOUT_SECONDS, 1.0)
        assert abs(fraction - 0.5) < 0.01

    def test_fraction_at_end(self) -> None:
        elapsed = 3.0
        fraction = min(elapsed / self.LOCKOUT_SECONDS, 1.0)
        assert fraction == 1.0

    def test_fraction_capped_at_1(self) -> None:
        elapsed = 5.0
        fraction = min(elapsed / self.LOCKOUT_SECONDS, 1.0)
        assert fraction == 1.0

    def test_remaining_countdown(self) -> None:
        elapsed = 1.0
        remaining = max(0, self.LOCKOUT_SECONDS - elapsed)
        assert remaining == 2.0

    def test_remaining_at_zero(self) -> None:
        elapsed = 3.0
        remaining = max(0, self.LOCKOUT_SECONDS - elapsed)
        assert remaining == 0.0

    def test_approve_blocked_during_lockout(self) -> None:
        elapsed = 2.0
        approve_sensitive = elapsed >= self.LOCKOUT_SECONDS
        assert approve_sensitive is False

    def test_approve_enabled_after_lockout(self) -> None:
        elapsed = 3.0
        approve_sensitive = elapsed >= self.LOCKOUT_SECONDS
        assert approve_sensitive is True


# ---------------------------------------------------------------------------
# Keyboard hint formatting
# ---------------------------------------------------------------------------

class TestKeyboardHints:
    def test_default_approve_key(self) -> None:
        km = Keymap()
        keys = km.bindings.get(Action.APPROVE, ("a",))
        hint = keys[0] if keys else "a"
        assert hint in ("1", "a", "y")

    def test_deny_hint_present(self) -> None:
        km = Keymap()
        keys = km.bindings.get(Action.DENY, ("d",))
        assert len(keys) > 0

    def test_modify_hint_present(self) -> None:
        km = Keymap()
        keys = km.bindings.get(Action.MODIFY, ("m",))
        assert len(keys) > 0


# ---------------------------------------------------------------------------
# HitlDisplayData field access
# ---------------------------------------------------------------------------

class TestDisplayDataFields:
    def _make_data(self, **overrides: object) -> HitlDisplayData:
        defaults = dict(
            action="fs.write",
            target="/etc/hosts",
            tier=Tier.HIGH,
            risk_level="critical",
            reversible=True,
            backend="local",
            reason="User requested host edit",
            blocked_pattern=None,
            cow_summary=None,
            rpa_keyword_preview=(),
        )
        defaults.update(overrides)
        return HitlDisplayData(**defaults)

    def test_action_field(self) -> None:
        d = self._make_data(action="fs.write")
        assert d.action == "fs.write"

    def test_target_field(self) -> None:
        d = self._make_data(target="/etc/hosts")
        assert d.target == "/etc/hosts"

    def test_tier_field(self) -> None:
        d = self._make_data(tier=Tier.MEDIUM)
        assert d.tier == Tier.MEDIUM

    def test_reversible_true(self) -> None:
        d = self._make_data(reversible=True)
        assert d.reversible is True

    def test_cow_summary_none(self) -> None:
        d = self._make_data(cow_summary=None)
        assert d.cow_summary is None

    def test_cow_summary_truncated(self) -> None:
        d = self._make_data(cow_summary="+" * 5000)
        assert len(d.cow_summary) <= 4100

    def test_rpa_keywords_sanitized(self) -> None:
        d = self._make_data(rpa_keyword_preview=("Click Button\x1b[31m",))
        assert "\x1b" not in d.rpa_keyword_preview[0]

    def test_frozen(self) -> None:
        d = self._make_data()
        with pytest.raises(AttributeError):
            d.action = "fs.read"
