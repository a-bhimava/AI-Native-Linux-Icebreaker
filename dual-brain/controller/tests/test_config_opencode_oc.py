"""Tests for the v6.13_OC Fix Q config + backend registry surface.

Covers:
  - `opencode_oc` backend registered via BP-1 registry pattern.
  - NoOpBrainBackend.complete() raises a clear, actionable message.
  - Config parses [qb.opencode_oc] with defaults + all overrides.
  - Schema rejects invalid types (boolean vs string, missing enum
    membership).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


# ── backend registry ──────────────────────────────────────────────────


def test_backend_registry_includes_opencode_oc():
    """Import backends package (which imports opencode_oc for the
    decorator side effect); look up in registry; assert the class is
    NoOpBrainBackend.

    Robust to other tests that call ``_reset_registry_for_tests()`` —
    we force a re-import via importlib.reload so the decorator fires
    even if the registry was cleared earlier in the same pytest run.
    """
    import importlib
    from controller.backends import opencode_oc as oc_mod
    from controller.backends.registry import _REGISTRY

    if "opencode_oc" not in _REGISTRY:
        importlib.reload(oc_mod)

    assert "opencode_oc" in _REGISTRY, \
        f"opencode_oc not in registry; registered: {sorted(_REGISTRY.keys())}"
    from controller.backends.opencode_oc import NoOpBrainBackend
    assert _REGISTRY["opencode_oc"] is NoOpBrainBackend


def test_noop_brain_backend_call_provider_raises_clear_message():
    """The subclass point (`_call_provider`) is what actually raises;
    `.complete()` on BrainBackend calls into `_call_provider`. Any user
    who ends up here needs to know WHY and HOW to fix."""
    from controller.backends.opencode_oc import NoOpBrainBackend

    # Instantiate with a dummy config
    class _DummyConfig:
        model = "opencode_oc"
        max_tokens = 0
        timeout_seconds = 0
    backend = NoOpBrainBackend(_DummyConfig())

    with pytest.raises(RuntimeError) as excinfo:
        # _call_provider is the abstract-method site the base class dispatches to.
        # Calling it directly bypasses the schema/retry machinery but
        # exercises the same guard.
        backend._call_provider(envelope=None)  # type: ignore[arg-type]

    msg = str(excinfo.value)
    assert "opencode_oc" in msg
    assert "cfg.qb.name" in msg  # tells the user WHERE the gate should be
    assert "external" in msg.lower() or "outside" in msg.lower()


# ── config parsing ────────────────────────────────────────────────────


_MINIMAL_TOML_OC = """
[qb]
backend = "opencode_oc"

[qb.opencode_oc]

[hitl]

[run]
mcpd_binary = "/tmp/mcpd"
audit_log = "/tmp/audit.log"
"""

_FULL_OVERRIDE_TOML_OC = """
[qb]
backend = "opencode_oc"

[qb.opencode_oc]
enabled = false
audit_bridge_enabled = false
mcpd_audit_path = "/custom/path/mcpd.log"
config_path = "/custom/qb_oc.json"

[hitl]

[run]
mcpd_binary = "/tmp/mcpd"
audit_log = "/tmp/audit.log"
"""


def _load_config_from_text(text: str):
    """Write a TOML string to a temp file, call config.load(path)."""
    from controller.config import load

    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(text)
        f.flush()
        path = Path(f.name)
    try:
        return load(path)
    finally:
        path.unlink(missing_ok=True)


def test_config_parses_opencode_oc_section_with_defaults():
    """Empty [qb.opencode_oc] should still produce an OpencodeOcConfig
    with sensible defaults — not None."""
    cfg = _load_config_from_text(_MINIMAL_TOML_OC)

    assert cfg.qb.name == "opencode_oc"
    assert cfg.opencode_oc is not None
    assert cfg.opencode_oc.enabled is True
    assert cfg.opencode_oc.audit_bridge_enabled is True
    assert cfg.opencode_oc.mcpd_audit_path == "/var/log/mcpd/audit.log"
    assert cfg.opencode_oc.config_path == "/etc/icebreaker/qb_oc.json"


def test_config_parses_opencode_oc_with_all_fields_overridden():
    cfg = _load_config_from_text(_FULL_OVERRIDE_TOML_OC)

    assert cfg.opencode_oc is not None
    assert cfg.opencode_oc.enabled is False
    assert cfg.opencode_oc.audit_bridge_enabled is False
    assert cfg.opencode_oc.mcpd_audit_path == "/custom/path/mcpd.log"
    assert cfg.opencode_oc.config_path == "/custom/qb_oc.json"


def test_config_current_edition_leaves_opencode_oc_none():
    """When qb.backend is gemini/anthropic/etc and no [qb.opencode_oc]
    section is present, cfg.opencode_oc should be None — nothing to
    configure or start."""
    toml = """
[qb]
backend = "gemini"

[qb.gemini]
model = "gemini-2.5-flash"
api_key_env = "GEMINI_API_KEY"
max_tokens = 4096
timeout_seconds = 30

[hitl]

[run]
mcpd_binary = "/tmp/mcpd"
audit_log = "/tmp/audit.log"
"""
    cfg = _load_config_from_text(toml)
    assert cfg.qb.name == "gemini"
    assert cfg.opencode_oc is None


# ── schema rejects invalid types ──────────────────────────────────────


def test_schema_rejects_invalid_opencode_oc_types():
    """`enabled = "yes"` should fail schema validation (boolean, not
    string). Same for audit_bridge_enabled."""
    bad_toml = """
[qb]
backend = "opencode_oc"

[qb.opencode_oc]
enabled = "yes"

[hitl]

[run]
mcpd_binary = "/tmp/mcpd"
audit_log = "/tmp/audit.log"
"""
    from controller.backends.base import BrainConfigError

    with pytest.raises(BrainConfigError):
        _load_config_from_text(bad_toml)


def test_schema_rejects_unknown_backend_name():
    """Sanity check: existing enum enforcement still works alongside
    the new opencode_oc addition."""
    bad_toml = """
[qb]
backend = "definitely_not_a_backend"

[hitl]

[run]
mcpd_binary = "/tmp/mcpd"
audit_log = "/tmp/audit.log"
"""
    from controller.backends.base import BrainConfigError

    with pytest.raises(BrainConfigError):
        _load_config_from_text(bad_toml)


# ── F-92 regression: OC-edition planner short-circuit ─────────────────
# v6.13_OC arm64 UTM live test 2026-07-27: after F-91 unblocked the
# daemon crash-loop, direct RPC via /usr/share/icebreaker/shell/ib_run.py
# hit `agent_graph_nodes._run` → `collab["qb"].complete(...)` → NoOp
# backend RuntimeError → `[error] opencode_oc backend has no in-process
# LLM...` in the user's shell. Same shape for `run_turn` (GUI chatbot
# path). Every planner entry needs to short-circuit BEFORE touching QB
# in OC mode.


class _StubSession:
    """Minimal SessionState-shaped fake for the short-circuit tests."""

    def __init__(self):
        self.session_id = "test-session"
        self.turn_count = 0
        self.backend = "opencode_oc"
        self.touched = 0

    def touch(self):
        self.touched += 1


def _oc_controller():
    """Build a Controller wired to opencode_oc + capture audit writes.

    Uses a minimal cfg stub so we don't have to load a full TOML; the
    short-circuit only reads `cfg.qb.name` and `cfg.session.user_name`.
    """
    from controller.main import Controller

    class _Cfg:
        class qb:
            name = "opencode_oc"
        class session:
            user_name = "icebreaker"
    class _Audit:
        def __init__(self): self.rows = []
        def write_fields(self, fields): self.rows.append(fields)
    controller = Controller.__new__(Controller)  # bypass __init__
    controller._cfg = _Cfg
    controller._audit = _Audit()
    controller._system_logger = None  # _log_exception tolerates None
    return controller


def test_run_turn_short_circuits_in_oc_edition():
    """F-92: `run_turn` must NOT invoke QB when cfg.qb.name == 'opencode_oc'.
    Instead returns a soft UNSUPPORTED TurnResult pointing at icebreaker-oc."""
    from controller.audit import Outcome

    ctrl = _oc_controller()
    session = _StubSession()
    # Sabotage _run_turn_inner so if the gate fails, the test blows up
    # loudly instead of quietly walking down the QB path.
    def _explode(*a, **kw):
        raise AssertionError("_run_turn_inner should NOT be reached in OC mode")
    ctrl._run_turn_inner = _explode

    result = ctrl.run_turn("what time is it", session)

    assert result.success is True, "soft outcome — not a hard error"
    assert result.outcome == Outcome.UNSUPPORTED
    assert result.backend == "opencode_oc"
    assert "icebreaker-oc" in result.output.lower() \
        or "opencode" in result.output.lower(), \
        f"redirect message must name the correct entry point; got: {result.output!r}"
    assert session.touched == 1
    # INV-8 R18: even the short-circuit gets audited.
    assert len(ctrl._audit.rows) == 1
    assert ctrl._audit.rows[0].outcome == Outcome.UNSUPPORTED
    assert ctrl._audit.rows[0].backend == "opencode_oc"


def test_run_turn_streaming_short_circuits_in_oc_edition():
    """F-92 streaming twin. Yields TokenEvent + ResultEvent (F-86 soft
    outcome pattern) so shell/GUI clients render text, not an [error]."""
    from controller.audit import Outcome
    from controller.turn_events import ResultEvent, TokenEvent

    ctrl = _oc_controller()
    session = _StubSession()

    events = list(ctrl.run_turn_streaming("what time is it", session))

    token_events = [e for e in events if isinstance(e, TokenEvent)]
    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(token_events) == 1, f"expected 1 TokenEvent, got {events!r}"
    assert token_events[0].final is True
    assert len(result_events) == 1
    r = result_events[0].result
    assert r.success is True
    assert r.outcome == Outcome.UNSUPPORTED
    assert r.backend == "opencode_oc"
    # INV-8 audit fired exactly once (BP-13: prevents F-82-style silent regression
    # where a new short-circuit path forgets to write audit).
    assert len(ctrl._audit.rows) == 1


def test_run_turn_does_NOT_short_circuit_when_qb_is_gemini():
    """Negative test: current edition daemon must still hit the QB path.
    Guards against a gate that accidentally fires for every backend."""
    from controller.main import Controller

    class _Cfg:
        class qb:
            name = "gemini"
        class session:
            user_name = "icebreaker"
    ctrl = Controller.__new__(Controller)
    ctrl._cfg = _Cfg
    ctrl._audit = type("_", (), {"write_fields": lambda *a, **kw: None})()

    called = {"n": 0}
    def _inner(*a, **kw):
        called["n"] += 1
        from controller.main import TurnResult
        from controller.audit import Outcome
        return TurnResult(success=True, output="ok", outcome=Outcome.EXECUTED)
    ctrl._run_turn_inner = _inner

    session = _StubSession()
    session.backend = "gemini"
    result = ctrl.run_turn("hello", session)
    assert called["n"] == 1, "gemini backend must still route through _run_turn_inner"
    assert result.output == "ok"
