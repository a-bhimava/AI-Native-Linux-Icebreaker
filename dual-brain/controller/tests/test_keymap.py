"""Tests for controller.keymap — G5.3 gate.

Covers:
  - Default keymap loads and has all expected bindings
  - User overlay replaces action bindings correctly
  - Duplicate-key collision raises KeymapValidationError
  - Multi-char and control-char bindings rejected
  - Esc (\\x1b) cannot be remapped to any action
  - '?' cannot be unbound from HELP
  - DENY must retain at least one user binding
  - legend() lists active keys in display order
  - lookup() resolves keys including Esc hard-reserve
  - Unknown action names rejected
  - Non-list binding values rejected
  - Empty binding list rejected
"""

from __future__ import annotations

import pytest

from controller.keymap import (
    DEFAULTS,
    Action,
    Keymap,
    KeymapValidationError,
    load_keymap,
)


class TestDefaultKeymap:
    def test_defaults_load(self):
        km = load_keymap(None)
        assert isinstance(km, Keymap)
        for action in Action:
            assert action in km.bindings
            assert len(km.bindings[action]) >= 1

    def test_defaults_match_constant(self):
        km = load_keymap(None)
        assert km.bindings == DEFAULTS

    def test_empty_dict_returns_defaults(self):
        km = load_keymap({})
        assert km.bindings == DEFAULTS


class TestLookup:
    def test_numeric_keys(self):
        km = Keymap()
        assert km.lookup("1") == Action.APPROVE
        assert km.lookup("2") == Action.DENY
        assert km.lookup("3") == Action.MODIFY
        assert km.lookup("4") == Action.EXPLAIN
        assert km.lookup("5") == Action.TRUST

    def test_mnemonic_keys(self):
        km = Keymap()
        assert km.lookup("a") == Action.APPROVE
        assert km.lookup("d") == Action.DENY
        assert km.lookup("m") == Action.MODIFY
        assert km.lookup("e") == Action.EXPLAIN
        assert km.lookup("t") == Action.TRUST

    def test_universal_keys(self):
        km = Keymap()
        assert km.lookup("y") == Action.APPROVE
        assert km.lookup("n") == Action.DENY

    def test_help_key(self):
        km = Keymap()
        assert km.lookup("?") == Action.HELP

    def test_esc_always_deny(self):
        km = Keymap()
        assert km.lookup("\x1b") == Action.DENY

    def test_unknown_key_returns_none(self):
        km = Keymap()
        assert km.lookup("z") is None
        assert km.lookup("!") is None
        assert km.lookup(" ") is None


class TestUserOverlay:
    def test_override_approve_keys(self):
        km = load_keymap({"approve": ["j"]})
        assert km.bindings[Action.APPROVE] == ("j",)
        assert km.lookup("j") == Action.APPROVE
        assert km.lookup("1") is None  # old default replaced

    def test_override_preserves_other_defaults(self):
        km = load_keymap({"approve": ["j"]})
        assert km.bindings[Action.DENY] == DEFAULTS[Action.DENY]
        assert km.bindings[Action.MODIFY] == DEFAULTS[Action.MODIFY]

    def test_override_multiple_actions(self):
        km = load_keymap({
            "approve": ["j"],
            "deny": ["k"],
        })
        assert km.lookup("j") == Action.APPROVE
        assert km.lookup("k") == Action.DENY


class TestLegend:
    def test_legend_contains_all_actions(self):
        km = Keymap()
        legend = km.legend()
        assert "Approve" in legend
        assert "Deny" in legend
        assert "Modify" in legend
        assert "Explain" in legend
        assert "Trust" in legend
        assert "Help" in legend

    def test_legend_hides_trust_when_requested(self):
        km = Keymap()
        legend = km.legend(include_trust=False)
        assert "Trust" not in legend
        assert "Approve" in legend
        assert "Help" in legend

    def test_legend_shows_custom_keys(self):
        km = load_keymap({"approve": ["j"]})
        legend = km.legend()
        assert "[j]" in legend


class TestValidationErrors:
    def test_duplicate_key_across_actions(self):
        with pytest.raises(KeymapValidationError, match="collides"):
            load_keymap({"approve": ["x"], "deny": ["x"]})

    def test_duplicate_key_case_insensitive(self):
        with pytest.raises(KeymapValidationError, match="collides"):
            load_keymap({"approve": ["A"], "deny": ["a"]})

    def test_multi_char_binding_rejected(self):
        with pytest.raises(KeymapValidationError, match="exactly one character"):
            load_keymap({"approve": ["ab"]})

    def test_empty_string_binding_rejected(self):
        with pytest.raises(KeymapValidationError, match="exactly one character"):
            load_keymap({"approve": [""]})

    def test_control_char_rejected(self):
        with pytest.raises(KeymapValidationError, match="control character"):
            load_keymap({"approve": ["\t"]})

    def test_null_char_rejected(self):
        with pytest.raises(KeymapValidationError, match="control character"):
            load_keymap({"approve": ["\x00"]})

    def test_space_rejected(self):
        with pytest.raises(KeymapValidationError, match="control character"):
            load_keymap({"approve": [" "]})

    def test_del_rejected(self):
        with pytest.raises(KeymapValidationError, match="control character"):
            load_keymap({"approve": ["\x7f"]})

    def test_esc_cannot_be_bound(self):
        with pytest.raises(KeymapValidationError, match="control character"):
            load_keymap({"approve": ["\x1b"]})

    def test_question_mark_cannot_be_unbound(self):
        with pytest.raises(KeymapValidationError, match="must remain bound"):
            load_keymap({"help": ["h"]})

    def test_deny_must_have_binding(self):
        with pytest.raises(KeymapValidationError, match="at least one binding"):
            load_keymap({"deny": []})

    def test_unknown_action_rejected(self):
        with pytest.raises(KeymapValidationError, match="unknown action"):
            load_keymap({"fire_missiles": ["x"]})

    def test_non_list_binding_rejected(self):
        with pytest.raises(KeymapValidationError, match="must be a list"):
            load_keymap({"approve": "y"})

    def test_non_string_item_rejected(self):
        with pytest.raises(KeymapValidationError, match="must be a string"):
            load_keymap({"approve": [1]})


class TestEscHardReserve:
    def test_esc_maps_to_deny_in_default(self):
        km = Keymap()
        assert km.lookup("\x1b") == Action.DENY

    def test_esc_maps_to_deny_in_custom(self):
        km = load_keymap({"approve": ["j"]})
        assert km.lookup("\x1b") == Action.DENY

    def test_esc_not_in_any_binding_tuple(self):
        km = Keymap()
        for action, keys in km.bindings.items():
            assert "\x1b" not in keys, f"Esc found in {action.value} bindings"


class TestActionsForDisplay:
    def test_returns_all_actions_with_keys(self):
        km = Keymap()
        pairs = km.actions_for_display()
        actions = [a for a, _ in pairs]
        for action in Action:
            assert action in actions

    def test_order_matches_enum_order(self):
        km = Keymap()
        pairs = km.actions_for_display()
        actions = [a for a, _ in pairs]
        expected = list(Action)
        assert actions == expected
