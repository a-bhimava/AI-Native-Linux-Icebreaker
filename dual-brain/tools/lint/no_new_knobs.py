#!/usr/bin/env python3
"""Phase 6 Scope B — CI gate: reject new hardcoded numeric knobs.

Every module-level ``NAME = <int|float>`` assignment on the hot path
must either (a) be in ``knobs_allowlist.toml`` with a justification, or
(b) move to ``controller_config.json`` as a user-tunable field.

Why not Ruff PLR2004 (magic-value-comparison)? Ruff's rule only fires
on *comparisons*, not on module-level assigns; its default allowlist is
tiny (``0``, ``1``, ``""``, ``"__main__"``); and it has no first-class
support for a project-scoped allowlist file (astral-sh/ruff #10009 open
as of 2026-07-10). Custom AST checker is ~80 LOC and precisely scoped.

Usage::

    python3 tools/lint/no_new_knobs.py
    # exit 0 on clean tree, exit 1 on violation with a report.

CI wires this as gate G23 in ``ci.sh``. Adding a new entry to
``knobs_allowlist.toml`` requires filling in a ``reason`` field — every
allowlist bump is a review signal ("should this be user-tunable?").
"""

from __future__ import annotations

import ast
import pathlib
import sys
from typing import Iterator


# ── Configuration ────────────────────────────────────────────────────────


_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
"""Repo root (dual-brain/) — walk from here."""


_SCAN_DIRS = (
    "controller",
    "gui",
    "terminal",
    "rpa_bridge",
    "gui_agent",
)


# Directories excluded from the scan. Tests, fixture corpora, and
# generated __pycache__ don't count as "hot path" — test files can and
# do use magic numbers for setup values.
_EXCLUDE_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    "tests",
    "corpus",
}


_ALLOWLIST_PATH = _PROJECT_ROOT / "tools" / "lint" / "knobs_allowlist.toml"


# ── Allowlist load ───────────────────────────────────────────────────────


try:
    import tomllib as _toml_reader  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as _toml_reader  # type: ignore


def _load_allowlist() -> set[tuple[str, str]]:
    """Return a set of ``(rel_path, name)`` tuples that are allowed."""
    if not _ALLOWLIST_PATH.exists():
        return set()
    data = _toml_reader.loads(_ALLOWLIST_PATH.read_text("utf-8"))
    entries = data.get("knob", [])
    if not isinstance(entries, list):
        return set()
    out: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        file = entry.get("file")
        name = entry.get("name")
        reason = entry.get("reason")
        if not (isinstance(file, str) and isinstance(name, str)):
            continue
        if not isinstance(reason, str) or not reason.strip():
            # Enforce: every allowlist entry must have a justification.
            # Blank reasons defeat the review purpose.
            print(
                f"ERROR: allowlist entry {file}::{name} missing a "
                "non-blank `reason` field.",
                file=sys.stderr,
            )
            sys.exit(2)
        out.add((file, name))
    return out


# ── AST walk ─────────────────────────────────────────────────────────────


def _iter_py_files() -> Iterator[pathlib.Path]:
    for scan in _SCAN_DIRS:
        base = _PROJECT_ROOT / scan
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if any(part in _EXCLUDE_DIR_NAMES for part in path.parts):
                continue
            yield path


def _module_level_numeric_assigns(
    tree: ast.Module,
) -> list[tuple[str, int]]:
    """Return ``(name, lineno)`` for every module-level ``NAME = <num>``.

    We deliberately IGNORE:
      * assignments inside functions or classes (not "module-level"),
      * `_` and `__all__` sinks (bookkeeping, not knobs),
      * tuple/multi-target assigns (rare + hard to justify — flag if we
        ever see one).
    """
    hits: list[tuple[str, int]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "__all__":
            continue
        # Only flag literal int/float constants. Computed values
        # (`_LIMIT = int(os.getenv(...))`) are not knobs — they're
        # already externally overridable.
        value = node.value
        if not isinstance(value, ast.Constant):
            continue
        if not isinstance(value.value, (int, float)) or isinstance(value.value, bool):
            continue
        hits.append((target.id, node.lineno))
    return hits


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> int:
    allow = _load_allowlist()
    violations: list[tuple[str, str, int]] = []
    for path in _iter_py_files():
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        rel = str(path.relative_to(_PROJECT_ROOT))
        for name, lineno in _module_level_numeric_assigns(tree):
            if (rel, name) in allow:
                continue
            violations.append((rel, name, lineno))
    if not violations:
        return 0
    print(
        "no_new_knobs: found module-level int/float constants not in "
        f"the allowlist ({_ALLOWLIST_PATH.relative_to(_PROJECT_ROOT)}):",
        file=sys.stderr,
    )
    for rel, name, lineno in sorted(violations):
        print(f"  {rel}:{lineno}  {name}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Fix by either (a) moving the value to controller_config.json "
        "as a user-tunable field (preferred), or (b) adding to "
        f"{_ALLOWLIST_PATH.relative_to(_PROJECT_ROOT)} with a "
        "`reason = \"...\"` justification.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
