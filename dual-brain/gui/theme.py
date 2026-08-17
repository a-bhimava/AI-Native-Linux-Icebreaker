"""Icebreaker design token system — oklch palette translated to GTK4 CSS.

Authoritative oklch source: ``docs/design-tokens.css``.
Hex values computed via coloraide oklch→sRGB→hex conversion.

The ``IcebreakerTheme`` class loads a ``Gtk.CssProvider`` that maps every
token to a named CSS class. Widgets use ``@ib-primary``, ``@ib-background``,
etc. via ``Gtk.Widget.add_css_class()``.
"""

from __future__ import annotations

from typing import Final

from .appearance import load_appearance


COLOR_TOKENS_CLASSIC_DARK: Final[dict[str, str]] = {
    "background":           "#111112",
    "foreground":           "#c0c0c0",
    "card":                 "#111111",
    "card_foreground":      "#c0c0c0",
    "primary":              "#e78952",
    "primary_foreground":   "#111112",
    "secondary":            "#5e8787",
    "secondary_foreground": "#111112",
    "muted":                "#222222",
    "muted_foreground":     "#888888",
    "accent":               "#333333",
    "accent_foreground":    "#c0c0c0",
    "destructive":          "#5e8787",
    "destructive_foreground": "#111112",
    "border":               "#222222",
    "input":                "#222222",
    "ring":                 "#e78952",
}

# Frosted Graphite is deliberately restrained: the elevated/transparent-looking
# treatment belongs to conversational content, never a human approval surface.
COLOR_TOKENS_FROSTED_DARK: Final[dict[str, str]] = {
    "background": "#15171c", "foreground": "#edf0f6",
    "card": "#22262e", "card_foreground": "#edf0f6",
    "primary": "#9dacd2", "primary_foreground": "#12141a",
    "secondary": "#78849c", "secondary_foreground": "#101217",
    "muted": "#1c2028", "muted_foreground": "#b7bfce",
    "accent": "#303744", "accent_foreground": "#edf0f6",
    "destructive": "#d87878", "destructive_foreground": "#17191e",
    "border": "#48515f", "input": "#20252e", "ring": "#b8c7ff",
}

# Backward-compatible public name.  Fresh installs now select Frosted.
COLOR_TOKENS_DARK: Final[dict[str, str]] = COLOR_TOKENS_FROSTED_DARK

COLOR_TOKENS_LIGHT: Final[dict[str, str]] = {
    "background":           "#ffffff",
    "foreground":           "#101827",
    "card":                 "#ffffff",
    "card_foreground":      "#101827",
    "primary":              "#d77843",
    "primary_foreground":   "#ffffff",
    "secondary":            "#517575",
    "secondary_foreground": "#ffffff",
    "muted":                "#f3f4f6",
    "muted_foreground":     "#6a7180",
    "accent":               "#ededed",
    "accent_foreground":    "#101827",
    "destructive":          "#ee4444",
    "destructive_foreground": "#ffffff",
    "border":               "#e5e7ea",
    "input":                "#e5e7ea",
    "ring":                 "#d77843",
}

FONTS: Final[dict[str, str]] = {
    "sans": "Geist Mono, ui-monospace, monospace",
    "mono": "JetBrains Mono, monospace",
}

RADIUS: Final[str] = "12px"


def _generate_css(tokens: dict[str, str], fonts: dict[str, str], radius: str, *, frosted: bool = False) -> str:
    """Build a GTK4 CSS stylesheet from design tokens."""
    lines = [
        "/* Auto-generated from docs/design-tokens.css — do not edit by hand. */",
        "",
    ]

    lines.append("window, .ib-window {")
    lines.append(f"  background-color: {tokens['background']};")
    if frosted:
        lines.append("  background-image: radial-gradient(circle at 18% 0%, rgba(151, 166, 205, 0.20), transparent 38%), linear-gradient(145deg, #1b1f28, #121419);")
    lines.append(f"  color: {tokens['foreground']};")
    lines.append(f"  font-family: {fonts['sans']};")
    lines.append("}")
    lines.append("")

    for name, color in tokens.items():
        css_class = f"ib-{name.replace('_', '-')}"
        lines.append(f".{css_class} {{")
        if "foreground" in name:
            lines.append(f"  color: {color};")
        else:
            lines.append(f"  background-color: {color};")
        lines.append("}")

    lines.append("")
    lines.append(".ib-card {")
    lines.append(f"  background-color: {tokens['card']};")
    lines.append(f"  color: {tokens['card_foreground']};")
    lines.append(f"  border-radius: {radius};")
    lines.append(f"  border: 1px solid {tokens['border']};")
    lines.append("}")
    lines.append("")
    lines.append(".ib-primary-button {")
    lines.append(f"  background-color: {tokens['primary']};")
    lines.append(f"  color: {tokens['primary_foreground']};")
    lines.append(f"  border-radius: {radius};")
    lines.append("}")
    lines.append("")
    lines.append(".ib-primary-button:hover {")
    lines.append(f"  opacity: 0.9;")
    lines.append("}")
    lines.append("")
    lines.append(".ib-secondary-button {")
    lines.append(f"  background-color: {tokens['secondary']};")
    lines.append(f"  color: {tokens['secondary_foreground']};")
    lines.append(f"  border-radius: {radius};")
    lines.append("}")
    lines.append("")
    lines.append(".ib-destructive-button {")
    lines.append(f"  background-color: {tokens['destructive']};")
    lines.append(f"  color: {tokens['destructive_foreground']};")
    lines.append(f"  border-radius: {radius};")
    lines.append("}")
    lines.append("")
    lines.append(".ib-muted-text {")
    lines.append(f"  color: {tokens['muted_foreground']};")
    lines.append("}")
    lines.append("")
    lines.append(".ib-input {")
    lines.append(f"  background-color: {tokens['input']};")
    lines.append(f"  color: {tokens['foreground']};")
    lines.append(f"  border-radius: {radius};")
    lines.append(f"  border: 1px solid {tokens['border']};")
    lines.append("}")
    lines.append("")
    lines.append(".ib-input:focus {")
    lines.append(f"  outline: 2px solid {tokens['ring']};")
    lines.append("}")
    lines.append("")
    lines.append(f".ib-mono {{ font-family: {fonts['mono']}; }}")
    lines.append("")

    lines.append(".ib-elevated {")
    lines.append(f"  background-color: {tokens['card']};")
    lines.append(f"  border-radius: {radius};")
    lines.append(f"  border: 1px solid {tokens['border']};")
    lines.append("  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.3);")
    lines.append("}")
    lines.append("")

    if frosted:
        lines.append(".ib-glass-card, .ib-glass-input {")
        lines.append("  background-color: rgba(37, 42, 52, 0.88);")
        lines.append("  border: 1px solid rgba(218, 226, 245, 0.22);")
        lines.append("  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.30), inset 0 1px rgba(255, 255, 255, 0.10);")
        lines.append(f"  border-radius: {radius};")
        lines.append("}")
        lines.append("")
        lines.append(".ib-glass-input { background-color: rgba(30, 35, 44, 0.92); }")
        lines.append("")

    lines.append(".ib-elevated-input {")
    lines.append(f"  background-color: {tokens['muted']};")
    lines.append(f"  border-radius: {radius};")
    lines.append(f"  border: 1px solid {tokens['border']};")
    lines.append("  box-shadow: 0 2px 16px rgba(0, 0, 0, 0.25);")
    lines.append("}")
    lines.append("")

    lines.append(".ib-chip {")
    lines.append(f"  border-radius: 20px;")
    lines.append(f"  border: 1px solid {tokens['border']};")
    lines.append(f"  padding: 6px 16px;")
    lines.append(f"  background-color: transparent;")
    lines.append(f"  color: {tokens['muted_foreground']};")
    lines.append("}")
    lines.append("")

    lines.append(".ib-chip:hover {")
    lines.append(f"  background-color: {tokens['accent']};")
    lines.append(f"  color: {tokens['foreground']};")
    lines.append("}")
    lines.append("")

    lines.append(".ib-greeting {")
    lines.append(f"  font-size: 28px;")
    lines.append(f"  font-weight: 300;")
    lines.append(f"  color: {tokens['foreground']};")
    lines.append("}")
    lines.append("")

    lines.append(".ib-sidebar {")
    lines.append(f"  background-color: {tokens['background']};")
    lines.append(f"  border-right: 1px solid {tokens['border']};")
    lines.append("}")
    lines.append("")

    lines.append(".ib-sidebar button {")
    lines.append(f"  border-radius: 8px;")
    lines.append(f"  min-width: 40px;")
    lines.append(f"  min-height: 40px;")
    lines.append("}")
    lines.append("")

    lines.append(".ib-sidebar button:hover {")
    lines.append(f"  background-color: {tokens['accent']};")
    lines.append("}")
    lines.append("")

    lines.append(".ib-badge {")
    lines.append("  padding: 2px 8px;")
    lines.append("  border-radius: 8px;")
    lines.append("  font-weight: 600;")
    lines.append("  font-size: 12px;")
    lines.append("}")
    lines.append("")

    lines.append(f".ib-tier-0 {{ background-color: {tokens['secondary']}; color: {tokens['secondary_foreground']}; }}")
    lines.append(f".ib-tier-1 {{ background-color: {tokens['foreground']}; color: {tokens['background']}; }}")
    lines.append(".ib-tier-2 { background-color: #fbbf24; color: #111112; }")
    lines.append(".ib-tier-3 { background-color: #f87171; color: #111112; }")
    lines.append("")

    lines.append(f".ib-secondary {{ color: {tokens['secondary']}; }}")
    lines.append(f".ib-destructive {{ color: #f87171; }}")
    lines.append("")

    lines.append(".ib-window {")
    lines.append(f"  background-color: {tokens['background']};")
    lines.append(f"  color: {tokens['foreground']};")
    lines.append("}")
    lines.append("")
    lines.append(".ib-security-surface, .ib-hitl-surface, .ib-audit-surface {")
    lines.append("  background-color: #17191f;")
    lines.append("  background-image: none;")
    lines.append("  opacity: 1;")
    lines.append("}")
    lines.append("")

    return "\n".join(lines) + "\n"


class IcebreakerTheme:
    """Loads and applies the Icebreaker design token CSS to a GTK4 display."""

    def __init__(self, *, dark: bool = True, profile: str | None = None) -> None:
        self._dark = dark
        self._profile = profile or load_appearance().profile
        if self._profile not in {"classic", "frosted"}:
            self._profile = "frosted"
        self._tokens = (
            (COLOR_TOKENS_FROSTED_DARK if self._profile == "frosted" else COLOR_TOKENS_CLASSIC_DARK)
            if dark else COLOR_TOKENS_LIGHT
        )
        self._css_text = _generate_css(self._tokens, FONTS, RADIUS, frosted=dark and self._profile == "frosted")

    @property
    def is_dark(self) -> bool:
        return self._dark

    @property
    def profile(self) -> str:
        return self._profile

    @property
    def tokens(self) -> dict[str, str]:
        return dict(self._tokens)

    @property
    def css_text(self) -> str:
        return self._css_text

    def apply(self, display: object | None = None) -> None:
        """Load CSS into the default (or given) Gdk display."""
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, Gtk

        provider = Gtk.CssProvider()
        provider.load_from_string(self._css_text)

        if display is None:
            display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
