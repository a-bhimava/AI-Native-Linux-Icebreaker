"""Tools page — read-only current mcpd tool inventory.

**v6.65 placeholder**. Reads the mcpd tool schemas installed at
``/usr/share/icebreaker/schemas/`` and displays each tool's name,
description, trust tier, and reversibility flags.

Phase 7 M7.2 will expand this into a real external-MCP-server allowlist
editor. For v6.65 the page ships intentionally read-only: users can see
what tools exist without being confused by disabled controls that don't
work yet.

The rendering (per-tool card with metadata) is the same shape M7.2 will
use, so the placeholder becomes a real page in one PR.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk


# Search paths, in priority order. Installed ISO uses /usr/share; a
# developer running the Control Center against a checked-out tree can
# still see something useful by pointing at the source schemas.
_SCHEMA_DIRS: list[Path] = [
    Path("/usr/share/icebreaker/schemas"),
    Path("/opt/icebreaker/share/schemas"),
    Path(__file__).resolve().parents[4] / "src" / "mcpd" / "schemas",
]

_TIER_LABELS = {
    0: "Tier 0 · read-only · auto",
    1: "Tier 1 · home-write · auto",
    2: "Tier 2 · review · auto after review",
    3: "Tier 3 · HITL blocking · human required",
}


class ToolsPage(Adw.PreferencesPage):
    def __init__(self) -> None:
        super().__init__(title="Tools", icon_name="applications-utilities-symbolic")
        self.set_name("tools")

        self._add_header_group()
        self._add_tool_list()

    # ── Header note about Phase 7 ───────────────────────────────────────

    def _add_header_group(self) -> None:
        group = Adw.PreferencesGroup(
            title="Built-in tools",
            description=(
                "The Privileged Brain can only call the tools listed below. "
                "This is the fixed catalogue that ships with the OS. "
                "External MCP server management (adding third-party tool "
                "servers with per-server trust tiers) arrives in Phase 7 "
                "v1.0 — this page will grow an allowlist editor there."
            ),
        )
        self.add(group)

        schema_dir = self._find_schema_dir()
        subtitle = (
            f"Loaded from {schema_dir}" if schema_dir
            else "No schema directory found — is mcpd installed?"
        )
        info_row = Adw.ActionRow(title="Schema source", subtitle=subtitle)
        group.add(info_row)

    def _find_schema_dir(self) -> Path | None:
        for candidate in _SCHEMA_DIRS:
            if candidate.exists() and candidate.is_dir():
                return candidate
        return None

    # ── Tool inventory ──────────────────────────────────────────────────

    def _add_tool_list(self) -> None:
        schema_dir = self._find_schema_dir()
        if schema_dir is None:
            group = Adw.PreferencesGroup(title="Tools")
            row = Adw.ActionRow(
                title="No tools discovered",
                subtitle=(
                    "Expected schemas under /usr/share/icebreaker/schemas/. "
                    "Is the icebreaker-mcpd package installed?"
                ),
            )
            group.add(row)
            self.add(group)
            return

        tools = self._load_tools(schema_dir)
        # Group by module: fs.*, network.*, package.*, process.*, service.*, system.*
        by_module: dict[str, list[dict[str, Any]]] = {}
        for tool in tools:
            name = tool.get("name", "unknown")
            module = name.split(".", 1)[0] if "." in name else "misc"
            by_module.setdefault(module, []).append(tool)

        for module in sorted(by_module):
            group = Adw.PreferencesGroup(title=f"{module}.*")
            for tool in sorted(by_module[module], key=lambda t: t.get("name", "")):
                self._add_tool_row(group, tool)
            self.add(group)

    def _load_tools(self, schema_dir: Path) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for path in sorted(schema_dir.glob("*.json")):
            if path.name == "schema-version.json":
                continue
            try:
                data = json.loads(path.read_text("utf-8"))
            except Exception:
                continue
            trust = data.get("x-icebreaker-trust", {}) or {}
            tools.append({
                "name": data.get("title", path.stem),
                "description": data.get("description", ""),
                "tier": trust.get("tier"),
                "reversible": trust.get("reversible"),
                "requires_hitl": trust.get("requires_hitl"),
                "requires_cow": trust.get("requires_cow"),
            })
        return tools

    def _add_tool_row(self, group: Adw.PreferencesGroup, tool: dict[str, Any]) -> None:
        title = tool.get("name", "?")
        description = tool.get("description", "")
        # Build a compact flag suffix so the metadata is skimmable.
        flags: list[str] = []
        tier = tool.get("tier")
        if isinstance(tier, int) and tier in _TIER_LABELS:
            flags.append(_TIER_LABELS[tier])
        else:
            flags.append("Tier · unknown")
        if tool.get("requires_hitl"):
            flags.append("HITL")
        if tool.get("requires_cow"):
            flags.append("COW")
        if tool.get("reversible") is False:
            flags.append("destructive")

        subtitle = description
        if flags:
            subtitle = f"{' · '.join(flags)}\n{description}" if description else " · ".join(flags)

        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row.set_subtitle_lines(6)
        group.add(row)
