"""Default Icebreaker theme — warm amber primary, teal secondary, dark surface.

Matches ``gui/theme.py:COLOR_TOKENS_DARK`` verbatim so both the Control
Center and the chatbot render with the same palette.
"""

NAME = "Icebreaker Default"
DESCRIPTION = "Warm amber accents on a dark charcoal surface."

TOKENS = {
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
