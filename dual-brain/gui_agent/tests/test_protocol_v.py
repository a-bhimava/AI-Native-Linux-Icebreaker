"""Tests for the 12 Fix V (v6.15) vision-grounded GUI schemas.

Separate file from test_protocol.py so the diff for V.4a stays
self-contained and the pre-existing test file stays authoritative
for the v6.14 baseline 8-tool surface.
"""

from __future__ import annotations

import jsonschema
import pytest

from gui_agent.protocol import (
    ALL_GUI_METHODS,
    GUI_CLICK_AT_COORDS,
    GUI_DRAG,
    GUI_GROUNDED_CLICK,
    GUI_GROUNDED_DRAG,
    GUI_GROUNDED_SCROLL,
    GUI_GROUNDED_TYPE,
    GUI_HOVER,
    GUI_KEY_SEQUENCE,
    GUI_PARSE_SCREEN,
    GUI_PRESS_KEY,
    GUI_READONLY_METHODS,
    GUI_SCROLL,
    GUI_TYPE_AT_COORDS,
    GUI_WRITE_METHODS,
    _PARAM_SCHEMAS,
    validate_gui_params,
)


# ═══ Constants layout ═════════════════════════════════════════════════


class TestFixVConstants:
    def test_all_12_new_constants_present(self):
        expected = {
            GUI_PARSE_SCREEN,
            GUI_CLICK_AT_COORDS, GUI_TYPE_AT_COORDS, GUI_DRAG,
            GUI_SCROLL, GUI_HOVER,
            GUI_PRESS_KEY, GUI_KEY_SEQUENCE,
            GUI_GROUNDED_CLICK, GUI_GROUNDED_TYPE,
            GUI_GROUNDED_DRAG, GUI_GROUNDED_SCROLL,
        }
        assert expected.issubset(ALL_GUI_METHODS)
        assert len(expected) == 12

    def test_readonly_bucket_includes_parse_and_hover(self):
        assert GUI_PARSE_SCREEN in GUI_READONLY_METHODS
        assert GUI_HOVER in GUI_READONLY_METHODS

    def test_write_bucket_includes_all_action_tools(self):
        for m in (GUI_CLICK_AT_COORDS, GUI_TYPE_AT_COORDS, GUI_DRAG,
                  GUI_SCROLL, GUI_PRESS_KEY, GUI_KEY_SEQUENCE,
                  GUI_GROUNDED_CLICK, GUI_GROUNDED_TYPE,
                  GUI_GROUNDED_DRAG, GUI_GROUNDED_SCROLL):
            assert m in GUI_WRITE_METHODS, f"{m} should be a write method"

    def test_readonly_and_write_stay_disjoint(self):
        assert GUI_READONLY_METHODS & GUI_WRITE_METHODS == frozenset()


# ═══ Every new tool has a schema entry ═══════════════════════════════


class TestSchemaRegistration:
    @pytest.mark.parametrize("method", [
        GUI_PARSE_SCREEN,
        GUI_CLICK_AT_COORDS, GUI_TYPE_AT_COORDS, GUI_DRAG,
        GUI_SCROLL, GUI_HOVER,
        GUI_PRESS_KEY, GUI_KEY_SEQUENCE,
        GUI_GROUNDED_CLICK, GUI_GROUNDED_TYPE,
        GUI_GROUNDED_DRAG, GUI_GROUNDED_SCROLL,
    ])
    def test_schema_present(self, method):
        assert method in _PARAM_SCHEMAS


# ═══ gui.parse_screen ════════════════════════════════════════════════


class TestParseScreen:
    def test_empty_params_ok(self):
        validate_gui_params(GUI_PARSE_SCREEN, {})

    def test_window_optional(self):
        validate_gui_params(GUI_PARSE_SCREEN, {"window": "Slack"})

    def test_prompt_hint_optional(self):
        validate_gui_params(GUI_PARSE_SCREEN,
                            {"window": "Firefox", "prompt_hint": "find the search box"})

    def test_extra_field_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_PARSE_SCREEN, {"junk": "value"})


# ═══ Coord tools (click/type/hover/scroll) ═══════════════════════════


class TestCoordTools:
    def test_click_at_coords_minimum(self):
        validate_gui_params(GUI_CLICK_AT_COORDS, {"x": 100, "y": 200})

    def test_click_at_coords_full(self):
        validate_gui_params(GUI_CLICK_AT_COORDS,
                            {"x": 500, "y": 300, "button": "right", "count": 2})

    def test_click_at_coords_bad_button(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_CLICK_AT_COORDS,
                                {"x": 1, "y": 1, "button": "middle-mouse"})

    def test_click_at_coords_bad_count(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_CLICK_AT_COORDS,
                                {"x": 1, "y": 1, "count": 5})

    def test_click_at_coords_negative_reject(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_CLICK_AT_COORDS, {"x": -1, "y": 200})

    def test_click_at_coords_over_max_reject(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_CLICK_AT_COORDS,
                                {"x": 100000, "y": 200})

    def test_type_at_coords_ok(self):
        validate_gui_params(GUI_TYPE_AT_COORDS,
                            {"x": 100, "y": 200, "text": "hello"})

    def test_type_at_coords_text_length_capped(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_TYPE_AT_COORDS,
                                {"x": 100, "y": 200, "text": "x" * 5000})

    def test_hover_ok(self):
        validate_gui_params(GUI_HOVER, {"x": 100, "y": 200})

    def test_scroll_full(self):
        validate_gui_params(GUI_SCROLL,
                            {"x": 500, "y": 300,
                             "direction": "down", "amount": 3})

    def test_scroll_bad_direction(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_SCROLL,
                                {"x": 1, "y": 1,
                                 "direction": "diagonal", "amount": 1})

    def test_scroll_amount_capped(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_SCROLL,
                                {"x": 1, "y": 1,
                                 "direction": "up", "amount": 500})


# ═══ Drag ═════════════════════════════════════════════════════════════


class TestDrag:
    def test_minimum(self):
        validate_gui_params(GUI_DRAG,
                            {"x1": 10, "y1": 10, "x2": 100, "y2": 100})

    def test_with_waypoints(self):
        validate_gui_params(GUI_DRAG, {
            "x1": 10, "y1": 10, "x2": 100, "y2": 100,
            "button": "left", "hold_ms": 100,
            "waypoints": [[50, 50], [75, 75]],
        })

    def test_too_many_waypoints_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_DRAG, {
                "x1": 1, "y1": 1, "x2": 2, "y2": 2,
                "waypoints": [[i, i] for i in range(20)],
            })

    def test_waypoint_wrong_shape_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_DRAG, {
                "x1": 1, "y1": 1, "x2": 2, "y2": 2,
                "waypoints": [[50]],
            })

    def test_missing_endpoint_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_DRAG, {"x1": 1, "y1": 1, "x2": 2})


# ═══ Keyboard ═════════════════════════════════════════════════════════


class TestKeyboard:
    def test_press_key_ok(self):
        validate_gui_params(GUI_PRESS_KEY, {"combo": "ctrl+s"})

    def test_press_key_named(self):
        validate_gui_params(GUI_PRESS_KEY, {"combo": "F12"})

    def test_press_key_rejects_shell_injection(self):
        # Schema rejects due to pattern (only [A-Za-z0-9+] allowed).
        # input_synth.py has a stricter allowlist too — defense in depth.
        for bad in ("$(rm -rf /)", "ctrl+`whoami`", "ctrl+s; ls",
                    "ctrl+ ", "  "):
            with pytest.raises(jsonschema.ValidationError):
                validate_gui_params(GUI_PRESS_KEY, {"combo": bad})

    def test_press_key_empty_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_PRESS_KEY, {"combo": ""})

    def test_press_key_missing_combo_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_PRESS_KEY, {})

    def test_key_sequence_strings(self):
        validate_gui_params(GUI_KEY_SEQUENCE,
                            {"items": ["ctrl+t", "google.com", "return"]})

    def test_key_sequence_dicts(self):
        validate_gui_params(GUI_KEY_SEQUENCE, {"items": [
            {"type": "key", "combo": "ctrl+t"},
            {"type": "text", "text": "hello"},
            {"type": "key", "combo": "return"},
        ]})

    def test_key_sequence_mixed(self):
        validate_gui_params(GUI_KEY_SEQUENCE, {"items": [
            {"type": "key", "combo": "ctrl+t"},
            "google.com",
            "return",
        ]})

    def test_key_sequence_empty_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_KEY_SEQUENCE, {"items": []})

    def test_key_sequence_too_many_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_KEY_SEQUENCE,
                                {"items": ["x"] * 100})

    def test_key_sequence_bad_type_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_KEY_SEQUENCE,
                                {"items": [{"type": "beep"}]})


# ═══ Grounded tools ═══════════════════════════════════════════════════


class TestGrounded:
    def test_grounded_click_ok(self):
        validate_gui_params(GUI_GROUNDED_CLICK,
                            {"window": "Slack", "prompt": "the Send button"})

    def test_grounded_click_with_button(self):
        validate_gui_params(GUI_GROUNDED_CLICK, {
            "window": "Firefox", "prompt": "first link", "button": "middle",
        })

    def test_grounded_click_missing_prompt_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_GROUNDED_CLICK, {"window": "Slack"})

    def test_grounded_click_missing_window_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_GROUNDED_CLICK, {"prompt": "send"})

    def test_grounded_type_ok(self):
        validate_gui_params(GUI_GROUNDED_TYPE, {
            "window": "Firefox", "prompt": "URL bar", "text": "example.com",
        })

    def test_grounded_type_missing_text_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_GROUNDED_TYPE,
                                {"window": "F", "prompt": "x"})

    def test_grounded_drag_ok(self):
        validate_gui_params(GUI_GROUNDED_DRAG, {
            "window": "Files",
            "source_prompt": "test.txt file",
            "target_prompt": "Documents folder",
        })

    def test_grounded_drag_both_endpoints_required(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_GROUNDED_DRAG, {
                "window": "Files", "source_prompt": "x",
            })

    def test_grounded_scroll_ok(self):
        validate_gui_params(GUI_GROUNDED_SCROLL, {
            "window": "Firefox", "prompt": "results list",
            "direction": "down", "amount": 5,
        })

    def test_grounded_scroll_bad_direction(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_GROUNDED_SCROLL, {
                "window": "F", "prompt": "x",
                "direction": "diagonal",
            })

    def test_grounded_prompt_length_capped(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_gui_params(GUI_GROUNDED_CLICK, {
                "window": "Slack", "prompt": "x" * 1000,
            })
