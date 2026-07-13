"""v6.8 M7.2 — Tier-0 fast path regression lock.

The fast path lives in tier0_fast_path.try_fast_path(). This test
covers:
- Tier gating (only Tier 0 qualifies)
- Every tool in TIER0_FAST_PATH_TOOLS produces a well-shaped tool_call
- Tools not in the allowlist fall through (return None)
- Malformed intents fall through without raising
- The constructed tool_call still needs to pass INV-2 validation
  (caller responsibility; asserted in test_intent_shape_matches_mcpd_schema)

Contract: docs/IMPLEMENTATION_PLAN_v6.8_2026-07-13.md §4.3 M7.2
"""

from __future__ import annotations

import pytest

from controller.tier0_fast_path import (
    TIER0_FAST_PATH_TOOLS,
    try_fast_path,
)


# ── Tier gating ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tier", [1, 2, 3])
def test_non_tier0_falls_through(tier):
    intent = {"action": "fs.list", "target": "/home"}
    assert try_fast_path(intent, tier) is None


def test_tier0_qualifies():
    intent = {"action": "fs.list", "target": "/home"}
    result = try_fast_path(intent, 0)
    assert result is not None
    assert result["tool"] == "fs.list"
    assert result["params"] == {"path": "/home"}


# ── No-args tools ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", [
    "system.status", "system.uptime", "system.cpu",
    "system.memory", "system.disk", "process.list", "network.status",
])
def test_no_args_tools_produce_empty_params(action):
    intent = {"action": action, "target": ""}
    result = try_fast_path(intent, 0)
    assert result == {"tool": action, "params": {}}


# ── Path-based tools ───────────────────────────────────────────────────────

@pytest.mark.parametrize("action", ["fs.list", "fs.read", "fs.stat"])
def test_path_tools_map_target_to_path(action):
    intent = {"action": action, "target": "/tmp/foo.txt"}
    result = try_fast_path(intent, 0)
    assert result == {"tool": action, "params": {"path": "/tmp/foo.txt"}}


# ── Named-target tools ─────────────────────────────────────────────────────

def test_service_logs_maps_target_to_service():
    intent = {"action": "service.logs", "target": "systemd-logind"}
    assert try_fast_path(intent, 0) == {
        "tool": "service.logs", "params": {"service": "systemd-logind"},
    }


def test_network_dns_read_maps_target_to_hostname():
    intent = {"action": "network.dns.read", "target": "example.com"}
    assert try_fast_path(intent, 0) == {
        "tool": "network.dns.read", "params": {"hostname": "example.com"},
    }


def test_package_query_maps_target_to_name():
    intent = {"action": "package.query", "target": "htop"}
    assert try_fast_path(intent, 0) == {
        "tool": "package.query", "params": {"name": "htop"},
    }


# ── Int-target coercion (process.inspect) ──────────────────────────────────

def test_process_inspect_coerces_target_to_int():
    intent = {"action": "process.inspect", "target": "1234"}
    assert try_fast_path(intent, 0) == {
        "tool": "process.inspect", "params": {"pid": 1234},
    }


def test_process_inspect_bad_pid_produces_zero_not_error():
    """Malformed pid → params.pid=0, which mcpd's schema rejects. The
    fast path must never raise a translation error to the user; the
    schema-validation gate at Step 7 surfaces the mistake."""
    intent = {"action": "process.inspect", "target": "not-a-number"}
    result = try_fast_path(intent, 0)
    assert result == {"tool": "process.inspect", "params": {"pid": 0}}


# ── Fallthrough conditions ────────────────────────────────────────────────

@pytest.mark.parametrize("action", [
    "fs.write", "fs.delete",              # Tier 1/3 writes
    "package.install", "package.remove",  # Tier 2 mutations
    "service.start", "service.stop",      # Tier 2 mutations
    "system.unsupported",                 # F-35 landing pad
    "gui.launch",                         # not in registry
    "",                                   # empty action
])
def test_non_allowlisted_action_falls_through(action):
    intent = {"action": action, "target": "/tmp"}
    assert try_fast_path(intent, 0) is None


def test_missing_action_falls_through():
    intent = {"target": "/home"}
    assert try_fast_path(intent, 0) is None


def test_none_action_falls_through():
    intent = {"action": None, "target": "/home"}
    assert try_fast_path(intent, 0) is None


def test_non_string_action_falls_through():
    intent = {"action": 42, "target": "/home"}
    assert try_fast_path(intent, 0) is None


# ── Missing target for path tool: empty string, not None ──────────────────

def test_missing_target_yields_empty_string_path():
    """The fast path defers to mcpd schema validation for target validity.
    Missing target -> path='' which mcpd rejects (minLength=1)."""
    intent = {"action": "fs.list"}
    assert try_fast_path(intent, 0) == {
        "tool": "fs.list", "params": {"path": ""},
    }


# ── Coverage: every action in the allowlist gets a test ───────────────────

def test_every_allowlisted_action_has_a_mapper():
    for action in TIER0_FAST_PATH_TOOLS:
        intent = {"action": action, "target": "/tmp"}
        result = try_fast_path(intent, 0)
        assert result is not None, f"mapper missing for {action}"
        assert result["tool"] == action
        assert isinstance(result["params"], dict)


# ── Config flag semantics (defensive check on RunConfig default) ──────────

def test_run_config_default_tier0_fast_path_is_true():
    from controller.config import RunConfig
    cfg = RunConfig()
    assert cfg.tier0_fast_path is True
