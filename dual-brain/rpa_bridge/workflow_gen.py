"""Workflow generator — translates PB tool call params into Robot Framework keyword sequences.

This is the critical security boundary for RPA operations (ADR-17).
Only keywords in ``ALLOWED_KEYWORDS`` may execute; everything else is
rejected before Robot Framework is ever imported.

Constraints:
  - Max 20 keywords per workflow (BP-10)
  - ``Sleep`` duration capped at 5 seconds
  - Keyword names validated against the allowlist (exact match, case-sensitive)
  - Argument count validated against (min_args, max_args) arity spec
  - ``.robot`` file written to scratch dir for audit trail (0o600 perms)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


MAX_KEYWORDS = 20
MAX_SLEEP_SECONDS = 5.0

_TIME_PATTERN = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)?)\s*(?:s(?:ec(?:ond)?s?)?)?|"
    r"(\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?|"
    r"(\d+)\s*ms|"
    r"(\d+(?:\.\d+)?))\s*$",
    re.IGNORECASE,
)


class WorkflowError(Exception):
    """Raised when workflow generation fails."""

    def __init__(self, message: str, keyword_name: str = "", reason: str = "") -> None:
        super().__init__(message)
        self.keyword_name = keyword_name
        self.reason = reason


# ── Keyword allowlist (security-critical) ──
# Maps keyword name → (min_args, max_args) for arity validation.
# Only UI-interaction, waiting, navigation, and screenshot keywords allowed.

ALLOWED_KEYWORDS: dict[str, tuple[int, int]] = {
    # Mouse interaction
    "Click Element":                    (1, 1),
    "Click Element At Coordinates":     (2, 2),
    "Double Click Element":             (1, 1),
    "Right Click Element":              (1, 1),

    # Keyboard interaction
    "Input Text":                       (2, 2),
    "Input Password":                   (2, 2),
    "Press Keys":                       (1, 10),
    "Clear Element Text":               (1, 1),

    # Element state queries (read-only)
    "Get Text":                         (1, 1),
    "Get Element Attribute":            (2, 2),
    "Get Value":                        (1, 1),
    "Element Should Be Visible":        (1, 1),
    "Element Should Be Enabled":        (1, 1),
    "Element Should Contain":           (2, 2),
    "Page Should Contain Element":      (1, 1),
    "Page Should Contain":              (1, 1),

    # Wait keywords
    "Wait Until Element Is Visible":    (1, 2),
    "Wait Until Element Is Enabled":    (1, 2),
    "Wait Until Page Contains Element": (1, 2),
    "Wait Until Page Contains":         (1, 2),
    "Sleep":                            (1, 1),

    # Selection
    "Select From List By Value":        (2, 2),
    "Select From List By Label":        (2, 2),
    "Select From List By Index":        (2, 2),
    "Select Checkbox":                  (1, 1),
    "Unselect Checkbox":                (1, 1),
    "Select Radio Button":              (2, 2),

    # Navigation
    "Go To":                            (1, 1),
    "Go Back":                          (0, 0),
    "Reload Page":                      (0, 0),

    # Window management
    "Switch Window":                    (1, 1),
    "Close Window":                     (0, 0),
    "Maximize Browser Window":          (0, 0),

    # Screenshot
    "Capture Page Screenshot":          (0, 1),

    # Scroll and focus
    "Scroll Element Into View":         (1, 1),
    "Set Focus To Element":             (1, 1),
}

# Explicitly denied keywords — documented for auditors and security reviewers.
DENIED_KEYWORDS: frozenset[str] = frozenset({
    # Arbitrary code execution
    "Evaluate", "Execute Javascript", "Execute Async Javascript",
    "Run", "Run Process", "Run And Return Rc",
    "Run And Return Rc And Output",
    "Run Keyword", "Run Keyword And Continue On Failure",
    "Run Keyword And Expect Error", "Run Keyword And Ignore Error",
    "Run Keyword And Return", "Run Keyword And Return Status",
    "Run Keyword If", "Run Keyword Unless", "Run Keywords",

    # Library/resource manipulation
    "Import Library", "Import Resource", "Import Variables",

    # Variable manipulation
    "Set Variable", "Set Global Variable", "Set Suite Variable",
    "Set Test Variable", "Set Variable If",

    # File system operations
    "Create File", "Remove File", "Remove Directory",
    "Copy File", "Move File", "Append To File",
    "Create Directory",

    # Process/system access
    "Start Process", "Terminate Process",
    "Get Process Result", "Wait For Process",

    # Logging that could leak data
    "Log", "Log To Console", "Log Many", "Log Variables",

    # Meta-keywords
    "Keyword Should Exist", "Get Library Instance",
    "Set Library Search Order",
})

# Keywords that only observe UI state — they never mutate it. Used by the
# screenshot policy ("state_changing" skips captures after these) and by
# effect verification (no screen change is expected after them). Every
# entry MUST also be in ALLOWED_KEYWORDS (regression-guarded in tests).
READ_ONLY_KEYWORDS: frozenset[str] = frozenset({
    "Get Text", "Get Element Attribute", "Get Value",
    "Element Should Be Visible", "Element Should Be Enabled",
    "Element Should Contain", "Page Should Contain Element",
    "Page Should Contain",
    "Wait Until Element Is Visible", "Wait Until Element Is Enabled",
    "Wait Until Page Contains Element", "Wait Until Page Contains",
    "Sleep", "Capture Page Screenshot",
})

# Interaction keywords whose FIRST argument is an element locator.
# ``insert_auto_waits`` prepends a bounded visibility wait before each of
# these. Deliberately excludes keywords whose first argument is not a
# locator (Press Keys, Select Radio Button, Click Element At Coordinates).
AUTO_WAIT_LOCATOR_KEYWORDS: frozenset[str] = frozenset({
    "Click Element", "Double Click Element", "Right Click Element",
    "Input Text", "Input Password", "Clear Element Text",
    "Select From List By Value", "Select From List By Label",
    "Select From List By Index",
    "Select Checkbox", "Unselect Checkbox",
    "Scroll Element Into View", "Set Focus To Element",
})

# Wait keywords whose first argument is a locator — used to detect that a
# workflow already waits on an element before interacting with it.
_LOCATOR_WAIT_KEYWORDS: frozenset[str] = frozenset({
    "Wait Until Element Is Visible", "Wait Until Element Is Enabled",
    "Wait Until Page Contains Element",
})

_AUTO_WAIT_KEYWORD = "Wait Until Element Is Visible"


def insert_auto_waits(
    validated: list[tuple[str, list[str]]], wait_seconds: float,
) -> list[tuple[str, list[str]]]:
    """Prepend a bounded visibility wait before each locator interaction.

    Takes an already-validated keyword list (output of
    ``WorkflowGenerator.validate_keywords``) and returns a new list where
    every keyword in ``AUTO_WAIT_LOCATOR_KEYWORDS`` is preceded by
    ``Wait Until Element Is Visible  <locator>  <wait_seconds>s`` — unless
    the immediately preceding keyword already waits on that locator.

    Security properties (this runs AFTER allowlist validation):
      - Only inserts ``_AUTO_WAIT_KEYWORD``, a read-only allowlisted keyword.
      - Never removes, reorders, or rewrites the caller's keywords.
      - Output length is bounded by 2 * len(validated) <= 2 * MAX_KEYWORDS.
    """
    if wait_seconds <= 0:
        return [(name, list(args)) for name, args in validated]

    if _AUTO_WAIT_KEYWORD not in ALLOWED_KEYWORDS or (
        _AUTO_WAIT_KEYWORD not in READ_ONLY_KEYWORDS
    ):
        raise WorkflowError(
            "auto-wait keyword is not allowlisted read-only — refusing to insert",
            keyword_name=_AUTO_WAIT_KEYWORD,
            reason="disallowed_keyword",
        )

    out: list[tuple[str, list[str]]] = []
    for name, args in validated:
        if name in AUTO_WAIT_LOCATOR_KEYWORDS and args:
            locator = args[0]
            prev = out[-1] if out else None
            already_waited = (
                prev is not None
                and prev[0] in _LOCATOR_WAIT_KEYWORDS
                and prev[1]
                and prev[1][0] == locator
            )
            if not already_waited:
                out.append((_AUTO_WAIT_KEYWORD, [locator, f"{wait_seconds:g}s"]))
        out.append((name, list(args)))
    return out


def _parse_sleep_seconds(value: str) -> float:
    """Parse a Robot Framework time string into seconds."""
    m = _TIME_PATTERN.match(value)
    if m is None:
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0

    if m.group(1) is not None:
        return float(m.group(1))
    if m.group(2) is not None:
        return float(m.group(2)) * 60
    if m.group(3) is not None:
        return int(m.group(3)) / 1000.0
    if m.group(4) is not None:
        return float(m.group(4))
    return 0.0


class WorkflowGenerator:
    """Translates PB tool call params into Robot Framework keyword sequences."""

    def __init__(self, scratch_dir: str | Path = "/tmp/icebreaker-rpa/") -> None:
        self._scratch = Path(scratch_dir)

    def validate_keywords(
        self, keywords: list[dict[str, Any]],
    ) -> list[tuple[str, list[str]]]:
        """Validate and normalize a keyword sequence.

        Returns list of (name, args) tuples ready for Robot Framework.

        Raises ``WorkflowError`` on:
          - len(keywords) > MAX_KEYWORDS
          - keyword name not in ALLOWED_KEYWORDS
          - keyword args count outside (min_args, max_args)
          - Sleep duration > MAX_SLEEP_SECONDS (caps instead of rejecting)
        """
        if len(keywords) > MAX_KEYWORDS:
            raise WorkflowError(
                f"Workflow has {len(keywords)} keywords — maximum is {MAX_KEYWORDS}. "
                "Split into smaller workflows or remove unnecessary steps.",
                reason="too_many_keywords",
            )

        result: list[tuple[str, list[str]]] = []
        for i, kw in enumerate(keywords):
            name = kw.get("name", "")
            args = kw.get("args", [])

            if name in DENIED_KEYWORDS:
                raise WorkflowError(
                    f"Keyword {name!r} is explicitly denied — it can execute "
                    "arbitrary code or access system resources. "
                    "Use only UI interaction keywords.",
                    keyword_name=name,
                    reason="disallowed_keyword",
                )

            if name not in ALLOWED_KEYWORDS:
                raise WorkflowError(
                    f"Keyword {name!r} is not in the allowlist. "
                    f"Available keywords: {sorted(ALLOWED_KEYWORDS.keys())}",
                    keyword_name=name,
                    reason="disallowed_keyword",
                )

            min_args, max_args = ALLOWED_KEYWORDS[name]
            if len(args) < min_args or len(args) > max_args:
                raise WorkflowError(
                    f"Keyword {name!r} requires {min_args}–{max_args} arguments, "
                    f"got {len(args)}.",
                    keyword_name=name,
                    reason="invalid_args",
                )

            if name == "Sleep" and args:
                seconds = _parse_sleep_seconds(args[0])
                if seconds > MAX_SLEEP_SECONDS:
                    args = [f"{MAX_SLEEP_SECONDS}s"]

            result.append((name, list(args)))

        return result

    def generate_suite(
        self, workflow_name: str, keywords: list[dict[str, Any]],
    ) -> Any:
        """Build a Robot Framework TestSuite from validated keywords.

        Uses ``robot.api.TestSuite`` programmatic API (ADR-17 — library, not CLI).
        Returns the suite object. Raises ``ImportError`` if robotframework
        is not installed.
        """
        validated = self.validate_keywords(keywords)

        from robot.api import TestSuite

        suite = TestSuite(name=workflow_name or "icebreaker_workflow")
        test = suite.tests.create(name="workflow")
        for name, args in validated:
            test.body.create_keyword(name=name, args=args)
        return suite

    def generate_robot_file(
        self,
        workflow_name: str,
        keywords: list[dict[str, Any]],
        *,
        validated: list[tuple[str, list[str]]] | None = None,
        note: str = "",
    ) -> Path:
        """Write a ``.robot`` file to scratch dir for audit/inspection.

        This is a secondary artifact. Execution uses ``generate_suite()``
        directly. The file gets 0o600 permissions.

        ``validated`` lets the caller pass the exact keyword list that will
        execute (e.g. after ``insert_auto_waits``) so the audit artifact
        matches execution; ``note`` adds a comment header explaining any
        transformation. When ``validated`` is omitted, ``keywords`` is
        validated here as before.
        """
        if validated is None:
            validated = self.validate_keywords(keywords)
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", workflow_name or "workflow")
        path = self._scratch / f"{safe_name}.robot"

        lines = []
        if note:
            safe_note = re.sub(r"[\r\n\x00-\x1f]", " ", note)
            lines.append(f"# {safe_note}")
        lines += [
            "*** Settings ***",
            "Library    SeleniumLibrary",
            "",
            "*** Test Cases ***",
            f"{safe_name}",
        ]
        for name, args in validated:
            arg_str = "    ".join(args)
            if arg_str:
                lines.append(f"    {name}    {arg_str}")
            else:
                lines.append(f"    {name}")

        self._scratch.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path
