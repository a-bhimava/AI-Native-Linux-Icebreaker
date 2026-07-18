"""v6.10 F-65 + F-68 discipline: verifier prompt anti-hide guards.

Bugs that these tests prevent:

- **F-65 (2026-07-18):** rubric #2 in ``prompts/qb_verifier.txt`` used
  to say "primary path parameter of ``tool_call.params`` (usually
  ``params.path``)". For ``package.install``, the correct param is
  ``params.package``, not ``params.path``. The LLM verifier read that
  as a mismatch and every ``install htop`` / ``remove wget`` /
  ``upgrade curl`` query was rejected with ``qb_verifier_rejected``.
  Fix: enumerate the primary target param name per tool prefix.

- **F-68 (2026-07-18):** ``_emit_catalogue_block`` used to filter out
  GUI + RPA tools from the QB prompt. QB emitted ``system.unsupported``
  for every "take a screenshot" / "click Save" query even though the
  wire was alive since Phase 6T. Fix: widen the filter to include
  gui_agent + rpa_bridge.

The lesson (per user's frustration 2026-07-17): **it's already hard
to build features; we cannot afford to build them and then hide them
from the LLM by accident.** Every feature that ships MUST have a
test that would loudly fail if a future refactor re-hides it.

These tests are the anti-hide guardrail for the verifier prompt
specifically. Sister guards:
- test_tool_catalogue_yaml.py::test_catalogue_block_has_gui_and_rpa_sections
- test_tool_catalogue_yaml.py::test_generated_catalogue_block_contains_every_qb_emittable_tool
"""
from __future__ import annotations

from pathlib import Path


_VERIFIER_PROMPT = (
    Path(__file__).resolve().parent.parent / "prompts" / "qb_verifier.txt"
)


def _read() -> str:
    return _VERIFIER_PROMPT.read_text(encoding="utf-8")


# ── F-65: primary target param mapping is complete ─────────────────────

# Every writable/queryable tool the classifier can route to MUST have
# its primary target parameter documented in the verifier rubric —
# otherwise the LLM verifier defaults to "params.path" and rejects the
# legitimate tool call because the shape doesn't match. Missing entries
# cause silent qb_verifier_rejected outcomes.
_REQUIRED_MAPPINGS = {
    # Filesystem — all use params.path
    "params.path": {"fs.*", "nav.cd"},
    # Package operations — install/remove/upgrade take params.package,
    # query takes params.pattern
    "params.package": {"package.install", "package.remove", "package.upgrade"},
    "params.pattern": {"package.query"},
    # Service operations
    "params.unit": {"service.start", "service.stop", "service.restart", "service.logs"},
    # Process introspection
    "params.pid": {"process.inspect"},
    # Network
    "params.hostname": {"network.dns.read"},
    # RPA
    "params.workflow": {"rpa.execute_workflow"},
    "params.template": {"rpa.find_by_image"},
    # GUI — accepts params.window OR params.name
}


def test_verifier_prompt_has_target_param_mapping_table() -> None:
    """The rubric MUST include the phrase 'primary target parameter'
    and enumerate the per-tool mapping table. If a future edit collapses
    this back to a single 'params.path' rule, every non-fs tool call
    fails verifier and users see the F-65 symptom again."""
    text = _read()
    assert "primary target parameter" in text.lower(), (
        "F-65 regression: qb_verifier.txt lost the 'primary target parameter' "
        "wording. This is the phrase that teaches the LLM verifier to look "
        "up which params key carries the target for each tool. Without it, "
        "every package.install / service.start / rpa.execute_workflow query "
        "fails verifier because their primary param is not 'path'. Restore "
        "the full mapping table in rubric check #2."
    )


def test_verifier_prompt_maps_each_tool_family() -> None:
    """Every tool family listed in _REQUIRED_MAPPINGS MUST appear in the
    verifier prompt. If someone deletes even one line from the mapping
    table (e.g. drops the ``package.*`` entry), the corresponding tool
    family becomes verifier-rejected — silently, from the user's PoV.
    """
    text = _read()
    for param_key, tools in _REQUIRED_MAPPINGS.items():
        assert param_key in text, (
            f"F-65 regression: qb_verifier.txt does not mention the "
            f"target parameter {param_key!r}. This makes every tool call "
            f"for {sorted(tools)!r} verifier-reject silently. Restore "
            f"the row in rubric #2's mapping table."
        )
        for tool in tools:
            assert tool in text or tool.rstrip(".*") in text, (
                f"F-65 regression: qb_verifier.txt does not mention "
                f"tool prefix {tool!r} in the target-parameter table. "
                f"The LLM verifier defaults to params.path for it and "
                f"rejects legitimate calls. Add the mapping row."
            )


def test_verifier_prompt_mentions_gui_and_rpa_families() -> None:
    """v6.10 F-68 companion: GUI + RPA tools ship (12 total across
    gui_agent + rpa_bridge providers). Their target-parameter mapping
    MUST be in the verifier prompt so the LLM verifier accepts calls
    like ``gui.click {window: "Editor", name: "Save"}`` and
    ``rpa.execute_workflow {workflow: "smoke_test"}``. If they get
    dropped from the mapping table, every GUI + RPA turn will be
    verifier-rejected AFTER Track K un-hid them."""
    text = _read()
    for prefix in ("gui.click", "gui.type", "gui.select", "gui.screenshot",
                    "rpa.execute_workflow", "rpa.find_by_image"):
        assert prefix in text, (
            f"F-68 companion regression: qb_verifier.txt lost the "
            f"mapping for {prefix!r}. Track K un-hid GUI + RPA from "
            f"the QB catalogue; they'll now be emitted. If the "
            f"verifier prompt doesn't know their target-param shape, "
            f"every call gets rejected — same symptom class as F-65 "
            f"for packages. Restore the mapping in rubric #2."
        )
