"""Tests for gui.theme — design token system."""

from __future__ import annotations

import re

import pytest

from gui.theme import (
    COLOR_TOKENS_DARK,
    COLOR_TOKENS_LIGHT,
    FONTS,
    RADIUS,
    IcebreakerTheme,
    _generate_css,
)


_HEX_RE = re.compile(r"^#[0-9a-f]{6}$")


class TestColorTokens:
    """Verify token dicts have valid hex values and required keys."""

    REQUIRED_KEYS = {
        "background", "foreground", "card", "card_foreground",
        "primary", "primary_foreground",
        "secondary", "secondary_foreground",
        "muted", "muted_foreground",
        "accent", "accent_foreground",
        "destructive", "destructive_foreground",
        "border", "input", "ring",
    }

    def test_dark_has_all_keys(self) -> None:
        assert self.REQUIRED_KEYS <= set(COLOR_TOKENS_DARK)

    def test_light_has_all_keys(self) -> None:
        assert self.REQUIRED_KEYS <= set(COLOR_TOKENS_LIGHT)

    def test_dark_values_are_hex(self) -> None:
        for key, val in COLOR_TOKENS_DARK.items():
            assert _HEX_RE.match(val), f"dark.{key} = {val!r} is not valid hex"

    def test_light_values_are_hex(self) -> None:
        for key, val in COLOR_TOKENS_LIGHT.items():
            assert _HEX_RE.match(val), f"light.{key} = {val!r} is not valid hex"

    def test_dark_and_light_have_same_keys(self) -> None:
        assert set(COLOR_TOKENS_DARK) == set(COLOR_TOKENS_LIGHT)

    def test_dark_background_is_dark(self) -> None:
        r = int(COLOR_TOKENS_DARK["background"][1:3], 16)
        assert r < 64, "dark background should have low luminance"

    def test_light_background_is_light(self) -> None:
        r = int(COLOR_TOKENS_LIGHT["background"][1:3], 16)
        assert r > 192, "light background should have high luminance"

    def test_primary_amber_dark(self) -> None:
        assert COLOR_TOKENS_DARK["primary"] == "#e78952"

    def test_secondary_teal_dark(self) -> None:
        assert COLOR_TOKENS_DARK["secondary"] == "#5e8787"


class TestFontsAndRadius:
    def test_sans_font_starts_with_geist(self) -> None:
        assert FONTS["sans"].startswith("Geist Mono")

    def test_mono_font_starts_with_jetbrains(self) -> None:
        assert FONTS["mono"].startswith("JetBrains Mono")

    def test_radius_is_12px(self) -> None:
        assert RADIUS == "12px"


class TestGenerateCSS:
    def test_output_contains_background(self) -> None:
        css = _generate_css(COLOR_TOKENS_DARK, FONTS, RADIUS)
        assert "#111112" in css

    def test_output_contains_primary_button(self) -> None:
        css = _generate_css(COLOR_TOKENS_DARK, FONTS, RADIUS)
        assert ".ib-primary-button" in css

    def test_output_contains_card_class(self) -> None:
        css = _generate_css(COLOR_TOKENS_DARK, FONTS, RADIUS)
        assert ".ib-card" in css

    def test_output_contains_font_family(self) -> None:
        css = _generate_css(COLOR_TOKENS_DARK, FONTS, RADIUS)
        assert "Geist Mono" in css

    def test_output_contains_border_radius(self) -> None:
        css = _generate_css(COLOR_TOKENS_DARK, FONTS, RADIUS)
        assert "border-radius: 12px" in css

    def test_output_contains_ring_focus(self) -> None:
        css = _generate_css(COLOR_TOKENS_DARK, FONTS, RADIUS)
        assert ".ib-input:focus" in css

    def test_light_theme_has_white_background(self) -> None:
        css = _generate_css(COLOR_TOKENS_LIGHT, FONTS, RADIUS)
        assert "#ffffff" in css

    def test_output_contains_elevated(self) -> None:
        css = _generate_css(COLOR_TOKENS_DARK, FONTS, RADIUS)
        assert ".ib-elevated" in css
        assert "box-shadow" in css

    def test_output_contains_chip(self) -> None:
        css = _generate_css(COLOR_TOKENS_DARK, FONTS, RADIUS)
        assert ".ib-chip" in css

    def test_output_contains_greeting(self) -> None:
        css = _generate_css(COLOR_TOKENS_DARK, FONTS, RADIUS)
        assert ".ib-greeting" in css

    def test_output_contains_sidebar(self) -> None:
        css = _generate_css(COLOR_TOKENS_DARK, FONTS, RADIUS)
        assert ".ib-sidebar" in css


class TestIcebreakerTheme:
    def test_dark_mode_default(self) -> None:
        theme = IcebreakerTheme()
        assert theme.is_dark is True

    def test_light_mode(self) -> None:
        theme = IcebreakerTheme(dark=False)
        assert theme.is_dark is False

    def test_css_text_is_string(self) -> None:
        theme = IcebreakerTheme()
        assert isinstance(theme.css_text, str)
        assert len(theme.css_text) > 100

    def test_tokens_returns_copy(self) -> None:
        theme = IcebreakerTheme()
        t1 = theme.tokens
        t2 = theme.tokens
        assert t1 == t2
        assert t1 is not t2
