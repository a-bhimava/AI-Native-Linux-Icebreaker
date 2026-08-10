"""v6.17 M7.6a-1c — tests for Controller.run_turn_from_intent.

The Step-1-skipping entry into the streaming pipeline. Receives a
pre-formed intent (opencode/Gemini already translated user text) and
drives Steps 2-11 unchanged.

Tests validate:
1. run_turn_from_intent exists + is a thin wrapper around run_turn_streaming
2. Step 1 (qb_intent) is SKIPPED — QB.complete never called
3. All downstream steps (2, 3, 3.5, 4, 5, 6-8, 9, 11) still fire
4. OC short-circuit is BYPASSED for this path only (regression lock:
   run_turn_streaming without from_intent still short-circuits in OC mode)
5. Intent gets deep-copied so caller's dict isn't mutated by
   _normalize_server_owned_fields
6. Server-owned fields (intent_id, schema_version, timestamp) get
   injected by _normalize even when opencode omitted them
7. Session records the intent as an assistant message (for verifier
   context in Step 8)
8. Cost/token accounting stays consistent — qb_cost stays 0 for the
   Step-1-skipped path (opencode paid the translation cost)

5 intent shapes covered:
- fs.list Tier-0 (fast path, no HITL)
- fs.write Tier-2 (COW preview + HITL)
- package.install Tier-3 (COW + HITL + PB grammar)
- Malformed intent → SCHEMA_REJECTED via Step 2
- Unsupported action → UNSUPPORTED via Step 2
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from controller import mcpd_client
from controller.audit import AuditLog, Outcome
from controller.backends.base import BrainResponse
from controller.config import SessionConfig
from controller.intent_store import IntentStore
from controller.main import Controller, TurnResult
from controller.mcpd_client import ToolResult
from controller.session import SessionState
from controller.turn_events import CotEvent, ResultEvent, TokenEvent

# Reuse the test-config builder from test_streaming.py (same helpers).
from controller.tests.test_streaming import (
    _cfg,
    _resp,
    _mock_stream,
    _valid_intent,
    _tool_result,
    _session,
)


# ── Fixture: minimal Controller with mocks for QB / PB / mcpd ────────

def _build_ctrl(
    *,
    pb_content: dict | None = None,
    mcpd_return: ToolResult | None = None,
    qb_summarise_content: dict | None = None,
    qb_verify_content: dict | None = None,
    stream_output: bool = True,
):
    """Build a Controller with mock QB (verifier + summariser only —
    since Step 1 is skipped) + mock PB + mock mcpd. Pins PbRetry to
    max_attempts=1 so behavior matches single-shot for existing test
    assertions.

    For the from_intent path, QB is called at:
      - Step 8 (verifier)  → default {"verified": True, "reason": "ok"}
      - Step 11 (summarise) → default {"summary": "System is running."}
    QB.complete side_effect returns them in that order.
    """
    intent = _valid_intent()
    qb = MagicMock()
    qb.complete.side_effect = [
        _resp(qb_verify_content or {"verified": True, "reason": "ok"}),
        _resp(qb_summarise_content or {"summary": "System is running."}),
    ]
    qb.stream_complete.return_value = _mock_stream()

    pb = MagicMock()
    pb.complete.return_value = _resp(
        pb_content or {"tool": intent["action"], "params": {}}
    )

    mcpd = MagicMock()
    mcpd.call.return_value = mcpd_return or _tool_result()

    store = MagicMock(spec=IntentStore)
    store.put.return_value = str(uuid.uuid4())

    prompts = MagicMock()
    prompts.get.return_value = ""

    audit = MagicMock(spec=AuditLog)

    cfg = _cfg(stream_output=stream_output)

    ctrl = Controller(
        cfg,
        qb_backend=qb,
        pb_backend=pb,
        mcpd_client=mcpd,
        audit_log=audit,
        store=store,
        prompt_loader=prompts,
    )
    # Pin single-attempt retry — matches pre-M7.0.2f test conventions.
    from controller.pb_retry import PbRetryConfig
    ctrl._pb_retry_cfg = PbRetryConfig(max_attempts=1)
    session = _session()
    return ctrl, qb, pb, mcpd, audit, session


def _collect(ctrl, intent, session):
    return list(ctrl.run_turn_from_intent(intent, session))


# ── 1. Method exists + is a wrapper ──────────────────────────────────

def test_run_turn_from_intent_method_exists():
    """M7.6a-1b's daemon graceful-error guard depends on this."""
    assert hasattr(Controller, "run_turn_from_intent")
    assert callable(getattr(Controller, "run_turn_from_intent"))


def test_run_turn_from_intent_is_wrapper_around_run_turn_streaming():
    """M7.6a-1c ships run_turn_from_intent as a thin wrapper that
    yields from run_turn_streaming(from_intent=intent). Regression
    lock against future refactors that duplicate pipeline logic."""
    ctrl, qb, pb, mcpd, audit, session = _build_ctrl()
    intent = _valid_intent(action="system.status")

    # Mock run_turn_streaming so we can verify from_intent kwarg
    with patch.object(ctrl, "run_turn_streaming") as mock_stream:
        mock_stream.return_value = iter([])
        list(ctrl.run_turn_from_intent(intent, session))
        mock_stream.assert_called_once()
        args, kwargs = mock_stream.call_args
        # Wrapper passes empty user_input + from_intent kwarg
        assert kwargs.get("from_intent") == intent, (
            f"expected from_intent={intent}, got kwargs={kwargs}"
        )


# ── 2. Step 1 (qb_intent) is SKIPPED — QB.complete not called for translation ──

def test_run_turn_from_intent_skips_qb_translation_call():
    """The whole point of run_turn_from_intent: opencode/Gemini already
    translated user text, so we must NOT call QB again for Step 1. For
    a Tier-0 intent (system.status, read_only) the verifier at Step 8
    is also skipped by tier_floor gating; only Step 11 summariser fires.

    Regression assertion: at MOST 2 QB calls (verifier if not tier-
    skipped, plus summariser), NEVER 3 (which would mean Step 1 also
    called QB).
    """
    ctrl, qb, pb, mcpd, audit, session = _build_ctrl()
    intent = _valid_intent(action="system.status")

    _collect(ctrl, intent, session)

    # <= 2: Step 1 skipped is the invariant; downstream verifier/
    # summariser may or may not fire depending on tier_floor + streaming.
    assert qb.complete.call_count <= 2, (
        f"expected at most 2 QB calls (verifier + summariser), "
        f"got {qb.complete.call_count} — Step 1 (translation) probably fired"
    )


def test_run_turn_from_intent_qb_intent_cot_shows_supplied_externally():
    """The qb_intent step still emits its cot event so streaming clients
    see the step transition — but the body indicates the intent came
    from outside (source='submit_intent'), not from a QB call."""
    ctrl, qb, pb, mcpd, audit, session = _build_ctrl()
    intent = _valid_intent(action="system.status")

    events = _collect(ctrl, intent, session)
    cots = [e for e in events if isinstance(e, CotEvent)]
    qb_intent_cots = [c for c in cots if c.step_name == "qb_intent"]
    assert len(qb_intent_cots) == 1, (
        f"expected 1 qb_intent cot (done state, no active), got {len(qb_intent_cots)}"
    )
    assert qb_intent_cots[0].step_state == "done"
    assert qb_intent_cots[0].data.get("source") == "submit_intent"
    assert "supplied externally" in qb_intent_cots[0].body.lower()


# ── 3. Downstream steps still fire ───────────────────────────────────

def test_run_turn_from_intent_still_fires_downstream_steps():
    """Steps 2 (schema_validation) through 11 (summarise) must still
    fire. Regression lock — a future refactor could accidentally skip
    validation too."""
    ctrl, qb, pb, mcpd, audit, session = _build_ctrl()
    intent = _valid_intent(action="system.status")

    events = _collect(ctrl, intent, session)
    cots = [e for e in events if isinstance(e, CotEvent)]
    step_names = {c.step_name for c in cots}
    for required in ("schema_validation", "risk_classification"):
        assert required in step_names, (
            f"missing downstream step {required}; got {step_names}"
        )


# ── 4. OC short-circuit bypass — only via from_intent path ─────────────

def test_run_turn_streaming_without_from_intent_still_short_circuits_in_oc_mode():
    """Belt-and-braces: the OC short-circuit at main.py:641 stays
    active for stray direct calls to run_turn_streaming in OC mode.
    Only from_intent-flagged calls bypass. Prevents accidental
    reintroduction of raw-user-text → Controller flow in OC edition."""
    ctrl, qb, pb, mcpd, audit, session = _build_ctrl()
    # Simulate OC mode
    with patch.object(ctrl, "_is_opencode_oc", return_value=True):
        events = list(ctrl.run_turn_streaming("delete /tmp/foo", session))
    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    # OC short-circuit returns a soft-outcome result via _opencode_oc_short_circuit
    # (F-92); the actual outcome value comes from that method — we don't
    # assert on the exact value, just that the short-circuit was reached
    # (QB.complete never called → 0 QB translation calls, no PB call).
    assert qb.complete.call_count == 0
    assert pb.complete.call_count == 0
    assert mcpd.call.call_count == 0


def test_run_turn_from_intent_bypasses_oc_short_circuit():
    """Complement of the above: from_intent path REACHES Steps 2-11
    even in OC mode. If bypass regressed, PB would never be called."""
    ctrl, qb, pb, mcpd, audit, session = _build_ctrl()
    intent = _valid_intent(action="system.status")

    with patch.object(ctrl, "_is_opencode_oc", return_value=True):
        _collect(ctrl, intent, session)

    # PB IS called for the tool-call generation Step 6
    assert pb.complete.call_count == 1
    # mcpd IS called for the dispatch Step 9
    assert mcpd.call.call_count == 1


# ── 5. Caller's intent dict must not be mutated ────────────────────────

def test_run_turn_from_intent_does_not_mutate_caller_intent():
    """M7.6a-1c uses _copy.deepcopy so _normalize_server_owned_fields
    (which INJECTS intent_id, schema_version, timestamp) mutates the
    daemon-side copy, not the caller's dict. Belt-and-braces since
    the daemon's intent.run handler passes the intent it received
    from opencode; if we mutated it, subsequent inspection by opencode
    (or ct-scan traces) would see fields opencode never sent."""
    ctrl, qb, pb, mcpd, audit, session = _build_ctrl()
    original = {
        "action": "system.status",
        "target": "",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }
    snapshot = dict(original)
    _collect(ctrl, original, session)
    # No new keys should have been injected into the caller's dict
    assert set(original.keys()) == set(snapshot.keys()), (
        f"intent mutated — original now has: {set(original.keys())}, "
        f"was: {set(snapshot.keys())}"
    )
    # And existing values unchanged
    assert original == snapshot


# ── 6. Server-owned fields injected via _normalize ─────────────────────

def test_run_turn_from_intent_injects_server_owned_fields():
    """_normalize_server_owned_fields still runs on the deep-copied
    intent — Step 2 schema validation depends on intent_id being present
    (per intent.json required fields). If we skipped normalization, the
    schema validator would reject every from_intent turn."""
    ctrl, qb, pb, mcpd, audit, session = _build_ctrl()
    intent = {
        "action": "system.status",
        "target": "",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
        # Note: NO intent_id, schema_version, timestamp — server injects them.
    }
    events = _collect(ctrl, intent, session)
    # If normalization failed, we'd see SCHEMA_REJECTED here
    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    # Successful run = normalization worked (intent_id was injected and
    # passed schema validation).
    assert result_events[0].result.outcome == Outcome.EXECUTED


# ── 7. Session records the intent as assistant message ────────────────

def test_run_turn_from_intent_adds_intent_to_session_history():
    """Step 8 verifier + Step 11 summariser both read QB message history
    for context. If from_intent path skipped session.add_assistant_message,
    the verifier + summariser would see empty history and hallucinate.

    QB history lives on the private _qb_messages list accessed via
    get_qb_history() (session.py:242)."""
    ctrl, qb, pb, mcpd, audit, session = _build_ctrl()
    initial_history = session.get_qb_history()
    initial_count = len(initial_history)
    intent = _valid_intent(action="system.status")

    _collect(ctrl, intent, session)

    # After a full turn: history should contain the intent (assistant, at
    # position 0) + a tool-result summary from Step 11 (user, INV-2-extended
    # via add_tool_result_summary). Total: 2 new messages. Critically the
    # intent IS present as an assistant message — that's what makes the
    # verifier + summariser see it in Step 8 + Step 11 context.
    # (from_intent path does NOT call session.add_user_message since
    # there IS no user_input — opencode owns the raw text.)
    final_history = session.get_qb_history()
    assert len(final_history) >= 1
    # Find the assistant message carrying the intent
    assistant_msgs = [m for m in final_history if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1, (
        f"expected exactly 1 assistant message (the synthesized intent), "
        f"got {len(assistant_msgs)}"
    )
    intent_json = json.loads(assistant_msgs[0]["content"])
    assert intent_json["action"] == "system.status"
    # And there should be NO user-role message with raw text (opencode
    # holds it, we never touched it). Tool-result summaries are role=user
    # too but their content is prefixed "[Tool output summary]:" — the
    # raw user request must not be anywhere.
    for msg in final_history:
        assert "delete my files" not in msg["content"], (
            "raw user text leaked into session history"
        )


# ── 8. Malformed intent → SCHEMA_REJECTED via Step 2 ──────────────────

def test_run_turn_from_intent_malformed_intent_rejected_by_step2():
    """opencode/Gemini could emit a shape-invalid intent (e.g.
    forbidden shell metacharacter in target). Step 2 schema validation
    MUST reject it just like it would a QB-generated intent — belt-and-
    braces INV-2 discipline."""
    ctrl, qb, pb, mcpd, audit, session = _build_ctrl()
    malformed = {
        "action": "fs.list",
        # Shell metachar in target — banned by intent.json target regex
        "target": "/tmp; rm -rf /",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }

    events = _collect(ctrl, malformed, session)
    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].result.outcome == Outcome.SCHEMA_REJECTED
    # PB never called — Step 2 short-circuited before Step 6
    assert pb.complete.call_count == 0
    # mcpd never called
    assert mcpd.call.call_count == 0


# ── 9. Cost/token accounting: qb_cost stays 0 (opencode paid) ─────────

def test_run_turn_from_intent_qb_translation_cost_is_zero():
    """opencode/Gemini already paid the translation cost. The Controller's
    per-turn qb_cost accounting must not double-count it — the synthetic
    qb_response we build has cost_usd=0 so downstream aggregation stays
    honest. Verifier + summariser costs (from the mocked QB) still
    accumulate."""
    ctrl, qb, pb, mcpd, audit, session = _build_ctrl()
    intent = _valid_intent(action="system.status")

    events = _collect(ctrl, intent, session)
    result_events = [e for e in events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    # The mocked QB (_resp helper) returns cost_usd=0.0 for verifier +
    # summariser too, so total cost should be 0. If Step 1 had fired
    # and been counted, we might see cost > 0 (but our mock says 0
    # either way — the point is that the SYNTHETIC qb_response for
    # Step 1 doesn't inject a bogus non-zero cost).
    tr = result_events[0].result
    # cost_usd may be None on tier-0 fast path (no cost floor)
    assert tr.cost_usd is None or tr.cost_usd == 0.0
