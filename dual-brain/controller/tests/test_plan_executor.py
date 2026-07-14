"""v6.8 Task #148 — plan_executor helper regression lock.

Covers:
- normalize_planner_output: backward compat with bare intents;
  multi-step plans; malformed inputs raise ValueError.
- resolve_step_markers: substitutes target + content; leaves action,
  params, pb_hint untouched (D15 scope); handles missing markers
  gracefully; strips control chars + caps length.
- step_marker_deps: extracts forward references so the executor can
  detect QB bugs (step 3 referencing step 5 output).
- plan_max_tier: aggregates across steps; treats classifier failure
  as Tier 3 (defensive).
- validate_resolved_intent: post-substitution strict validation;
  unresolved markers ($STEP_) fail because intent.json forbids `$`.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller.plan_executor import (
    load_plan_schema,
    normalize_planner_output,
    plan_max_tier,
    resolve_step_markers,
    step_marker_deps,
    validate_resolved_intent,
)


# ── normalize_planner_output ────────────────────────────────────────


def test_normalize_bare_intent_wraps_as_one_step():
    raw = {"action": "fs.list", "target": "/tmp", "reason": "user_requested",
           "risk_level": "low"}
    plan = normalize_planner_output(raw)
    assert plan == [raw]
    # Independent copy — mutating one shouldn't affect the other.
    plan[0]["action"] = "MUTATED"
    assert raw["action"] == "fs.list"


def test_normalize_plan_wrapper_returns_list():
    raw = {"plan": [
        {"action": "network.status", "target": ""},
        {"action": "fs.write", "target": "/tmp/x.txt", "content": "$STEP_0_STDOUT"},
    ]}
    plan = normalize_planner_output(raw)
    assert len(plan) == 2
    assert plan[0]["action"] == "network.status"
    assert plan[1]["content"] == "$STEP_0_STDOUT"


def test_normalize_rejects_non_dict():
    with pytest.raises(ValueError, match="must be a dict"):
        normalize_planner_output("not a dict")  # type: ignore[arg-type]


def test_normalize_rejects_empty_plan():
    with pytest.raises(ValueError, match="non-empty"):
        normalize_planner_output({"plan": []})


def test_normalize_rejects_malformed_step():
    with pytest.raises(ValueError, match="plan\\[0\\]"):
        normalize_planner_output({"plan": [{"target": "no action"}]})


def test_normalize_rejects_neither_plan_nor_action():
    with pytest.raises(ValueError, match="neither"):
        normalize_planner_output({"target": "just a target"})


# ── resolve_step_markers ────────────────────────────────────────────


def test_resolve_substitutes_marker_in_target():
    step = {"action": "fs.list", "target": "$STEP_0_STDOUT"}
    resolved = resolve_step_markers(step, {0: "/home/user"})
    assert resolved["target"] == "/home/user"
    assert resolved["action"] == "fs.list"


def test_resolve_substitutes_marker_in_content():
    step = {"action": "fs.write", "target": "/tmp/x.txt",
            "content": "IP: $STEP_0_STDOUT\n"}
    resolved = resolve_step_markers(step, {0: "10.0.0.1"})
    assert resolved["content"] == "IP: 10.0.0.1\n"


def test_resolve_leaves_action_untouched_even_if_it_would_contain_marker():
    """D15: action never gets substitution. Even if a bug placed a
    marker in action, we do not substitute — the action must remain
    a compile-time constant."""
    step = {"action": "fs.list", "target": "/tmp", "params": {"k": "$STEP_0_STDOUT"}}
    resolved = resolve_step_markers(step, {0: "value"})
    # params is dict-only (not substituted in v6.8). Value stays literal.
    assert resolved["params"]["k"] == "$STEP_0_STDOUT"


def test_resolve_missing_step_result_substitutes_empty_string():
    """If QB references a step that never emitted a result, we
    substitute empty rather than crashing. Downstream intent.json
    validation surfaces the bug loudly (minLength=1 on target)."""
    step = {"action": "fs.list", "target": "$STEP_5_STDOUT"}
    resolved = resolve_step_markers(step, {})
    assert resolved["target"] == ""


def test_resolve_strips_trailing_newlines():
    step = {"action": "fs.write", "target": "/tmp/x", "content": "$STEP_0_STDOUT"}
    resolved = resolve_step_markers(step, {0: "1.2.3.4\n\n"})
    assert resolved["content"] == "1.2.3.4"


def test_resolve_strips_control_characters():
    """intent.json's target pattern forbids C0/C1 control chars.
    Sanitize before returning."""
    step = {"action": "fs.list", "target": "$STEP_0_STDOUT"}
    resolved = resolve_step_markers(step, {0: "hello\x00\x1fworld"})
    assert resolved["target"] == "helloworld"


def test_resolve_caps_result_at_4096_chars():
    step = {"action": "fs.write", "target": "/tmp/x",
            "content": "$STEP_0_STDOUT"}
    long_stdout = "A" * 10000
    resolved = resolve_step_markers(step, {0: long_stdout})
    assert len(resolved["content"]) == 4096


def test_resolve_leaves_step_dict_input_unchanged():
    """Returned dict is a fresh object; the input is not mutated."""
    step = {"action": "fs.list", "target": "$STEP_0_STDOUT"}
    original = dict(step)
    resolve_step_markers(step, {0: "x"})
    assert step == original


def test_resolve_multiple_markers_in_same_string():
    step = {"action": "fs.write", "target": "/tmp/x",
            "content": "IP: $STEP_0_STDOUT, PID: $STEP_1_STDOUT"}
    resolved = resolve_step_markers(step, {0: "1.2.3.4", 1: "42"})
    assert resolved["content"] == "IP: 1.2.3.4, PID: 42"


# ── step_marker_deps ────────────────────────────────────────────────


def test_step_marker_deps_empty_step_returns_empty():
    assert step_marker_deps({"action": "fs.list", "target": "/tmp"}) == []


def test_step_marker_deps_extracts_from_target():
    assert step_marker_deps({"action": "fs.list", "target": "$STEP_0_STDOUT"}) == [0]


def test_step_marker_deps_extracts_from_content():
    step = {"action": "fs.write", "target": "/tmp/x",
            "content": "$STEP_2_STDOUT"}
    assert step_marker_deps(step) == [2]


def test_step_marker_deps_deduplicates():
    step = {"action": "fs.write", "target": "$STEP_0_STDOUT",
            "content": "$STEP_0_STDOUT and $STEP_1_STDOUT"}
    assert step_marker_deps(step) == [0, 1]


# ── plan_max_tier ───────────────────────────────────────────────────


def _mock_classifier(tier_map: dict[str, int]):
    def _c(step: dict):
        return SimpleNamespace(tier=tier_map.get(step.get("action", ""), 0))
    return _c


def test_plan_max_tier_returns_max_across_steps():
    plan = [
        {"action": "network.status"},
        {"action": "fs.write"},
        {"action": "package.install"},
    ]
    tier = plan_max_tier(plan, _mock_classifier({
        "network.status": 0, "fs.write": 1, "package.install": 2,
    }))
    assert tier == 2


def test_plan_max_tier_empty_plan_returns_negative_one():
    assert plan_max_tier([], _mock_classifier({})) == -1


def test_plan_max_tier_classifier_exception_maps_to_tier_3():
    def _crashes(step):
        raise RuntimeError("broken")
    plan = [{"action": "fs.list"}]
    assert plan_max_tier(plan, _crashes) == 3


# ── validate_resolved_intent ───────────────────────────────────────


def _intent_schema() -> dict:
    schema_path = Path(__file__).parent.parent / "schemas" / "intent.json"
    with schema_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def test_validate_passes_on_clean_intent():
    schema = _intent_schema()
    resolved = {
        "action": "fs.list",
        "target": "/tmp",
        "params": {},
        "reason": "user_requested",
        "risk_level": "low",
    }
    assert validate_resolved_intent(resolved, schema) is None


def test_validate_fails_on_unresolved_marker():
    """The whole point: if a $STEP_ marker survives to validation,
    the intent.json target-pattern rejects `$` and we know substitution
    was skipped."""
    schema = _intent_schema()
    broken = {
        "action": "fs.write",
        "target": "$STEP_0_STDOUT",
        "params": {},
        "reason": "user_requested",
        "risk_level": "medium",
    }
    err = validate_resolved_intent(broken, schema)
    assert err is not None
    assert "schema" in err.lower()


# ── load_plan_schema ─────────────────────────────────────────────────


def test_load_plan_schema_returns_expected_shape():
    schemas_dir = Path(__file__).parent.parent / "schemas"
    schema = load_plan_schema(schemas_dir)
    assert schema["$id"].endswith("/plan/v1")
    assert "plan" in schema["properties"]
    assert schema["properties"]["plan"]["maxItems"] == 10


def test_load_plan_schema_defines_step_items_shape():
    schemas_dir = Path(__file__).parent.parent / "schemas"
    schema = load_plan_schema(schemas_dir)
    step_schema = schema["properties"]["plan"]["items"]
    assert "action" in step_schema["properties"]
    assert "target" in step_schema["properties"]
    # Action pattern must reject a literal `$` char (D15 anti-substitution).
    # We test the semantic — try to match an action string containing `$`.
    import re
    action_pattern = step_schema["properties"]["action"]["pattern"]
    assert re.match(action_pattern, "fs.list") is not None  # valid action
    assert re.match(action_pattern, "$STEP_0_FOO") is None  # marker-shaped rejected
