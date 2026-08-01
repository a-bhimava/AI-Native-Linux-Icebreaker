"""Tests for gui_agent.vision — VLM-backed screen parser.

Fully mocked — no litellm calls, no network. Live tests against real
Gemini live in ``incremental/tests/test_vision_live.py`` behind
``RUN_LIVE_TESTS=1``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from gui_agent.vision import (
    ElementBox,
    ParseResult,
    VisionAllBackendsFailed,
    VisionCostCeilingExceeded,
    VisionDisabled,
    VisionGrounder,
    VisionMalformedResponse,
    _parse_and_validate,
)


# 1x1 PNG so file-load path works without needing test fixtures on disk.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\x99c```\x00"
    b"\x00\x00\x04\x00\x01\x0b\xa4\x03\xed\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _fake_completion_ok(elements: list[dict], cost: float = 0.0001):
    """Build a canned litellm.completion response."""
    def _fn(**kwargs: Any) -> dict:
        return {
            "choices": [{"message": {"content": json.dumps({"elements": elements})}}],
            "_hidden_params": {"response_cost": cost},
        }
    return _fn


def _fake_completion_malformed():
    """Returns invalid JSON so the parser retries."""
    calls = {"n": 0}

    def _fn(**kwargs: Any) -> dict:
        calls["n"] += 1
        # First call returns garbage; second returns valid.
        if calls["n"] == 1:
            content = "this is not JSON at all { broken"
        else:
            content = json.dumps({"elements": [
                {"id": 0, "box": [10, 20, 30, 40], "caption": "OK",
                 "kind": "button", "confidence": 0.9},
            ]})
        return {
            "choices": [{"message": {"content": content}}],
            "_hidden_params": {"response_cost": 0.0001},
        }
    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


def _fake_completion_raises():
    def _fn(**kwargs: Any):
        raise RuntimeError("simulated Gemini 429")
    return _fn


# ═══ Core behavior ═══════════════════════════════════════════════════


def test_parse_screen_returns_bounded_list():
    """Happy path: mocked VLM → parsed elements returned as ParseResult."""
    grounder = VisionGrounder(
        config={"enabled": True, "backend": "gemini/gemini-2.5-flash",
                "cost_ceiling_usd_per_turn": 1.0},
        litellm_completion=_fake_completion_ok([
            {"id": 0, "box": [10, 20, 100, 40],
             "caption": "Sign in", "kind": "button", "confidence": 0.95},
            {"id": 1, "box": [10, 80, 200, 30],
             "caption": "Email", "kind": "input", "confidence": 0.9},
        ]),
    )
    result = grounder.parse_screen(_TINY_PNG)
    assert isinstance(result, ParseResult)
    assert len(result.elements) == 2
    assert result.elements[0].caption == "Sign in"
    assert result.elements[0].kind == "button"
    assert result.elements[0].centroid == (60, 40)
    assert result.elements[1].caption == "Email"
    assert not result.cached
    assert result.backend_used == "gemini/gemini-2.5-flash"
    assert result.vlm_cost_usd == 0.0001
    assert len(result.screenshot_sha256) == 64  # hex sha-256


def test_parse_screen_hits_cache_on_repeat_call():
    """Second call with the same image returns cached=True and doesn't invoke
    the VLM again."""
    n_calls = {"n": 0}

    def _counting_fn(**kwargs: Any) -> dict:
        n_calls["n"] += 1
        return {
            "choices": [{"message": {"content": json.dumps({"elements": [
                {"id": 0, "box": [1, 2, 3, 4], "caption": "x",
                 "kind": "button", "confidence": 1.0},
            ]})}}],
            "_hidden_params": {"response_cost": 0.0001},
        }

    grounder = VisionGrounder(
        config={"enabled": True, "backend": "gemini/gemini-2.5-flash",
                "cost_ceiling_usd_per_turn": 1.0, "cache_ttl_seconds": 10.0},
        litellm_completion=_counting_fn,
    )
    r1 = grounder.parse_screen(_TINY_PNG)
    r2 = grounder.parse_screen(_TINY_PNG)
    assert n_calls["n"] == 1, "VLM was called twice — cache miss"
    assert not r1.cached
    assert r2.cached
    assert r1.screenshot_sha256 == r2.screenshot_sha256
    assert r1.elements == r2.elements


def test_cache_expires_after_ttl():
    """Same image after TTL elapses → VLM re-invoked."""
    n_calls = {"n": 0}

    def _counting_fn(**kwargs: Any) -> dict:
        n_calls["n"] += 1
        return {
            "choices": [{"message": {"content": json.dumps({"elements": [
                {"id": 0, "box": [1, 2, 3, 4], "caption": "x",
                 "kind": "button", "confidence": 1.0},
            ]})}}],
            "_hidden_params": {"response_cost": 0.0001},
        }

    grounder = VisionGrounder(
        # 0-second TTL — every call is a cache miss.
        config={"enabled": True, "backend": "gemini/gemini-2.5-flash",
                "cost_ceiling_usd_per_turn": 1.0, "cache_ttl_seconds": 0.0},
        litellm_completion=_counting_fn,
    )
    grounder.parse_screen(_TINY_PNG)
    time.sleep(0.01)
    grounder.parse_screen(_TINY_PNG)
    assert n_calls["n"] == 2


# ═══ Robustness ═══════════════════════════════════════════════════════


def test_malformed_json_retries_then_succeeds():
    """First call returns invalid JSON → grounder retries with lower
    temperature → second call succeeds."""
    fn = _fake_completion_malformed()
    grounder = VisionGrounder(
        config={"enabled": True, "backend": "gemini/gemini-2.5-flash",
                "cost_ceiling_usd_per_turn": 1.0, "retry_on_malformed_json": 1},
        litellm_completion=fn,
    )
    result = grounder.parse_screen(_TINY_PNG)
    assert fn.calls["n"] == 2  # type: ignore[attr-defined]
    assert len(result.elements) == 1
    assert result.elements[0].caption == "OK"


def test_malformed_json_gives_up_after_retries():
    """VLM always returns garbage → VisionMalformedResponse after retries
    exhausted."""

    def _always_garbage(**kwargs: Any) -> dict:
        return {
            "choices": [{"message": {"content": "not json { at all"}}],
            "_hidden_params": {"response_cost": 0.0001},
        }

    grounder = VisionGrounder(
        config={"enabled": True, "backend": "gemini/gemini-2.5-flash",
                "cost_ceiling_usd_per_turn": 1.0, "retry_on_malformed_json": 1},
        litellm_completion=_always_garbage,
    )
    with pytest.raises(VisionMalformedResponse):
        grounder.parse_screen(_TINY_PNG)


def test_fallback_backend_used_when_primary_raises():
    """Primary raises → fallback backend called and succeeds."""
    calls: list[str] = []

    def _fn(**kwargs: Any) -> dict:
        calls.append(kwargs["model"])
        if kwargs["model"] == "gemini/gemini-2.5-flash":
            raise RuntimeError("simulated 429")
        return {
            "choices": [{"message": {"content": json.dumps({"elements": [
                {"id": 0, "box": [5, 5, 5, 5], "caption": "fb",
                 "kind": "button", "confidence": 1.0},
            ]})}}],
            "_hidden_params": {"response_cost": 0.0002},
        }

    grounder = VisionGrounder(
        config={"enabled": True,
                "backend": "gemini/gemini-2.5-flash",
                "fallback_backend": "anthropic/claude-haiku-4-5",
                "cost_ceiling_usd_per_turn": 1.0},
        litellm_completion=_fn,
    )
    result = grounder.parse_screen(_TINY_PNG)
    assert calls == ["gemini/gemini-2.5-flash", "anthropic/claude-haiku-4-5"]
    assert result.backend_used == "anthropic/claude-haiku-4-5"
    assert result.elements[0].caption == "fb"


def test_all_backends_fail_raises():
    """Every backend errors → VisionAllBackendsFailed."""
    grounder = VisionGrounder(
        config={"enabled": True,
                "backend": "gemini/gemini-2.5-flash",
                "fallback_backend": "anthropic/claude-haiku-4-5",
                "cost_ceiling_usd_per_turn": 1.0},
        litellm_completion=_fake_completion_raises(),
    )
    with pytest.raises(VisionAllBackendsFailed):
        grounder.parse_screen(_TINY_PNG)


# ═══ Cost governance (BP-10) ═════════════════════════════════════════


def test_cost_ceiling_denies_before_call():
    """The cost check is BEFORE the VLM call, not after — a would-be
    over-budget call never even touches the network."""
    n_calls = {"n": 0}

    def _fn(**kwargs: Any) -> dict:
        n_calls["n"] += 1
        return {"choices": [{"message": {"content": '{"elements": []}'}}],
                "_hidden_params": {"response_cost": 0.5}}

    grounder = VisionGrounder(
        # Ceiling is smaller than the est_cost of 0.0003 → first call denied.
        config={"enabled": True, "backend": "gemini/gemini-2.5-flash",
                "cost_ceiling_usd_per_turn": 0.0001},
        litellm_completion=_fn,
    )
    with pytest.raises(VisionCostCeilingExceeded) as exc_info:
        grounder.parse_screen(_TINY_PNG)
    assert n_calls["n"] == 0  # denied before the call
    assert exc_info.value.ceiling_usd == 0.0001


def test_turn_cost_accumulates_and_resets():
    """Multiple calls in one turn add up; reset_turn_cost() zeroes."""
    grounder = VisionGrounder(
        config={"enabled": True, "backend": "gemini/gemini-2.5-flash",
                "cost_ceiling_usd_per_turn": 1.0, "cache_ttl_seconds": 0.0},
        litellm_completion=_fake_completion_ok([
            {"id": 0, "box": [1, 2, 3, 4], "caption": "x",
             "kind": "button", "confidence": 1.0}
        ], cost=0.0001),
    )
    grounder.parse_screen(_TINY_PNG)
    grounder.parse_screen(_TINY_PNG + b"\x00")  # different bytes → new sha
    assert grounder.turn_cost_usd() == pytest.approx(0.0002)
    grounder.reset_turn_cost()
    assert grounder.turn_cost_usd() == 0.0


# ═══ Disabled state ═══════════════════════════════════════════════════


def test_disabled_raises_vision_disabled():
    grounder = VisionGrounder(
        config={"enabled": False, "backend": "gemini/gemini-2.5-flash"},
        litellm_completion=_fake_completion_raises(),
    )
    with pytest.raises(VisionDisabled):
        grounder.parse_screen(_TINY_PNG)


# ═══ Parsing edge cases ═══════════════════════════════════════════════


def test_markdown_fence_stripped():
    """Gemini sometimes wraps JSON in ```json ... ``` fences."""
    def _fenced(**kwargs: Any) -> dict:
        return {
            "choices": [{"message": {"content":
                "```json\n" + json.dumps({"elements": [
                    {"id": 0, "box": [1, 2, 3, 4], "caption": "fenced",
                     "kind": "button", "confidence": 1.0},
                ]}) + "\n```"}}],
            "_hidden_params": {"response_cost": 0.0001},
        }

    grounder = VisionGrounder(
        config={"enabled": True, "backend": "gemini/gemini-2.5-flash",
                "cost_ceiling_usd_per_turn": 1.0},
        litellm_completion=_fenced,
    )
    result = grounder.parse_screen(_TINY_PNG)
    assert len(result.elements) == 1
    assert result.elements[0].caption == "fenced"


def test_invalid_kind_coerced_to_other():
    parsed = _parse_and_validate(json.dumps({"elements": [
        {"id": 0, "box": [1, 2, 3, 4], "caption": "x", "kind": "wingdings",
         "confidence": 1.0},
    ]}), max_elements=50)
    assert parsed is not None
    assert parsed[0].kind == "other"


def test_max_elements_truncates():
    """More elements than max_elements → truncated."""
    parsed = _parse_and_validate(json.dumps({"elements": [
        {"id": i, "box": [i, i, 1, 1], "caption": f"e{i}",
         "kind": "button", "confidence": 1.0}
        for i in range(200)
    ]}), max_elements=5)
    assert parsed is not None
    assert len(parsed) == 5


def test_elements_renumbered_contiguously():
    """Elements with sparse IDs get renumbered 0..N-1."""
    parsed = _parse_and_validate(json.dumps({"elements": [
        {"id": 99, "box": [1, 1, 1, 1], "caption": "a",
         "kind": "button", "confidence": 1.0},
        {"id": 5, "box": [2, 2, 1, 1], "caption": "b",
         "kind": "button", "confidence": 1.0},
    ]}), max_elements=50)
    assert parsed is not None
    assert [e.id for e in parsed] == [0, 1]


def test_element_box_centroid():
    e = ElementBox(id=0, box=(100, 200, 40, 20), caption="x",
                   kind="button", confidence=1.0)
    assert e.centroid == (120, 210)
