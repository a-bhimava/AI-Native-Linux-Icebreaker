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

import yaml  # v6.9 Scope O Layer 1 — reads tool_catalogue.yaml

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

    # ── Manifest-served (v6.9 Scope O Layer 2A) (1) ─────────────────────
    # nav.cd is dispatched by controller/manifest_loader before mcpd
    # sees the intent (see manifests/nav.cd.yaml + impl_kinds/
    # session_op.py). Included in TOOL_CATEGORIES so the risk classifier
    # + tier 0 fast path recognize it. Without this the classifier hits
    # BP-5 escalate-only and slaps Tier 3 on every nav.cd — Bug B in
    # the 2026-07-17 UTM sweep, F-# entry in GROUND_TRUTH § 7.
    "nav.cd":            "tier0",
}

# Expected total — guards against silent merges that change the count.
# v6.9 (2026-07-17): 22 mcpd + 1 manifest (nav.cd) = 23.
EXPECTED_TOOL_COUNT = 23


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


# ── v6.9 Scope O Layer 1: YAML catalogue loader + prompt-block + supported-actions writers ─
#
# The YAML lives at dual-brain/controller/tool_catalogue.yaml. Its purpose is
# to be the ONE source that describes every tool. TOOL_CATEGORIES stays as
# the risk-classifier's tier map (unchanged by v6.9); the YAML must agree
# with it for mcpd-provided entries. Drift in either direction fails the
# generator so nothing else can regenerate on top of it.

_CATALOGUE_YAML = (
    Path(__file__).resolve().parent.parent / "controller" / "tool_catalogue.yaml"
)


def _load_catalogue_yaml() -> list[dict]:
    if not _CATALOGUE_YAML.exists():
        print(f"ERROR: tool_catalogue.yaml not found at {_CATALOGUE_YAML}",
              file=sys.stderr)
        sys.exit(3)
    with _CATALOGUE_YAML.open("r", encoding="utf-8") as fh:
        entries = yaml.safe_load(fh)
    if not isinstance(entries, list):
        print("ERROR: tool_catalogue.yaml must be a top-level list of dicts",
              file=sys.stderr)
        sys.exit(3)
    required = {"name", "provided_by", "tier_hint", "risk_level_default", "one_line"}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            print(f"ERROR: tool_catalogue.yaml entry #{i} is not a dict",
                  file=sys.stderr)
            sys.exit(3)
        missing = required - set(entry.keys())
        if missing:
            print(f"ERROR: tool_catalogue.yaml entry '{entry.get('name','?')}' "
                  f"missing required fields: {sorted(missing)}", file=sys.stderr)
            sys.exit(3)
    return entries


def _yaml_vs_tool_categories_drift(entries: list[dict]) -> tuple[set[str], set[str], set[str]]:
    """Returns (dispatchable_missing_in_categories, categories_missing_in_yaml, tier_mismatches).

    dispatchable_missing_in_categories: names in YAML with provided_by ∈
                                        {mcpd, manifest} that are NOT in
                                        TOOL_CATEGORIES. The risk classifier
                                        reads TOOL_CATEGORIES for every tool
                                        it might dispatch (mcpd calls +
                                        manifest-served controller-side
                                        tools like nav.cd); a tool missing
                                        here hits BP-5 escalate-only default
                                        Tier 3.
    categories_missing_in_yaml: names in TOOL_CATEGORIES that are NOT in YAML.
    tier_mismatches: names where the YAML tier_hint disagrees with
                     TOOL_CATEGORIES for the same tool.

    v6.9 (2026-07-17, Bug B): previously filtered on provided_by="mcpd"
    only, which let nav.cd (provided_by="manifest") slip past the drift
    check. Extended to catch every dispatchable tool.
    """
    yaml_dispatchable = {
        e["name"]: e["tier_hint"]
        for e in entries
        if e.get("provided_by") in {"mcpd", "manifest"}
    }
    cat_names = set(TOOL_CATEGORIES.keys())
    yaml_names = set(yaml_dispatchable.keys())
    tier_mismatches = {
        name for name in yaml_names & cat_names
        if yaml_dispatchable[name] != TOOL_CATEGORIES[name]
    }
    return (
        yaml_names - cat_names,
        cat_names - yaml_names,
        tier_mismatches,
    )


def _emit_catalogue_block(entries: list[dict], out_path: Path) -> None:
    """Write the prompt block QB prompts consume via {{CATALOGUE}} placeholder.

    Groups mcpd-provided entries by tier_hint (tier0/conditional/system_write/
    destructive) and appends the controller-side `system.unsupported` meta
    action at the end. Format matches what qb_anthropic / qb_openai already
    ship inside their <catalogue>...</catalogue> blocks, so the substitution
    is a drop-in for those two; qb_gemini + qb_local wrap it in their own
    section headers.
    """
    groups: dict[str, list[str]] = {
        "tier0": [], "conditional": [], "system_write": [], "destructive": [], "meta": [],
        "gui_readonly": [], "gui_write": [],
        "rpa_readonly": [], "rpa_write": [],
    }
    # v6.10 F-68 (2026-07-18): un-hide GUI + RPA from the QB prompt.
    # PRIOR filter excluded provided_by ∈ {gui_agent, rpa_bridge} on the
    # rationale that "GUI + RPA are dispatched from higher-level
    # intents" — but main.py:1078-1086 explicitly dispatches
    # tool_name.startswith("gui.") to GuiAgent and startswith("rpa.")
    # to _execute_rpa_workflow. The wire was alive; QB was blindfolded.
    # Every "open Firefox" / "take a screenshot" query landed in
    # system.unsupported because QB literally couldn't see those tools.
    for e in entries:
        if e.get("provided_by") not in {
            "mcpd", "controller", "manifest", "gui_agent", "rpa_bridge",
        }:
            continue
        groups.setdefault(e["tier_hint"], []).append(e["name"])
    for v in groups.values():
        v.sort()

    lines = [
        "# AUTO-GENERATED by scripts/export_mcpd_catalogue.py from",
        "# controller/tool_catalogue.yaml. DO NOT EDIT MANUALLY.",
        "# QB prompts substitute this via the {{CATALOGUE}} placeholder.",
        "",
    ]
    if groups["tier0"]:
        lines.append(
            f"read-only → risk_level=low: {', '.join(groups['tier0'])}"
        )
    if groups["conditional"]:
        # fs.write is the only conditional today; document the special case
        # inline so QB emits the correct risk_level per path scope.
        lines.append(
            f"write inside $HOME → risk_level=low: {', '.join(groups['conditional'])}"
        )
    if groups["system_write"]:
        lines.append(
            f"system writes → risk_level=medium: {', '.join(groups['system_write'])}"
        )
    if groups["destructive"] or "fs.write" in groups["conditional"]:
        d = list(groups["destructive"])
        if "fs.write" in groups["conditional"]:
            d.append("fs.write (outside $HOME)")
        lines.append(
            f"destructive / outside home → risk_level=critical: {', '.join(sorted(d))}"
        )
    # v6.10 F-68: GUI + RPA lines. GUI = accessibility (AT-SPI) —
    # readonly = query the tree / screenshot; write = click/type/select.
    # RPA = Robot Framework — readonly = ping/find/list; write = run a
    # workflow (Tier 3 because arbitrary uinput synthesis).
    if groups["gui_readonly"]:
        lines.append(
            f"GUI read-only (AT-SPI query) → risk_level=low: "
            f"{', '.join(groups['gui_readonly'])}"
        )
    if groups["gui_write"]:
        lines.append(
            f"GUI writes (AT-SPI action) → risk_level=medium: "
            f"{', '.join(groups['gui_write'])}"
        )
    if groups["rpa_readonly"]:
        lines.append(
            f"RPA read-only (Robot query) → risk_level=low: "
            f"{', '.join(groups['rpa_readonly'])}"
        )
    if groups["rpa_write"]:
        lines.append(
            f"RPA writes (Robot workflow, uinput) → risk_level=high: "
            f"{', '.join(groups['rpa_write'])}"
        )
    if groups["meta"]:
        # F-35 wording preserved verbatim — this is load-bearing for QB behavior.
        for name in groups["meta"]:
            lines.append(
                f"no matching tool → risk_level=low: {name} "
                f"(F-35 — the ONLY correct action when no listed tool fits; "
                f"NEVER substitute a lookalike)"
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _emit_supported_actions(entries: list[dict], out_path: Path) -> None:
    """Write controller/_intent_corpus_supported.py — the SUPPORTED_ACTIONS
    frozenset test_intent_corpus.py imports.

    Scope: actions QB is permitted to EMIT — mcpd-provided tools + the
    controller-side meta escape hatch (system.unsupported) + Layer 2
    manifest-served tools (nav.cd, ...). GUI + RPA are excluded because
    QB doesn't emit them directly (the controller synthesizes those
    calls from higher-level intents); they would fail main.py's
    `_SUPPORTED_ACTIONS` guard and rewrite to system.unsupported.
    Keeping them out here preserves parity with main.py so
    test_supported_actions_match_controller_source stays green."""
    names = sorted({
        e["name"] for e in entries
        if e.get("provided_by") in {"mcpd", "controller", "manifest"}
    })
    lines = [
        "# AUTO-GENERATED by scripts/export_mcpd_catalogue.py from",
        "# controller/tool_catalogue.yaml. DO NOT EDIT MANUALLY.",
        "# Regenerate with: python scripts/export_mcpd_catalogue.py emit --from-categories",
        "",
        "SUPPORTED_ACTIONS = frozenset({",
        *[f'    "{n}",' for n in names],
        "})",
        "",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def _emit_layer1_artifacts(entries: list[dict], controller_dir: Path) -> None:
    """Run the two v6.9 writers as a pair. Called from _emit and
    _emit_from_categories after _mcpd_tools.py lands."""
    block_path = controller_dir / "prompts" / "_catalogue_block.txt"
    supported_path = controller_dir / "_intent_corpus_supported.py"
    _emit_catalogue_block(entries, block_path)
    _emit_supported_actions(entries, supported_path)
    _append_non_mcpd_frozensets(entries, controller_dir / "_mcpd_tools.py")
    print(f"Wrote {block_path.relative_to(controller_dir.parent)} "
          f"({block_path.stat().st_size} bytes)")
    print(f"Wrote {supported_path.relative_to(controller_dir.parent)} "
          f"({supported_path.stat().st_size} bytes)")
    print(f"Appended GUI + RPA frozensets to "
          f"{(controller_dir / '_mcpd_tools.py').relative_to(controller_dir.parent)}")


def _append_non_mcpd_frozensets(entries: list[dict], mcpd_tools_path: Path) -> None:
    """v6.9 Scope O Layer 1: risk_classifier.py imports GUI_* and RPA_*
    frozensets alongside mcpd's. Before Layer 1 these were hand-appended
    to _mcpd_tools.py after regen (brittle: every emit wiped them). Now
    they live in tool_catalogue.yaml with `provided_by: gui_agent | rpa_bridge`
    and this function appends them to the auto-generated file."""
    gui_ro = sorted(e["name"] for e in entries
                    if e.get("provided_by") == "gui_agent"
                    and e.get("tier_hint") == "gui_readonly")
    gui_wr = sorted(e["name"] for e in entries
                    if e.get("provided_by") == "gui_agent"
                    and e.get("tier_hint") == "gui_write")
    rpa_ro = sorted(e["name"] for e in entries
                    if e.get("provided_by") == "rpa_bridge"
                    and e.get("tier_hint") == "rpa_readonly")
    rpa_wr = sorted(e["name"] for e in entries
                    if e.get("provided_by") == "rpa_bridge"
                    and e.get("tier_hint") == "rpa_write")

    def _fset(name: str, items: list[str]) -> list[str]:
        lines = [f"{name} = frozenset({{"]
        for it in items:
            lines.append(f'    "{it}",')
        lines.append("})")
        return lines

    appendix = [
        "",
        "",
        "# ── GUI Agent tools (v6.9 Scope O Layer 1: from tool_catalogue.yaml) ─",
        "",
        *_fset("GUI_READONLY_TOOLS", gui_ro),
        "",
        *_fset("GUI_WRITE_TOOLS", gui_wr),
        "",
        "ALL_GUI_TOOLS = GUI_READONLY_TOOLS | GUI_WRITE_TOOLS",
        "",
        "",
        "# ── RPA Bridge tools (v6.9 Scope O Layer 1: from tool_catalogue.yaml) ─",
        "",
        *_fset("RPA_READONLY_TOOLS", rpa_ro),
        "",
        *_fset("RPA_WRITE_TOOLS", rpa_wr),
        "",
        "ALL_RPA_TOOLS = RPA_READONLY_TOOLS | RPA_WRITE_TOOLS",
        "",
    ]
    with mcpd_tools_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(appendix))


# ─────────────────────────────────────────────────────────────────────────


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

    # v6.9 Scope O Layer 1: also assert YAML ↔ TOOL_CATEGORIES parity so
    # the YAML source-of-truth can never diverge from the classifier map.
    entries = _load_catalogue_yaml()
    yaml_missing, cat_missing, tier_mismatches = _yaml_vs_tool_categories_drift(entries)
    if yaml_missing:
        print("DRIFT: tool_catalogue.yaml lists mcpd tools NOT in TOOL_CATEGORIES:",
              file=sys.stderr)
        for t in sorted(yaml_missing):
            print(f"  + {t}", file=sys.stderr)
        drifted = True
    if cat_missing:
        print("DRIFT: TOOL_CATEGORIES lists tools NOT in tool_catalogue.yaml:",
              file=sys.stderr)
        for t in sorted(cat_missing):
            print(f"  - {t}", file=sys.stderr)
        drifted = True
    if tier_mismatches:
        print("DRIFT: tier_hint mismatches between YAML and TOOL_CATEGORIES:",
              file=sys.stderr)
        for t in sorted(tier_mismatches):
            e = next(x for x in entries if x["name"] == t)
            print(f"  {t}: yaml={e['tier_hint']} categories={TOOL_CATEGORIES[t]}",
                  file=sys.stderr)
        drifted = True

    if drifted:
        print("FAIL — classifier catalogue is OUT OF SYNC.", file=sys.stderr)
        return 1
    print("OK — classifier catalogue is in sync with mcpd AND tool_catalogue.yaml.")
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

    # v6.9 Scope O Layer 1: also emit prompt block + supported-actions
    # frozenset from tool_catalogue.yaml. YAML must agree with mcpd; the
    # check above already covers mcpd ↔ TOOL_CATEGORIES.
    entries = _load_catalogue_yaml()
    ym, cm, tm = _yaml_vs_tool_categories_drift(entries)
    if ym or cm or tm:
        print("ERROR: tool_catalogue.yaml drifted vs TOOL_CATEGORIES; "
              "refusing to emit Layer 1 artifacts. Run `check` for detail.",
              file=sys.stderr)
        return 1
    _emit_layer1_artifacts(entries, out_path.parent)
    return 0


def _emit_from_categories(out_path: Path) -> int:
    """Offline emit mode (Mac dev): generate _mcpd_tools.py from TOOL_CATEGORIES
    without invoking mcpd. Used when no mcpd binary is available locally.

    The canonical drift check still happens on the VM via `check` mode against
    a live mcpd; this function only writes the module the classifier imports.
    schema_version is tagged "from-categories" so consumers know the file
    hasn't been cross-checked against a running mcpd.
    """
    actual_names = sorted(TOOL_CATEGORIES.keys())

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
        "# AUTO-GENERATED by scripts/export_mcpd_catalogue.py (--from-categories)",
        "# DO NOT EDIT MANUALLY. Regenerate with:",
        "#   python scripts/export_mcpd_catalogue.py emit --from-categories",
        "# or the canonical mcpd-verified version with:",
        "#   python scripts/export_mcpd_catalogue.py emit --mcpd <path>",
        "#",
        "# schema_version is 'from-categories' when emitted offline; the canonical",
        "# CI gate (G2) regenerates this from a live mcpd on the VM.",
        "",
        'MCPD_SCHEMA_VERSION = "from-categories"',
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
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes) [from-categories]")
    print(f"  ALL_TOOLS:               {len(actual_names)}")
    print(f"  TIER0_TOOLS:             {len(by_category['tier0'])}")
    print(f"  CONDITIONAL_TIER_TOOLS:  {len(by_category['conditional'])}")
    print(f"  DESTRUCTIVE_TOOLS:       {len(by_category['destructive'])}")
    print(f"  SYSTEM_WRITE_TOOLS:      {len(by_category['system_write'])}")

    # v6.9 Scope O Layer 1: also emit prompt block + supported-actions
    # frozenset from tool_catalogue.yaml.
    entries = _load_catalogue_yaml()
    ym, cm, tm = _yaml_vs_tool_categories_drift(entries)
    if ym or cm or tm:
        print("ERROR: tool_catalogue.yaml drifted vs TOOL_CATEGORIES; "
              "refusing to emit Layer 1 artifacts. Run `check` for detail.",
              file=sys.stderr)
        return 1
    _emit_layer1_artifacts(entries, out_path.parent)
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
    parser.add_argument(
        "--from-categories",
        action="store_true",
        help="emit only: generate the module from TOOL_CATEGORIES without "
             "invoking mcpd. Useful on Mac where no mcpd binary is built. "
             "Canonical CI gate (check mode) still requires a live mcpd.",
    )
    args = parser.parse_args()

    if args.command == "emit" and args.out is None:
        # Default: emit next to controller package, relative to this script's parent dir
        script_dir = Path(__file__).resolve().parent
        args.out = script_dir.parent / "controller" / "_mcpd_tools.py"

    if args.command == "emit" and args.from_categories:
        return _emit_from_categories(Path(args.out))

    if args.from_categories and args.command != "emit":
        print("ERROR: --from-categories is only valid with `emit`.", file=sys.stderr)
        return 2

    mcpd_bin = _find_mcpd(args.mcpd)

    if args.command == "check":
        return _check(mcpd_bin)
    elif args.command == "emit":
        return _emit(mcpd_bin, Path(args.out))
    return 2


if __name__ == "__main__":
    sys.exit(main())
