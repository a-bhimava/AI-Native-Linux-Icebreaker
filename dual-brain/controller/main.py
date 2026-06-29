"""Controller — 14-step orchestration pipeline.

Wires QB, PB, mcpd, HITL, audit, and session into a single run_turn() call.
All dependencies are injected so the class is fully testable without live servers.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator, Optional

import jsonschema

from .audit import AuditFields, AuditLog, Outcome, make_entry
from .hitl import Decision, HitlPresenter, HitlPrompt, TerminalPresenter
from .presenters import make_presenter
from .intent_schema import IntentValidationError, validate
from .intent_store import IntentStore
from .mcpd_client import JsonRpcError, McpdClient, McpdProcessError, McpdTimeoutError, ToolResult
from .risk_classifier import ClassificationResult, Tier, classify
from .tier2_review import Tier2Reviewer, get_reviewer
from .trust_store import TrustStore
from .logger import SystemLogger
from .backends.base import BrainSchemaError
from .verifier import VerifierConfig, VerifierStrategy, make_verifier

_SUMMARISE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

_EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string"},
    },
    "required": ["explanation"],
    "additionalProperties": False,
}

_MAX_MODIFY_CYCLES = 3

_PIPELINE_STEPS = [
    ("qb_intent",           "Generating intent..."),
    ("schema_validation",   "Validating schema..."),
    ("risk_classification", "Classifying risk..."),
    ("tier2_review",        "Reviewing (Tier 2)..."),
    ("trust_consult",       "Checking trust..."),
    ("hitl_gate",           "Awaiting approval..."),
    ("intent_store",        "Storing intent..."),
    ("pb_tool_call",        "Generating tool call..."),
    ("tool_validation",     "Validating tool call..."),
    ("qb_verify",           "Verifying intent..."),
    ("mcpd_dispatch",       "Executing tool..."),
    ("cow_approval",        "Awaiting COW approval..."),
    ("qb_summarize",        "Summarizing result..."),
    ("audit",               "Recording audit..."),
]

_COT_HEADINGS: dict[str, str] = {
    "qb_intent":           "Intent Generation",
    "schema_validation":   "Schema Validation",
    "risk_classification": "Risk Classification",
    "tier2_review":        "Tier-2 Review",
    "trust_consult":       "Trust Check",
    "hitl_gate":           "HITL Approval",
    "intent_store":        "Intent Store",
    "pb_tool_call":        "Tool Call Generation",
    "tool_validation":     "Tool Call Validation",
    "qb_verify":           "Intent Verification",
    "mcpd_dispatch":       "Tool Execution",
    "cow_approval":        "COW Preview",
    "qb_summarize":        "Result Summary",
    "audit":               "Audit Record",
}

_STEP_INDEX: dict[str, int] = {
    name: i for i, (name, _) in enumerate(_PIPELINE_STEPS)
}


@dataclass(frozen=True)
class TurnResult:
    success: bool
    output: str             # user-facing text (QB summary or error message)
    outcome: Outcome
    tier: int = 0
    backend: str = ""
    duration_ms: float = 0.0
    cost_usd: Optional[float] = None
    tokens_in: int = 0
    tokens_out: int = 0


class Controller:
    """Orchestrates one multi-turn conversation session.

    All collaborators are injected — no live servers required for testing.
    """

    def __init__(
        self,
        cfg: Any,
        *,
        qb_backend: Any,
        pb_backend: Any,
        mcpd_client: McpdClient,
        audit_log: AuditLog,
        store: IntentStore,
        prompt_loader: Any,
        trust_store: TrustStore | None = None,
        presenter_factory: Any = None,
    ) -> None:
        self._cfg = cfg
        self._qb = qb_backend
        self._pb = pb_backend
        self._mcpd = mcpd_client
        self._audit = audit_log
        self._store = store
        self._prompts = prompt_loader
        self._presenter_factory = presenter_factory
        self._trust_store = trust_store or (
            TrustStore() if cfg.hitl.trust_ttl_seconds > 0 else None
        )
        self._tier2: Tier2Reviewer | None = None
        if getattr(cfg, "tier2", None) and cfg.tier2.enabled:
            self._tier2 = get_reviewer(
                cfg.tier2.strategy,
                qb_backend=qb_backend,
                max_retries=cfg.tier2.max_retries,
            )
        self._schemas_dir = self._resolve_schemas_dir()
        self._intent_schema: dict = json.loads(
            (Path(__file__).parent / "schemas" / "intent.json").read_text(encoding="utf-8")
        )
        vcfg = getattr(cfg, "verifier", None) or VerifierConfig()
        self._verifier: VerifierStrategy = make_verifier(vcfg)
        self._rpa_step_events: list = []
        self._system_logger = SystemLogger("/var/log/icebreaker/system.jsonl")

    def backend_name(self) -> str:
        return self._cfg.qb.name

    def run_turn(self, user_input: str, session: Any) -> TurnResult:
        t0 = time.monotonic()
        try:
            result = self._run_turn_inner(user_input, session, t0)
        except Exception as exc:
            duration = (time.monotonic() - t0) * 1000
            try:
                self._audit.write_fields(self._make_error_fields(session, str(exc), duration))
            except Exception:
                pass
            session.touch()
            return TurnResult(
                success=False,
                output=f"Internal error: {type(exc).__name__}: {exc}",
                outcome=Outcome.BRAIN_ERROR,
                backend=session.backend,
                duration_ms=duration,
            )
        return result

    def run_turn_streaming(
        self, user_input: str, session: Any
    ) -> Generator:
        """Yield TurnEvent objects for each pipeline step.

        The existing ``run_turn()`` is unchanged (BP-2 backward compat).
        """
        from .turn_events import (
            CotEvent,
            ErrorEvent,
            GuiEvent,
            InfoEvent,
            ProgressEvent,
            ResultEvent,
            RpaEvent,
            TokenEvent,
        )

        t0 = time.monotonic()
        step_idx = 0
        total = len(_PIPELINE_STEPS)
        current_step = ""

        def _cot(
            name: str, state: str, body: str = "", **data: Any,
        ) -> CotEvent:
            return CotEvent(
                step_index=_STEP_INDEX.get(name, 0),
                step_name=name,
                step_state=state,
                heading=_COT_HEADINGS.get(name, name),
                body=body,
                data=dict(data) if data else {},
                timestamp_ms=(time.monotonic() - t0) * 1000,
            )

        def _progress(name: str) -> ProgressEvent:
            nonlocal step_idx, current_step
            for i, (sn, sl) in enumerate(_PIPELINE_STEPS):
                if sn == name:
                    step_idx = i
                    current_step = name
                    return ProgressEvent(
                        step_name=sn,
                        step_label=sl,
                        step_index=i,
                        total_steps=total,
                        elapsed_ms=(time.monotonic() - t0) * 1000,
                    )
            current_step = name
            return ProgressEvent(
                step_name=name,
                step_label=name,
                step_index=step_idx,
                total_steps=total,
                elapsed_ms=(time.monotonic() - t0) * 1000,
            )

        try:
            qb_system = self._prompts.get(f"qb_{session.backend}")
            qb_cost = 0.0
            qb_tokens_in = qb_tokens_out = 0

            # Step 1: QB → Intent Object
            yield _progress("qb_intent")
            yield _cot("qb_intent", "active",
                        body="Parsing natural language into structured intent")
            session.add_user_message(user_input)
            qb_response = self._qb.complete(
                system=qb_system, user=user_input,
                schema=self._intent_schema,
                max_retries=self._cfg.run.qb_max_retries,
            )
            raw_intent = qb_response.content_json
            session.add_assistant_message(json.dumps(raw_intent))
            qb_cost += qb_response.cost_usd or 0.0
            qb_tokens_in += qb_response.tokens_in
            qb_tokens_out += qb_response.tokens_out
            yield _cot("qb_intent", "done", body="Intent generated",
                        action=raw_intent.get("action", ""),
                        target=raw_intent.get("target", ""),
                        risk_level=raw_intent.get("risk_level", ""))

            # Step 2: Schema validation
            yield _progress("schema_validation")
            yield _cot("schema_validation", "active",
                        body="Validating intent against JSON Schema (INV-2)")
            try:
                validated = validate(raw_intent)
            except IntentValidationError as exc:
                yield _cot("schema_validation", "failed", body=str(exc))
                yield ResultEvent(
                    result=self._schema_rejected(session, str(exc), qb_response, t0)
                )
                return
            yield _cot("schema_validation", "done", body="Schema valid")

            intent = validated.intent
            raw_target = intent.get("target", "")
            if raw_target:
                intent["target_realpath"] = os.path.realpath(raw_target)
            else:
                intent["target_realpath"] = ""

            try:
                self._system_logger.log("controller", "intent_generated", {"intent": intent})
            except Exception as e:
                yield _cot("system_logger", "failed", body=f"Failed to log intent: {e}")

            # Step 3: Risk classification
            yield _progress("risk_classification")
            yield _cot("risk_classification", "active",
                        body="Classifying risk tier")
            cls_result = classify(intent)
            yield _cot("risk_classification", "done",
                        body=f"Tier {int(cls_result.tier)} — {cls_result.reason}",
                        tier=int(cls_result.tier),
                        reason=cls_result.reason,
                        reversible=cls_result.reversible)

            # Step 3b: Tier-2 review
            original_tier = cls_result.tier
            if self._tier2 and cls_result.tier == Tier.MEDIUM:
                yield _progress("tier2_review")
                yield _cot("tier2_review", "active",
                            body="Escalate-only second review")
                t2_decision = self._tier2.review(intent, cls_result)
                if t2_decision.escalate:
                    cls_result = ClassificationResult(
                        tier=Tier.HIGH, reason=t2_decision.reason,
                        reversible=cls_result.reversible,
                    )
                assert cls_result.tier >= original_tier
                yield _cot("tier2_review", "done",
                            body="Escalated to Tier 3" if t2_decision.escalate
                            else "No escalation",
                            escalated=t2_decision.escalate,
                            final_tier=int(cls_result.tier))
            else:
                yield _cot("tier2_review", "done", body="Skipped",
                            skipped=True)

            # Step 3c: Trust consult
            trust_ttl = self._cfg.hitl.trust_ttl_seconds
            cls_result_requires_hitl = cls_result.requires_hitl
            if self._trust_store and trust_ttl > 0 and cls_result.requires_hitl:
                yield _progress("trust_consult")
                yield _cot("trust_consult", "active",
                            body="Checking trust grants")
                grant = self._trust_store.is_trusted(
                    intent["action"], intent["target"],
                    cls_result.tier, session.session_id,
                )
                if grant:
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id,
                        turn_index=session.turn_index,
                        intent_id="", action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"],
                        risk_level=intent["risk_level"],
                        outcome=Outcome.TRUST_APPLIED, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=qb_tokens_in, tokens_out=qb_tokens_out,
                        cost_estimate_usd=qb_cost,
                        extra={"grant_id": grant.grant_id},
                    ))
                    cls_result_requires_hitl = False
                    yield _cot("trust_consult", "done",
                                body="Auto-approved via trust grant",
                                trusted=True,
                                grant_id=grant.grant_id)
                else:
                    yield _cot("trust_consult", "done",
                                body="No matching trust grant",
                                trusted=False)
            else:
                yield _cot("trust_consult", "done", body="Skipped",
                            skipped=True)

            # Step 4: HITL gate
            if cls_result_requires_hitl:
                yield _progress("hitl_gate")
                yield _cot("hitl_gate", "active",
                            body=f"Tier {int(cls_result.tier)} — awaiting human decision")
                modify_count = 0
                while True:
                    hitl_prompt = HitlPrompt(
                        intent, cls_result, backend=session.backend,
                        lockout_seconds=self._cfg.hitl.lockout_seconds,
                        timeout_seconds=self._cfg.hitl.timeout_seconds,
                        presenter=self._build_presenter(),
                    )
                    decision = hitl_prompt.ask()
                    hitl_extra = {
                        "decision_id": hitl_prompt.decision_id,
                        "key_pressed_class": hitl_prompt.key_pressed_class,
                        "latency_ms": hitl_prompt.decision_latency_ms,
                    }
                    if decision == Decision.EXPLAIN:
                        explanation = self._qb_explain(intent, cls_result)
                        yield InfoEvent(message=f"\n  {explanation}\n")
                        continue
                    if decision == Decision.MODIFY:
                        modify_count += 1
                        if modify_count >= _MAX_MODIFY_CYCLES:
                            yield _cot("hitl_gate", "failed",
                                        body="Modify limit reached — denied",
                                        decision="modify_limit")
                            yield InfoEvent(message="\n  Modify limit reached — operation denied.")
                            yield ResultEvent(result=self._denied(
                                session, intent, cls_result, Decision.DENIED,
                                qb_response, t0, extra=hitl_extra,
                            ))
                            return
                        yield InfoEvent(message="\n  [Modify] — not yet wired to intent revision (M5.1d)")
                        continue
                    if decision == Decision.TRUST:
                        if trust_ttl <= 0:
                            yield InfoEvent(message="\n  Trust is disabled in config (trust_ttl_seconds = 0).\n")
                            continue
                        if cls_result.tier >= Tier.HIGH:
                            yield InfoEvent(message="\n  Cannot trust Tier 3+ operations.\n")
                            continue
                        grant = self._trust_store.grant(
                            action=intent["action"],
                            target_prefix=intent["target"],
                            max_tier=cls_result.tier,
                            session_id=session.session_id,
                            ttl_seconds=trust_ttl,
                        )
                        self._audit.write_fields(AuditFields(
                            session_id=session.session_id,
                            turn_index=session.turn_index,
                            intent_id="", action=intent["action"],
                            target=intent["target"], tier=int(cls_result.tier),
                            reason=intent["reason"],
                            risk_level=intent["risk_level"],
                            outcome=Outcome.TRUST_GRANTED, duration_ms=0,
                            backend=session.backend, model=self._cfg.qb.model,
                            tokens_in=0, tokens_out=0,
                            cost_estimate_usd=0.0,
                            extra={"grant_id": grant.grant_id, "ttl_seconds": trust_ttl, **hitl_extra},
                        ))
                        yield _cot("hitl_gate", "done",
                                    body="Trust granted — approved",
                                    decision="trust")
                        break
                    if decision != Decision.APPROVED:
                        yield _cot("hitl_gate", "failed",
                                    body="Operation denied by user",
                                    decision=decision.name.lower())
                        yield ResultEvent(result=self._denied(
                            session, intent, cls_result, decision,
                            qb_response, t0, extra=hitl_extra,
                        ))
                        return
                    yield _cot("hitl_gate", "done",
                                body="Approved",
                                decision="approved")
                    break

            else:
                yield _cot("hitl_gate", "done", body="Skipped (no HITL required)",
                            skipped=True)

            # Step 5: Store intent
            yield _progress("intent_store")
            yield _cot("intent_store", "active", body="Persisting intent as opaque UUID")
            intent_id = self._store.put(intent)
            yield _cot("intent_store", "done", body="Stored",
                        intent_id=intent_id)

            # Step 6: PB → tool call
            yield _progress("pb_tool_call")
            yield _cot("pb_tool_call", "active",
                        body="Privileged Brain generating MCP tool call")
            tool_schema = self._get_tool_schema(intent["action"])
            pb_user = session.build_pb_user_turn(intent_id, intent["action"], tool_schema)
            pb_system = self._prompts.get("pb")
            try:
                pb_response = self._pb.complete(
                    system=pb_system, user=pb_user, schema=None, max_retries=1,
                )
                tool_call = pb_response.content_json
            except BrainSchemaError as exc:
                raw = exc.last_payload_excerpt.strip()
                if raw.upper().startswith("REFUSE:"):
                    reason = raw[len("REFUSE:"):].strip()
                    yield _cot("pb_tool_call", "failed",
                                body=f"PB refused: {reason}")
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id,
                        turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"],
                        risk_level=intent["risk_level"],
                        outcome=Outcome.PB_SCHEMA_ERROR, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=0,
                        cost_estimate_usd=qb_cost,
                        extra={"pb_refused": True,
                               "refusal_reason": reason},
                    ))
                    yield ResultEvent(result=TurnResult(
                        success=False,
                        output=f"Privileged Brain refused: {reason}",
                        outcome=Outcome.PB_SCHEMA_ERROR,
                        tier=int(cls_result.tier),
                        backend=session.backend, duration_ms=duration,
                        cost_usd=qb_cost if qb_cost > 0 else None,
                        tokens_in=total_tokens_in, tokens_out=0,
                    ))
                    return
                raise
            pb_cost = pb_response.cost_usd or 0.0
            total_cost = qb_cost + pb_cost
            total_tokens_in = qb_tokens_in + pb_response.tokens_in
            total_tokens_out = qb_tokens_out + pb_response.tokens_out
            yield _cot("pb_tool_call", "done",
                        body=f"Tool call: {tool_call.get('tool', '?')}",
                        tool=tool_call.get("tool", ""))

            # Step 7: Validate tool call
            yield _progress("tool_validation")
            yield _cot("tool_validation", "active",
                        body="Validating PB output against tool schema")
            try:
                self._validate_tool_call(tool_call, intent["action"])
            except ValueError as exc:
                yield _cot("tool_validation", "failed", body=str(exc))
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=Outcome.PB_SCHEMA_ERROR, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    cost_estimate_usd=total_cost,
                ))
                yield ResultEvent(result=TurnResult(
                    success=False, output=f"PB output validation failed: {exc}",
                    outcome=Outcome.PB_SCHEMA_ERROR, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                ))
                return
            yield _cot("tool_validation", "done", body="Tool call valid")

            # Step 8: QB verifier
            yield _progress("qb_verify")
            yield _cot("qb_verify", "active",
                        body="QB verifying intent↔tool-call alignment")
            verifier_system = self._prompts.get("qb_verifier")
            vresult = self._verifier.verify(intent, tool_call, self._qb, verifier_system)
            if not vresult.verified:
                yield _cot("qb_verify", "failed",
                            body=f"Rejected: {vresult.reason}",
                            reason=vresult.reason,
                            votes_cast=vresult.votes_cast,
                            verified_count=vresult.verified_count)
                duration = (time.monotonic() - t0) * 1000
                extra = {}
                if vresult.votes_cast > 1:
                    extra["verifier_votes"] = vresult.votes_cast
                    extra["verified_count"] = vresult.verified_count
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=Outcome.QB_VERIFIER_REJECTED, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    cost_estimate_usd=total_cost,
                    extra=extra if extra else None,
                ))
                yield ResultEvent(result=TurnResult(
                    success=False,
                    output=f"Verifier rejected: {vresult.reason}",
                    outcome=Outcome.QB_VERIFIER_REJECTED, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                ))
                return
            yield _cot("qb_verify", "done",
                        body="Verified",
                        votes_cast=vresult.votes_cast,
                        verified_count=vresult.verified_count)

            # Step 9: tool dispatch (mcpd, GUI Agent, or RPA Bridge)
            tool_name = tool_call.get("tool", "")
            is_gui = tool_name.startswith("gui.")
            is_rpa = tool_name.startswith("rpa.")

            yield _progress("mcpd_dispatch")
            if is_gui:
                yield _cot("mcpd_dispatch", "active",
                            body=f"Executing {tool_name} via GUI Agent",
                            tool=tool_name, execution_tier="gui")
                yield GuiEvent(
                    phase="preview",
                    action=tool_name,
                    window_title=tool_call.get("params", {}).get("window", ""),
                    element_role=tool_call.get("params", {}).get("role", ""),
                    element_name=tool_call.get("params", {}).get("name", ""),
                    predicted_outcome=f"{tool_name} on target element",
                    timestamp_ms=(time.monotonic() - t0) * 1000,
                )
                gui_cfg = getattr(self._cfg, "gui", None)
                if gui_cfg and not gui_cfg.enabled:
                    yield _cot("mcpd_dispatch", "failed",
                                body="GUI Agent disabled in config")
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id, turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"], risk_level=intent["risk_level"],
                        outcome=Outcome.GUI_DENIED, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        cost_estimate_usd=total_cost,
                        extra={"execution_tier": "gui"},
                    ))
                    yield ResultEvent(result=TurnResult(
                        success=False,
                        output="GUI automation is disabled. Enable with [gui] enabled = true.",
                        outcome=Outcome.GUI_DENIED, tier=int(cls_result.tier),
                        backend=session.backend, duration_ms=duration,
                        cost_usd=total_cost if total_cost > 0 else None,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    ))
                    return

                yield GuiEvent(
                    phase="executing",
                    action=tool_name,
                    window_title=tool_call.get("params", {}).get("window", ""),
                    element_role=tool_call.get("params", {}).get("role", ""),
                    element_name=tool_call.get("params", {}).get("name", ""),
                    timestamp_ms=(time.monotonic() - t0) * 1000,
                )

                try:
                    from gui_agent.agent import GuiAgent
                    gui = GuiAgent(scratch_dir=getattr(
                        gui_cfg, "screenshot_dir", "/tmp/icebreaker-gui"
                    ) if gui_cfg else "/tmp/icebreaker-gui")
                    gui_result = gui.handle_request(
                        tool_name, tool_call.get("params", {}),
                    )
                except Exception as exc:
                    yield _cot("mcpd_dispatch", "failed", body=str(exc))
                    yield GuiEvent(
                        phase="complete",
                        action=tool_name,
                        window_title=tool_call.get("params", {}).get("window", ""),
                        error=str(exc),
                        timestamp_ms=(time.monotonic() - t0) * 1000,
                    )
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id, turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"], risk_level=intent["risk_level"],
                        outcome=Outcome.GUI_ERROR, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        cost_estimate_usd=total_cost,
                        extra={"execution_tier": "gui", "gui_error": str(exc)},
                    ))
                    yield ResultEvent(result=TurnResult(
                        success=False, output=f"GUI error: {exc}",
                        outcome=Outcome.GUI_ERROR, tier=int(cls_result.tier),
                        backend=session.backend, duration_ms=duration,
                        cost_usd=total_cost if total_cost > 0 else None,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    ))
                    return

                gui_error = gui_result.get("error")
                gui_success = gui_result.get("success", True) if gui_error is None else False
                gui_reason = gui_result.get("reason", "")

                # ── GUI→RPA escalation check ──
                _ESCALATABLE_REASONS = frozenset({
                    "no_a11y_tree", "atspi_unavailable", "element_too_small",
                })
                rpa_cfg = getattr(self._cfg, "rpa", None)
                if (
                    not gui_success
                    and gui_reason in _ESCALATABLE_REASONS
                    and rpa_cfg
                    and rpa_cfg.enabled
                ):
                    yield _cot("mcpd_dispatch", "active",
                                body=f"GUI failed ({gui_reason}) — escalating to RPA Bridge",
                                execution_tier="rpa", gui_reason=gui_reason)

                    rpa_keywords = self._gui_action_to_rpa_keywords(
                        tool_name, tool_call.get("params", {}),
                    )
                    rpa_workflow_name = f"escalation_{tool_name}"
                    rpa_timeout = rpa_cfg.timeout_seconds

                    keyword_preview = tuple(
                        f"{kw.get('name', '?')}  {' '.join(kw.get('args', []))}"
                        for kw in rpa_keywords
                    )

                    yield RpaEvent(
                        phase="preview",
                        workflow_name=rpa_workflow_name,
                        keyword_total=len(rpa_keywords),
                        timeout_remaining_ms=rpa_timeout * 1000,
                        timestamp_ms=(time.monotonic() - t0) * 1000,
                    )

                    rpa_result = self._execute_rpa_workflow(
                        rpa_workflow_name, rpa_keywords, rpa_timeout,
                        session, intent, t0,
                    )

                    # Drain per-keyword step events collected during execution
                    for step_event in self._rpa_step_events:
                        yield step_event
                    self._rpa_step_events.clear()

                    rpa_success = rpa_result.get("success", False)
                    rpa_error = rpa_result.get("error", "")
                    rpa_timed_out = rpa_result.get("timed_out", False)

                    if rpa_timed_out:
                        rpa_outcome = Outcome.RPA_TIMEOUT
                    elif rpa_success:
                        rpa_outcome = Outcome.RPA_EXECUTED
                    elif rpa_result.get("qb_paused_at_keyword") is not None:
                        rpa_outcome = Outcome.RPA_QB_PAUSED
                    else:
                        rpa_outcome = Outcome.RPA_ERROR

                    from .audit import sanitize_gui_field
                    rpa_extra: dict[str, Any] = {
                        "execution_tier": "rpa_fallback",
                        "rpa_fallback_reason": gui_reason,
                        "rpa_keywords_executed": rpa_result.get("keywords_executed", 0),
                        "rpa_timeout_ms": rpa_timeout * 1000,
                        "rpa_elapsed_ms": rpa_result.get("elapsed_ms", 0),
                        "rpa_screenshot_hashes": rpa_result.get("screenshot_hashes", []),
                    }

                    yield RpaEvent(
                        phase="complete",
                        workflow_name=rpa_workflow_name,
                        keyword_index=rpa_result.get("keywords_executed", 0),
                        keyword_total=len(rpa_keywords),
                        error=rpa_error,
                        timestamp_ms=(time.monotonic() - t0) * 1000,
                    )

                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id, turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"], risk_level=intent["risk_level"],
                        outcome=rpa_outcome, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        cost_estimate_usd=total_cost,
                        extra=rpa_extra,
                    ))

                    from .mcpd_client import ToolResult
                    tool_result = ToolResult(result=rpa_result, request_id=0)
                    yield _cot("mcpd_dispatch", "done",
                                body=f"RPA fallback {'complete' if rpa_success else 'failed'}",
                                execution_tier="rpa_fallback",
                                requires_cow=False)
                else:
                    # Normal GUI result (success or non-escalatable error)
                    gui_outcome = Outcome.GUI_EXECUTED if gui_success else Outcome.GUI_ERROR

                    from .audit import sanitize_gui_field
                    extra_fields: dict[str, Any] = {
                        "execution_tier": "gui",
                        "element_role": sanitize_gui_field(
                            tool_call.get("params", {}).get("role", ""),
                        ),
                        "element_name": sanitize_gui_field(
                            tool_call.get("params", {}).get("name", ""),
                        ),
                        "window_title": sanitize_gui_field(
                            tool_call.get("params", {}).get("window", ""),
                        ),
                    }
                    sha = gui_result.get("sha256", "")
                    if sha:
                        extra_fields["screenshot_before_hash"] = sha

                    yield GuiEvent(
                        phase="complete",
                        action=tool_name,
                        window_title=tool_call.get("params", {}).get("window", ""),
                        element_role=tool_call.get("params", {}).get("role", ""),
                        element_name=tool_call.get("params", {}).get("name", ""),
                        screenshot_before_hash=sha,
                        error=gui_error or "",
                        timestamp_ms=(time.monotonic() - t0) * 1000,
                    )

                    from .mcpd_client import ToolResult
                    tool_result = ToolResult(
                        result=gui_result, request_id=0,
                    )
                    yield _cot("mcpd_dispatch", "done",
                                body=f"GUI execution {'complete' if gui_success else 'failed'}",
                                execution_tier="gui",
                                requires_cow=False)
            elif is_rpa:
                yield _cot("mcpd_dispatch", "active",
                            body=f"Executing {tool_name} via RPA Bridge",
                            tool=tool_name, execution_tier="rpa")

                rpa_cfg = getattr(self._cfg, "rpa", None)
                if rpa_cfg and not rpa_cfg.enabled:
                    yield _cot("mcpd_dispatch", "failed",
                                body="RPA Bridge disabled in config")
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id, turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"], risk_level=intent["risk_level"],
                        outcome=Outcome.RPA_DENIED, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        cost_estimate_usd=total_cost,
                        extra={"execution_tier": "rpa"},
                    ))
                    yield ResultEvent(result=TurnResult(
                        success=False,
                        output="RPA automation is disabled. Enable with [rpa] enabled = true.",
                        outcome=Outcome.RPA_DENIED, tier=int(cls_result.tier),
                        backend=session.backend, duration_ms=duration,
                        cost_usd=total_cost if total_cost > 0 else None,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    ))
                    return

                rpa_params = tool_call.get("params", {})
                rpa_keywords = rpa_params.get("keywords", [])
                rpa_workflow_name = rpa_params.get("workflow_name", "workflow")
                rpa_timeout = rpa_cfg.timeout_seconds if rpa_cfg else 30

                keyword_preview = tuple(
                    f"{kw.get('name', '?')}  {' '.join(kw.get('args', []))}"
                    for kw in rpa_keywords
                )

                yield RpaEvent(
                    phase="preview",
                    workflow_name=rpa_workflow_name,
                    keyword_total=len(rpa_keywords),
                    timeout_remaining_ms=rpa_timeout * 1000,
                    timestamp_ms=(time.monotonic() - t0) * 1000,
                )

                rpa_result = self._execute_rpa_workflow(
                    rpa_workflow_name, rpa_keywords, rpa_timeout,
                    session, intent, t0,
                )

                for step_event in self._rpa_step_events:
                    yield step_event
                self._rpa_step_events.clear()

                rpa_success = rpa_result.get("success", False)
                rpa_error = rpa_result.get("error", "")
                rpa_timed_out = rpa_result.get("timed_out", False)

                if rpa_timed_out:
                    rpa_outcome = Outcome.RPA_TIMEOUT
                elif rpa_success:
                    rpa_outcome = Outcome.RPA_EXECUTED
                elif rpa_result.get("qb_paused_at_keyword") is not None:
                    rpa_outcome = Outcome.RPA_QB_PAUSED
                else:
                    rpa_outcome = Outcome.RPA_ERROR

                from .audit import sanitize_gui_field
                rpa_extra: dict[str, Any] = {
                    "execution_tier": "rpa",
                    "rpa_keywords_executed": rpa_result.get("keywords_executed", 0),
                    "rpa_timeout_ms": rpa_timeout * 1000,
                    "rpa_elapsed_ms": rpa_result.get("elapsed_ms", 0),
                    "rpa_screenshot_hashes": rpa_result.get("screenshot_hashes", []),
                    "rpa_fallback_reason": sanitize_gui_field(
                        rpa_params.get("fallback_reason", "")
                    ),
                }

                yield RpaEvent(
                    phase="complete",
                    workflow_name=rpa_workflow_name,
                    keyword_index=rpa_result.get("keywords_executed", 0),
                    keyword_total=len(rpa_keywords),
                    error=rpa_error,
                    timestamp_ms=(time.monotonic() - t0) * 1000,
                )

                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=rpa_outcome, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    cost_estimate_usd=total_cost,
                    extra=rpa_extra,
                ))

                from .mcpd_client import ToolResult
                tool_result = ToolResult(result=rpa_result, request_id=0)
                yield _cot("mcpd_dispatch", "done",
                            body=f"RPA execution {'complete' if rpa_success else 'failed'}",
                            execution_tier="rpa",
                            requires_cow=False)
            else:
                yield _cot("mcpd_dispatch", "active",
                            body=f"Executing {tool_name} via mcpd",
                            tool=tool_name)
                try:
                    tool_result = self._mcpd.call(
                        tool_call["tool"], tool_call.get("params"),
                        timeout=self._cfg.run.mcpd_timeout_seconds,
                    )
                except McpdTimeoutError:
                    yield _cot("mcpd_dispatch", "failed", body="Timeout")
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id, turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"], risk_level=intent["risk_level"],
                        outcome=Outcome.TOOL_TIMEOUT, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        cost_estimate_usd=total_cost,
                    ))
                    yield ResultEvent(result=TurnResult(
                        success=False, output="mcpd timed out — operation did not complete.",
                        outcome=Outcome.TOOL_TIMEOUT, tier=int(cls_result.tier),
                        backend=session.backend, duration_ms=duration,
                        cost_usd=total_cost if total_cost > 0 else None,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    ))
                    return
                except (McpdProcessError, JsonRpcError) as exc:
                    yield _cot("mcpd_dispatch", "failed", body=str(exc))
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id, turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"], risk_level=intent["risk_level"],
                        outcome=Outcome.TOOL_ERROR, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        cost_estimate_usd=total_cost,
                    ))
                    yield ResultEvent(result=TurnResult(
                        success=False, output=f"Tool error: {exc}",
                        outcome=Outcome.TOOL_ERROR, tier=int(cls_result.tier),
                        backend=session.backend, duration_ms=duration,
                        cost_usd=total_cost if total_cost > 0 else None,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    ))
                    return
                yield _cot("mcpd_dispatch", "done", body="Execution complete",
                            requires_cow=tool_result.requires_cow_approval)

            # Step 10: COW approval
            if tool_result.requires_cow_approval:
                yield _progress("cow_approval")
                yield _cot("cow_approval", "active",
                            body="Destructive op — awaiting COW preview approval")
                cow_preview_text = ""
                if tool_result.cow_preview:
                    cow_preview_text = json.dumps(tool_result.cow_preview, indent=2)
                cow_prompt = HitlPrompt(
                    intent, cls_result, backend=session.backend,
                    cow_summary=cow_preview_text,
                    lockout_seconds=self._cfg.hitl.lockout_seconds,
                    timeout_seconds=self._cfg.hitl.timeout_seconds,
                    presenter=self._build_presenter(),
                )
                decision = cow_prompt.ask()
                if decision != Decision.APPROVED:
                    cow_extra = {
                        "decision_id": cow_prompt.decision_id,
                        "key_pressed_class": cow_prompt.key_pressed_class,
                        "latency_ms": cow_prompt.decision_latency_ms,
                    }
                    yield _cot("cow_approval", "failed",
                                body="COW preview denied",
                                decision=decision.name.lower())
                    yield ResultEvent(result=self._denied(
                        session, intent, cls_result, decision,
                        qb_response, t0, intent_id=intent_id,
                        extra_cost=pb_cost,
                        extra_tokens_in=pb_response.tokens_in,
                        extra_tokens_out=pb_response.tokens_out,
                        extra=cow_extra,
                    ))
                    return
                yield _cot("cow_approval", "done", body="COW approved")
            else:
                yield _cot("cow_approval", "done", body="Skipped (no COW required)",
                            skipped=True)

            # Step 11: QB summarisation (with optional streaming)
            yield _progress("qb_summarize")
            yield _cot("qb_summarize", "active",
                        body="QB summarizing tool output for user")
            raw_output = json.dumps(tool_result.result)
            truncated = "\n".join(
                raw_output.splitlines()[:self._cfg.session.max_tool_output_lines]
            )

            if self._cfg.session.stream_output:
                summarise_system = (
                    "Summarise the following tool output in 1-3 plain sentences "
                    "for the user. Be concise and factual. Output only JSON: "
                    '{"summary": "<text>"}'
                )
                user_msg = json.dumps(
                    {"tool_output": truncated, "action": intent.get("action"),
                     "target": intent.get("target")},
                    separators=(",", ":"),
                )
                accumulated = ""
                try:
                    for chunk, acc, is_final in self._qb.stream_complete(
                        system=summarise_system, user=user_msg,
                        schema=_SUMMARISE_SCHEMA, max_retries=1,
                    ):
                        accumulated = acc
                        if not is_final and chunk:
                            yield TokenEvent(
                                token=chunk, accumulated=acc, final=False,
                            )
                    try:
                        parsed = json.loads(accumulated)
                        summary = parsed.get("summary", accumulated[:200])
                    except (json.JSONDecodeError, AttributeError):
                        summary = accumulated[:200]
                    yield TokenEvent(token="", accumulated=summary, final=True)
                except Exception:
                    summary = truncated[:200]
            else:
                summary = self._qb_summarise(truncated, intent)

            session.add_tool_result_summary(summary)
            yield _cot("qb_summarize", "done", body="Summary ready")

            # Step 12: Audit
            yield _progress("audit")
            yield _cot("audit", "active", body="Writing audit record (INV-8)")
            duration = (time.monotonic() - t0) * 1000
            self._audit.write_fields(AuditFields(
                session_id=session.session_id, turn_index=session.turn_index,
                intent_id=intent_id, action=intent["action"],
                target=intent["target"], tier=int(cls_result.tier),
                reason=intent["reason"], risk_level=intent["risk_level"],
                outcome=Outcome.EXECUTED, duration_ms=duration,
                backend=session.backend, model=self._cfg.qb.model,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                cost_estimate_usd=total_cost,
            ))
            yield _cot("audit", "done", body="Audit recorded",
                        outcome="executed", intent_id=intent_id)

            session.touch()

            yield ResultEvent(result=TurnResult(
                success=True, output=summary, outcome=Outcome.EXECUTED,
                tier=int(cls_result.tier), backend=session.backend,
                duration_ms=duration,
                cost_usd=total_cost if total_cost > 0 else None,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
            ))

        except GeneratorExit:
            duration = (time.monotonic() - t0) * 1000
            try:
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id="", action="", target="", tier=0,
                    reason="cancelled", risk_level="unknown",
                    outcome=Outcome.CANCELLED, duration_ms=duration,
                    backend=session.backend,
                    model=getattr(self._cfg.qb, "model", ""),
                    tokens_in=0, tokens_out=0, cost_estimate_usd=0.0,
                    extra={"cancelled_at_step": current_step},
                ))
            except Exception:
                pass
            return
        except Exception as exc:
            duration = (time.monotonic() - t0) * 1000
            try:
                self._audit.write_fields(self._make_error_fields(session, str(exc), duration))
            except Exception:
                pass
            yield ErrorEvent(
                error_type=type(exc).__name__,
                message=str(exc),
                cancelled_at_step=current_step,
            )

    def _run_turn_inner(self, user_input: str, session: Any, t0: float) -> TurnResult:
        qb_system = self._prompts.get(f"qb_{session.backend}")
        qb_cost = 0.0
        qb_tokens_in = qb_tokens_out = 0

        # ── Step 1: QB → Intent Object ────────────────────────────────────────
        session.add_user_message(user_input)
        qb_response = self._qb.complete(
            system=qb_system,
            user=user_input,
            schema=self._intent_schema,
            max_retries=self._cfg.run.qb_max_retries,
        )
        raw_intent = qb_response.content_json
        session.add_assistant_message(json.dumps(raw_intent))
        qb_cost += qb_response.cost_usd or 0.0
        qb_tokens_in += qb_response.tokens_in
        qb_tokens_out += qb_response.tokens_out

        # ── Step 2: Schema validation ─────────────────────────────────────────
        try:
            validated = validate(raw_intent)
        except IntentValidationError as exc:
            return self._schema_rejected(session, str(exc), qb_response, t0)

        intent = validated.intent

        # ── Step 2b: Resolve target realpath (TOCTOU mitigation, SF-10) ──────
        raw_target = intent.get("target", "")
        if raw_target:
            intent["target_realpath"] = os.path.realpath(raw_target)
        else:
            intent["target_realpath"] = ""

        # ── Step 3: Risk classification ───────────────────────────────────────
        cls_result = classify(intent)

        # ── Step 3b: Tier-2 review (escalate-only) ───────────────────────────
        original_tier = cls_result.tier
        if self._tier2 and cls_result.tier == Tier.MEDIUM:
            t2_decision = self._tier2.review(intent, cls_result)
            if t2_decision.escalate:
                cls_result = ClassificationResult(
                    tier=Tier.HIGH, reason=t2_decision.reason,
                    reversible=cls_result.reversible,
                )
            assert cls_result.tier >= original_tier, "Tier-2 review must never downgrade"

        # ── Step 3c: Trust consult (skip HITL if trusted) ────────────────────
        trust_ttl = self._cfg.hitl.trust_ttl_seconds
        if self._trust_store and trust_ttl > 0 and cls_result.requires_hitl:
            grant = self._trust_store.is_trusted(
                intent["action"], intent["target"],
                cls_result.tier, session.session_id,
            )
            if grant:
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id="", action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=Outcome.TRUST_APPLIED, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=qb_tokens_in, tokens_out=qb_tokens_out,
                    cost_estimate_usd=qb_cost,
                    extra={"grant_id": grant.grant_id},
                ))
                remaining = max(0, int(grant.expires_at - time.monotonic()))
                print(
                    f"\n  ✓ Auto-approved via trust grant "
                    f"{grant.action} → {grant.target_prefix} "
                    f"(expires in {remaining}s, /trust revoke {grant.grant_id})\n",
                    flush=True,
                )
                # Skip HITL, fall through to Step 5
                cls_result_requires_hitl = False
            else:
                cls_result_requires_hitl = True
        else:
            cls_result_requires_hitl = cls_result.requires_hitl

        # ── Step 4: HITL gate (Tier 3 only) ──────────────────────────────────
        if cls_result_requires_hitl:
            modify_count = 0
            while True:
                hitl_prompt = HitlPrompt(
                    intent, cls_result,
                    backend=session.backend,
                    lockout_seconds=self._cfg.hitl.lockout_seconds,
                    timeout_seconds=self._cfg.hitl.timeout_seconds,
                    presenter=self._build_presenter(),
                )
                decision = hitl_prompt.ask()

                hitl_extra = {
                    "decision_id": hitl_prompt.decision_id,
                    "key_pressed_class": hitl_prompt.key_pressed_class,
                    "latency_ms": hitl_prompt.decision_latency_ms,
                }

                if decision == Decision.EXPLAIN:
                    explanation = self._qb_explain(intent, cls_result)
                    print(f"\n  {explanation}\n", flush=True)
                    continue

                if decision == Decision.MODIFY:
                    modify_count += 1
                    if modify_count >= _MAX_MODIFY_CYCLES:
                        print("\n  Modify limit reached — operation denied.", flush=True)
                        return self._denied(
                            session, intent, cls_result, Decision.DENIED,
                            qb_response, t0, extra=hitl_extra,
                        )
                    print("\n  [Modify] — not yet wired to intent revision (M5.1d)", flush=True)
                    continue

                if decision == Decision.TRUST:
                    if trust_ttl <= 0:
                        print("\n  Trust is disabled in config (trust_ttl_seconds = 0).\n", flush=True)
                        continue
                    if cls_result.tier >= Tier.HIGH:
                        print("\n  Cannot trust Tier 3+ operations.\n", flush=True)
                        continue
                    grant = self._trust_store.grant(
                        action=intent["action"],
                        target_prefix=intent["target"],
                        max_tier=cls_result.tier,
                        session_id=session.session_id,
                        ttl_seconds=trust_ttl,
                    )
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id,
                        turn_index=session.turn_index,
                        intent_id="", action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"],
                        risk_level=intent["risk_level"],
                        outcome=Outcome.TRUST_GRANTED, duration_ms=0,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=0, tokens_out=0,
                        cost_estimate_usd=0.0,
                        extra={
                            "grant_id": grant.grant_id,
                            "ttl_seconds": trust_ttl,
                            **hitl_extra,
                        },
                    ))
                    print(
                        f"\n  ✓ Trust granted for {grant.action} "
                        f"→ {grant.target_prefix} ({trust_ttl}s)\n",
                        flush=True,
                    )
                    break  # treat as approved

                if decision != Decision.APPROVED:
                    return self._denied(
                        session, intent, cls_result, decision,
                        qb_response, t0, extra=hitl_extra,
                    )
                break

        # ── Step 5: Store intent → opaque UUID ───────────────────────────────
        intent_id = self._store.put(intent)

        # ── Step 6: PB → tool call ────────────────────────────────────────────
        tool_schema = self._get_tool_schema(intent["action"])
        pb_user = session.build_pb_user_turn(intent_id, intent["action"], tool_schema)
        pb_system = self._prompts.get("pb")
        try:
            pb_response = self._pb.complete(
                system=pb_system,
                user=pb_user,
                schema=None,
                max_retries=1,
            )
            tool_call = pb_response.content_json
        except BrainSchemaError as exc:
            raw = exc.last_payload_excerpt.strip()
            if raw.upper().startswith("REFUSE:"):
                reason = raw[len("REFUSE:"):].strip()
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id,
                    turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"],
                    risk_level=intent["risk_level"],
                    outcome=Outcome.PB_SCHEMA_ERROR, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=0,
                    cost_estimate_usd=qb_cost,
                    extra={"pb_refused": True,
                           "refusal_reason": reason},
                ))
                return TurnResult(
                    success=False,
                    output=f"Privileged Brain refused: {reason}",
                    outcome=Outcome.PB_SCHEMA_ERROR,
                    tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=qb_cost if qb_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=0,
                )
            raise
        pb_cost = pb_response.cost_usd or 0.0
        total_cost = qb_cost + pb_cost
        total_tokens_in = qb_tokens_in + pb_response.tokens_in
        total_tokens_out = qb_tokens_out + pb_response.tokens_out

        # ── Step 7: Post-validate tool call ───────────────────────────────────
        try:
            self._validate_tool_call(tool_call, intent["action"])
        except ValueError as exc:
            duration = (time.monotonic() - t0) * 1000
            self._audit.write_fields(AuditFields(
                session_id=session.session_id, turn_index=session.turn_index,
                intent_id=intent_id, action=intent["action"],
                target=intent["target"], tier=int(cls_result.tier),
                reason=intent["reason"], risk_level=intent["risk_level"],
                outcome=Outcome.PB_SCHEMA_ERROR, duration_ms=duration,
                backend=session.backend, model=self._cfg.qb.model,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                cost_estimate_usd=total_cost,
            ))
            return TurnResult(
                success=False, output=f"PB output validation failed: {exc}",
                outcome=Outcome.PB_SCHEMA_ERROR, tier=int(cls_result.tier),
                backend=session.backend, duration_ms=duration,
                cost_usd=total_cost if total_cost > 0 else None,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
            )

        # ── Step 8: QB verifier round-trip ────────────────────────────────────
        verifier_system = self._prompts.get("qb_verifier")
        vresult = self._verifier.verify(intent, tool_call, self._qb, verifier_system)
        if not vresult.verified:
            duration = (time.monotonic() - t0) * 1000
            extra = {}
            if vresult.votes_cast > 1:
                extra["verifier_votes"] = vresult.votes_cast
                extra["verified_count"] = vresult.verified_count
            self._audit.write_fields(AuditFields(
                session_id=session.session_id, turn_index=session.turn_index,
                intent_id=intent_id, action=intent["action"],
                target=intent["target"], tier=int(cls_result.tier),
                reason=intent["reason"], risk_level=intent["risk_level"],
                outcome=Outcome.QB_VERIFIER_REJECTED, duration_ms=duration,
                backend=session.backend, model=self._cfg.qb.model,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                cost_estimate_usd=total_cost,
                extra=extra if extra else None,
            ))
            return TurnResult(
                success=False,
                output=f"Verifier rejected: {vresult.reason}",
                outcome=Outcome.QB_VERIFIER_REJECTED, tier=int(cls_result.tier),
                backend=session.backend, duration_ms=duration,
                cost_usd=total_cost if total_cost > 0 else None,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
            )

        # ── Step 9: Dispatch via McpdClient or GUI Agent ───────────────────
        tool_name = tool_call.get("tool", "")
        is_gui = tool_name.startswith("gui.")

        if is_gui:
            gui_cfg = getattr(self._cfg, "gui", None)
            if gui_cfg and not gui_cfg.enabled:
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=Outcome.GUI_DENIED, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    cost_estimate_usd=total_cost,
                    extra={"execution_tier": "gui"},
                ))
                return TurnResult(
                    success=False,
                    output="GUI automation is disabled. Enable with [gui] enabled = true.",
                    outcome=Outcome.GUI_DENIED, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                )
            try:
                from gui_agent.agent import GuiAgent
                gui = GuiAgent(scratch_dir=getattr(
                    gui_cfg, "screenshot_dir", "/tmp/icebreaker-gui"
                ) if gui_cfg else "/tmp/icebreaker-gui")
                gui_result = gui.handle_request(
                    tool_name, tool_call.get("params", {}),
                )
            except Exception as exc:
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=Outcome.GUI_ERROR, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    cost_estimate_usd=total_cost,
                    extra={"execution_tier": "gui", "gui_error": str(exc)},
                ))
                return TurnResult(
                    success=False, output=f"GUI error: {exc}",
                    outcome=Outcome.GUI_ERROR, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                )
            tool_result = ToolResult(result=gui_result, request_id=0)
        else:
            try:
                tool_result = self._mcpd.call(
                    tool_call["tool"],
                    tool_call.get("params"),
                    timeout=self._cfg.run.mcpd_timeout_seconds,
                )
            except McpdTimeoutError:
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=Outcome.TOOL_TIMEOUT, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    cost_estimate_usd=total_cost,
                ))
                return TurnResult(
                    success=False, output="mcpd timed out — operation did not complete.",
                    outcome=Outcome.TOOL_TIMEOUT, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                )
            except (McpdProcessError, JsonRpcError) as exc:
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=Outcome.TOOL_ERROR, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    cost_estimate_usd=total_cost,
                ))
                return TurnResult(
                    success=False, output=f"Tool error: {exc}",
                    outcome=Outcome.TOOL_ERROR, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                )

        # ── Step 10: COW approval (if mcpd requires it) ───────────────────────
        if tool_result.requires_cow_approval:
            cow_preview_text = ""
            if tool_result.cow_preview:
                cow_preview_text = json.dumps(tool_result.cow_preview, indent=2)
            cow_prompt = HitlPrompt(
                intent, cls_result,
                backend=session.backend,
                cow_summary=cow_preview_text,
                lockout_seconds=self._cfg.hitl.lockout_seconds,
                timeout_seconds=self._cfg.hitl.timeout_seconds,
                presenter=self._build_presenter(),
            )
            decision = cow_prompt.ask()
            if decision != Decision.APPROVED:
                cow_extra = {
                    "decision_id": cow_prompt.decision_id,
                    "key_pressed_class": cow_prompt.key_pressed_class,
                    "latency_ms": cow_prompt.decision_latency_ms,
                }
                return self._denied(session, intent, cls_result, decision,
                                    qb_response, t0, intent_id=intent_id,
                                    extra_cost=pb_cost,
                                    extra_tokens_in=pb_response.tokens_in,
                                    extra_tokens_out=pb_response.tokens_out,
                                    extra=cow_extra)

        # ── Step 11: QB summarisation (INV-2-extended) ───────────────────────
        raw_output = json.dumps(tool_result.result)
        truncated = "\n".join(raw_output.splitlines()[:self._cfg.session.max_tool_output_lines])
        summary = self._qb_summarise(truncated, intent)
        session.add_tool_result_summary(summary)

        # ── Step 12: Audit ────────────────────────────────────────────────────
        duration = (time.monotonic() - t0) * 1000
        self._audit.write_fields(AuditFields(
            session_id=session.session_id, turn_index=session.turn_index,
            intent_id=intent_id, action=intent["action"],
            target=intent["target"], tier=int(cls_result.tier),
            reason=intent["reason"], risk_level=intent["risk_level"],
            outcome=Outcome.EXECUTED, duration_ms=duration,
            backend=session.backend, model=self._cfg.qb.model,
            tokens_in=total_tokens_in, tokens_out=total_tokens_out,
            cost_estimate_usd=total_cost,
        ))

        # ── Step 13: touch session ────────────────────────────────────────────
        session.touch()

        return TurnResult(
            success=True, output=summary, outcome=Outcome.EXECUTED,
            tier=int(cls_result.tier), backend=session.backend,
            duration_ms=duration,
            cost_usd=total_cost if total_cost > 0 else None,
            tokens_in=total_tokens_in, tokens_out=total_tokens_out,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _qb_summarise(self, raw_output: str, intent: dict) -> str:
        summarise_system = (
            "Summarise the following tool output in 1-3 plain sentences for the user. "
            "Be concise and factual. Output only JSON: {\"summary\": \"<text>\"}"
        )
        user_msg = json.dumps(
            {"tool_output": raw_output, "action": intent.get("action"), "target": intent.get("target")},
            separators=(",", ":"),
        )
        try:
            resp = self._qb.complete(
                system=summarise_system,
                user=user_msg,
                schema=_SUMMARISE_SCHEMA,
                max_retries=1,
            )
            return resp.content_json.get("summary", raw_output[:200])
        except Exception:
            return raw_output[:200]

    def _qb_explain(self, intent: dict, cls_result: Any) -> str:
        explain_system = (
            "Explain in 2-3 plain sentences what this system action will do and "
            "any risks. Be concise and factual. Output only JSON: "
            '{"explanation": "<text>"}'
        )
        user_msg = json.dumps({
            "action": intent.get("action"),
            "target": intent.get("target"),
            "risk_level": intent.get("risk_level"),
            "tier": int(cls_result.tier),
            "reversible": cls_result.reversible,
        }, separators=(",", ":"))
        try:
            resp = self._qb.complete(
                system=explain_system,
                user=user_msg,
                schema=_EXPLAIN_SCHEMA,
                max_retries=1,
            )
            return resp.content_json.get("explanation", "No explanation available.")
        except Exception:
            return "Could not generate explanation."

    def _validate_tool_call(self, tool_call: dict, expected_action: str) -> None:
        if not isinstance(tool_call, dict):
            raise ValueError("PB output is not a JSON object")
        tool = tool_call.get("tool")
        if tool != expected_action:
            raise ValueError(
                f"PB returned tool {tool!r} but intent action is {expected_action!r}"
            )
        params = tool_call.get("params")
        if params is not None and not isinstance(params, dict):
            raise ValueError("PB params must be a JSON object or absent")
        if params is not None:
            tool_schema = self._get_tool_schema(expected_action)
            if tool_schema.get("properties"):
                try:
                    jsonschema.validate(params, tool_schema)
                except jsonschema.ValidationError as exc:
                    path = " -> ".join(
                        str(p) for p in exc.absolute_path
                    ) or "root"
                    raise ValueError(
                        f"PB params failed schema validation at "
                        f"{path}: {exc.message}"
                    ) from None

    def _get_tool_schema(self, action: str) -> dict:
        if self._schemas_dir:
            schema_path = Path(self._schemas_dir) / f"{action}.json"
            if schema_path.exists():
                try:
                    return json.loads(schema_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
        return {"type": "object"}

    def _resolve_schemas_dir(self) -> str:
        if self._cfg.run.mcpd_schemas_dir:
            return str(Path(self._cfg.run.mcpd_schemas_dir).expanduser())
        # Auto-detect: mcpd_binary is src/mcpd/target/release/mcpd → src/mcpd/schemas/
        binary = Path(self._cfg.run.mcpd_binary)
        candidate = binary.parent.parent.parent / "schemas"
        if candidate.exists():
            return str(candidate)
        distro_candidate = Path("/usr/share/icebreaker/schemas")
        if distro_candidate.exists():
            return str(distro_candidate)
        return ""

    def _build_presenter(self) -> HitlPresenter:
        if self._presenter_factory is not None:
            return self._presenter_factory()
        keymap = getattr(self._cfg, "keymap", None)
        presenter_name = getattr(self._cfg.hitl, "presenter", "terminal")
        try:
            return make_presenter(presenter_name, keymap=keymap)
        except ValueError:
            return TerminalPresenter(keymap=keymap)

    def _execute_rpa_workflow(
        self,
        workflow_name: str,
        keywords: list[dict],
        timeout_seconds: int,
        session: Any,
        intent: dict,
        t0: float,
    ) -> dict:
        """Spawn RPA Bridge subprocess, stream progress, and QB-monitor each step.

        Reads stdout line-by-line to capture ``rpa.keyword_progress``
        notifications as they arrive. After each keyword, builds a text
        state summary and sends it to QB for on-track/off-track assessment.
        If QB flags off-track, kills the subprocess and returns partial
        results with ``qb_paused_at_keyword``.
        """
        import json as _json
        import select as _select
        import subprocess
        import sys
        import threading

        from .turn_events import RpaEvent

        rpa_request = {
            "jsonrpc": "2.0",
            "method": "rpa.execute_workflow",
            "id": 1,
            "params": {
                "workflow_name": workflow_name,
                "keywords": keywords,
            },
        }

        effective_timeout = timeout_seconds + 5

        _EMPTY_RESULT: dict = {
            "success": False,
            "timed_out": False,
            "keywords_executed": 0,
            "keywords_total": len(keywords),
            "elapsed_ms": 0,
            "keyword_results": [],
            "error": "",
            "screenshot_hashes": [],
        }

        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "rpa_bridge"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._scrubbed_rpa_env(),
            )
        except (FileNotFoundError, OSError) as exc:
            return {**_EMPTY_RESULT, "error": f"Failed to start RPA Bridge: {exc}"}

        # Send request and close stdin so the Bridge knows input is complete
        try:
            proc.stdin.write((_json.dumps(rpa_request) + "\n").encode())
            proc.stdin.flush()
            proc.stdin.close()
        except OSError as exc:
            proc.kill()
            proc.wait()
            return {**_EMPTY_RESULT, "error": f"Failed to write to RPA Bridge: {exc}"}

        # SIGKILL watchdog in a background thread (defense-in-depth)
        killed_by_watchdog = threading.Event()

        def _watchdog() -> None:
            if not killed_by_watchdog.wait(effective_timeout):
                try:
                    proc.kill()
                except OSError:
                    pass
                killed_by_watchdog.set()

        watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
        watchdog_thread.start()

        rpa_result: dict = dict(_EMPTY_RESULT)
        screenshot_hashes: list[str] = []
        keyword_results: list[dict] = []
        qb_paused_at: int | None = None

        try:
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = _json.loads(line)
                except _json.JSONDecodeError:
                    continue

                # JSON-RPC notification: per-keyword progress
                if msg.get("method") == "rpa.keyword_progress":
                    p = msg.get("params", {})
                    kw_name = p.get("keyword_name", "")
                    kw_idx = p.get("keyword_index", 0)
                    kw_total = p.get("keyword_total", len(keywords))
                    kw_status = p.get("status", "")
                    elapsed = p.get("elapsed_ms", 0)
                    remaining = p.get("timeout_remaining_ms", 0)
                    sh = p.get("screenshot_hash", "")
                    if sh:
                        screenshot_hashes.append(sh)

                    keyword_results.append({
                        "index": kw_idx, "name": kw_name,
                        "status": kw_status, "elapsed_ms": elapsed,
                        "screenshot_hash": sh,
                    })

                    # QB progress monitor
                    on_track, concern = self._check_rpa_progress(
                        session, kw_name, kw_status, kw_idx, kw_total, intent,
                    )

                    # Emit step event (consumed by daemon→client→companion)
                    self._rpa_step_events.append(RpaEvent(
                        phase="step",
                        workflow_name=workflow_name,
                        keyword_index=kw_idx + 1,
                        keyword_total=kw_total,
                        current_keyword=kw_name,
                        keyword_status=kw_status,
                        timeout_remaining_ms=remaining,
                        screenshot_hash=sh,
                        qb_on_track=on_track,
                        qb_concern=concern,
                        timestamp_ms=(time.monotonic() - t0) * 1000,
                    ))

                    if not on_track:
                        qb_paused_at = kw_idx
                        self._rpa_step_events.append(RpaEvent(
                            phase="paused",
                            workflow_name=workflow_name,
                            keyword_index=kw_idx + 1,
                            keyword_total=kw_total,
                            current_keyword=kw_name,
                            qb_on_track=False,
                            qb_concern=concern,
                            timestamp_ms=(time.monotonic() - t0) * 1000,
                        ))
                        proc.kill()
                        break

                # JSON-RPC response: final result
                elif "result" in msg:
                    rpa_result.update(msg["result"])
                elif "error" in msg:
                    err = msg["error"]
                    rpa_result["error"] = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    rpa_result["success"] = False
        except Exception:
            pass

        # Wait for process to exit
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        # Cancel watchdog
        killed_by_watchdog.set()

        if killed_by_watchdog.is_set() and proc.returncode == -9:
            rpa_result["timed_out"] = True
            rpa_result["error"] = rpa_result.get("error") or "RPA Bridge process timed out (SIGKILL)"

        rpa_result["keyword_results"] = keyword_results
        rpa_result["screenshot_hashes"] = screenshot_hashes
        rpa_result["keywords_executed"] = len(keyword_results)
        if qb_paused_at is not None:
            rpa_result["qb_paused_at_keyword"] = qb_paused_at
            rpa_result["success"] = False
            rpa_result["error"] = rpa_result.get("error") or f"QB flagged off-track at keyword {qb_paused_at}"

        return rpa_result

    def _check_rpa_progress(
        self,
        session: Any,
        keyword_name: str,
        keyword_status: str,
        keyword_index: int,
        keyword_total: int,
        intent: dict,
    ) -> tuple[bool, str]:
        """Ask QB if RPA workflow is on track. Returns (on_track, concern).

        QB receives a sanitized text summary only (INV-1: no raw pixels).
        Fails safe: if QB is unreachable or returns garbage, assume on-track
        to avoid blocking the workflow on a monitoring failure.
        """
        state_summary = (
            f"Keyword {keyword_index + 1}/{keyword_total}: "
            f"{keyword_name} → {keyword_status}. "
            f"Original intent: {intent.get('action', '')} on {intent.get('target', '')}"
        )
        _PROGRESS_SCHEMA = {
            "type": "object",
            "properties": {
                "on_track": {"type": "boolean"},
                "concern": {"type": "string"},
            },
            "required": ["on_track"],
        }
        try:
            response = self._qb.complete(
                system=(
                    "You monitor RPA workflow progress. Given a keyword "
                    "execution summary, assess if the workflow is on track. "
                    'Output JSON: {"on_track": true/false, "concern": "text"}'
                ),
                user=state_summary,
                schema=_PROGRESS_SCHEMA,
                max_retries=1,
            )
            result = response.content_json
            return result.get("on_track", True), result.get("concern", "")
        except Exception:
            return True, ""

    @staticmethod
    def _gui_action_to_rpa_keywords(tool_name: str, params: dict) -> list[dict]:
        """Translate a failed GUI action into RPA Bridge keywords.

        Maps gui.click/gui.type/gui.select to their Robot Framework
        equivalents. Uses the element name as a locator since AT-SPI
        identifiers often map to accessible names that Selenium/RPA can find.
        """
        window = params.get("window", "")
        role = params.get("role", "")
        name = params.get("name", "")

        # Build a CSS-like locator from the AT-SPI identity
        if name:
            locator = f"name={name}"
        elif role:
            locator = f"role={role}"
        else:
            locator = "xpath=//body"

        keywords: list[dict] = []

        # Navigate to window if specified
        if window:
            keywords.append({
                "name": "Wait Until Page Contains Element",
                "args": [locator, "10"],
            })

        if tool_name == "gui.click":
            keywords.append({"name": "Click Element", "args": [locator]})

        elif tool_name == "gui.type":
            text = params.get("text", "")
            keywords.append({"name": "Click Element", "args": [locator]})
            keywords.append({"name": "Input Text", "args": [locator, text]})

        elif tool_name == "gui.select":
            value = params.get("value", "")
            keywords.append({
                "name": "Select From List By Value",
                "args": [locator, value],
            })

        else:
            keywords.append({"name": "Click Element", "args": [locator]})

        return keywords

    @staticmethod
    def _scrubbed_rpa_env() -> dict[str, str]:
        """Build a minimal environment for the RPA Bridge subprocess (BP-8)."""
        safe = {
            "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
            "TERM", "TZ", "TMPDIR",
            "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
            "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
        }
        return {k: v for k, v in os.environ.items() if k in safe}

    def _denied(
        self, session: Any, intent: dict, cls_result: Any, decision: Decision,
        qb_response: Any, t0: float,
        intent_id: str = "",
        extra_cost: float = 0.0, extra_tokens_in: int = 0, extra_tokens_out: int = 0,
        extra: Optional[dict] = None,
    ) -> TurnResult:
        outcome = decision.to_outcome() or Outcome.HITL_DENIED
        duration = (time.monotonic() - t0) * 1000
        cost = (qb_response.cost_usd or 0.0) + extra_cost
        self._audit.write_fields(AuditFields(
            session_id=session.session_id, turn_index=session.turn_index,
            intent_id=intent_id, action=intent["action"],
            target=intent["target"], tier=int(cls_result.tier),
            reason=intent["reason"], risk_level=intent["risk_level"],
            outcome=outcome, duration_ms=duration,
            backend=session.backend, model=self._cfg.qb.model,
            tokens_in=qb_response.tokens_in + extra_tokens_in,
            tokens_out=qb_response.tokens_out + extra_tokens_out,
            cost_estimate_usd=cost,
            extra=extra,
        ))
        return TurnResult(
            success=False, output="Operation denied.",
            outcome=outcome, tier=int(cls_result.tier),
            backend=session.backend, duration_ms=duration,
            cost_usd=cost if cost > 0 else None,
            tokens_in=qb_response.tokens_in + extra_tokens_in,
            tokens_out=qb_response.tokens_out + extra_tokens_out,
        )

    def _schema_rejected(
        self, session: Any, error: str, qb_response: Any, t0: float,
    ) -> TurnResult:
        duration = (time.monotonic() - t0) * 1000
        cost = qb_response.cost_usd or 0.0
        self._audit.write_fields(AuditFields(
            session_id=session.session_id, turn_index=session.turn_index,
            intent_id="", action="", target="", tier=0,
            reason="schema_rejected", risk_level="unknown",
            outcome=Outcome.SCHEMA_REJECTED, duration_ms=duration,
            backend=session.backend, model=self._cfg.qb.model,
            tokens_in=qb_response.tokens_in, tokens_out=qb_response.tokens_out,
            cost_estimate_usd=cost,
        ))
        return TurnResult(
            success=False, output=f"Could not parse intent: {error}",
            outcome=Outcome.SCHEMA_REJECTED, backend=session.backend,
            duration_ms=duration, cost_usd=cost if cost > 0 else None,
            tokens_in=qb_response.tokens_in, tokens_out=qb_response.tokens_out,
        )

    def _make_error_fields(self, session: Any, error: str, duration: float) -> AuditFields:
        return AuditFields(
            session_id=session.session_id, turn_index=session.turn_index,
            intent_id="", action="", target="", tier=0,
            reason=f"internal_error:{error[:80]}", risk_level="unknown",
            outcome=Outcome.BRAIN_ERROR, duration_ms=duration,
            backend=session.backend, model=getattr(self._cfg.qb, "model", ""),
            tokens_in=0, tokens_out=0, cost_estimate_usd=0.0,
        )
