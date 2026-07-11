"""JSON Schema reader for Control Center pages.

Phase 6 Scope B: single source of truth for numeric knob bounds and
"restart vs. hot-reload" semantics. Pages ask for a field's bounds
here rather than hardcoding min/max/description in Python dataclasses.

If the GUI limits drift from the schema, the schema wins — the daemon
validates against it on load, so any UI value the schema rejects is a
UI bug.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SchemaReadError(RuntimeError):
    """Raised when the controller_config.json file cannot be loaded.

    Callers should catch and either surface via `Adw.Banner` (page-level
    breakage) or fall back to dataclass defaults (single-field
    degraded rendering). Never silently mask — see Scope A rules.
    """


# ── Resolution ────────────────────────────────────────────────────────────


def _candidate_paths() -> tuple[Path, ...]:
    """Ordered list of places where the JSON Schema may live.

    ISO install path first, then the in-repo path for developer runs,
    then a peer-installed venv location. First hit wins."""
    return (
        Path("/usr/share/icebreaker/schemas/controller_config.json"),
        Path(__file__).resolve().parents[3]
            / "controller" / "schemas" / "controller_config.json",
        Path(__file__).resolve().parents[2]
            / "controller" / "schemas" / "controller_config.json",
    )


def _load_schema() -> dict[str, Any]:
    for candidate in _candidate_paths():
        if candidate.exists():
            try:
                return json.loads(candidate.read_text("utf-8"))
            except Exception as exc:
                raise SchemaReadError(
                    f"controller_config.json exists at {candidate} but "
                    f"failed to parse: {type(exc).__name__}: {exc}"
                ) from exc
    raise SchemaReadError(
        "controller_config.json not found in any known location: "
        + ", ".join(str(p) for p in _candidate_paths())
    )


# Cache the parsed schema — validation-driven bounds are read from every
# knob widget instantiation, and re-reading disk on every SpinRow build
# would be wasteful in the GUI hot path. Invalidated only on process
# restart (which is when config changes take effect anyway).
_SCHEMA_CACHE: dict[str, Any] | None = None


def get_schema() -> dict[str, Any]:
    """Return the parsed controller_config.json. Cached per-process."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = _load_schema()
    return _SCHEMA_CACHE


# ── Lookup ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldSpec:
    """Bounds + metadata for a single leaf field in the config schema."""
    minimum: float | None
    maximum: float | None
    description: str
    reload: str            # "hot" | "restart" | "unspecified"
    type_name: str         # "integer" | "number" | "string" | "boolean"
    default: Any | None


def _walk(schema: dict, section: tuple[str, ...], key: str) -> dict | None:
    """Walk `properties` down the section path, then look up key.

    Returns the JSON Schema fragment for the leaf, or None if the path
    doesn't exist. Handles the top-level object shape:
        properties -> <section> -> properties -> <key>
    """
    node: Any = schema
    for part in section:
        if not isinstance(node, dict):
            return None
        props = node.get("properties", {})
        if not isinstance(props, dict) or part not in props:
            return None
        node = props[part]
    if not isinstance(node, dict):
        return None
    leaf_props = node.get("properties", {})
    if not isinstance(leaf_props, dict) or key not in leaf_props:
        return None
    leaf = leaf_props[key]
    return leaf if isinstance(leaf, dict) else None


def get_field_spec(section: tuple[str, ...], key: str) -> FieldSpec | None:
    """Return the FieldSpec for `[section].key`, or None if unspecified.

    Callers use None to fall back to a dataclass / hardcoded default —
    this is deliberate for pages that mix schema-defined and legacy
    knobs during the Scope B rollout.
    """
    try:
        schema = get_schema()
    except SchemaReadError:
        return None
    leaf = _walk(schema, section, key)
    if leaf is None:
        return None
    return FieldSpec(
        minimum=_num(leaf.get("minimum")),
        maximum=_num(leaf.get("maximum")),
        description=str(leaf.get("description", "")),
        reload=str(leaf.get("x-reload", "unspecified")),
        type_name=str(leaf.get("type", "")),
        default=leaf.get("default"),
    )


def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
