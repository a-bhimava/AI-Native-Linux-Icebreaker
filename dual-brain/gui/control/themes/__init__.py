"""Icebreaker Control Center themes.

Adding a new theme = drop a file in this directory with a ``TOKENS`` dict
(same keys as ``COLOR_TOKENS_DARK`` in ``gui/theme.py``) and a ``NAME``
string. The registry auto-discovers it.

Every theme MUST provide the same set of keys because ``gui/theme.py``'s
``_generate_css`` walks the token dict — missing a key raises KeyError at
CSS render time.

Reference: the canonical dark palette lives in ``gui/theme.py``
(``COLOR_TOKENS_DARK``). New themes usually start there and swap 4–6
colors — the primary, background, and border variables are the most
visually impactful.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Dict, Optional


# Populated lazily by ``discover()``. Maps display-name → module.
_REGISTRY: Optional[Dict[str, "ThemeModule"]] = None


class ThemeModule:
    """Wraps an imported theme module so callers get a stable interface."""

    def __init__(self, name: str, tokens: dict, description: str = "") -> None:
        self.name = name
        self.tokens = tokens
        self.description = description


def discover() -> Dict[str, ThemeModule]:
    """Import every sibling module and build the registry."""
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY

    registry: Dict[str, ThemeModule] = {}
    package_name = __name__  # gui.control.themes

    for finder, mod_name, is_pkg in pkgutil.iter_modules(__path__):
        if is_pkg or mod_name.startswith("_"):
            continue
        module = importlib.import_module(f"{package_name}.{mod_name}")
        name = getattr(module, "NAME", mod_name)
        tokens = getattr(module, "TOKENS", None)
        description = getattr(module, "DESCRIPTION", "")
        if tokens is None:
            continue  # not a valid theme file
        registry[name] = ThemeModule(name=name, tokens=tokens, description=description)

    _REGISTRY = registry
    return registry


def get(name: str) -> Optional[ThemeModule]:
    return discover().get(name)


def names() -> list[str]:
    return sorted(discover().keys())
