"""Tests for WorkflowGenerator — keyword allowlist, validation, rejection (PR #28)."""

from __future__ import annotations

import pytest

from rpa_bridge.workflow_gen import (
    ALLOWED_KEYWORDS,
    DENIED_KEYWORDS,
    MAX_KEYWORDS,
    MAX_SLEEP_SECONDS,
    WorkflowError,
    WorkflowGenerator,
)


@pytest.fixture
def gen(tmp_path):
    return WorkflowGenerator(scratch_dir=tmp_path)


class TestIntentToKeywords:
    def test_click_element_produces_single_keyword(self, gen):
        result = gen.validate_keywords([
            {"name": "Click Element", "args": ["id=submit-btn"]},
        ])
        assert result == [("Click Element", ["id=submit-btn"])]

    def test_multi_step_form_fill(self, gen):
        result = gen.validate_keywords([
            {"name": "Input Text", "args": ["id=username", "alice"]},
            {"name": "Input Text", "args": ["id=password", "secret123"]},
            {"name": "Click Element", "args": ["id=login"]},
        ])
        assert len(result) == 3
        assert result[0][0] == "Input Text"
        assert result[1][0] == "Input Text"
        assert result[2][0] == "Click Element"

    def test_browser_navigation_workflow(self, gen):
        result = gen.validate_keywords([
            {"name": "Go To", "args": ["https://example.com"]},
            {"name": "Wait Until Page Contains Element", "args": ["id=content", "10"]},
            {"name": "Click Element", "args": ["id=btn"]},
        ])
        assert len(result) == 3
        assert result[0] == ("Go To", ["https://example.com"])

    def test_sleep_duration_capped(self, gen):
        result = gen.validate_keywords([
            {"name": "Sleep", "args": ["60s"]},
        ])
        assert result[0][1] == [f"{MAX_SLEEP_SECONDS}s"]

    def test_sleep_within_limit_unchanged(self, gen):
        result = gen.validate_keywords([
            {"name": "Sleep", "args": ["3s"]},
        ])
        assert result[0][1] == ["3s"]


class TestRejection:
    def test_rejects_more_than_max_keywords(self, gen):
        keywords = [{"name": "Click Element", "args": ["id=btn"]}] * (MAX_KEYWORDS + 1)
        with pytest.raises(WorkflowError, match="maximum") as exc_info:
            gen.validate_keywords(keywords)
        assert exc_info.value.reason == "too_many_keywords"

    def test_rejects_explicitly_denied_keyword(self, gen):
        with pytest.raises(WorkflowError, match="explicitly denied") as exc_info:
            gen.validate_keywords([
                {"name": "Execute Javascript", "args": ["return 1"]},
            ])
        assert exc_info.value.reason == "disallowed_keyword"
        assert exc_info.value.keyword_name == "Execute Javascript"

    def test_rejects_unknown_keyword(self, gen):
        with pytest.raises(WorkflowError, match="not in the allowlist") as exc_info:
            gen.validate_keywords([
                {"name": "Custom Magic Keyword", "args": []},
            ])
        assert exc_info.value.reason == "disallowed_keyword"

    def test_rejects_wrong_arity_too_few(self, gen):
        with pytest.raises(WorkflowError, match="requires") as exc_info:
            gen.validate_keywords([
                {"name": "Click Element", "args": []},
            ])
        assert exc_info.value.reason == "invalid_args"

    def test_rejects_wrong_arity_too_many(self, gen):
        with pytest.raises(WorkflowError, match="requires") as exc_info:
            gen.validate_keywords([
                {"name": "Click Element", "args": ["a", "b"]},
            ])
        assert exc_info.value.reason == "invalid_args"

    @pytest.mark.parametrize("denied", [
        "Evaluate", "Run Process", "Import Library",
        "Set Variable", "Create File", "Start Process",
        "Log", "Execute Async Javascript",
    ])
    def test_all_denied_keywords_rejected(self, gen, denied):
        with pytest.raises(WorkflowError, match="denied"):
            gen.validate_keywords([{"name": denied, "args": []}])


class TestAllowlistIntegrity:
    def test_no_overlap_between_allowed_and_denied(self):
        overlap = set(ALLOWED_KEYWORDS.keys()) & DENIED_KEYWORDS
        assert overlap == set(), f"Keywords in both allowed and denied: {overlap}"

    def test_allowlist_has_reasonable_size(self):
        assert len(ALLOWED_KEYWORDS) >= 30
        assert len(ALLOWED_KEYWORDS) <= 50

    def test_denied_list_has_critical_keywords(self):
        for critical in ("Evaluate", "Execute Javascript", "Run Process", "Import Library"):
            assert critical in DENIED_KEYWORDS


class TestRobotFileGeneration:
    def test_generates_robot_file(self, gen, tmp_path):
        gen = WorkflowGenerator(scratch_dir=tmp_path)
        path = gen.generate_robot_file("test_flow", [
            {"name": "Click Element", "args": ["id=btn"]},
            {"name": "Go Back"},
        ])
        assert path.exists()
        assert path.suffix == ".robot"
        content = path.read_text()
        assert "Click Element" in content
        assert "Go Back" in content
        assert (path.stat().st_mode & 0o777) == 0o600
