"""Phase 6 Scope B — regression test for the no-new-knobs AST checker.

Two guarantees:

1. Running the checker on the current tree returns 0 (baseline
   allowlist is up-to-date). If a legitimate new constant lands
   without an allowlist entry, this test fails on the same commit
   that introduces it — before it reaches CI G23.

2. The checker's allowlist file exists, parses, and every entry
   carries a non-blank ``reason`` field (the whole point of the
   allowlist is the justification, not the entries themselves).

The checker itself lives at ``tools/lint/no_new_knobs.py``. We invoke
it as a subprocess to match the exact CI G23 invocation shape.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest


_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CHECKER = _PROJECT_ROOT / "tools" / "lint" / "no_new_knobs.py"
_ALLOWLIST = _PROJECT_ROOT / "tools" / "lint" / "knobs_allowlist.toml"


def test_checker_returns_zero_on_current_tree() -> None:
    """Baseline: the current tree is clean under the current
    allowlist. A new module-level int/float constant landing without
    an allowlist entry breaks this test."""
    result = subprocess.run(
        [sys.executable, str(_CHECKER)],
        cwd=str(_PROJECT_ROOT),
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            "no_new_knobs.py flagged new hardcoded constants:\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}\n"
            "Either add the offender to tools/lint/knobs_allowlist.toml "
            "with a `reason = \"...\"` justification, or move it to "
            "controller_config.json as a user-tunable field."
        )


def test_allowlist_file_exists_and_parses() -> None:
    """A missing or unparseable allowlist file would make G23 pass by
    accident (every constant becomes a violation but the checker
    itself would also fail earlier). Pin the file's presence."""
    assert _ALLOWLIST.exists(), (
        f"allowlist file missing at {_ALLOWLIST}"
    )
    try:
        import tomllib as toml
    except ImportError:  # pragma: no cover
        import tomli as toml  # type: ignore
    data = toml.loads(_ALLOWLIST.read_text("utf-8"))
    entries = data.get("knob", [])
    assert isinstance(entries, list) and entries, (
        "allowlist has no [[knob]] entries — did the file get emptied?"
    )


def test_every_allowlist_entry_has_a_justification() -> None:
    """No blank reasons — every allowlist bump must be a review signal."""
    try:
        import tomllib as toml
    except ImportError:  # pragma: no cover
        import tomli as toml  # type: ignore
    data = toml.loads(_ALLOWLIST.read_text("utf-8"))
    entries = data.get("knob", [])
    for i, entry in enumerate(entries):
        assert isinstance(entry, dict), f"entry {i} not a table"
        assert isinstance(entry.get("file"), str) and entry["file"], (
            f"entry {i} missing `file`"
        )
        assert isinstance(entry.get("name"), str) and entry["name"], (
            f"entry {i} ({entry['file']}) missing `name`"
        )
        assert isinstance(entry.get("reason"), str) and entry["reason"].strip(), (
            f"entry {i} ({entry['file']}::{entry['name']}) needs a "
            "non-blank `reason` justification."
        )
