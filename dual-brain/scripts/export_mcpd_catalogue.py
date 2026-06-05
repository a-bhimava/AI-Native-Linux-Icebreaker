#!/usr/bin/env python3
"""
export_mcpd_catalogue.py — keep risk_classifier.py in sync with mcpd.

mcpd is the authoritative source of which tools exist. The classifier
needs a hand-curated mapping of each tool to its tier (read-only, write,
destructive, conditional-path). This script:

  1. Invokes the mcpd binary with a JSON-RPC `tools/list` request.
  2. Compares the returned tool list against TOOL_CATEGORIES below.
  3. In `emit` mode: writes dual-brain/controller/_mcpd_tools.py with
     the four canonical frozensets the classifier imports.
  4. In `check` mode: asserts no drift between TOOL_CATEGORIES and
     mcpd's actual catalogue; exits 0 on parity, 1 on drift.

This script IS the M2.0 fix for the risk_classifier.py drift bug.

Usage:
    # When developing locally and the mcpd binary lives at a known path
    python scripts/export_mcpd_catalogue.py emit \
        --mcpd ~/icebreaker/src/mcpd/target/release/mcpd

    # In CI (G2 gate)
    python scripts/export_mcpd_catalogue.py check

If a new tool is added to mcpd, this script will FAIL with a clear
message saying "uncategorised tool: X". The fix is to add the tool to
TOOL_CATEGORIES below with the correct category, then re-run `emit`.
Adding a tool that mcpd doesn't ship also fails the check.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# ── Canonical categorisation ─────────────────────────────────────────────────
#
# Every tool mcpd ships MUST appear here. Every tool here MUST exist in mcpd.
# This dict is the single source of truth for the classifier's tier behaviour;
# the four frozensets emitted into _mcpd_tools.py are derived from it.
#
# Categories:
#   tier0           → read-only; auto-execute; no prompt; no audit-worthy
#                     side effect
#   conditional     → tier depends on runtime params (e.g. fs.write tier
#                     1 in $HOME, tier 3 elsewhere); classifier handles
#                     the conditional in code
#   destructive     → always Tier 3 (HITL required); irreversible by
#                     nature (fs.delete, firewall flushes)
#   system_write    → Tier 2 medium-risk; auto-execute with audit + user
#                     notification
TOOL_CATEGORIES: dict[str, str] = {
    # ── Tier 0 read-only (14) ────────────────────────────────────────────
    "system.status":     "tier0",
    "system.uptime":     "tier0",
    "system.cpu":        "tier0",
    "system.memory":     "tier0",
    "system.disk":       "tier0",
    "process.list":      "tier0",
    "process.inspect":   "tier0",
    "fs.read":           "tier0",
    "fs.list":           "tier0",
    "fs.stat":           "tier0",
    "service.logs":      "tier0",
    "network.status":    "tier0",
    "network.dns.read":  "tier0",
    "package.query":     "tier0",

    # ── Conditional tier (1) ────────────────────────────────────────────
    # fs.write: Tier 1 inside $HOME, Tier 3 outside. The classifier
    # makes the decision at runtime against the actual path.
    "fs.write":          "conditional",

    # ── Always Tier 3 destructive (1) ────────────────────────────────────
    "fs.delete":         "destructive",

    # ── Tier 2 system writes (6) ────────────────────────────────────────
    "service.start":     "system_write",
    "service.stop":      "system_write",
    "service.restart":   "system_write",
    "package.install":   "system_write",
    "package.remove":    "system_write",
    "package.upgrade":   "system_write",
}

# Expected total — guards against silent merges that change the count
EXPECTED_TOOL_COUNT = 22


# ── mcpd interaction ────────────────────────────────────────────────────────

def _default_mcpd_paths() -> list[Path]:
    """Common locations to search for the mcpd binary."""
    return [
        Path.home() / "icebreaker" / "src" / "mcpd" / "target" / "release" / "mcpd",
        Path.cwd() / "src" / "mcpd" / "target" / "release" / "mcpd",
        Path.home() / "dual-brain" / ".." / "src" / "mcpd" / "target" / "release" / "mcpd",
    ]


def _find_mcpd(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            print(f"ERROR: mcpd binary not found at {p}", file=sys.stderr)
            sys.exit(2)
        return p
    for cand in _default_mcpd_paths():
        if cand.exists():
            return cand.resolve()
    print(
        "ERROR: mcpd binary not found in any default location. "
        "Pass --mcpd /path/to/mcpd.",
        file=sys.stderr,
    )
    for cand in _default_mcpd_paths():
        print(f"  searched: {cand}", file=sys.stderr)
    sys.exit(2)


def _list_tools(mcpd_bin: Path) -> tuple[list[str], str]:
    """Run mcpd with a tools/list request, return (tool names, schema_version)."""
    request = json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 1}) + "\n"
    try:
        proc = subprocess.run(
            [str(mcpd_bin)],
            input=request,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        print("ERROR: mcpd timed out responding to tools/list", file=sys.stderr)
        sys.exit(3)

    if proc.returncode != 0:
        print(
            f"ERROR: mcpd exited {proc.returncode}\n"
            f"  stderr: {proc.stderr[:400]}",
            file=sys.stderr,
        )
        sys.exit(3)

    # mcpd writes the response on the first stdout line; further lines may be
    # info/warn logs depending on RUST_LOG.
    first_line = proc.stdout.splitlines()[0] if proc.stdout else ""
    try:
        response = json.loads(first_line)
    except json.JSONDecodeError as e:
        print(f"ERROR: could not parse mcpd response: {e}", file=sys.stderr)
        print(f"  raw: {first_line[:400]}", file=sys.stderr)
        sys.exit(3)

    if "error" in response:
        print(f"ERROR: mcpd returned JSON-RPC error: {response['error']}", file=sys.stderr)
        sys.exit(3)

    result = response.get("result", {})
    tools = result.get("tools", [])
    schema_version = result.get("schema_version", "?")
    names = sorted(t["name"] for t in tools if "name" in t)
    return names, schema_version


# ── Drift check ────────────────────────────────────────────────────────────

def _drift(actual: set[str], known: set[str]) -> tuple[set[str], set[str]]:
    """Returns (missing_in_known, extra_in_known)."""
    return actual - known, known - actual


def _check(mcpd_bin: Path) -> int:
    actual_names, schema_version = _list_tools(mcpd_bin)
    actual_set = set(actual_names)
    known_set = set(TOOL_CATEGORIES.keys())

    print(f"mcpd schema_version: {schema_version}")
    print(f"mcpd advertises {len(actual_set)} tools (expected {EXPECTED_TOOL_COUNT})")

    missing, extra = _drift(actual_set, known_set)

    if missing:
        print("DRIFT: mcpd ships tools NOT in TOOL_CATEGORIES:", file=sys.stderr)
        for t in sorted(missing):
            print(f"  + {t}", file=sys.stderr)
    if extra:
        print("DRIFT: TOOL_CATEGORIES lists tools NOT in mcpd:", file=sys.stderr)
        for t in sorted(extra):
            print(f"  - {t}", file=sys.stderr)
    if len(actual_set) != EXPECTED_TOOL_COUNT:
        print(
            f"DRIFT: mcpd tool count is {len(actual_set)}, "
            f"expected {EXPECTED_TOOL_COUNT}. Update EXPECTED_TOOL_COUNT.",
            file=sys.stderr,
        )

    drifted = bool(missing or extra or len(actual_set) != EXPECTED_TOOL_COUNT)
    if drifted:
        print("FAIL — classifier catalogue is OUT OF SYNC with mcpd.", file=sys.stderr)
        return 1
    print("OK — classifier catalogue is in sync with mcpd.")
    return 0


# ── Emit ──────────────────────────────────────────────────────────────────

def _emit(mcpd_bin: Path, out_path: Path) -> int:
    actual_names, schema_version = _list_tools(mcpd_bin)
    actual_set = set(actual_names)
    known_set = set(TOOL_CATEGORIES.keys())

    if actual_set != known_set:
        # Run check() to surface the diff, then refuse to emit
        print("Refusing to emit while drifted. Running check for detail:", file=sys.stderr)
        return _check(mcpd_bin)

    by_category: dict[str, list[str]] = {
        "tier0": [],
        "conditional": [],
        "destructive": [],
        "system_write": [],
    }
    for tool, cat in TOOL_CATEGORIES.items():
        by_category[cat].append(tool)
    for v in by_category.values():
        v.sort()

    lines = [
        "# AUTO-GENERATED by scripts/export_mcpd_catalogue.py",
        "# DO NOT EDIT MANUALLY. Regenerate with:",
        "#   python scripts/export_mcpd_catalogue.py emit --mcpd <path>",
        "#",
        "# This module is the canonical mapping of mcpd tools to tier behaviour.",
        "# risk_classifier imports from here; CI G2 asserts parity via",
        "#   python scripts/export_mcpd_catalogue.py check",
        "",
        f'MCPD_SCHEMA_VERSION = "{schema_version}"',
        "",
        f"# All {len(actual_names)} tools mcpd advertises",
        "ALL_TOOLS = frozenset({",
        *[f'    "{t}",' for t in sorted(actual_names)],
        "})",
        "",
        f"# Tier 0 — read-only; auto-execute; no prompt ({len(by_category['tier0'])} tools)",
        "TIER0_TOOLS = frozenset({",
        *[f'    "{t}",' for t in by_category["tier0"]],
        "})",
        "",
        f"# Tier depends on runtime params (e.g. fs.write — $HOME vs outside) ({len(by_category['conditional'])} tools)",
        "CONDITIONAL_TIER_TOOLS = frozenset({",
        *[f'    "{t}",' for t in by_category["conditional"]],
        "})",
        "",
        f"# Always Tier 3 — destructive; HITL required ({len(by_category['destructive'])} tools)",
        "DESTRUCTIVE_TOOLS = frozenset({",
        *[f'    "{t}",' for t in by_category["destructive"]],
        "})",
        "",
        f"# Tier 2 — system writes; auto-execute with audit ({len(by_category['system_write'])} tools)",
        "SYSTEM_WRITE_TOOLS = frozenset({",
        *[f'    "{t}",' for t in by_category["system_write"]],
        "})",
        "",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")
    print(f"  ALL_TOOLS:               {len(actual_names)}")
    print(f"  TIER0_TOOLS:             {len(by_category['tier0'])}")
    print(f"  CONDITIONAL_TIER_TOOLS:  {len(by_category['conditional'])}")
    print(f"  DESTRUCTIVE_TOOLS:       {len(by_category['destructive'])}")
    print(f"  SYSTEM_WRITE_TOOLS:      {len(by_category['system_write'])}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("command", choices=["emit", "check"])
    parser.add_argument(
        "--mcpd",
        default=None,
        help="Path to the mcpd binary. Default: search common locations.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output path for the generated module (emit mode). "
             "Default: <repo>/dual-brain/controller/_mcpd_tools.py",
    )
    args = parser.parse_args()

    mcpd_bin = _find_mcpd(args.mcpd)

    if args.command == "check":
        return _check(mcpd_bin)
    elif args.command == "emit":
        if args.out is None:
            # Default: emit next to controller package, relative to this script's parent dir
            script_dir = Path(__file__).resolve().parent
            args.out = script_dir.parent / "controller" / "_mcpd_tools.py"
        return _emit(mcpd_bin, Path(args.out))
    return 2


if __name__ == "__main__":
    sys.exit(main())
