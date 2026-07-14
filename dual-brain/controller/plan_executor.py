"""v6.8 Task #148 — Plan-mode helpers.

Split from agent_graph_nodes.py so tests exercise pure functions
without spinning up a LangGraph. All functions here are:

- Pure (no I/O, no side effects, no clocks).
- Deterministic (same input → same output).
- Side-effect-free enough to be safe inside a LangGraph node that may
  re-run on interrupt resume.

Public API used by agent_graph_nodes.py:

- ``normalize_planner_output(raw)`` — accepts either a bare Intent
  ({"action": ...}) OR a plan wrapper ({"plan": [...]}). Returns
  a canonical list-of-steps. Backward compat entry point.
- ``resolve_step_markers(step, step_results)`` — substitutes
  ``$STEP_<N>_STDOUT`` markers in step.target and step.content using
  prior-step results. Returns a fresh dict (does not mutate).
- ``plan_max_tier(plan, risk_classify)`` — returns the max tier
  across every step. Used by RiskClassifier for whole-plan HITL gate.
- ``validate_resolved_intent(resolved, intent_schema)`` — post-
  substitution strict validation against intent.json (which rejects
  ``$`` in target — proves substitution actually happened).
- ``load_plan_schema(schemas_dir)`` — one-shot load of plan.json.

Marker syntax (D10): ``$STEP_<N>_STDOUT`` where N is 0-based step
index. Only supported today; richer markers can be added incrementally.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional


# ── Marker syntax ────────────────────────────────────────────────────

# Compile once; used in the hot path per step.
_MARKER_RE = re.compile(r"\$STEP_(\d+)_STDOUT")


def _extract_marker_indices(text: str) -> list[int]:
    """Return every step index referenced by markers in `text`.

    Used by tests + the executor to sanity-check that a step doesn't
    reference a future step (which would deadlock — step N cannot
    depend on step N+1)."""
    if not isinstance(text, str) or "$STEP_" not in text:
        return []
    return [int(m.group(1)) for m in _MARKER_RE.finditer(text)]


def _resolve_string(text: Any, step_results: dict[int, str]) -> Any:
    """Substitute ``$STEP_<N>_STDOUT`` markers in a string.

    Non-string inputs are returned unchanged (e.g. ints, None). Missing
    step results substitute the literal empty string — the resulting
    resolved intent will then fail intent.json validation (target
    minLength=1 for most tools), which surfaces the bug loudly rather
    than silently dispatching a broken tool call.
    """
    if not isinstance(text, str) or "$STEP_" not in text:
        return text

    def _sub(match: re.Match) -> str:
        idx = int(match.group(1))
        raw = step_results.get(idx, "")
        return _sanitize_result(raw)

    return _MARKER_RE.sub(_sub, text)


def _sanitize_result(raw: Any) -> str:
    """Convert a prior step result into a safe substitution string.

    Rules:
    - Trim trailing newlines that would land inside intent.target
      (fs.list, service.logs, etc. return trailing newlines).
    - Strip control characters that would trip intent.json's pattern.
    - Cap at 4096 chars — a runaway result must not blow up the next
      intent's target length limit.
    """
    if raw is None:
        return ""
    text = str(raw).rstrip("\n\r")
    # Strip C0/C1 control chars except tab/space.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    return text[:4096]


# ── Public helpers ──────────────────────────────────────────────────


def normalize_planner_output(raw: Any) -> list[dict]:
    """Accept either a bare Intent OR a plan wrapper; always return the
    canonical list-of-steps.

    Backward compat: a v6.7 QB emits `{"action": ..., "target": ...}` —
    that gets wrapped as a 1-step plan.

    A v6.8 QB may emit `{"plan": [step1, step2, ...]}` — that gets
    returned unwrapped.

    Raises ValueError if `raw` is neither shape (mis-typed / crashed
    QB response). Caller (planner_node) handles the error and reports
    planner failure to the user.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"planner output must be a dict, got {type(raw).__name__}")

    if "plan" in raw:
        plan = raw["plan"]
        if not isinstance(plan, list) or not plan:
            raise ValueError("plan must be a non-empty list")
        for i, step in enumerate(plan):
            if not isinstance(step, dict) or "action" not in step:
                raise ValueError(
                    f"plan[{i}] must be a dict with 'action', got {type(step).__name__}"
                )
        return list(plan)

    if "action" not in raw:
        raise ValueError("planner output has neither 'plan' nor 'action'")
    # Bare intent → 1-step plan.
    return [dict(raw)]


def resolve_step_markers(
    step: dict, step_results: dict[int, str]
) -> dict:
    """Return a new step dict with all supported marker fields resolved.

    D15 scope: only ``target`` and ``content`` get substitution. Action,
    params, pb_hint, reason, risk_level pass through unchanged. Any
    marker referencing an index >= the current step is a bug — caller
    is expected to only pass results for step indices strictly less
    than the current one, but this function does not enforce that; it
    substitutes whatever is in step_results (missing → "").
    """
    resolved = dict(step)
    if "target" in resolved:
        resolved["target"] = _resolve_string(resolved["target"], step_results)
    if "content" in resolved:
        resolved["content"] = _resolve_string(resolved["content"], step_results)
    return resolved


def step_marker_deps(step: dict) -> list[int]:
    """Return the sorted deduplicated list of prior-step indices this
    step depends on (via markers in target/content).

    Used for logging + sanity checks — a step referencing $STEP_5_STDOUT
    that runs at step_index=3 is a QB bug (forward reference).
    """
    deps = set()
    for field in ("target", "content"):
        for i in _extract_marker_indices(step.get(field, "") or ""):
            deps.add(i)
    return sorted(deps)


def plan_max_tier(
    plan: list[dict],
    risk_classify: Callable[[dict], Any],
) -> int:
    """Return the maximum Tier value across all steps.

    Used by RiskClassifier when a plan has more than one step —
    max-tier wins so HITL fires ONCE for the whole plan (D13).

    Any classifier that raises on a specific step is treated as
    Tier 3 (defensive): a step we cannot classify is high-risk by
    definition.
    """
    if not plan:
        return -1

    max_tier = -1
    for step in plan:
        try:
            result = risk_classify(step)
            tier_int = int(getattr(result, "tier", -1))
        except Exception:  # noqa: BLE001 — defensive; unclassifiable → HIGH
            tier_int = 3
        if tier_int > max_tier:
            max_tier = tier_int
    return max_tier


def validate_resolved_intent(
    resolved: dict, intent_schema: dict,
) -> Optional[str]:
    """Return None if the resolved intent passes intent.json validation,
    or a short human-facing error string if it does not.

    Called AFTER marker resolution. Any surviving ``$STEP_`` substring
    in target would fail intent.json's pattern (which forbids ``$``) —
    that's how we prove substitution actually happened.
    """
    try:
        import jsonschema
    except ImportError:  # pragma: no cover — jsonschema is a hard dep
        return None

    # intent.json requires intent_id; QB doesn't set it (server-owned).
    # Inject a placeholder for validation purposes only — the real UUID
    # comes from IntentStore.put at dispatch time.
    candidate = dict(resolved)
    candidate.setdefault(
        "intent_id", "00000000-0000-0000-0000-000000000000",
    )
    candidate.setdefault("params", {})
    candidate.setdefault("reason", "user_requested")
    candidate.setdefault("risk_level", "low")
    try:
        jsonschema.validate(candidate, intent_schema)
        return None
    except jsonschema.ValidationError as exc:
        return f"resolved intent failed schema: {exc.message[:200]}"


def load_plan_schema(schemas_dir: Path) -> dict:
    """Load plan.json from the shipped schemas directory.

    Kept module-level so the ~4 KB file lands in memory once at
    Controller.__init__ time; tests may pass their own dict.
    """
    path = Path(schemas_dir) / "plan.json"
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
