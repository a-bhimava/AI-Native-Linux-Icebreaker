"""Tests for V.6b HitlPrompt.preview_image_path plumbing.

V.6b threads the optional preview_image_path from HitlPrompt.__init__
through _build_display_data() into HitlDisplayData. Population of the
field from the grounded_* orchestrators is deferred to v6.16
(architectural change — HITL currently fires at the tier-2 gate
BEFORE the agent runs parse). V.6b lays the plumbing so any caller
that DOES have a preview path can supply it today.
"""

from __future__ import annotations

import pytest

from controller.hitl import HitlPrompt, HitlDisplayData
from controller.risk_classifier import ClassificationResult, Tier


def _cls(tier=Tier.HIGH):
    return ClassificationResult(
        tier=tier, reason="test", reversible=False,
        blocked_pattern=None,
    )


def _intent():
    return {
        "action": "click",
        "target": "Slack Send button",
        "risk_level": "tier_3",
        "reason": "grounded_click",
    }


# ── Default: no preview supplied ────────────────────────────────────


def test_default_preview_image_path_is_none():
    """Existing callers not passing the new kwarg get None — full
    backward compat."""
    prompt = HitlPrompt(_intent(), _cls(), backend="test")
    data = prompt._build_display_data()
    assert data.preview_image_path is None


# ── Preview supplied ─────────────────────────────────────────────────


def test_valid_preview_path_threads_through_to_display_data():
    valid = "/tmp/icebreaker-gui/preview-abc123.png"
    prompt = HitlPrompt(
        _intent(), _cls(),
        backend="test",
        preview_image_path=valid,
    )
    data = prompt._build_display_data()
    assert data.preview_image_path == valid


def test_invalid_preview_path_dropped_at_dataclass_layer():
    """V.5a's sanitize kicks in on the HitlDisplayData side. HitlPrompt
    just threads whatever it's given — sanitization is the field's job."""
    prompt = HitlPrompt(
        _intent(), _cls(),
        backend="test",
        preview_image_path="/etc/passwd",
    )
    data = prompt._build_display_data()
    # V.5a sanitizer rejected the path → None.
    assert data.preview_image_path is None


def test_preview_path_survives_ask_flow_construction():
    """Constructing HitlPrompt with a valid preview doesn't affect the
    other fields — action/target/tier still populate correctly."""
    valid = "/tmp/icebreaker-gui/preview-xyz.png"
    prompt = HitlPrompt(
        _intent(), _cls(Tier.HIGH),
        backend="opencode_oc",
        preview_image_path=valid,
    )
    data = prompt._build_display_data()
    assert data.action == "click"
    assert data.target == "Slack Send button"
    assert data.tier == Tier.HIGH
    assert data.preview_image_path == valid


def test_preview_field_traversal_dropped_at_dataclass():
    prompt = HitlPrompt(
        _intent(), _cls(),
        backend="test",
        preview_image_path="/tmp/icebreaker-gui/../etc/x.png",
    )
    data = prompt._build_display_data()
    assert data.preview_image_path is None


# ── Explicit None still works ───────────────────────────────────────


def test_explicit_none_preview_is_none():
    prompt = HitlPrompt(
        _intent(), _cls(),
        backend="test",
        preview_image_path=None,
    )
    data = prompt._build_display_data()
    assert data.preview_image_path is None
