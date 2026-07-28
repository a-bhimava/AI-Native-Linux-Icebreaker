"""Tests for RPA Bridge protocol definitions (PR #28)."""

from __future__ import annotations

import jsonschema
import pytest

from rpa_bridge.protocol import (
    ALL_RPA_METHODS,
    RPA_EXECUTE_WORKFLOW,
    RPA_FIND_BY_IMAGE,
    RPA_LIST_WORKFLOWS,
    RPA_PING,
    RPA_READONLY_METHODS,
    RPA_WRITE_METHODS,
    validate_rpa_params,
)


class TestMethodConstants:
    def test_all_methods_is_union(self):
        assert ALL_RPA_METHODS == RPA_READONLY_METHODS | RPA_WRITE_METHODS

    def test_write_methods_disjoint_from_readonly(self):
        assert RPA_WRITE_METHODS & RPA_READONLY_METHODS == frozenset()

    def test_all_methods_start_with_rpa(self):
        for method in ALL_RPA_METHODS:
            assert method.startswith("rpa.")


class TestControlCharRejection:
    """F-101.1 (2026-07-28): shell metachars are now ACCEPTED for keyword
    names (Robot Framework's own parser reads them, not a shell). The
    `template_path` field for rpa.find_by_image keeps the strict pattern
    because it's a filesystem path that MIGHT be piped somewhere in
    error handling.
    """

    @pytest.mark.parametrize("shell_metachar_ok", [
        "ok;rm -rf /",
        "$(whoami)",
        "test|cat",
        "a`cmd`b",
        "foo<bar",
        "foo>bar",
        "foo&bar",
    ])
    def test_shell_metachar_in_keyword_name_now_accepted(self, shell_metachar_ok):
        # No exception — Robot Framework validates its own keyword names
        # + the allowlist in workflow_gen.py filters unknown keywords.
        validate_rpa_params(RPA_EXECUTE_WORKFLOW, {
            "keywords": [{"name": shell_metachar_ok, "args": []}],
        })

    @pytest.mark.parametrize("bad_value", ["null\x00byte", "nl\ninject", "cr\rinject"])
    def test_control_char_in_keyword_name_still_rejected(self, bad_value):
        with pytest.raises(Exception):
            validate_rpa_params(RPA_EXECUTE_WORKFLOW, {
                "keywords": [{"name": bad_value, "args": []}],
            })

    def test_shell_metachar_in_template_path_still_rejected(self):
        """F-101.1: filesystem-path fields retain the strict pattern."""
        with pytest.raises(Exception):
            validate_rpa_params("rpa.find_by_image", {
                "template_path": "/tmp/evil;rm.png",
            })

    def test_too_many_keywords_rejected(self):
        keywords = [{"name": "Click Element", "args": ["id=btn"]}] * 21
        with pytest.raises(Exception):
            validate_rpa_params(RPA_EXECUTE_WORKFLOW, {"keywords": keywords})


class TestValidParams:
    def test_ping_accepts_empty(self):
        validate_rpa_params(RPA_PING, {})

    def test_execute_workflow_happy_path(self):
        validate_rpa_params(RPA_EXECUTE_WORKFLOW, {
            "keywords": [
                {"name": "Click Element", "args": ["id=submit"]},
                {"name": "Input Text", "args": ["id=name", "hello"]},
            ],
            "timeout_seconds": 30,
        })

    def test_find_by_image_happy_path(self):
        validate_rpa_params(RPA_FIND_BY_IMAGE, {
            "template_path": "/tmp/template.png",
            "confidence": 0.9,
        })

    def test_list_workflows_accepts_empty(self):
        validate_rpa_params(RPA_LIST_WORKFLOWS, {})

    def test_unknown_method_raises(self):
        with pytest.raises(ValueError, match="unknown RPA method"):
            validate_rpa_params("rpa.nonexistent", {})


class TestNewWorkflowParams:
    """Schema coverage for auto_wait_seconds + screenshot_policy (v6.11)."""

    def test_auto_wait_and_policy_accepted(self):
        validate_rpa_params(RPA_EXECUTE_WORKFLOW, {
            "keywords": [{"name": "Click Element", "args": ["id=x"]}],
            "auto_wait_seconds": 7.5,
            "screenshot_policy": "state_changing",
        })

    def test_invalid_screenshot_policy_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_rpa_params(RPA_EXECUTE_WORKFLOW, {
                "keywords": [{"name": "Click Element", "args": ["id=x"]}],
                "screenshot_policy": "sometimes",
            })

    def test_auto_wait_out_of_range_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            validate_rpa_params(RPA_EXECUTE_WORKFLOW, {
                "keywords": [{"name": "Click Element", "args": ["id=x"]}],
                "auto_wait_seconds": 31.0,
            })
