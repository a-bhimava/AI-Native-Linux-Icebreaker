"""v6.10 F-67 discipline: every ``Outcome.CANCELLED`` audit row MUST
carry a ``cancel_reason`` in its ``extra`` dict.

Rationale:
- The 150-query corpus run 2026-07-17 recorded 22 rows with
  ``outcome=cancelled``. No emission site had a diagnostic reason,
  so operators had zero visibility into WHY these turns disappeared.
  Best guess without a reason: client Ctrl+C, socket drop, cost limit,
  rate limit, admin abort, or a race with the daemon reader thread.
  All feel identical in the audit.
- Track M added ``cancel_reason: "client_disconnect_during_streaming"``
  to the GeneratorExit handler in ``main.py``. That's the one
  emission path today.
- Future paths (cost limit in Phase 7, rate limit, admin abort) will
  add their own strings via the same mechanism. This test is the
  anti-hide guardrail: adding a Cancelled emission without a reason
  fails here loudly.

Sister anti-hide guards this test complements:
- test_verifier_prompt_scope.py — verifier prompt target-param mapping
- test_tool_catalogue_yaml.py — GUI/RPA visibility in QB catalogue
- test_no_silent_swallow.py — F-53 exception surfacing baseline

The pattern is: **every place we drop a signal on the floor, add a
test that would fail loudly if a future refactor re-drops it.**
"""
from __future__ import annotations

import ast
from pathlib import Path


_MAIN_PY = (
    Path(__file__).resolve().parent.parent / "main.py"
)


def _load_main() -> ast.Module:
    src = _MAIN_PY.read_text(encoding="utf-8")
    return ast.parse(src)


def _iter_cancelled_audit_writes(tree: ast.Module):
    """Yield every AST node that writes AuditFields with
    outcome=Outcome.CANCELLED. Returns the AuditFields(...) Call node
    so callers can inspect the ``extra`` kwarg."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # We're looking for AuditFields(...) calls where a kwarg
        # ``outcome=Outcome.CANCELLED`` is present.
        func = node.func
        func_name = ""
        if isinstance(func, ast.Name):
            func_name = func.id
        elif isinstance(func, ast.Attribute):
            func_name = func.attr
        if func_name != "AuditFields":
            continue
        outcome_kw = next(
            (k for k in node.keywords if k.arg == "outcome"), None
        )
        if outcome_kw is None:
            continue
        val = outcome_kw.value
        if not (
            isinstance(val, ast.Attribute)
            and isinstance(val.value, ast.Name)
            and val.value.id == "Outcome"
            and val.attr == "CANCELLED"
        ):
            continue
        yield node


def _extra_dict_keys(call_node: ast.Call) -> set[str]:
    """Return the set of literal keys in the ``extra={...}`` kwarg of
    an AuditFields Call. If ``extra`` is not a literal dict (e.g. a
    variable reference), return an empty set — the anti-hide check
    fails cleanly rather than silently accepting the assumption."""
    extra_kw = next(
        (k for k in call_node.keywords if k.arg == "extra"), None
    )
    if extra_kw is None:
        return set()
    if not isinstance(extra_kw.value, ast.Dict):
        return set()
    keys: set[str] = set()
    for k in extra_kw.value.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.add(k.value)
    return keys


def test_every_cancelled_audit_write_has_cancel_reason() -> None:
    """F-67 anti-hide guard: any ``AuditFields(outcome=Outcome.CANCELLED,
    extra={...})`` MUST include ``"cancel_reason"`` in the extra dict.

    Adding a new Cancelled emission without a reason fails here with a
    clear message. Even if the reason string is a placeholder
    ("unknown"), the field's PRESENCE forces operators to think about
    diagnostic surface — matches the same discipline BP-13 codifies
    for regression guards."""
    tree = _load_main()
    calls = list(_iter_cancelled_audit_writes(tree))
    assert calls, (
        "F-67 anti-hide check: expected at least one AuditFields "
        "call with outcome=Outcome.CANCELLED in main.py — the streaming "
        "GeneratorExit path should still be there. If Track M's edit "
        "got reverted, this fails; restore the site."
    )
    for call in calls:
        keys = _extra_dict_keys(call)
        assert "cancel_reason" in keys, (
            f"F-67 regression at main.py:{call.lineno}: this "
            "AuditFields(outcome=Outcome.CANCELLED, ...) call does NOT "
            "populate `extra={'cancel_reason': ...}`. Every Cancelled "
            "audit row MUST carry a cancel_reason so operators can "
            "distinguish client_disconnect from cost_limit from admin_abort. "
            "Add the field; use 'unknown' if you truly have no diagnosis."
        )
        assert "cancelled_at_step" in keys, (
            f"F-67 companion: the same audit row at main.py:{call.lineno} "
            "should also carry cancelled_at_step so we know WHERE in the "
            "pipeline the cancel fired. If that's dropped, add it back."
        )
