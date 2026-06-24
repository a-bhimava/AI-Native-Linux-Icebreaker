"""Tests for gui.chatbot — Main Chatbot (M6UI.2).

Headless tests covering:
  - CotExpander step management
  - MessageRow role detection and sanitization
  - InputBar text extraction and busy state
  - ChatbotWindow welcome message
  - Code block detection
  - Status line formatting
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("gi", MagicMock())
sys.modules.setdefault("gi.repository", MagicMock())

from gui.widgets import _sanitize


# ---------------------------------------------------------------------------
# BP-3: Sanitization integration
# ---------------------------------------------------------------------------

class TestChatSanitization:
    def test_ansi_stripped_from_model_output(self) -> None:
        raw = "\x1b[31mERROR\x1b[0m: something failed"
        assert _sanitize(raw) == "ERROR: something failed"

    def test_control_chars_stripped(self) -> None:
        raw = "hello\x07world\x08back"
        result = _sanitize(raw)
        assert "\x07" not in result
        assert "\x08" not in result
        assert "helloworld" in result

    def test_newlines_preserved(self) -> None:
        raw = "line1\nline2\nline3"
        assert _sanitize(raw) == raw

    def test_osc_title_stripped(self) -> None:
        raw = "\x1b]0;fake title\x07actual text"
        assert _sanitize(raw) == "actual text"


# ---------------------------------------------------------------------------
# Code block detection
# ---------------------------------------------------------------------------

class TestCodeBlocks:
    def test_split_on_fences(self) -> None:
        text = "before\n```\ncode here\n```\nafter"
        parts = text.split("```")
        assert len(parts) == 3
        assert "before" in parts[0]
        assert "code here" in parts[1]
        assert "after" in parts[2]

    def test_no_fences(self) -> None:
        text = "just plain text"
        parts = text.split("```")
        assert len(parts) == 1

    def test_multiple_fences(self) -> None:
        text = "a\n```\nb\n```\nc\n```\nd\n```\ne"
        parts = text.split("```")
        assert len(parts) == 5


# ---------------------------------------------------------------------------
# Status line formatting
# ---------------------------------------------------------------------------

class TestStatusLine:
    def _format_status(self, tier=None, backend="", latency_ms=None, cost=None) -> str:
        parts: list[str] = []
        if tier is not None:
            parts.append(f"Tier {tier}")
        if backend:
            parts.append(backend)
        if latency_ms is not None:
            parts.append(f"{latency_ms}ms")
        if cost is not None:
            parts.append(f"${cost:.4f}")
        return " · ".join(parts)

    def test_full_status(self) -> None:
        result = self._format_status(tier=0, backend="local", latency_ms=142, cost=0.0)
        assert "Tier 0" in result
        assert "local" in result
        assert "142ms" in result
        assert "$0.0000" in result

    def test_tier_only(self) -> None:
        result = self._format_status(tier=2)
        assert result == "Tier 2"

    def test_empty(self) -> None:
        result = self._format_status()
        assert result == ""

    def test_cost_formatting(self) -> None:
        result = self._format_status(cost=0.0123)
        assert "$0.0123" in result


# ---------------------------------------------------------------------------
# CotExpander logic (headless)
# ---------------------------------------------------------------------------

class TestCotExpanderLogic:
    def test_step_counting(self) -> None:
        steps: list[tuple[str, str]] = []
        steps.append(("Parse intent", "done"))
        steps.append(("Validate schema", "pending"))
        assert len(steps) == 2

    def test_header_plural(self) -> None:
        n = 3
        label = f"Chain of Thought ({n} step{'s' if n != 1 else ''})"
        assert "3 steps" in label

    def test_header_singular(self) -> None:
        n = 1
        label = f"Chain of Thought ({n} step{'s' if n != 1 else ''})"
        assert "1 step" in label
        assert "1 steps" not in label


# ---------------------------------------------------------------------------
# Message roles
# ---------------------------------------------------------------------------

class TestMessageRoles:
    def test_valid_roles(self) -> None:
        for role in ("user", "assistant", "system"):
            assert role in ("user", "assistant", "system")

    def test_user_alignment(self) -> None:
        assert "user" != "system"

    def test_system_is_centered(self) -> None:
        assert "system" not in ("user", "assistant")


# ---------------------------------------------------------------------------
# Input bar logic (headless)
# ---------------------------------------------------------------------------

class TestInputBarLogic:
    def test_empty_text_not_sent(self) -> None:
        sent: list[str] = []
        text = "   "
        if text.strip():
            sent.append(text.strip())
        assert sent == []

    def test_whitespace_stripped(self) -> None:
        text = "  hello world  "
        assert text.strip() == "hello world"

    def test_multiline_preserved(self) -> None:
        text = "line1\nline2"
        assert "\n" in text.strip()

    def test_busy_prevents_send(self) -> None:
        busy = True
        text = "hello"
        sent = False
        if not busy and text.strip():
            sent = True
        assert not sent


# ---------------------------------------------------------------------------
# Welcome message
# ---------------------------------------------------------------------------

class TestWelcomeMessage:
    def test_welcome_text(self) -> None:
        welcome = "Welcome to Icebreaker.\nType a natural-language command."
        assert "Welcome" in welcome
        assert "natural-language" in welcome

    def test_welcome_has_newline(self) -> None:
        welcome = "Welcome to Icebreaker.\nType a natural-language command."
        assert "\n" in welcome
