"""Phase 6 Scope C — cloud QB preset registry.

The Models page dropdown used to hardcode `_BACKEND_ORDER` and
`_MODEL_PRESETS` as Python tuples inside `gui/control/pages/models_page.py`.
Any new provider or model required a code change. Scope C makes this
data-driven: presets live in `controller/catalogue.toml` alongside the
local GGUF manifests, and this module reads them.

Two-layer merge (BP-1, BP-2):
    1. system catalogue at `controller/catalogue.toml` (shipped, read-only)
    2. optional user overlay at `$XDG_CONFIG_HOME/icebreaker/presets.toml`

Merge semantics — arrays REPLACE, never concatenate. Deep-merge with
concatenation is the trap that would let a malicious overlay silently
add a preset. If the overlay names a provider, the entire preset list
for that provider is replaced (BP-9 — least privilege for user-supplied
config).

The registry has NO knowledge of live verification — that's
`preset_verifier.py`. This module is pure catalogue parsing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


try:
    import tomllib as _toml_reader  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as _toml_reader  # type: ignore


class PresetRegistryError(RuntimeError):
    """Raised when the catalogue cannot be loaded or its shape is wrong."""


# ── Data model ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Preset:
    """A single dropdown entry in the Models page."""
    id: str                            # exact string sent to the provider SDK
    provider: str                      # "gemini" | "anthropic" | "openai" | "local"
    display_name: str
    context_window: int
    output_token_limit: int | None
    status: str                        # "recommended" | "experimental" | "deprecated" | "unverified"
    capabilities: tuple[str, ...]
    notes: str
    verified_at: str | None            # ISO date of catalogue-time verification, or None


@dataclass(frozen=True)
class Provider:
    """A grouped view of one provider's presets, in catalogue order."""
    key: str                           # "gemini" | "anthropic" | ...
    display_name: str
    presets: tuple[Preset, ...]


# ── Provider display names ────────────────────────────────────────────────
# Users see these in the backend dropdown; ordering here defines the
# ordering in the UI. Extending to a new provider is a two-line edit
# here plus a new `[[preset]] provider = "..."` block in the catalogue.


_PROVIDER_DISPLAY: dict[str, str] = {
    "gemini":    "Gemini (cloud)",
    "anthropic": "Anthropic Claude (cloud)",
    "openai":    "OpenAI (cloud)",
    "local":     "Local (llama.cpp)",
}


_PROVIDER_ORDER: tuple[str, ...] = ("gemini", "anthropic", "openai", "local")


# Statuses that a fresh preset in the catalogue is allowed to carry. Any
# other value at load time is a typo — reject at load so it never
# silently degrades to "no badge".
_VALID_STATUSES: frozenset[str] = frozenset({
    "recommended",
    "experimental",
    "deprecated",
    "unverified",
})


# ── Path resolution ───────────────────────────────────────────────────────


def _system_catalogue_paths() -> tuple[Path, ...]:
    """Ordered list of places the shipped catalogue may live. First hit
    wins. Matches the existing model_registry.py convention."""
    return (
        Path("/usr/share/icebreaker/catalogue.toml"),
        Path(__file__).resolve().parent / "catalogue.toml",
    )


def _user_overlay_path() -> Path:
    """`$XDG_CONFIG_HOME/icebreaker/presets.toml` or the fallback under
    `~/.config`. Purely a lookup — does not create the file."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "icebreaker" / "presets.toml"


def _resolve_system_catalogue() -> Path:
    for candidate in _system_catalogue_paths():
        if candidate.exists():
            return candidate
    raise PresetRegistryError(
        "catalogue.toml not found in any known location: "
        + ", ".join(str(p) for p in _system_catalogue_paths())
    )


# ── Parsing ───────────────────────────────────────────────────────────────


def _parse_presets(raw: dict[str, Any], source: str) -> list[Preset]:
    """Extract every `[[preset]]` entry from a parsed TOML doc.

    Rejects entries with invalid `status` values loudly — a typo like
    `status = "recommend"` would otherwise fall through to no-badge.
    """
    entries = raw.get("preset", [])
    if not isinstance(entries, list):
        raise PresetRegistryError(
            f"{source}: `preset` must be an array of tables"
        )
    presets: list[Preset] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise PresetRegistryError(
                f"{source}: preset #{i} is not a table"
            )
        try:
            preset_id = str(entry["id"])
            provider = str(entry["provider"])
            display_name = str(entry.get("display_name", preset_id))
        except KeyError as exc:
            raise PresetRegistryError(
                f"{source}: preset #{i} missing required field: {exc}"
            ) from exc
        status = str(entry.get("status", "unverified"))
        if status not in _VALID_STATUSES:
            raise PresetRegistryError(
                f"{source}: preset {preset_id!r} has invalid status "
                f"{status!r} (allowed: {sorted(_VALID_STATUSES)})"
            )
        caps = entry.get("capabilities", [])
        if not isinstance(caps, list):
            caps = []
        presets.append(Preset(
            id=preset_id,
            provider=provider,
            display_name=display_name,
            context_window=int(entry.get("context_window", 0)),
            output_token_limit=(
                int(entry["output_token_limit"])
                if "output_token_limit" in entry else None
            ),
            status=status,
            capabilities=tuple(str(c) for c in caps),
            notes=str(entry.get("notes", "")),
            verified_at=(
                str(entry["verified_at"])
                if "verified_at" in entry else None
            ),
        ))
    return presets


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return _toml_reader.load(f)
    except Exception as exc:
        raise PresetRegistryError(
            f"failed to parse {path}: {type(exc).__name__}: {exc}"
        ) from exc


# ── Public API ───────────────────────────────────────────────────────────


def load_registry(
    *,
    system_path: Path | None = None,
    user_path: Path | None = None,
) -> tuple[Provider, ...]:
    """Load the presets grouped by provider, in the canonical UI order.

    - `system_path`: override for tests / CI. Defaults to the shipped
      catalogue resolved via `_resolve_system_catalogue`.
    - `user_path`: override for tests. Defaults to
      `$XDG_CONFIG_HOME/icebreaker/presets.toml`; absent = no overlay.

    Overlay merge — per-provider REPLACE (not concatenate). If the
    overlay ships any preset for a provider, the system's presets for
    that provider are dropped in favor of the overlay's list.
    """
    sys_path = system_path if system_path is not None else _resolve_system_catalogue()
    sys_presets = _parse_presets(_load_toml(sys_path), source=str(sys_path))

    overlay_path = user_path if user_path is not None else _user_overlay_path()
    if overlay_path.exists():
        overlay_presets = _parse_presets(
            _load_toml(overlay_path), source=str(overlay_path),
        )
        overlay_providers = {p.provider for p in overlay_presets}
        # Drop any system preset whose provider is present in the overlay.
        sys_presets = [
            p for p in sys_presets if p.provider not in overlay_providers
        ]
        sys_presets.extend(overlay_presets)

    # Group by provider, preserving intra-provider catalogue order.
    grouped: dict[str, list[Preset]] = {}
    for preset in sys_presets:
        grouped.setdefault(preset.provider, []).append(preset)

    providers: list[Provider] = []
    # Emit in canonical order first, then any additional providers
    # (custom user provider names) alphabetically.
    seen: set[str] = set()
    for key in _PROVIDER_ORDER:
        if key in grouped:
            providers.append(Provider(
                key=key,
                display_name=_PROVIDER_DISPLAY.get(key, key),
                presets=tuple(grouped[key]),
            ))
            seen.add(key)
    for key in sorted(set(grouped) - seen):
        providers.append(Provider(
            key=key,
            display_name=_PROVIDER_DISPLAY.get(key, key),
            presets=tuple(grouped[key]),
        ))
    return tuple(providers)


def preset_ids(providers: tuple[Provider, ...], provider_key: str) -> list[str]:
    """Return the preset IDs for one provider — the shape models_page's
    `_MODEL_PRESETS[backend]` used to return."""
    for provider in providers:
        if provider.key == provider_key:
            return [p.id for p in provider.presets]
    return []


def backend_order(providers: tuple[Provider, ...]) -> list[tuple[str, str]]:
    """Return the `[(key, display_name), ...]` shape models_page's
    `_BACKENDS` used to hold."""
    return [(p.key, p.display_name) for p in providers]
