"""Tests for the V.5a HitlDisplayData.preview_image_path field + sanitization.

Guards:
- Backward-compat: existing HitlDisplayData construction works unchanged
  (no callers need to pass preview_image_path).
- Path injection: only paths under /tmp/icebreaker-gui/ that end in .png
  pass through. Anything else silently → None so the modal still fires
  with a fallback text-only view.
"""

from __future__ import annotations

import pytest

from controller.hitl import HitlDisplayData, _sanitize_preview_path
from controller.risk_classifier import Tier


# ── Backward compatibility ────────────────────────────────────────────


def test_construction_without_preview_field_still_works():
    d = HitlDisplayData(
        action="click",
        target="/etc/passwd",
        tier=Tier.HIGH,
        risk_level="tier_2",
        reversible=False,
        backend="test",
        reason="testing",
        blocked_pattern=None,
        cow_summary=None,
    )
    assert d.preview_image_path is None


def test_default_preview_image_path_is_none():
    d = HitlDisplayData(
        action="a", target="t", tier=Tier.HIGH, risk_level="tier_2",
        reversible=True, backend="b", reason="r",
        blocked_pattern=None, cow_summary=None,
    )
    assert d.preview_image_path is None


# ── Happy path ─────────────────────────────────────────────────────────


def test_valid_preview_path_preserved():
    valid = "/tmp/icebreaker-gui/preview-abc123.png"
    d = HitlDisplayData(
        action="a", target="t", tier=Tier.HIGH, risk_level="tier_2",
        reversible=True, backend="b", reason="r",
        blocked_pattern=None, cow_summary=None,
        preview_image_path=valid,
    )
    assert d.preview_image_path == valid


def test_valid_preview_path_in_nested_subdir():
    """Paths under a subdir of /tmp/icebreaker-gui/ also pass."""
    valid = "/tmp/icebreaker-gui/session-42/preview-abc.png"
    d = HitlDisplayData(
        action="a", target="t", tier=Tier.HIGH, risk_level="tier_2",
        reversible=True, backend="b", reason="r",
        blocked_pattern=None, cow_summary=None,
        preview_image_path=valid,
    )
    assert d.preview_image_path == valid


# ── Path-injection rejects ─────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "/etc/passwd",                                   # totally wrong dir
    "/root/.ssh/id_rsa.png",                         # right suffix, wrong dir
    "/tmp/icebreaker-gui/../etc/passwd",             # traversal
    "/tmp/icebreaker-gui/../../etc/shadow.png",      # traversal + png
    "/tmp/icebreaker/preview.png",                   # wrong parent (no `-gui`)
    "preview.png",                                   # relative
    "./preview.png",                                 # relative
    "/tmp/icebreaker-gui/preview.txt",               # wrong suffix
    "/tmp/icebreaker-gui/preview",                   # no suffix
    "/tmp/icebreaker-gui/preview.pngX",              # tries to hide via suffix trick
    "",                                              # empty
    " ",                                             # whitespace-only
])
def test_bad_preview_path_dropped_to_none(bad):
    d = HitlDisplayData(
        action="a", target="t", tier=Tier.HIGH, risk_level="tier_2",
        reversible=True, backend="b", reason="r",
        blocked_pattern=None, cow_summary=None,
        preview_image_path=bad,
    )
    assert d.preview_image_path is None, \
        f"path {bad!r} should have been rejected but survived as {d.preview_image_path!r}"


def test_non_string_preview_path_dropped():
    """int, bytes, Path — anything non-str is dropped."""
    from pathlib import Path
    for bad in (123, b"/tmp/icebreaker-gui/x.png",
                Path("/tmp/icebreaker-gui/x.png"), None, [], {}):
        d = HitlDisplayData(
            action="a", target="t", tier=Tier.HIGH, risk_level="tier_2",
            reversible=True, backend="b", reason="r",
            blocked_pattern=None, cow_summary=None,
            preview_image_path=bad,   # type: ignore[arg-type]
        )
        assert d.preview_image_path is None


def test_overlong_preview_path_dropped():
    """A 5000-char path is dropped even if it starts with the right prefix
    — no legitimate screenshot filename is that long."""
    bad = "/tmp/icebreaker-gui/" + "x" * 5000 + ".png"
    d = HitlDisplayData(
        action="a", target="t", tier=Tier.HIGH, risk_level="tier_2",
        reversible=True, backend="b", reason="r",
        blocked_pattern=None, cow_summary=None,
        preview_image_path=bad,
    )
    assert d.preview_image_path is None


# ── Sanitize function is stable + pure ─────────────────────────────────


def test_sanitize_helper_none_input():
    assert _sanitize_preview_path(None) is None


def test_sanitize_helper_preserves_valid():
    p = "/tmp/icebreaker-gui/preview-abc.png"
    assert _sanitize_preview_path(p) == p


def test_sanitize_helper_rejects_traversal():
    assert _sanitize_preview_path("/tmp/icebreaker-gui/../etc/x.png") is None


def test_sanitize_helper_rejects_absolute_lookalike():
    """A path that string-startswith '/tmp/icebreaker-gui' but is
    actually a different directory should NOT pass."""
    # e.g. /tmp/icebreaker-gui-fake/…
    assert _sanitize_preview_path(
        "/tmp/icebreaker-gui-fake/preview.png"
    ) is None


# ── Interaction with other fields ─────────────────────────────────────


def test_preview_field_alongside_all_other_fields():
    d = HitlDisplayData(
        action="click",
        target="Slack — Send button",
        tier=Tier.HIGH,
        risk_level="tier_2",
        reversible=False,
        backend="opencode_oc",
        reason="grounded_click",
        blocked_pattern=None,
        cow_summary="no COW; UI action",
        rpa_keyword_preview=(),
        preview_image_path="/tmp/icebreaker-gui/preview-xyz.png",
    )
    # Every field preserved.
    assert d.action == "click"
    assert d.target == "Slack — Send button"
    assert d.tier == Tier.HIGH
    assert d.preview_image_path == "/tmp/icebreaker-gui/preview-xyz.png"
    assert d.cow_summary == "no COW; UI action"
