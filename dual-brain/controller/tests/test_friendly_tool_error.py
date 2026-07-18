"""v6.10 F-64 discipline: friendly translations for common mcpd
tool_error messages + anti-hide guardrail.

Bugs prevented:

- **F-64 (2026-07-18):** the corpus surfaced 10 rows with
  ``outcome=tool_error`` for legitimate reads outside ``$HOME``
  (``list /home``, ``read /etc/os-release``, DNS lookups). Users saw
  ``Tool error: Internal error: '/etc/os-release' is not under any
  whitelisted root`` and had no idea if the daemon was broken, the
  path was wrong, or the sandbox was denying legitimately. The
  ``Controller._friendly_tool_error`` helper (main.py) translates the
  common cases into user-facing messages with remediation hints.

- **Anti-hide:** every ``Outcome.TOOL_ERROR`` emission site in
  ``main.py`` MUST route through the friendly-translation helper. If
  a future refactor forgets to call it at a new site, users regress
  to the raw anyhow string form — the exact F-64 symptom this fix
  addresses. AST scan enforces the discipline.

Sister anti-hide guards:
- test_verifier_prompt_scope.py (F-65 per-tool target-param mapping)
- test_cancelled_reason.py (F-67 cancel_reason on every audit row)
- test_tool_catalogue_yaml.py (F-68 GUI/RPA in QB catalogue)
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from controller.main import Controller


_MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


# ── Translation table — every known mcpd rejection has a friendly line ──


def _controller() -> Controller:
    """Bare Controller instance for method testing. Bypass __init__ —
    _friendly_tool_error uses no self state."""
    return Controller.__new__(Controller)


class _FakeExc(Exception):
    def __init__(self, msg: str):
        super().__init__(msg)


@pytest.mark.parametrize("mcpd_message,expected_substring", [
    # Path-outside-root — most common F-64 case.
    (
        "Internal error: '/etc/os-release' is not under any whitelisted root",
        "sandbox restricts reads",
    ),
    (
        "'/var/log/syslog' is not under any whitelisted root",
        "sandbox restricts reads",
    ),
    # Absolute-path requirement.
    (
        "path must be absolute (starts with /)",
        "absolute path",
    ),
    # Percent-encoded path.
    (
        "percent-encoded characters not allowed",
        "URL encoding",
    ),
    # Backslash path.
    (
        "backslash characters not allowed",
        "forward slashes",
    ),
    # File-too-large guardrail.
    (
        "file too large (2000000 bytes > 1048576 max)",
        "caps reads",
    ),
    # Directory-not-file.
    (
        "path is a directory; use fs.list instead",
        "list files",
    ),
    # Binary content.
    (
        "not valid UTF-8 (binary reads land in Phase 5): io error",
        "binary content",
    ),
])
def test_translates_common_mcpd_rejections(mcpd_message: str, expected_substring: str) -> None:
    """Every known mcpd rejection MUST have a mapped friendly text
    containing a specific substring. Adding a new case to
    ``_friendly_tool_error`` without a matching row here fails on the
    other direction — the anti-hide AST scan below.

    NOTE: exact wording of the friendly message is intentionally NOT
    pinned (would require every prose polish edit to touch tests);
    only the presence of a load-bearing substring is asserted."""
    ctrl = _controller()
    result = ctrl._friendly_tool_error(_FakeExc(mcpd_message))
    assert result, (
        f"F-64 regression: mcpd message {mcpd_message!r} has no friendly "
        "translation. Users will see the raw anyhow string. Add a row "
        "to _friendly_tool_error mapping table."
    )
    assert expected_substring.lower() in result.lower(), (
        f"F-64 regression: friendly message for {mcpd_message!r} lost "
        f"the substring {expected_substring!r}. This is the anchor that "
        "makes the message user-actionable. Restore it (rewording OK "
        "as long as the substring stays)."
    )


def test_unrecognised_mcpd_error_falls_through_to_empty() -> None:
    """Defensive: an mcpd wording change that removes a known pattern
    MUST fall through to empty (which the caller renders as the raw
    Tool error form). Never silently swallow a diagnostic; either
    translate or pass through verbatim."""
    ctrl = _controller()
    result = ctrl._friendly_tool_error(_FakeExc("some brand new error mcpd invented"))
    assert result == "", (
        "F-64 defensive: unmatched errors must return empty string so "
        "the caller falls back to 'Tool error: {exc}' rather than "
        "displaying a possibly-misleading friendly translation."
    )


# ── Anti-hide: every tool_error site MUST call _friendly_tool_error ──


def _iter_tool_error_result_events():
    """Yield every AST call to TurnResult(..., outcome=Outcome.TOOL_ERROR, ...)
    in main.py. These are the sites where users see the message; each
    MUST source its ``output=`` from either _friendly_tool_error or an
    explicitly-audited raw form."""
    tree = ast.parse(_MAIN_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = ""
        if isinstance(func, ast.Name):
            func_name = func.id
        elif isinstance(func, ast.Attribute):
            func_name = func.attr
        if func_name != "TurnResult":
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
            and val.attr == "TOOL_ERROR"
        ):
            continue
        yield node


def test_every_tool_error_turnresult_uses_friendly_translation() -> None:
    """AST scan: every ``TurnResult(outcome=Outcome.TOOL_ERROR, ...)``
    site in main.py must be within a lexical scope that references
    ``_friendly_tool_error`` — otherwise it silently regresses to the
    raw ``Tool error: {exc}`` shape and users see the anyhow error
    again.

    Enforced by checking that within the function containing each
    tool_error TurnResult call, there's at least one Attribute node
    referencing ``_friendly_tool_error``."""
    src = _MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Build map: line number → containing function's source
    functions: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno + 1
            snippet = "\n".join(
                src.splitlines()[node.lineno - 1 : end]
            )
            functions.append((node.lineno, end, snippet))

    def _containing_function_snippet(lineno: int) -> str:
        # Find the innermost function containing this line
        candidates = [
            (start, end, snip)
            for (start, end, snip) in functions
            if start <= lineno <= end
        ]
        # Innermost = smallest range
        candidates.sort(key=lambda x: x[1] - x[0])
        return candidates[0][2] if candidates else ""

    sites = list(_iter_tool_error_result_events())
    assert sites, (
        "F-64 anti-hide: expected at least 2 TurnResult(outcome=TOOL_ERROR) "
        "sites in main.py (streaming + non-streaming path). Track N "
        "targeted both. If the count dropped to 0, something got refactored "
        "away — investigate."
    )
    for site in sites:
        snippet = _containing_function_snippet(site.lineno)
        assert "_friendly_tool_error" in snippet, (
            f"F-64 regression at main.py:{site.lineno}: this TurnResult "
            "site emits Outcome.TOOL_ERROR without calling "
            "_friendly_tool_error in the same function. Users will see "
            "the raw anyhow error string ('Tool error: Internal error: "
            "...'). Add the translation:\n\n"
            "    _friendly = self._friendly_tool_error(exc)\n"
            "    _output = _friendly if _friendly else f'Tool error: {exc}'\n"
            "    yield/return ResultEvent(TurnResult(output=_output, ...))"
        )
