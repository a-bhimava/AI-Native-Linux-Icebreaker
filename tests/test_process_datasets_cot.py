"""Tests for CoT extensions in privileged-brain/scripts/process_datasets.py (Architecture 1)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "privileged-brain" / "scripts"))

from process_datasets import (
    _key_tool,
    _make_reasoning,
    to_chatml_cot,
    assert_cot_format,
    SYSTEM_PROMPT_COT,
)


class TestKeyTool:
    def test_simple_command(self):
        # _key_tool extracts the first non-sudo token
        result = _key_tool("df -h")
        assert result == "df"

    def test_sudo_skipped(self):
        result = _key_tool("sudo systemctl restart nginx")
        assert result == "systemctl"

    def test_pipeline(self):
        result = _key_tool("ps aux | grep nginx")
        assert result == "ps"


class TestMakeReasoning:
    def test_returns_string(self):
        r = _make_reasoning("show disk usage", "df -h")
        assert isinstance(r, str)
        assert len(r) > 10

    def test_contains_tool(self):
        r = _make_reasoning("show disk usage", "df -h")
        assert "df" in r

    def test_refuse_handling(self):
        r = _make_reasoning("delete /boot", "REFUSE: dangerous operation")
        assert "refused" in r.lower() or "refuse" in r.lower()


class TestToChatmlCot:
    def test_output_structure(self):
        ex = to_chatml_cot("show disk usage", "df -h")
        assert "messages" in ex
        assert len(ex["messages"]) == 3
        roles = [m["role"] for m in ex["messages"]]
        assert roles == ["system", "user", "assistant"]

    def test_system_prompt_is_cot(self):
        ex = to_chatml_cot("show disk usage", "df -h")
        assert ex["messages"][0]["content"] == SYSTEM_PROMPT_COT

    def test_assistant_has_reasoning_prefix(self):
        ex = to_chatml_cot("show disk usage", "df -h")
        content = ex["messages"][2]["content"]
        assert content.startswith("REASONING:")

    def test_assistant_has_command_line(self):
        ex = to_chatml_cot("show disk usage", "df -h")
        content = ex["messages"][2]["content"]
        lines = content.strip().splitlines()
        assert any(l.startswith("COMMAND:") for l in lines)

    def test_command_line_contains_bash(self):
        ex = to_chatml_cot("show disk usage", "df -h")
        content = ex["messages"][2]["content"]
        cmd_line = next(l for l in content.splitlines() if l.startswith("COMMAND:"))
        assert "df -h" in cmd_line

    def test_custom_reasoning_used(self):
        custom = "Custom reasoning here about df."
        ex = to_chatml_cot("show disk usage", "df -h", reasoning=custom)
        content = ex["messages"][2]["content"]
        assert custom in content

    def test_refuse_example(self):
        ex = to_chatml_cot("delete /boot", "REFUSE: dangerous operation")
        content = ex["messages"][2]["content"]
        assert "REFUSE" in content


class TestAssertCotFormat:
    def test_valid_example_passes(self):
        ex = to_chatml_cot("show disk usage", "df -h")
        assert_cot_format(ex)  # should not raise

    def test_missing_reasoning_raises(self):
        bad = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "show disk usage"},
                {"role": "assistant", "content": "df -h"},  # no REASONING prefix
            ]
        }
        with pytest.raises(ValueError):
            assert_cot_format(bad)

    def test_missing_command_raises(self):
        bad = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "show disk usage"},
                {"role": "assistant", "content": "REASONING: some reasoning here"},
            ]
        }
        with pytest.raises(ValueError):
            assert_cot_format(bad)

    def test_single_line_raises(self):
        bad = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "show disk usage"},
                {"role": "assistant", "content": "REASONING: blah"},
            ]
        }
        with pytest.raises(ValueError):
            assert_cot_format(bad)

    def test_no_assistant_raises(self):
        bad = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "query"},
            ]
        }
        with pytest.raises(ValueError, match="No assistant"):
            assert_cot_format(bad)
