"""v6.9 Scope O — integration tests for the two manifest dispatch
sites (P0-4 of the 2026-07-15 CT scan).

Unit tests in ``test_manifest_loader.py`` verify the loader +
session_op in isolation. These tests exercise the SEAMS — the
boundaries between Controller and ManifestRegistry, between the
LangGraph mcpd_dispatcher_node and the registry, and between the
Tier-0 fast path and manifest dispatch. Every failure mode surfaced
here would otherwise first appear on the UTM sweep of a shipped ISO.

Design decisions:
- Uses the REAL ``ManifestRegistry`` (via ``manifest_loader.load()``)
  so we exercise the actual meta-schema validation, actual
  ``nav.cd.yaml`` manifest, actual ``session_op.set_cwd`` handler.
  Substitution would defeat the purpose of an integration test.
- Mocks only external dependencies (mcpd_client, audit_log,
  session_store). Everything internal to the seam is real.
- Uses the pytest ``tmp_path`` fixture for the target directory of
  ``nav.cd`` because ``session_op.set_cwd`` requires the path to
  exist and be a directory — a hardcoded ``/tmp/foo`` would be
  flaky.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def real_registry():
    """Load the actual controller/manifests directory so nav.cd
    dispatch flows through the real ManifestRegistry, not a stub."""
    from controller.manifest_loader import load
    return load()


@pytest.fixture
def nav_cd_session(tmp_path):
    """A SessionState-shaped stub with a real absolute path to nav
    into. ``tmp_path`` is guaranteed to exist + be a directory, which
    is what ``session_op.set_cwd`` requires."""
    state = SimpleNamespace(
        session_id="scope-o-integ",
        session_cwd="",
        backend="gemini",
        turn_index=0,
    )
    return state, tmp_path


# ── AgentGraph mcpd_dispatcher_node seam ──────────────────────────────


def test_nav_cd_via_agent_graph_mcpd_dispatcher_node(real_registry, nav_cd_session):
    """Manifest branch through the LangGraph dispatcher: mcpd is NOT
    called, session.session_cwd is mutated, and the stashed result is
    a real ``ToolResult`` (P0-3 shape lock)."""
    from controller.agent_graph_nodes import mcpd_dispatcher_node
    from controller.mcpd_client import ToolResult

    session, tmp_path = nav_cd_session
    tool_call = {"tool": "nav.cd", "params": {"path": str(tmp_path)}}
    turn_content = {"tc-hash": tool_call}

    session_store = MagicMock()
    session_store.get.return_value = session
    mcpd_mock = MagicMock()

    collab = {
        "mcpd": mcpd_mock,
        "manifests": real_registry,
        "turn_content": turn_content,
        "audit": MagicMock(),
        "session_store": session_store,
    }
    node = mcpd_dispatcher_node(collab)
    state = {
        "session_id": session.session_id,
        "tool_call_hash": "tc-hash",
        "step_index": 0,
        "total_steps": 1,
        "plan_id": "p1",
    }
    result_patch = node(state)

    # mcpd never touched — this is the whole point of manifest dispatch.
    mcpd_mock.call.assert_not_called()

    # Session cwd mutated by session_op.set_cwd:
    assert session.session_cwd == str(tmp_path.resolve()), (
        f"session.session_cwd should be {tmp_path.resolve()}, "
        f"got {session.session_cwd!r}"
    )

    # Stashed at plan_id:step_index:result — this is where downstream
    # marker substitution + responder look:
    stashed = turn_content.get("p1:0:result")
    assert isinstance(stashed, ToolResult), (
        f"expected ToolResult, got {type(stashed).__name__}"
    )
    assert stashed.result["stdout"] == str(tmp_path.resolve())
    assert stashed.result["manifest_dispatched"] is True
    assert stashed.result["manifest_name"] == "nav.cd"
    # ToolResult properties resolve — future Tier-2 manifests will
    # depend on this:
    assert stashed.requires_cow_approval is False
    assert stashed.status is None

    # Step advanced (multi-step plans loop back to executor via
    # after_mcpd until step_index == total_steps):
    assert result_patch.get("step_index") == 1


# ── Controller streaming path helper seam ─────────────────────────────


def test_nav_cd_via_controller_streaming_dispatch(real_registry, nav_cd_session):
    """Manifest branch through Controller._try_manifest_dispatch: the
    helper wraps ImplResult into ToolResult with the expected keys.
    We invoke the helper directly rather than driving
    run_turn_streaming end-to-end so the seam is isolated from backends,
    prompt loading, HITL, etc."""
    from controller.main import Controller
    from controller.mcpd_client import ToolResult

    session, tmp_path = nav_cd_session
    tool_call = {"tool": "nav.cd", "params": {"path": str(tmp_path)}}

    ctrl = Controller.__new__(Controller)
    ctrl._manifests = real_registry
    ctrl._audit = MagicMock()

    result = ctrl._try_manifest_dispatch(tool_call, session)
    assert isinstance(result, ToolResult), (
        f"streaming path should wrap manifest dispatch in ToolResult; "
        f"got {type(result).__name__}"
    )
    assert result.result["stdout"] == str(tmp_path.resolve())
    assert result.result["ok"] is True
    # Session cwd mutated by session_op.set_cwd via the impl:
    assert session.session_cwd == str(tmp_path.resolve())
    # No COW required for a Tier-0 manifest:
    assert result.requires_cow_approval is False


def test_manifest_dispatch_helper_returns_none_for_unknown_tool(
    real_registry, nav_cd_session
):
    """The helper's contract: return None (not raise, not empty
    ToolResult) when the tool isn't in the manifest registry. Otherwise
    the caller can't distinguish 'fall through to mcpd' from 'manifest
    dispatch succeeded but produced no output'."""
    from controller.main import Controller

    session, _ = nav_cd_session
    # system.status is a legacy mcpd tool, NOT a manifest tool:
    tool_call = {"tool": "system.status", "params": {}}

    ctrl = Controller.__new__(Controller)
    ctrl._manifests = real_registry
    ctrl._audit = MagicMock()

    result = ctrl._try_manifest_dispatch(tool_call, session)
    assert result is None, (
        f"unknown-tool call should fall through with None, got {result!r}"
    )


# ── Tier-0 fast path constructs the right shape ───────────────────────


def test_tier0_fast_path_constructs_nav_cd_shape(nav_cd_session):
    """Round-trip: Tier-0 fast path takes a nav.cd intent and produces
    the exact tool_call shape ({\"tool\":\"nav.cd\",\"params\":{\"path\":X}})
    the manifest dispatcher expects. If this drifts, nav.cd works only
    for the PB-decoded path and silently fails for the fast path."""
    from controller.tier0_fast_path import try_fast_path

    _, tmp_path = nav_cd_session
    intent = {
        "action": "nav.cd",
        "target": str(tmp_path),
        "reason": "user_requested",
        "risk_level": "low",
    }
    # tier=0 required for fast-path qualification:
    tool_call = try_fast_path(intent, tier=0)
    assert tool_call is not None, (
        "nav.cd must be in TIER0_FAST_PATH_TOOLS so the fast path can "
        "skip PB for this Tier-0 action"
    )
    assert tool_call["tool"] == "nav.cd"
    assert tool_call["params"] == {"path": str(tmp_path)}, (
        f"unexpected params shape: {tool_call['params']!r}"
    )


def test_tier0_fast_path_does_not_qualify_at_higher_tier(nav_cd_session):
    """Belt-and-braces: even nav.cd (a Tier-0 tool) must NOT qualify
    for the fast path if the caller says the classifier upgraded the
    tier. Prevents future tier-drift from silently bypassing the
    verifier."""
    from controller.tier0_fast_path import try_fast_path

    _, tmp_path = nav_cd_session
    intent = {
        "action": "nav.cd",
        "target": str(tmp_path),
        "reason": "user_requested",
        "risk_level": "low",
    }
    assert try_fast_path(intent, tier=1) is None
    assert try_fast_path(intent, tier=2) is None
    assert try_fast_path(intent, tier=3) is None
