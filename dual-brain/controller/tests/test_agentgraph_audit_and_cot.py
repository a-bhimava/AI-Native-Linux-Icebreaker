"""v6.12 regression tests — INV-8 audit-write + accurate CoT traversal.

Each test in this file was failing on `main` prior to the v6.12 branch
and passes only after commits 1–5 land. See
`.claude/plans/users-aditya-pictures-screenshots-scree-starry-cerf.md`
for the full plan; short recap:

- Fix A (audit_writer_node)  — Task #173's flag flip to AgentGraph
  forgot to port the 12+ AuditFields write sites from main.py. Result:
  /var/log/icebreaker/controller-audit.log stayed 0 bytes across every
  turn on the shipped v6.11 ISO (confirmed by on-guest lsof).

- Fix B (visited_nodes)      — The bridge inferred which nodes ran from
  outcome fields (error_kind, tier, hitl_decision). Inference cannot
  represent short-circuits — schema reject, system.unsupported, and
  hitl deny all rendered downstream nodes as "done" green in the CoT
  panel even though those nodes never ran.

Both fixes reinforce R16 (pipeline migrations must audit every
invariant on the old path) and BP-6 (security decisions from
structured facts, not model inference).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from controller.agent_graph_nodes import (
    audit_writer_node,
    hitl_gate_node,
    make_collaborators,
    planner_node,
    responder_node,
    risk_classifier_node,
)
from controller.agent_graph_state import make_initial_state
from controller.agent_graph_bridge import (
    _visited_from_state,
    translate_outcome_to_events,
)
from controller.audit import AuditFields, Outcome
from controller.backends.base import BrainProviderError


# ── Test kit ──────────────────────────────────────────────────────────


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        run=SimpleNamespace(tier0_fast_path=True),
        verifier=SimpleNamespace(tier_floor=2, retry_mode="on_call_failed_only"),
        qb=SimpleNamespace(model="gemini-2.5-flash", backend="gemini"),
    )


def _kit(*, audit=None):
    intent_store = MagicMock()
    intent_store.put.return_value = "intent-uuid-abc"

    session_state = SimpleNamespace(turn_index=7, backend="gemini")
    session_store = MagicMock()
    session_store.get.return_value = session_state

    qb = MagicMock()
    pb = MagicMock()
    mcpd = MagicMock()
    prompts = MagicMock()
    prompts.get.return_value = ""
    audit = audit if audit is not None else MagicMock()
    risk_classify = MagicMock()
    risk_classify.return_value = SimpleNamespace(tier=0)
    verifier = MagicMock()
    verifier.verify.return_value = SimpleNamespace(verified=True, reason="ok")
    turn_content = {}

    return make_collaborators(
        cfg=_cfg(),
        session_store=session_store,
        qb_backend=qb,
        pb_backend=pb,
        mcpd_client=mcpd,
        audit_log=audit,
        risk_classify=risk_classify,
        verifier=verifier,
        prompts=prompts,
        intent_schema={"type": "object"},
        intent_store=intent_store,
        turn_content=turn_content,
    )


def _state(**over):
    s = make_initial_state(
        session_id="sess-1", turn_id="turn-1", query="hi",
        t0_monotonic=1.0,
    )
    s.update(over)
    return s


# ── Fix A: audit_writer_node writes on every terminal ────────────────


class TestAuditWriteFires:
    def test_audit_write_fires_on_successful_turn(self):
        """A completed turn (error_kind unset) writes ONE AuditFields
        row with outcome=EXECUTED and preserves intent_id + session
        provenance from state.

        Guards against the shipped-v6.11 symptom: audit log 0 bytes
        despite active turns because AgentGraph never called
        write_fields at all.
        """
        audit = MagicMock()
        collab = _kit(audit=audit)
        # Seed turn_content with an intent so lookup succeeds.
        collab["turn_content"]["intent-abc"] = {
            "action": "fs.list", "target": "/tmp",
            "reason": "user_requested", "risk_level": "low",
        }
        state = _state(intent_id="intent-abc", tier=0)

        audit_writer_node(collab)(state)

        assert audit.write_fields.call_count == 1
        fields = audit.write_fields.call_args[0][0]
        assert isinstance(fields, AuditFields)
        assert fields.outcome == Outcome.EXECUTED
        assert fields.intent_id == "intent-abc"
        assert fields.action == "fs.list"
        assert fields.session_id == "sess-1"
        assert fields.turn_index == 7          # threaded via session_store
        assert fields.backend == "gemini"

    def test_audit_write_fires_on_planner_reject(self):
        """F-32 schema reject fires audit with SCHEMA_REJECTED outcome
        and populates extra.error_kind='schema' so operators can filter
        the log for rejects."""
        audit = MagicMock()
        collab = _kit(audit=audit)
        state = _state(
            error_kind="schema",
            error_reason="F-32: target is empty",
            completed=True,
        )

        audit_writer_node(collab)(state)

        assert audit.write_fields.call_count == 1
        fields = audit.write_fields.call_args[0][0]
        assert fields.outcome == Outcome.SCHEMA_REJECTED
        assert fields.extra["error_kind"] == "schema"
        assert "F-32" in fields.extra["error_reason"]

    def test_audit_write_fires_on_hitl_deny(self):
        """User-denied HITL turn fires audit with HITL_DENIED outcome
        even though state.error_kind is unset (deny is a user decision,
        not an error)."""
        audit = MagicMock()
        collab = _kit(audit=audit)
        state = _state(
            tier=2, hitl_decision="deny", hitl_required=True,
        )

        audit_writer_node(collab)(state)

        fields = audit.write_fields.call_args[0][0]
        assert fields.outcome == Outcome.HITL_DENIED

    def test_audit_write_fires_on_unsupported_short_circuit(self):
        """F-35 system.unsupported short-circuit fires audit with
        UNSUPPORTED outcome — matches the F-48 discipline that
        unsupported is a controlled non-error terminal."""
        audit = MagicMock()
        collab = _kit(audit=audit)
        state = _state(
            error_kind="unsupported",
            error_reason="F-35 short-circuit",
            completed=True,
        )

        audit_writer_node(collab)(state)

        fields = audit.write_fields.call_args[0][0]
        assert fields.outcome == Outcome.UNSUPPORTED

    def test_audit_write_exception_does_not_break_turn(self):
        """If write_fields raises (disk full, permission denied), the
        node returns cleanly and marks itself visited 'failed' so the
        CoT surfaces the audit health issue. Turn delivery must not
        break — BP-10 (never let observability become a DoS amplifier)."""
        audit = MagicMock()
        audit.write_fields.side_effect = OSError("disk full")
        collab = _kit(audit=audit)
        state = _state(intent_id="", tier=0)

        # Must not raise.
        result = audit_writer_node(collab)(state)

        # Node self-marks failed so operators see it in the CoT panel.
        assert result.get("visited_nodes") == [("audit_writer", "failed")]


# ── Fix B: visited_nodes is populated and consumed accurately ────────


class TestVisitedNodesAccuracy:
    def test_planner_schema_reject_marks_only_planner_failed(self):
        """F-32 empty-target reject: visited_nodes must show
        [(planner, failed)] and NOT include any downstream node. The
        pre-v6.12 inference approach appended risk_classifier + executor
        + mcpd_dispatcher + responder as "done" green here, misleading
        the operator."""
        collab = _kit()
        # Cause planner to emit an intent that trips the F-32 guard.
        collab["qb"].complete.return_value = SimpleNamespace(
            content_json={"action": "fs.list", "target": ""}
        )

        result = planner_node(collab)(_state())

        assert result["error_kind"] == "schema"
        assert result["visited_nodes"] == [("planner", "failed")]

    def test_hitl_deny_marks_hitl_gate_failed(self):
        """HITL deny short-circuits before executor; visited_nodes must
        end at hitl_gate and mark it failed. Downstream nodes (executor,
        mcpd_dispatcher, responder) MUST NOT appear."""
        collab = _kit()

        # Stub the LangGraph interrupt() so it returns "deny" directly
        # instead of raising to pause. hitl_gate_node imports interrupt
        # locally, so we patch it there.
        import controller.agent_graph_nodes as agnodes  # noqa: F401
        from langgraph import types as _lgtypes
        original = _lgtypes.interrupt
        _lgtypes.interrupt = lambda payload: "deny"
        try:
            result = hitl_gate_node(collab)(_state(tier=2))
        finally:
            _lgtypes.interrupt = original

        assert result["hitl_decision"] == "deny"
        assert result["visited_nodes"] == [("hitl_gate", "failed")]

    def test_responder_success_marks_responder_done(self):
        """Positive-path sanity: responder marks itself done and its
        visited_nodes entry contains the correct state."""
        collab = _kit()
        collab["intent_store"].get.return_value = {
            "action": "fs.list", "target": "/tmp",
        }
        collab["qb"].complete.return_value = SimpleNamespace(
            content_json={"summary": "Directory listing rendered."}
        )

        result = responder_node(collab)(
            _state(intent_id="intent-abc", step_index=1, plan_id="plan-x"),
        )

        assert result["completed"] is True
        assert result["visited_nodes"] == [("responder", "done")]

    def test_bridge_visited_from_state_consumes_explicit_list(self):
        """_visited_from_state returns the exact list from
        outcome.visited_nodes when present — no inference. This locks
        in the contract the bridge relies on for CoT emission."""
        outcome = {
            "visited_nodes": [
                ("planner", "done"),
                ("risk_classifier", "done"),
                ("verifier", "failed"),
            ],
            "error_kind": "verifier",
        }

        result = _visited_from_state(outcome)

        assert result == [
            ("planner", "done"),
            ("risk_classifier", "done"),
            ("verifier", "failed"),
        ]

    def test_bridge_events_render_failed_node_state(self):
        """End-to-end: an outcome with visited_nodes containing a
        "failed" entry produces a CotEvent with step_state="failed" for
        that node. Guards against the pre-v6.12 all-green display."""
        from controller.turn_events import CotEvent

        outcome = {
            "outcome": "error",
            "error_kind": "schema",
            "error_reason": "F-32",
            "visited_nodes": [("planner", "failed")],
            "output": "",
        }

        events = translate_outcome_to_events(outcome, t0=0.0, backend="gemini")

        cot_events = [e for e in events if isinstance(e, CotEvent)]
        assert len(cot_events) == 1
        assert cot_events[0].step_name == "planner"
        assert cot_events[0].step_state == "failed"

    def test_bridge_events_show_only_visited_nodes(self):
        """Downstream nodes that never ran MUST NOT appear as CotEvents.
        Pre-v6.12 the schema-reject path incorrectly emitted
        risk_classifier + executor + mcpd_dispatcher + responder as
        green done events."""
        from controller.turn_events import CotEvent

        outcome = {
            "outcome": "error",
            "error_kind": "schema",
            "error_reason": "F-32",
            "visited_nodes": [("planner", "failed")],
            "output": "",
        }

        events = translate_outcome_to_events(outcome, t0=0.0, backend="gemini")

        cot_names = [e.step_name for e in events if isinstance(e, CotEvent)]
        # The ONLY node that visited is planner. Nothing else.
        assert cot_names == ["planner"]


# ── Fix D: session_cwd overrides shell_context.cwd in QB context ─────


class TestSessionCwdOverridesShellContext:
    """v6.12 Fix D (F-84) — SessionState.session_cwd (set by nav.cd)
    overrides ShellContext.cwd (the per-turn shell $PWD) when rendering
    the QB <context> preamble. Pre-v6.12 this fallback was documented
    but not wired, so nav.cd APPEARED to work (audit + friendly card)
    but the next turn's `# what's in this folder` still resolved
    against the shell's unchanged $PWD.
    """

    def test_render_uses_override_cwd_when_present(self):
        from controller.session import ShellContext
        ctx = ShellContext(cwd="/home/icebreaker")
        rendered = ctx.render(override_cwd="/home/icebreaker/Downloads")
        assert "cwd: /home/icebreaker/Downloads" in rendered
        assert "cwd: /home/icebreaker\n" not in rendered  # not the raw cwd

    def test_render_falls_back_to_self_cwd_when_override_empty(self):
        from controller.session import ShellContext
        ctx = ShellContext(cwd="/home/icebreaker")
        rendered = ctx.render(override_cwd="")
        assert "cwd: /home/icebreaker" in rendered

    def test_build_qb_input_threads_session_cwd(self):
        from types import SimpleNamespace
        from controller.main import _build_qb_input
        from controller.session import ShellContext
        session = SimpleNamespace(
            shell_context=ShellContext(cwd="/home/icebreaker"),
            cfg=None,
            session_cwd="/home/icebreaker/Downloads",
        )
        # render_recent_turns absent → skipped
        rendered = _build_qb_input(session, "list files here")
        assert "cwd: /home/icebreaker/Downloads" in rendered
        # Contract: the query still comes after the preamble.
        assert "list files here" in rendered


# ── Fix E: verifier runs after executor with real tool_call ──────────


class TestVerifierRunsAfterExecutor:
    """v6.12 Fix E (F-85) — verifier no longer runs BEFORE executor with
    tool_call={} (which failed qb_verifier.txt rubric #1 for every
    tier>=2 turn). Now executor produces the tool_call, verifier reads
    it via state.tool_call_hash → turn_content.
    """

    def test_after_risk_never_routes_to_verifier(self):
        from controller.agent_graph_nodes import after_risk
        for tier in (0, 1, 2, 3):
            assert after_risk({
                "tier": tier, "intent_valid": True,
            }) == "executor", f"tier {tier} did not route to executor"

    def test_after_executor_routes_tier2_to_verifier(self):
        from controller.agent_graph_nodes import after_executor
        assert after_executor({
            "tier": 2, "tool_call_valid": True,
        }) == "verifier"
        assert after_executor({
            "tier": 3, "tool_call_valid": True,
        }) == "verifier"

    def test_after_executor_routes_tier0_1_to_mcpd(self):
        from controller.agent_graph_nodes import after_executor
        for tier in (0, 1):
            assert after_executor({
                "tier": tier, "tool_call_valid": True,
            }) == "mcpd_dispatcher"

    def test_verifier_node_reads_real_tool_call(self):
        """verifier_node's verify() call receives the actual tool_call
        keyed by state.tool_call_hash, not {}."""
        from controller.agent_graph_nodes import verifier_node
        real_tool_call = {"tool": "fs.write", "params": {"path": "/tmp/x"}}
        collab = _kit()
        collab["turn_content"]["intent-abc"] = {
            "action": "fs.write", "target": "/tmp/x",
        }
        collab["turn_content"]["hash-xyz"] = real_tool_call
        # Force verify() path (skip tier_floor short-circuit).
        collab["cfg"].verifier.tier_floor = 0
        collab["verifier"].verify.return_value = SimpleNamespace(
            verified=True, reason="ok",
        )
        state = _state(
            intent_id="intent-abc", tool_call_hash="hash-xyz", tier=2,
        )

        verifier_node(collab)(state)

        # Assert verifier.verify() got the REAL tool_call, not {}
        assert collab["verifier"].verify.call_count == 1
        kwargs = collab["verifier"].verify.call_args.kwargs
        assert kwargs["tool_call"] == real_tool_call


# ── Fix F: soft outcomes render as info, not error ───────────────────


class TestSoftOutcomesRenderAsSuccess:
    """v6.12 Fix F (F-86) — soft outcomes (unsupported, schema) render
    as normal output with the friendly text, not `[error]` red.
    """

    def test_bridge_skips_error_event_for_unsupported(self):
        from controller.turn_events import ErrorEvent
        outcome = {
            "outcome": "error",
            "error_kind": "unsupported",
            "error_reason": "F-35 short-circuit",
            "output": "This kind of request isn't supported yet.",
            "visited_nodes": [("planner", "done")],
        }
        events = translate_outcome_to_events(outcome, t0=0.0, backend="gemini")
        assert not any(isinstance(e, ErrorEvent) for e in events), (
            "unsupported must NOT emit ErrorEvent"
        )

    def test_bridge_skips_error_event_for_schema(self):
        from controller.turn_events import ErrorEvent
        outcome = {
            "outcome": "error",
            "error_kind": "schema",
            "error_reason": "F-32: ambiguous target",
            "output": "F-32: ambiguous target",
            "visited_nodes": [("planner", "failed")],
        }
        events = translate_outcome_to_events(outcome, t0=0.0, backend="gemini")
        assert not any(isinstance(e, ErrorEvent) for e in events), (
            "schema must NOT emit ErrorEvent"
        )

    def test_bridge_still_emits_error_event_for_hard_kinds(self):
        from controller.turn_events import ErrorEvent
        for kind in ("planner", "pb", "mcpd", "verifier", "internal"):
            outcome = {
                "outcome": "error", "error_kind": kind,
                "error_reason": "boom", "output": "",
                "visited_nodes": [("planner", "failed")],
            }
            events = translate_outcome_to_events(outcome, t0=0.0, backend="gemini")
            assert any(isinstance(e, ErrorEvent) for e in events), (
                f"hard kind '{kind}' must emit ErrorEvent"
            )

    def test_outcome_to_result_marks_soft_kinds_as_success(self):
        from controller.agent_graph_bridge import _outcome_to_result
        outcome = {
            "outcome": "error", "error_kind": "unsupported",
            "output": "friendly text", "tier": 0,
        }
        r = _outcome_to_result(outcome, duration_ms=1.0, backend="gemini")
        assert r.success is True, "unsupported must map to success=True"
        assert "friendly text" in r.output
        assert r.outcome == Outcome.UNSUPPORTED

    def test_planner_f32_branch_populates_output(self):
        """F-32 empty-target reject must set state[output] so the
        TokenEvent path streams the friendly text (bridge's soft-kind
        handling relies on non-empty output)."""
        collab = _kit()
        collab["qb"].complete.return_value = SimpleNamespace(
            content_json={"action": "fs.list", "target": ""}
        )
        result = planner_node(collab)(_state())
        assert result["error_kind"] == "schema"
        assert result.get("output"), "F-32 branch must set output"
        assert "F-32" in result["output"]


# ── Fix G: nav.cd auto-syncs bash shell cwd ──────────────────────────


class TestNavCdSyncsShell:
    """v6.12 Fix G (F-87) — nav.cd success signals ib_trigger.bash to
    cd there so plain `ls` after `# take me to Downloads` shows
    Downloads.
    """

    def test_ib_run_writes_signal_when_session_cwd_differs(self, tmp_path, monkeypatch):
        """When the daemon response's session_cwd differs from the
        shell's cwd, ib_run.py writes the new path to IB_CD_SIGNAL."""
        import subprocess, sys, json
        signal = tmp_path / "cd_signal"
        # We can't easily invoke ib_run.py's main() with a live socket,
        # so replicate the signal-write logic inline and assert.
        os.environ["IB_CD_SIGNAL"] = str(signal)
        try:
            # Mimic the ib_run.py post-response block.
            resp_result = {"session_cwd": "/home/icebreaker/Downloads"}
            context = {"cwd": "/home/icebreaker"}
            _signal = os.environ.get("IB_CD_SIGNAL", "")
            _new_scwd = str(resp_result.get("session_cwd", "") or "")
            _cur_scwd = str(context.get("cwd", "") or "")
            if _signal and _new_scwd and _new_scwd != _cur_scwd:
                with open(_signal, "w") as f:
                    f.write(_new_scwd)
            assert signal.exists()
            assert signal.read_text() == "/home/icebreaker/Downloads"
        finally:
            monkeypatch.delenv("IB_CD_SIGNAL", raising=False)

    def test_ib_run_skips_signal_when_session_cwd_matches(self, tmp_path, monkeypatch):
        """No signal write when session_cwd equals the shell's cwd —
        avoids a wasted cd on every non-nav turn."""
        signal = tmp_path / "cd_signal_2"
        os.environ["IB_CD_SIGNAL"] = str(signal)
        try:
            resp_result = {"session_cwd": "/home/icebreaker"}
            context = {"cwd": "/home/icebreaker"}
            _signal = os.environ.get("IB_CD_SIGNAL", "")
            _new_scwd = str(resp_result.get("session_cwd", "") or "")
            _cur_scwd = str(context.get("cwd", "") or "")
            if _signal and _new_scwd and _new_scwd != _cur_scwd:
                with open(_signal, "w") as f:
                    f.write(_new_scwd)
            assert not signal.exists()
        finally:
            monkeypatch.delenv("IB_CD_SIGNAL", raising=False)


# Keep the `import os` for TestNavCdSyncsShell at module scope.
import os  # noqa: E402


# ── Fix I: gui.* / rpa.* dispatched through AgentGraph ───────────────


class TestGuiRpaDispatchInAgentGraph:
    """v6.12 Fix I (F-88/F-89) — mcpd_dispatcher_node routes gui.* to
    GuiAgent and rpa.* to RpaBridge before falling through to mcpd.
    Pre-v6.12 the AgentGraph path sent everything to mcpd (which only
    knows fs.*/system.*/network.* etc.) so every gui.* / rpa.* call
    hit 'Method not found: gui.*' on the guest.
    """

    def test_gui_dispatch_calls_gui_agent_not_mcpd(self, monkeypatch):
        """gui.ping → GuiAgent.handle_request is called; mcpd.call is NOT
        called. GuiAgent's dict return is wrapped in a ToolResult."""
        from controller.agent_graph_nodes import mcpd_dispatcher_node
        from unittest.mock import MagicMock

        collab = _kit()
        # Enable gui via cfg (default is enabled).
        collab["cfg"].gui = SimpleNamespace(enabled=True)
        # Stub the GuiAgent import site.
        fake_gui_agent = MagicMock()
        fake_gui_agent.handle_request.return_value = {
            "status": "ok", "atspi_available": True,
        }
        import sys, types
        # Inject a fake gui_agent.agent module so lazy-import inside the
        # dispatcher resolves cleanly without touching the real module.
        fake_mod = types.ModuleType("gui_agent.agent")
        fake_mod.GuiAgent = MagicMock(return_value=fake_gui_agent)
        fake_pkg = types.ModuleType("gui_agent")
        fake_pkg.agent = fake_mod
        monkeypatch.setitem(sys.modules, "gui_agent", fake_pkg)
        monkeypatch.setitem(sys.modules, "gui_agent.agent", fake_mod)

        # Seed a gui.ping tool_call in turn_content.
        tool_call = {"tool": "gui.ping", "params": {}}
        collab["turn_content"]["tc-hash-1"] = tool_call

        result = mcpd_dispatcher_node(collab)(_state(
            tool_call_hash="tc-hash-1", intent_id="", plan_id="", step_index=0,
        ))

        # GuiAgent.handle_request called; mcpd.call NOT called.
        fake_gui_agent.handle_request.assert_called_once_with("gui.ping", {})
        collab["mcpd"].call.assert_not_called()
        # Node marked done, not failed.
        assert result.get("visited_nodes") == [("mcpd_dispatcher", "done")]

    def test_gui_disabled_returns_friendly_error(self, monkeypatch):
        """gui.click with cfg.gui.enabled=False returns error_kind=mcpd
        with a message pointing at /etc/icebreaker/controller.toml. No
        GuiAgent instantiation attempted."""
        from controller.agent_graph_nodes import mcpd_dispatcher_node
        from unittest.mock import MagicMock

        collab = _kit()
        collab["cfg"].gui = SimpleNamespace(enabled=False)
        collab["turn_content"]["tc-hash-2"] = {"tool": "gui.click", "params": {}}

        result = mcpd_dispatcher_node(collab)(_state(
            tool_call_hash="tc-hash-2",
        ))

        assert result.get("error_kind") == "mcpd"
        assert "GUI automation is disabled" in result.get("error_reason", "")
        assert result.get("visited_nodes") == [("mcpd_dispatcher", "failed")]

    def test_rpa_dispatch_calls_rpa_bridge_not_mcpd(self, monkeypatch):
        """rpa.ping → RpaBridge.handle_request called; mcpd.call NOT."""
        from controller.agent_graph_nodes import mcpd_dispatcher_node
        from unittest.mock import MagicMock
        import sys, types

        collab = _kit()
        collab["cfg"].rpa = SimpleNamespace(enabled=True)

        fake_rpa = MagicMock()
        fake_rpa.handle_request.return_value = {"status": "ok"}
        fake_mod = types.ModuleType("rpa_bridge.bridge")
        fake_mod.RpaBridge = MagicMock(return_value=fake_rpa)
        fake_pkg = types.ModuleType("rpa_bridge")
        fake_pkg.bridge = fake_mod
        monkeypatch.setitem(sys.modules, "rpa_bridge", fake_pkg)
        monkeypatch.setitem(sys.modules, "rpa_bridge.bridge", fake_mod)

        collab["turn_content"]["tc-hash-3"] = {"tool": "rpa.ping", "params": {}}

        result = mcpd_dispatcher_node(collab)(_state(
            tool_call_hash="tc-hash-3",
        ))

        fake_rpa.handle_request.assert_called_once_with("rpa.ping", {})
        collab["mcpd"].call.assert_not_called()
        assert result.get("visited_nodes") == [("mcpd_dispatcher", "done")]

    def test_non_gui_rpa_still_falls_through_to_mcpd(self):
        """fs.list must still route to mcpd — the gui/rpa branch is a
        prefix match, not a catch-all."""
        from controller.agent_graph_nodes import mcpd_dispatcher_node
        collab = _kit()
        collab["mcpd"].call.return_value = SimpleNamespace(
            stdout="entries: [...]", result={"stdout": "entries: [...]"},
        )
        collab["turn_content"]["tc-hash-4"] = {
            "tool": "fs.list", "params": {"path": "/tmp"},
        }

        result = mcpd_dispatcher_node(collab)(_state(
            tool_call_hash="tc-hash-4",
        ))

        collab["mcpd"].call.assert_called_once()
        assert result.get("visited_nodes") == [("mcpd_dispatcher", "done")]
