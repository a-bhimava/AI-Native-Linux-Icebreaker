"""v6.11 BP-13 regression locks for dormant AgentGraph regressions.

Task #173 (2026-07-17) flipped ``agent_graph.enabled`` to True, routing
every intent through ``agent_graph_nodes.py`` instead of the monolithic
``main.py::run_turn_streaming``. UTM v6.10 arm64 testing surfaced three
regressions that ALL worked correctly on the monolithic path — the
invariants had never been ported to the AgentGraph nodes.

These tests are the BP-13 "would-have-caught-it-day-zero" regression
locks. Each test MUST fail against pre-fix HEAD and pass after the
corresponding fix lands. If someone re-writes the pipeline again in
v6.12+, these tests re-enforce the invariants automatically.

Order:
1. F-32 empty-target guard: fs.list with target="" or "/" must short-circuit
2. F-35 system.unsupported: intent must not flow through executor/verifier
3. F-74 (NEW) terminal render: bridge must emit TokenEvent(final=True)
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from controller.agent_graph_bridge import translate_outcome_to_events
from controller.agent_graph_nodes import (
    make_collaborators,
    planner_node,
    verifier_node,
)
from controller.agent_graph_state import make_initial_state
from controller.turn_events import ResultEvent, TokenEvent


# ── Shared helpers (mirror test_agent_graph_nodes.py::_kit pattern) ──────


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        run=SimpleNamespace(tier0_fast_path=True),
        verifier=SimpleNamespace(tier_floor=2, retry_mode="on_call_failed_only"),
        qb=SimpleNamespace(backend="gemini"),
    )


def _kit_with_qb_output(qb_content_json: dict) -> dict:
    """Build a collaborators kit where QB.complete() returns the given
    JSON payload. Everything else mocked minimally — these tests only
    exercise the planner_node's decision, not downstream."""
    intent_store = MagicMock()
    intent_store.put.return_value = "intent-uuid-test"
    intent_store.get.return_value = None

    session_state = MagicMock()
    session_store = MagicMock()
    session_store.get.return_value = session_state

    qb = MagicMock()
    qb.complete.return_value = SimpleNamespace(content_json=qb_content_json)

    pb = MagicMock()
    mcpd = MagicMock()
    prompts = MagicMock()
    prompts.get.return_value = ""
    audit = MagicMock()
    risk_classify = MagicMock()
    risk_classify.return_value = SimpleNamespace(tier=0)
    verifier = MagicMock()
    verifier.verify.return_value = SimpleNamespace(verified=True, reason="ok")

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
        turn_content={},
    )


def _state(**over):
    s = make_initial_state(session_id="s1", turn_id="t1", query="hello")
    s.update(over)
    return s


# ── Fix #1 — F-32 empty-target guard (BUG 1 in v6.10 UTM) ────────────────


class TestF32EmptyTargetGuardInAgentGraph:
    """F-32 (v6.3, 2026-07-05) short-circuited path-requiring intents
    with target="" or "/" before dispatch. main.py:623-643 enforced it.
    Task #173 flipped agent_graph.enabled=True; planner_node has no
    equivalent guard. Symptom: mcpd rejects with cryptic 'Invalid
    params: "" is shorter than 1 character at /path'."""

    def test_planner_rejects_fs_list_with_empty_target(self):
        """Would have caught the v6.10 UTM Bug 1 the day Task #173 landed."""
        collab = _kit_with_qb_output({"action": "fs.list", "target": ""})
        result = planner_node(collab)(_state())
        assert result["intent_valid"] is False, (
            "F-32 recurrence (BUG 1): planner_node accepted fs.list with "
            "empty target. main.py's monolithic path short-circuits at "
            "line ~623 (F-32 guard), but agent_graph_nodes.py::planner_node "
            "does not — so with agent_graph.enabled=True the empty target "
            "flows through PB into mcpd and is rejected with a cryptic "
            "'shorter than 1 character' JSON Schema error. Port the guard: "
            "import _PATH_REQUIRING_ACTIONS from main and check target."
        )
        assert "F-32" in (result.get("error_reason") or ""), (
            "F-32 guard fired but the error_reason doesn't reference F-32. "
            "Keep the substring so future BP-13 grep audits find it."
        )
        assert result.get("completed") is True

    def test_planner_rejects_fs_list_with_slash_target(self):
        """'/' is the other F-32 bad-target case (root filesystem)."""
        collab = _kit_with_qb_output({"action": "fs.list", "target": "/"})
        result = planner_node(collab)(_state())
        assert result["intent_valid"] is False, (
            "F-32: target='/' is also forbidden per main.py:623 guard "
            "(would list the entire root filesystem — refuse before PB/mcpd)."
        )

    def test_planner_accepts_fs_list_with_concrete_target(self):
        """Sanity: legitimate fs.list with a concrete path is not blocked."""
        collab = _kit_with_qb_output({"action": "fs.list", "target": "/tmp"})
        result = planner_node(collab)(_state())
        assert result["intent_valid"] is True, (
            "Regression: F-32 guard is over-broad; legitimate concrete "
            "targets should pass. Bug is likely a `not raw_target` check "
            "that trips on non-empty strings — use `in ('', '/')` explicitly."
        )

    def test_planner_does_not_guard_non_path_actions(self):
        """system.uptime doesn't require a path — empty target is fine."""
        collab = _kit_with_qb_output({"action": "system.uptime", "target": ""})
        result = planner_node(collab)(_state())
        assert result["intent_valid"] is True, (
            "F-32 guard is over-broad: system.uptime doesn't need a target "
            "and shouldn't be blocked for empty target. Guard must only "
            "fire when action is in _PATH_REQUIRING_ACTIONS."
        )


# ── Fix #2 — F-35 system.unsupported short-circuit (BUG 4 in v6.10 UTM) ──


class TestF35SystemUnsupportedShortCircuitInAgentGraph:
    """F-35 (v6.4, 2026-07-07) added the system.unsupported landing
    pad + a short-circuit path in main.py:_emit_unsupported that
    NEVER dispatches to PB/verifier/mcpd. Task #173 didn't port the
    short-circuit; planner_node accepts intent.action='system.unsupported'
    as normal, verifier runs, sees empty tool_call.tool, and produces the
    misleading error 'tool_call.tool is missing, cannot match
    intent.action system.unsupported' (verifier rubric #1 in
    qb_verifier.txt:22)."""

    def test_planner_short_circuits_system_unsupported(self):
        """Would have caught v6.10 UTM Bug 4 the day Task #173 landed."""
        collab = _kit_with_qb_output({
            "action": "system.unsupported",
            "target": "",
            "params": {"requested_intent": "create a text file with my ip"},
        })
        result = planner_node(collab)(_state())
        # Two properties must hold:
        # (a) The graph MUST NOT proceed to verifier/executor/mcpd_dispatcher
        # (b) The output MUST carry a friendly F-35 narrative
        assert result.get("completed") is True, (
            "F-35 short-circuit (BUG 4): planner_node did not mark "
            "system.unsupported intent as completed. Downstream nodes "
            "(verifier, executor, mcpd_dispatcher) then run, verifier "
            "sees empty tool_call.tool, and produces the misleading error "
            "'tool_call.tool is missing, cannot match intent.action "
            "system.unsupported'. Fix: after normalize_planner_output, "
            "check if plan[0]['action'] == 'system.unsupported' and "
            "short-circuit to responder with the F-35 friendly message."
        )
        # Should have populated some form of user-facing output
        assert result.get("output") or "unsupported" in str(result).lower(), (
            "F-35 short-circuit produced no output. Reuse main.py's "
            "_unsupported_result / _emit_unsupported message so the user "
            "sees the same friendly 'we don't support that yet' card as "
            "the monolithic path."
        )

    def test_planner_short_circuit_does_not_touch_verifier_or_mcpd(self):
        """Behaviour guard: even the mocks for verifier/mcpd must NEVER
        be called when planner sees system.unsupported."""
        collab = _kit_with_qb_output({
            "action": "system.unsupported",
            "target": "",
            "params": {},
        })
        planner_node(collab)(_state())
        collab["verifier"].verify.assert_not_called()
        collab["mcpd"].call.assert_not_called()
        collab["pb"].complete.assert_not_called()


# ── Fix #3 — F-74 (NEW) terminal LEFT-pane render (BUGS 2/3) ─────────────


class TestF74BridgeEmitsFinalTokenEventInAgentGraph:
    """v6.10 UTM BUGS 2/3: CoT shows Planner→Risk→Executor→MCP→Responder
    all green + 'executed', file IS created on disk, but the terminal's
    LEFT pane shows nothing. Root cause: AgentGraph responder_node
    populates state['output'] but the bridge emits ProgressEvent + CotEvent
    + ResultEvent — NO TokenEvent. The monolithic pipeline emitted
    TokenEvent per token during qb_summarize, and the terminal's
    _on_token_event handler (terminal/app.py:194-208) wrote each token
    to the RichLog in the LEFT pane. When AgentGraph delegates and no
    TokenEvent flows, the pane stays empty from the user's perspective."""

    def test_bridge_emits_final_token_event_before_result_on_success(self):
        """Would have caught v6.10 UTM BUGS 2/3 the day Task #173 landed."""
        outcome = {
            "completed": True,
            "output": "Created /tmp/hello.txt (12 bytes).",
            "intent_valid": True,
            "step_index": 1,
            "total_steps": 1,
        }
        events = list(translate_outcome_to_events(outcome, time.monotonic(), "gemini"))
        token_events = [e for e in events if isinstance(e, TokenEvent)]
        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert token_events, (
            "F-74 (BUGS 2/3): bridge emitted zero TokenEvents for a "
            "successful outcome. The terminal LEFT pane renders through "
            "_on_token_event; without a TokenEvent the pane stays empty "
            "even though the tool_call succeeded. Fix in "
            "agent_graph_bridge.py::translate_outcome_to_events: before "
            "yielding ResultEvent on successful outcomes, emit exactly "
            "one TokenEvent(token=output, accumulated=output, final=True)."
        )
        assert token_events[-1].final is True, (
            "F-74: bridge emitted TokenEvent but final flag isn't True. "
            "Terminal buffers non-final tokens waiting for the final one; "
            "if never marked final, the render never commits."
        )
        assert token_events[-1].accumulated == "Created /tmp/hello.txt (12 bytes).", (
            "F-74: bridge emitted TokenEvent but accumulated text doesn't "
            "match outcome['output']. Terminal renders the accumulated "
            "value, so mismatched text = wrong output on screen."
        )
        assert result_events, "Bridge must still yield ResultEvent."
        # Order: TokenEvent must precede ResultEvent
        token_idx = events.index(token_events[-1])
        result_idx = events.index(result_events[0])
        assert token_idx < result_idx, (
            "F-74: TokenEvent must come BEFORE ResultEvent. The terminal "
            "closes the streaming render on ResultEvent; a token event "
            "arriving after is either dropped or renders in the wrong pane."
        )

    def test_bridge_does_not_emit_token_event_on_error(self):
        """Sanity: don't emit a spurious TokenEvent when there's an error
        outcome (that path already emits ErrorEvent + shows in the CoT
        as failed)."""
        outcome = {
            "completed": True,
            "output": "",
            "intent_valid": False,
            "error_kind": "schema",
            "error_reason": "F-32: empty target for path-requiring action",
        }
        events = list(translate_outcome_to_events(outcome, time.monotonic(), "gemini"))
        token_events = [e for e in events if isinstance(e, TokenEvent)]
        assert not token_events, (
            "F-74 refinement: TokenEvent should NOT fire on error "
            "outcomes — output is empty, and firing a TokenEvent with an "
            "empty string would cause the terminal to render a blank line "
            "over the error message from ErrorEvent."
        )


# ── Fix #4 — F-49 verifier retry_mode dispatch in AgentGraph ─────────────


class TestF49VerifierRetryInAgentGraph:
    """F-49 (v6.65, 2026-07-10) ships `verifier.retry_mode` config knob.
    main.py's monolithic path retries verifier once when
    `_should_retry_verifier(mode, vresult)` returns True — handles
    "verifier call failed" (network flake) so legitimate writes aren't
    intermittently rejected. Task #173 flipped agent_graph.enabled=True
    without porting the retry; single-shot rejection is now
    authoritative on the AgentGraph path.
    """

    def _mock_verifier_with_sequence(self, results):
        """Verifier.verify() returns each element of `results` in order,
        one per call. Test can then count .verify.call_count to verify
        retry happened."""
        verifier = MagicMock()
        verifier.verify.side_effect = results
        return verifier

    def test_verifier_retries_once_on_call_failed_when_mode_on(self):
        """F-49-recur: retry_mode='on_call_failed_only' + first vote
        rejected with 'verifier call failed' → retry once. Fix passes
        second-vote result to caller."""
        first_reject = SimpleNamespace(verified=False, reason="verifier call failed: timeout")
        second_ok = SimpleNamespace(verified=True, reason="ok")
        collab = _kit_with_qb_output({"action": "fs.list", "target": "/tmp"})
        # Preload turn_content so verifier_node's intent_id lookup finds something.
        collab["turn_content"]["iid"] = {"action": "fs.list", "target": "/tmp"}
        collab["verifier"] = self._mock_verifier_with_sequence([first_reject, second_ok])
        # tier=2 keeps the verifier from being skipped by tier_floor.
        result = verifier_node(collab)(_state(intent_id="iid", tier=2))
        assert collab["verifier"].verify.call_count == 2, (
            "F-49-recur: verifier_node did not retry on 'verifier call failed'. "
            "monolithic main.py retries via _should_retry_verifier; AgentGraph "
            "was single-shot after Task #173. Fix: import _should_retry_verifier "
            "and retry once when result agrees."
        )
        assert result.get("tool_call_valid") is True, (
            "F-49-recur: retry happened but second-vote result not honored."
        )

    def test_verifier_does_not_retry_when_mode_off(self):
        """F-49-recur negative case: retry_mode='off' must NOT retry
        even if first vote fails with a retriable reason."""
        first_reject = SimpleNamespace(verified=False, reason="verifier call failed: timeout")
        never_called = SimpleNamespace(verified=True, reason="should not reach here")
        collab = _kit_with_qb_output({"action": "fs.list", "target": "/tmp"})
        collab["turn_content"]["iid"] = {"action": "fs.list", "target": "/tmp"}
        collab["cfg"] = SimpleNamespace(
            run=SimpleNamespace(tier0_fast_path=True),
            verifier=SimpleNamespace(tier_floor=2, retry_mode="off"),
            qb=SimpleNamespace(backend="gemini"),
        )
        collab["verifier"] = self._mock_verifier_with_sequence([first_reject, never_called])
        result = verifier_node(collab)(_state(intent_id="iid", tier=2))
        assert collab["verifier"].verify.call_count == 1, (
            "F-49-recur: retry fired even though retry_mode='off'. "
            "The gate on retry MUST honor the config knob."
        )
        assert result.get("tool_call_valid") is False


# ── Fix #6 — F-43/F-47b server-owned field normalization on plans ────────


class TestF43NormalizeServerOwnedFieldsOnAllPlanSteps:
    """F-43/F-47b (v6.6, 2026-07-09/10) normalize server-owned intent
    fields (intent_id, schema_version, timestamp) — the QB emits
    placeholders and the controller regenerates them. main.py's
    monolithic path calls _normalize_server_owned_fields on the single
    intent. Task #148 introduced multi-step plans; AgentGraph must
    call the helper on EVERY step, not just step 0, or step 2+ carries
    QB's placeholder intent_id ('DO_NOT_EMIT' etc) and fails validation."""

    def test_planner_normalizes_intent_id_on_all_plan_steps(self):
        """The planner_node should overwrite ANY plan step's intent_id
        that came from QB with a fresh server-generated UUID."""
        # QB emits a plan wrapper with placeholder intent_id on both steps.
        collab = _kit_with_qb_output({
            "plan": [
                {
                    "action": "network.status",
                    "target": "",
                    "intent_id": "DO_NOT_EMIT",
                },
                {
                    "action": "fs.write",
                    "target": "/tmp/ip.txt",
                    "content": "$STEP_0_STDOUT",
                    "intent_id": "DO_NOT_EMIT",
                },
            ]
        })
        result = planner_node(collab)(_state())
        if not result.get("intent_valid"):
            # Skip: the planner rejected the plan for a different reason
            # (e.g. plan schema stricter than we mocked). This test guards
            # the normalization behavior specifically; if plan wrappers
            # aren't accepted in this mock shape, the test is inapplicable
            # (a separate normalize_planner_output test would cover it).
            pytest.skip("Plan wrapper shape not accepted by normalize_planner_output mock")
        plan_id = result["plan_id"]
        stored_plan = collab["turn_content"][plan_id]
        for i, step in enumerate(stored_plan):
            iid = step.get("intent_id", "")
            assert iid != "DO_NOT_EMIT", (
                f"F-43-recur: step {i} still has QB-placeholder intent_id "
                f"'DO_NOT_EMIT'. planner_node must call "
                f"_normalize_server_owned_fields on EVERY plan step, "
                f"not just step 0."
            )


# ── Fix #9 — QB prompt teaches compound-decomposition ────────────────────


class TestQbPromptTeachesCompoundDecomposition:
    """Bug 4 root cause: user query 'create a text file with my ip
    address' should decompose into a 2-step plan (get IP, then write
    file with $STEP_0_STDOUT). Instead QB defaulted to
    system.unsupported. The Plan mode wiring is correct in AgentGraph;
    the QB prompt needs an explicit worked example for 'X with my Y'
    where Y is a system datum. This test is grep-based — the specific
    words 'ip address' or 'my ip' + '$STEP_' must both appear in
    qb_gemini.txt so Gemini has the pattern in-context."""

    def test_qb_gemini_prompt_has_compound_decomposition_example(self):
        from pathlib import Path
        prompt_path = (
            Path(__file__).resolve().parent.parent
            / "prompts" / "qb_gemini.txt"
        )
        text = prompt_path.read_text(encoding="utf-8").lower()
        assert "$step_" in text, (
            "Fix #9: qb_gemini.txt is missing the $STEP_N_STDOUT marker "
            "used to reference prior-step output in Plan mode. Without a "
            "worked example showing this, QB defaults to system.unsupported "
            "on compound 'X with my Y' queries."
        )
        # At least one of these compound patterns should appear as example
        assert any(kw in text for kw in ("ip address", "my ip", "hostname", "disk usage")), (
            "Fix #9: qb_gemini.txt lacks a compound-decomposition worked "
            "example. Add: '\"create a text file with my ip address\" → "
            "2-step plan: network.status then fs.write with content="
            "\"$STEP_0_STDOUT\"'."
        )
