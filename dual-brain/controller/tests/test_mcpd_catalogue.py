"""Tests for the generated _mcpd_tools module.

These tests pin the shape of the catalogue against mcpd's actual 22-tool
inventory and guard against the historical drift bug where the classifier
referenced phantom tool names. The canonical drift check against a live
mcpd binary lives in `scripts/export_mcpd_catalogue.py check` and runs
on the VM (G2 gate).
"""

from controller._mcpd_tools import (
    ALL_TOOLS,
    CONDITIONAL_TIER_TOOLS,
    DESTRUCTIVE_TOOLS,
    SYSTEM_WRITE_TOOLS,
    TIER0_TOOLS,
)


# v6.9 Bug B (2026-07-17): bumped 22 → 23. ALL_TOOLS now includes the
# manifest tool nav.cd alongside the 22 mcpd tools so the risk classifier
# resolves nav.cd as Tier 0 instead of BP-5 escalating to Tier 3. Matches
# EXPECTED_TOOL_COUNT in export_mcpd_catalogue.py.
EXPECTED_TOOL_COUNT = 24  # v6.10 P3: +demo.uptime (Layer 2B pilot).

# Names that the pre-M2.0 risk_classifier referenced but mcpd does NOT ship.
# Asserted absent so a future regression can't silently reintroduce them.
PHANTOM_TOOLS = frozenset({
    "service.status",
    "network.interfaces",
    "network.dns",
    "package.list",
    "package.search",
    "package.info",
    "package.purge",
    "network.firewall_flush",
    "network.firewall_delete",
    "network.firewall_add",
    "network.firewall_modify",
})


def test_all_tools_count():
    assert len(ALL_TOOLS) == EXPECTED_TOOL_COUNT, (
        f"Expected {EXPECTED_TOOL_COUNT} mcpd tools; got {len(ALL_TOOLS)}. "
        "If mcpd added or removed a tool, regenerate _mcpd_tools.py via "
        "`python scripts/export_mcpd_catalogue.py emit --mcpd <path>` and "
        "update EXPECTED_TOOL_COUNT here + in the export script."
    )


def test_tier_sets_are_disjoint():
    # Tier 0 and Tier 3 (destructive) must never overlap — a read-only
    # tool can't also be destructive. Same for conditional vs. fixed tiers.
    assert TIER0_TOOLS.isdisjoint(DESTRUCTIVE_TOOLS)
    assert TIER0_TOOLS.isdisjoint(SYSTEM_WRITE_TOOLS)
    assert TIER0_TOOLS.isdisjoint(CONDITIONAL_TIER_TOOLS)
    assert DESTRUCTIVE_TOOLS.isdisjoint(SYSTEM_WRITE_TOOLS)
    assert DESTRUCTIVE_TOOLS.isdisjoint(CONDITIONAL_TIER_TOOLS)
    assert SYSTEM_WRITE_TOOLS.isdisjoint(CONDITIONAL_TIER_TOOLS)


def test_tier_sets_cover_all_tools():
    union = TIER0_TOOLS | CONDITIONAL_TIER_TOOLS | DESTRUCTIVE_TOOLS | SYSTEM_WRITE_TOOLS
    assert union == ALL_TOOLS, (
        f"Tier sets do not cover ALL_TOOLS. "
        f"Missing from any tier: {sorted(ALL_TOOLS - union)}; "
        f"in a tier but not in ALL_TOOLS: {sorted(union - ALL_TOOLS)}"
    )


def test_no_phantom_tools_leak_in():
    leaked = PHANTOM_TOOLS & ALL_TOOLS
    assert not leaked, (
        f"Phantom tool names from the pre-M2.0 classifier appeared in ALL_TOOLS: "
        f"{sorted(leaked)}. mcpd does not ship these — fix TOOL_CATEGORIES "
        f"in scripts/export_mcpd_catalogue.py."
    )


def test_canonical_mcpd_tools_present():
    # Sanity: a handful of the real mcpd tools must be in the catalogue.
    # If any of these go missing, the export script's TOOL_CATEGORIES is wrong.
    required = {
        "system.status", "system.cpu", "system.disk",
        "process.list", "process.inspect",
        "fs.read", "fs.write", "fs.delete",
        "service.start", "service.logs",
        "network.status", "network.dns.read",
        "package.query", "package.install", "package.upgrade",
    }
    missing = required - ALL_TOOLS
    assert not missing, f"Required mcpd tools missing from ALL_TOOLS: {sorted(missing)}"


def test_fs_write_is_only_conditional_tool():
    # fs.write is the ONE tool whose tier depends on the target path.
    # If a second conditional tool appears, the classifier needs a new
    # branch — fail loudly so we don't silently misclassify.
    assert CONDITIONAL_TIER_TOOLS == {"fs.write"}, (
        f"CONDITIONAL_TIER_TOOLS changed shape; got {sorted(CONDITIONAL_TIER_TOOLS)}. "
        "If a new conditional tool was added, extend classify() to handle it."
    )
