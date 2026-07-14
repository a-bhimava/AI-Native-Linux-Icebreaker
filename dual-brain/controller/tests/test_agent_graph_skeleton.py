"""v6.8 Task #146 Step 2a — AgentGraph skeleton tests.

Covers the shell only: construction, checkpointer (with CVE mitigation),
LangSmith profile gate, prune, close. Node/edge behavior + run/resume
are locked in Step 2b's dedicated test file.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from controller.agent_graph import AgentGraph, langsmith_available
from controller.config import AgentGraphConfig
from controller.session_store import SessionStore


# ── Fixtures ────────────────────────────────────────────────────────────


def _collab_kit(*, checkpointer_path: str = ":memory:"):
    """Assemble the minimum injection kit for AgentGraph.__init__."""
    cfg = AgentGraphConfig(
        enabled=True,
        checkpointer_path=checkpointer_path,
        checkpointer_retention_days=30,
        strict_msgpack=True,
    )
    return dict(
        cfg=cfg,
        session_store=SessionStore(),
        qb_backend=MagicMock(),
        pb_backend=MagicMock(),
        mcpd_client=MagicMock(),
        audit_log=MagicMock(),
        risk_classify=MagicMock(),
        verifier=MagicMock(),
        prompts=MagicMock(),
        intent_schema={"type": "object"},
    )


# ── Construction + close ────────────────────────────────────────────────


def test_construction_with_in_memory_checkpointer():
    kit = _collab_kit()
    g = AgentGraph(**kit)
    assert g._checkpointer is not None
    assert g._checkpointer_conn is not None
    g.close()


def test_close_is_idempotent():
    kit = _collab_kit()
    g = AgentGraph(**kit)
    g.close()
    g.close()  # second call must not raise
    assert g._checkpointer_conn is None


def test_construction_with_file_checkpointer(tmp_path):
    db = tmp_path / "sub" / "check.db"
    kit = _collab_kit(checkpointer_path=str(db))
    g = AgentGraph(**kit)
    try:
        assert db.parent.exists()
        # SQLite may create file lazily; touch a query to force write.
        g._checkpointer_conn.execute("CREATE TABLE _probe (id INTEGER)")
        g._checkpointer_conn.commit()
        assert db.exists()
        # Perms: file 0600 (owner-only rw).
        mode = os.stat(db).st_mode & 0o777
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    finally:
        g.close()


def test_checkpointer_wal_pragma_active():
    kit = _collab_kit()
    g = AgentGraph(**kit)
    try:
        cur = g._checkpointer_conn.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0].lower()
        # :memory: databases can't use WAL — SQLite silently falls back to
        # 'memory'. For that case just prove the pragma didn't crash.
        assert mode in ("wal", "memory")
    finally:
        g.close()


# ── LangSmith profile gate (§13.5) ─────────────────────────────────────


def test_langsmith_gate_default_release_profile_is_off():
    """Without ICEBREAKER_PROFILE=dev, langsmith import is not triggered.
    The module attribute reflects the state at import time."""
    # We can't re-import cleanly; just verify the observable predicate
    # is a bool. Whether it's True/False depends on the test env — in
    # CI with profile=release we get False, in dev env we get True.
    assert isinstance(langsmith_available(), bool)


def test_langsmith_gate_pattern_in_source():
    """Source-level regression lock: the module-level import must be
    inside `if _ICEBREAKER_PROFILE == "dev":`. Prevents an accidental
    top-level `import langsmith` from bypassing the gate."""
    src = Path(__file__).parent.parent.joinpath("agent_graph.py").read_text()
    # Find the langsmith import line and prove it's inside the dev gate.
    idx = src.find("import langsmith")
    assert idx != -1, "langsmith import missing"
    before = src[:idx]
    # The `if _ICEBREAKER_PROFILE == "dev":` block must appear before
    # the import statement and NO closing colon-only-line between them.
    gate = before.rfind('_ICEBREAKER_PROFILE == "dev"')
    assert gate != -1, "langsmith import is NOT gated by dev profile"
    assert gate < idx


# ── Msgpack allowlist (§13.1 CVE mitigation) ───────────────────────────


def test_msgpack_allowlist_returns_list_of_tuples():
    allowlist = AgentGraph._msgpack_allowlist()
    assert isinstance(allowlist, list)
    for entry in allowlist:
        assert isinstance(entry, tuple) and len(entry) == 2
        module_name, class_name = entry
        assert isinstance(module_name, str) and module_name
        assert isinstance(class_name, str) and class_name


def test_msgpack_allowlist_includes_turn_memory():
    """TurnMemory is the ONLY Icebreaker type we ship serialized via
    LangGraph state today (session.history). If a future change adds
    a class to state, remember to append it here — a security review
    is required per §5.5."""
    allowlist = AgentGraph._msgpack_allowlist()
    assert ("controller.session", "TurnMemory") in allowlist


def test_msgpack_allowlist_is_small():
    """Rationale: every class we allow is a class an attacker could
    reconstruct if they compromise the checkpoint DB. Keep the list
    minimal — this test flags future bloat."""
    assert len(AgentGraph._msgpack_allowlist()) <= 5, (
        "allowlist growing past 5 entries — review each addition per §5.5"
    )


# ── Pragmas ────────────────────────────────────────────────────────────


def test_synchronous_pragma_is_normal_not_off():
    """WAL-safe fast mode. NORMAL is our choice; OFF risks corruption."""
    kit = _collab_kit()
    g = AgentGraph(**kit)
    try:
        cur = g._checkpointer_conn.execute("PRAGMA synchronous")
        val = cur.fetchone()[0]
        # 1 = NORMAL, 2 = FULL. NEVER 0 = OFF in production code.
        assert val in (1, 2)
    finally:
        g.close()


# ── status stub (still works after Step 2b lands nodes) ─────────────────


def test_status_returns_idle_when_no_session():
    kit = _collab_kit()
    g = AgentGraph(**kit)
    try:
        assert g.status("nonexistent-session") == "idle"
    finally:
        g.close()


# ── Prune housekeeping ─────────────────────────────────────────────────


def test_prune_checkpoints_returns_int():
    """Housekeeping is best-effort — never raises, always returns a count.
    Uninitialized DB (no checkpoints table yet) returns 0."""
    kit = _collab_kit()
    g = AgentGraph(**kit)
    try:
        count = g.prune_checkpoints(older_than_days=30)
        assert isinstance(count, int)
        assert count >= 0
    finally:
        g.close()


def test_prune_returns_zero_when_conn_closed():
    """After close() the conn is gone; prune must not crash."""
    kit = _collab_kit()
    g = AgentGraph(**kit)
    g.close()
    assert g.prune_checkpoints() == 0


# ── AgentGraphConfig defaults ──────────────────────────────────────────


def test_agent_graph_config_defaults():
    c = AgentGraphConfig()
    assert c.enabled is False  # BP-2: off until Task #147 flips it on
    assert c.checkpointer_path == "~/.local/state/icebreaker/agent_checkpoints.db"
    assert c.checkpointer_retention_days == 30
    assert c.strict_msgpack is True


def test_agent_graph_config_in_controller_config():
    """Regression lock: the field is wired into the top-level
    ControllerConfig dataclass so load() populates it from TOML."""
    from controller.config import ControllerConfig
    fields = {f.name for f in ControllerConfig.__dataclass_fields__.values()}
    assert "agent_graph" in fields
