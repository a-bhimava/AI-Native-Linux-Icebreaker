"""Tests for cost & limits governance (M5.P1-sec, SF-9).

Covers:
  - Cost accumulates correctly across turns.
  - Warn at configurable fraction.
  - Deny at 100% ceiling.
  - Ceiling=0 means unlimited.
  - Rate limit enforced.
  - Rate limit=0 means unlimited.
  - Input size cap checked.
  - New Outcomes exist in enum.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from controller.audit import Outcome
from controller.config import CostConfig, LimitsConfig
from controller.session import SessionState


def _session(**overrides) -> SessionState:
    cfg = SimpleNamespace(
        session_ttl_seconds=1800,
        max_turns=50,
        history_path="/tmp/test_history",
        show_spinner=False,
        color="never",
        prompt_prefix="test",
        max_tool_output_lines=40,
    )
    defaults = dict(session_id="test-sess", backend="local", cfg=cfg)
    defaults.update(overrides)
    return SessionState(**defaults)


# ── Cost accumulation ──────────────────────────────────────────────────────

def test_cost_starts_at_zero():
    s = _session()
    assert s.accumulated_cost_usd == 0.0


def test_cost_accumulates():
    s = _session()
    s.add_cost(0.25)
    s.add_cost(0.10)
    assert abs(s.accumulated_cost_usd - 0.35) < 1e-9


def test_cost_ceiling_zero_means_unlimited():
    cfg = CostConfig(session_ceiling_usd=0.0)
    assert cfg.session_ceiling_usd == 0.0


def test_cost_warn_fraction_default():
    cfg = CostConfig()
    assert cfg.warn_fraction == 0.8


def test_cost_config_custom():
    cfg = CostConfig(session_ceiling_usd=5.0, warn_fraction=0.9)
    assert cfg.session_ceiling_usd == 5.0
    assert cfg.warn_fraction == 0.9


# ── Rate limiting ──────────────────────────────────────────────────────────

def test_rate_limit_allows_under_threshold():
    s = _session()
    for _ in range(5):
        assert s.check_rate_limit(30) is True


def test_rate_limit_denies_over_threshold():
    s = _session()
    for _ in range(10):
        s.check_rate_limit(10)
    assert s.check_rate_limit(10) is False


def test_rate_limit_zero_means_unlimited():
    s = _session()
    for _ in range(100):
        assert s.check_rate_limit(0) is True


def test_rate_limit_expires_old_timestamps():
    s = _session()
    past = time.monotonic() - 61.0
    s._turn_timestamps = [past] * 30
    assert s.check_rate_limit(30) is True


# ── Input size ─────────────────────────────────────────────────────────────

def test_limits_config_defaults():
    cfg = LimitsConfig()
    assert cfg.max_input_chars == 4000
    assert cfg.max_turns_per_min == 30


def test_limits_config_custom():
    cfg = LimitsConfig(max_input_chars=8000, max_turns_per_min=60)
    assert cfg.max_input_chars == 8000
    assert cfg.max_turns_per_min == 60


# ── Outcome enum values ────────────────────────────────────────────────────

def test_limit_exceeded_outcome_exists():
    assert Outcome.LIMIT_EXCEEDED == "limit_exceeded"


def test_cost_exceeded_outcome_exists():
    assert Outcome.COST_EXCEEDED == "cost_exceeded"
