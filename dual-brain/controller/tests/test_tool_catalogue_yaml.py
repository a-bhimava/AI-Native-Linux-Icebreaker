"""v6.9 Scope O Layer 1 — regression floor for the tool catalogue.

tool_catalogue.yaml is the single source of truth for the mcpd tool
surface. Three consumers derive from it:

  1. controller/_mcpd_tools.py                — tier frozensets (risk classifier)
  2. controller/_intent_corpus_supported.py   — SUPPORTED_ACTIONS (this suite)
  3. controller/prompts/_catalogue_block.txt  — the block QB prompts substitute

Any of the three drifting from the YAML is a review-blocker. The tests
here are the drift-detector for the offline path (no mcpd binary
required — that lives in the export script's `check` mode + G25 gate
in ci.sh).
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml


_HERE = pathlib.Path(__file__).resolve().parent
_CONTROLLER = _HERE.parent
_CATALOGUE_YAML = _CONTROLLER / "tool_catalogue.yaml"
_MCPD_TOOLS_PY = _CONTROLLER / "_mcpd_tools.py"
_SUPPORTED_PY = _CONTROLLER / "_intent_corpus_supported.py"
_CATALOGUE_BLOCK = _CONTROLLER / "prompts" / "_catalogue_block.txt"


@pytest.fixture(scope="module")
def entries() -> list[dict]:
    with _CATALOGUE_YAML.open() as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, list), "tool_catalogue.yaml must be a top-level list"
    return data


# ─── Structural validation of the YAML ─────────────────────────────────────

_REQUIRED_FIELDS = {"name", "provided_by", "tier_hint", "risk_level_default", "one_line"}
_ALLOWED_TIER_HINTS = {
    "tier0", "conditional", "system_write", "destructive", "meta",
    "gui_readonly", "gui_write", "rpa_readonly", "rpa_write",
}
_ALLOWED_RISK_LEVELS = {"low", "medium", "high", "critical"}
_ALLOWED_PROVIDED_BY = {"mcpd", "controller", "manifest", "gui_agent", "rpa_bridge"}


def test_yaml_file_exists() -> None:
    assert _CATALOGUE_YAML.exists(), (
        f"{_CATALOGUE_YAML} missing — this file is Layer 1's single source"
    )


def test_yaml_has_entries(entries: list[dict]) -> None:
    assert len(entries) >= 20, (
        f"catalogue has {len(entries)} entries — expected ≥20 tools"
    )


def test_every_entry_has_required_fields(entries: list[dict]) -> None:
    for i, e in enumerate(entries):
        missing = _REQUIRED_FIELDS - set(e.keys())
        assert not missing, (
            f"catalogue entry #{i} ({e.get('name', '?')}) missing "
            f"required fields: {sorted(missing)}"
        )


def test_every_entry_has_valid_enums(entries: list[dict]) -> None:
    for e in entries:
        assert e["tier_hint"] in _ALLOWED_TIER_HINTS, (
            f"{e['name']}: unknown tier_hint {e['tier_hint']!r}"
        )
        assert e["risk_level_default"] in _ALLOWED_RISK_LEVELS, (
            f"{e['name']}: unknown risk_level_default {e['risk_level_default']!r}"
        )
        assert e["provided_by"] in _ALLOWED_PROVIDED_BY, (
            f"{e['name']}: unknown provided_by {e['provided_by']!r}"
        )


def test_no_duplicate_names(entries: list[dict]) -> None:
    names = [e["name"] for e in entries]
    seen: dict[str, int] = {}
    for i, n in enumerate(names):
        if n in seen:
            pytest.fail(f"duplicate name {n!r} at rows {seen[n]} and {i}")
        seen[n] = i


def test_names_have_valid_shape(entries: list[dict]) -> None:
    # Enforce dotted lowercase — matches mcpd tool naming convention and
    # everything that grep for tools relies on.
    pattern = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    for e in entries:
        assert pattern.match(e["name"]), (
            f"name {e['name']!r} violates dotted-lowercase convention"
        )


def test_meta_entries_are_controller_provided(entries: list[dict]) -> None:
    for e in entries:
        if e["tier_hint"] == "meta":
            assert e["provided_by"] == "controller", (
                f"{e['name']}: meta actions must be controller-provided, "
                f"not mcpd (mcpd tools/list would falsely include them)"
            )


# ─── Drift vs the three generated artifacts ───────────────────────────────

def test_generated_mcpd_tools_agrees_with_yaml(entries: list[dict]) -> None:
    """_mcpd_tools.py ALL_TOOLS must equal every dispatchable tool the
    classifier is expected to route — mcpd tools + manifest tools.

    v6.9 Bug B (2026-07-17): expanded from ``provided_by == "mcpd"`` to
    ``provided_by in {"mcpd","manifest"}`` because nav.cd (manifest) now
    lives in ALL_TOOLS so the risk classifier finds it and doesn't
    escalate to Tier 3 via BP-5. Prior narrower drift check let nav.cd
    slip past the classifier — that was the exact Bug B failure mode.
    """
    assert _MCPD_TOOLS_PY.exists(), (
        f"{_MCPD_TOOLS_PY} missing — run "
        "`python scripts/export_mcpd_catalogue.py emit --from-categories`"
    )
    from controller._mcpd_tools import ALL_TOOLS

    yaml_dispatchable = {
        e["name"]
        for e in entries
        if e["provided_by"] in {"mcpd", "manifest"}
    }
    if set(ALL_TOOLS) != yaml_dispatchable:
        diff = set(ALL_TOOLS) ^ yaml_dispatchable
        pytest.fail(
            f"DRIFT: _mcpd_tools.py ALL_TOOLS != YAML dispatchable "
            f"(mcpd + manifest) entries. Symmetric diff: {sorted(diff)}. "
            "Regenerate with "
            "`python scripts/export_mcpd_catalogue.py emit --from-categories`."
        )


def test_generated_supported_actions_agrees_with_yaml(entries: list[dict]) -> None:
    """_intent_corpus_supported.SUPPORTED_ACTIONS must equal YAML entries
    whose provided_by is mcpd, controller, or manifest. GUI + RPA entries
    are excluded because QB doesn't emit them directly (see generator
    docstring + main.py::_SUPPORTED_ACTIONS parity)."""
    assert _SUPPORTED_PY.exists(), (
        f"{_SUPPORTED_PY} missing — run the emit script"
    )
    from controller._intent_corpus_supported import SUPPORTED_ACTIONS

    yaml_qb_emittable = {
        e["name"] for e in entries
        if e.get("provided_by") in {"mcpd", "controller", "manifest"}
    }
    if set(SUPPORTED_ACTIONS) != yaml_qb_emittable:
        diff = set(SUPPORTED_ACTIONS) ^ yaml_qb_emittable
        pytest.fail(
            f"DRIFT: SUPPORTED_ACTIONS != YAML QB-emittable names. "
            f"Symmetric diff: {sorted(diff)}. Regenerate."
        )


def test_generated_catalogue_block_contains_every_qb_emittable_tool(entries: list[dict]) -> None:
    """prompts/_catalogue_block.txt must mention EVERY dispatchable entry
    the controller knows how to route: mcpd + controller meta + Layer 2
    manifest + gui_agent + rpa_bridge.

    v6.10 F-68 (2026-07-18): widened from `{mcpd, controller, manifest}`.
    Legacy comment "GUI + RPA are excluded — QB doesn't emit them
    directly" was stale; main.py:1078-1086 has been dispatching gui.*
    to GuiAgent and rpa.* to _execute_rpa_workflow for the full Phase 6T
    era. The prompt block filter was the only thing keeping QB blind —
    every "take a screenshot" / "click login" query landed in
    system.unsupported because QB never saw those tools. This test is
    the regression guard: if a future refactor re-hides them, it fails
    with a clear name."""
    assert _CATALOGUE_BLOCK.exists(), (
        f"{_CATALOGUE_BLOCK} missing — run the emit script"
    )
    block_text = _CATALOGUE_BLOCK.read_text(encoding="utf-8")
    for e in entries:
        if e.get("provided_by") not in {
            "mcpd", "controller", "manifest", "gui_agent", "rpa_bridge",
        }:
            continue
        assert e["name"] in block_text, (
            f"catalogue block does not mention {e['name']!r} — "
            "regenerate via `emit --from-categories`"
        )


def test_catalogue_block_has_gui_and_rpa_sections(entries: list[dict]) -> None:
    """v6.10 F-68 regression pin — the block MUST include the GUI + RPA
    sections. If the export script's tier-hint routing drops them or a
    future refactor filters `gui_agent`/`rpa_bridge` provided_by, this
    test fails loudly (instead of silently regressing to invisible
    GUI/RPA tools like v6.9 shipped)."""
    block_text = _CATALOGUE_BLOCK.read_text(encoding="utf-8")
    yaml_names = {e["name"] for e in entries}
    for prefix, label in (("gui.", "GUI"), ("rpa.", "RPA")):
        expected = {n for n in yaml_names if n.startswith(prefix)}
        if not expected:
            continue  # None shipped for this prefix; nothing to guard.
        for tool in expected:
            assert tool in block_text, (
                f"F-68 regression: {label} tool {tool!r} is in the yaml "
                f"but missing from _catalogue_block.txt. QB will emit "
                f"system.unsupported for every {label} query if this "
                f"drift lands. Regenerate via `emit --from-categories`."
            )


def test_generated_catalogue_block_has_f35_wording(entries: list[dict]) -> None:
    """The F-35 wording ('the ONLY correct action...') is load-bearing for
    QB behavior — a paraphrase in the generator would silently regress
    every unsupported-intent turn. Lock the exact substring."""
    block_text = _CATALOGUE_BLOCK.read_text(encoding="utf-8")
    if any(e["tier_hint"] == "meta" for e in entries):
        assert "F-35" in block_text, "meta entries present but F-35 marker missing"
        assert "the ONLY correct action" in block_text, (
            "meta entries present but F-35 wording changed — regressing "
            "unsupported-intent routing"
        )


# ─── Drift vs TOOL_CATEGORIES (the generator's other input) ───────────────

def test_yaml_mcpd_tier_hints_agree_with_tool_categories(entries: list[dict]) -> None:
    """TOOL_CATEGORIES in scripts/export_mcpd_catalogue.py stays as the
    risk-classifier's authoritative tier map. YAML mcpd entries must
    agree with it to prevent silent divergence."""
    import importlib.util
    script_path = _CONTROLLER.parent / "scripts" / "export_mcpd_catalogue.py"
    spec = importlib.util.spec_from_file_location("_export_script", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tool_categories = module.TOOL_CATEGORIES

    for e in entries:
        if e["provided_by"] != "mcpd":
            continue
        assert e["name"] in tool_categories, (
            f"{e['name']} in YAML but not in TOOL_CATEGORIES — "
            "add it to scripts/export_mcpd_catalogue.py::TOOL_CATEGORIES "
            "with the matching tier."
        )
        assert e["tier_hint"] == tool_categories[e["name"]], (
            f"{e['name']}: YAML tier_hint={e['tier_hint']!r} but "
            f"TOOL_CATEGORIES={tool_categories[e['name']]!r}"
        )


# ─── Prompt loader substitution round-trip ────────────────────────────────

def test_prompt_loader_substitutes_catalogue_placeholder() -> None:
    """Loading any of the 4 QB prompts must produce text WITHOUT
    {{CATALOGUE}} and WITH at least one known tool name from the YAML.
    This is the end-to-end check that Layer 1's wiring survives."""
    from types import SimpleNamespace
    from controller.config import PromptLoader

    cfg = SimpleNamespace(
        prompts_dir=str(_CONTROLLER / "prompts"),
        qb_local="", qb_anthropic="", qb_gemini="", qb_openai="",
        pb="", qb_verifier="",
    )
    pl = PromptLoader(cfg)
    for name in ("qb_anthropic", "qb_gemini", "qb_openai", "qb_local"):
        text = pl.get(name)
        assert "{{CATALOGUE}}" not in text, (
            f"{name}: placeholder survived substitution"
        )
        assert "system.status" in text, (
            f"{name}: expected system.status in substituted text"
        )
        assert "system.unsupported" in text, (
            f"{name}: expected system.unsupported in substituted text (F-35)"
        )
