"""Fix V.6e — F-51 marker presence regression guards.

The GT rows F-103..F-107 claim specific marker strings in specific
source files. The codebase-ct-scan agent (2026-08-02) found the
markers were MISSING — GT documentation-only. This test enforces
their presence so a future refactor can't silently drop them.

Marker format matches the existing pattern from
`incremental/versions/v2.manifest:112-136`:
  `Fnn-hint:filepath:substring`

Each marker below is `(fix-id, project-relative-path,
required-marker-string)`. Test asserts the marker string appears in
the file's raw contents.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).parent.parent.parent


_F51_MARKERS: list[tuple[str, str, str]] = [
    ("F-103", "gui_agent/vision.py",              "F-103-vision"),
    ("F-103", "gui_agent/input_synth.py",         "F-103-input-synth"),
    ("F-104", "gui_agent/annotate.py",            "F-104-annotate"),
    ("F-105", "gui_agent/trust_store.py",         "F-105-trust"),
    ("F-105", "scripts/ib_trust.py",              "F-105-cli"),
    ("F-105", "../cx-distro/distro/gui_trust_defaults.jsonl",
                                                   "F-105-defaults"),
    ("F-106", "gui_agent/geometry.py",            "F-106-geometry"),
    ("F-107", "gui_agent/sandbox.py",             "F-107-landlock-bits"),
]


@pytest.mark.parametrize("fix_id,rel_path,marker", _F51_MARKERS)
def test_f51_marker_present_in_source(fix_id: str, rel_path: str, marker: str):
    """Each Fix V source file must carry its F-51 marker string.
    Removing the marker == breaking the GT regression-lock claim."""
    path = (_REPO_ROOT / rel_path).resolve()
    assert path.is_file(), (
        f"{fix_id}: source file missing at {path}. "
        f"F-51 marker `{marker}` cannot be verified — "
        f"either the file moved or the marker table is stale."
    )
    content = path.read_text()
    assert marker in content, (
        f"{fix_id}: marker string `{marker}` missing from "
        f"{rel_path}. GT F-{fix_id.split('-')[1]} claims a "
        f"regression-lock via this marker — the claim is now false."
    )


def test_all_f51_markers_registered_in_gt():
    """Cross-check: every marker in this test corresponds to a
    GROUND_TRUTH.md row that mentions it."""
    gt_path = (_REPO_ROOT / ".." / "incremental" / "GROUND_TRUTH.md").resolve()
    if not gt_path.is_file():
        pytest.skip(f"GROUND_TRUTH.md not at {gt_path}")
    gt_content = gt_path.read_text()
    for fix_id, _, marker in _F51_MARKERS:
        assert marker in gt_content, (
            f"{fix_id}: marker `{marker}` is claimed by the source "
            f"file but NOT documented in GROUND_TRUTH.md. Update GT "
            f"or drop the marker."
        )
