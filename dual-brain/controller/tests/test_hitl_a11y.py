"""Tests for HITL accessibility and $NO_COLOR support — extends G5.1.

Verifies:
  - $NO_COLOR set → no ANSI escape codes in rendered output
  - Non-UTF-8 locale → ASCII fallback (no box-drawing chars)
  - Glyphs switch between UTF-8 and ASCII sets
"""

from __future__ import annotations

import os
import re

import pytest

from controller.hitl import (
    HitlDisplayData,
    HitlPrompt,
    TerminalPresenter,
    _ascii_safe,
    _colors_enabled,
    _g,
)
from controller.keymap import Keymap
from controller.risk_classifier import ClassificationResult, Tier

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _intent(**overrides) -> dict:
    base = dict(
        intent_id="test-uuid",
        action="fs.delete",
        target="/etc/hosts",
        params={},
        reason="user_requested",
        risk_level="critical",
    )
    return {**base, **overrides}


def _cls(**overrides) -> ClassificationResult:
    base = dict(tier=Tier.HIGH, reason="critical path", reversible=False)
    return ClassificationResult(**{**base, **overrides})


class TestNoColor:
    def test_no_color_disables_colors(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        assert _colors_enabled() is False

    def test_no_color_empty_enables_colors_if_tty(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        # In test context, stdout is usually not a TTY
        # This just verifies the function runs without error
        result = _colors_enabled()
        assert isinstance(result, bool)

    def test_render_no_ansi_when_no_color(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        presenter = TerminalPresenter(keymap=Keymap())
        data = HitlDisplayData(
            action="fs.delete", target="/etc/hosts",
            tier=Tier.HIGH, risk_level="critical",
            reversible=False, backend="local",
            reason="test", blocked_pattern=None,
            cow_summary=None,
        )
        rendered = presenter._render(data)
        assert not _ANSI_ESCAPE.search(rendered), (
            f"Rendered output contains ANSI escapes with $NO_COLOR set:\n{rendered}"
        )


class TestAsciiSafe:
    def test_utf8_locale_not_ascii_safe(self, monkeypatch):
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LC_CTYPE", raising=False)
        assert _ascii_safe() is False

    def test_c_locale_is_ascii_safe(self, monkeypatch):
        monkeypatch.setenv("LANG", "C")
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LC_CTYPE", raising=False)
        assert _ascii_safe() is True

    def test_posix_locale_is_ascii_safe(self, monkeypatch):
        monkeypatch.setenv("LANG", "POSIX")
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LC_CTYPE", raising=False)
        assert _ascii_safe() is True

    def test_no_locale_is_ascii_safe(self, monkeypatch):
        monkeypatch.delenv("LANG", raising=False)
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LC_CTYPE", raising=False)
        assert _ascii_safe() is True

    def test_lc_all_utf8_overrides(self, monkeypatch):
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        monkeypatch.setenv("LANG", "C")
        assert _ascii_safe() is False

    def test_lc_ctype_utf8_overrides(self, monkeypatch):
        monkeypatch.setenv("LC_CTYPE", "en_US.UTF-8")
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.setenv("LANG", "C")
        assert _ascii_safe() is False


class TestGlyphs:
    def test_utf8_glyphs(self, monkeypatch):
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LC_CTYPE", raising=False)
        assert _g("warn") == "⚠"
        assert _g("ok") == "✓"
        assert _g("fail") == "✗"
        assert _g("line") == "─"

    def test_ascii_glyphs(self, monkeypatch):
        monkeypatch.setenv("LANG", "C")
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LC_CTYPE", raising=False)
        assert _g("warn") == "!"
        assert _g("ok") == "[OK]"
        assert _g("fail") == "[X]"
        assert _g("line") == "-"


class TestRenderAscii:
    def test_ascii_render_no_box_drawing(self, monkeypatch):
        monkeypatch.setenv("LANG", "C")
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LC_CTYPE", raising=False)
        monkeypatch.setenv("NO_COLOR", "1")

        presenter = TerminalPresenter(keymap=Keymap())
        data = HitlDisplayData(
            action="fs.write", target="/tmp/test.txt",
            tier=Tier.MEDIUM, risk_level="medium",
            reversible=True, backend="local",
            reason="test file write", blocked_pattern=None,
            cow_summary=None,
        )
        rendered = presenter._render(data)

        assert "─" not in rendered, "Box-drawing char found in ASCII mode"
        assert "═" not in rendered, "Double-line char found in ASCII mode"
        assert "⚠" not in rendered, "Unicode warning in ASCII mode"
        assert "✓" not in rendered, "Unicode check in ASCII mode"
        assert "✗" not in rendered, "Unicode cross in ASCII mode"
        assert "⌛" not in rendered, "Unicode hourglass in ASCII mode"

        assert "fs.write" in rendered
        assert "/tmp/test.txt" in rendered
