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

from .audit import AuditFields, AuditLog, Outcome, make_entry
from .hitl import Decision, HitlPresenter, HitlPrompt, TerminalPresenter
from .presenters import make_presenter
from .intent_schema import IntentValidationError, validate
from .intent_store import IntentStore
from .mcpd_client import JsonRpcError, McpdClient, McpdProcessError, McpdTimeoutError
from .risk_classifier import ClassificationResult, Tier, classify
from .tier2_review import Tier2Reviewer, get_reviewer
from .trust_store import TrustStore
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
            ErrorEvent,
            InfoEvent,
            ProgressEvent,
            ResultEvent,
            TokenEvent,
        )

        t0 = time.monotonic()
        step_idx = 0
        total = len(_PIPELINE_STEPS)
        current_step = ""

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

            # Step 2: Schema validation
            yield _progress("schema_validation")
            try:
                validated = validate(raw_intent)
            except IntentValidationError as exc:
                yield ResultEvent(
                    result=self._schema_rejected(session, str(exc), qb_response, t0)
                )
                return

            intent = validated.intent
            raw_target = intent.get("target", "")
            if raw_target:
                intent["target_realpath"] = os.path.realpath(raw_target)
            else:
                intent["target_realpath"] = ""

            # Step 3: Risk classification
            yield _progress("risk_classification")
            cls_result = classify(intent)

            # Step 3b: Tier-2 review
            original_tier = cls_result.tier
            if self._tier2 and cls_result.tier == Tier.MEDIUM:
                yield _progress("tier2_review")
                t2_decision = self._tier2.review(intent, cls_result)
                if t2_decision.escalate:
                    cls_result = ClassificationResult(
                        tier=Tier.HIGH, reason=t2_decision.reason,
                        reversible=cls_result.reversible,
                    )
                assert cls_result.tier >= original_tier

            # Step 3c: Trust consult
            trust_ttl = self._cfg.hitl.trust_ttl_seconds
            cls_result_requires_hitl = cls_result.requires_hitl
            if self._trust_store and trust_ttl > 0 and cls_result.requires_hitl:
                yield _progress("trust_consult")
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

            # Step 4: HITL gate
            if cls_result_requires_hitl:
                yield _progress("hitl_gate")
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
                        break
                    if decision != Decision.APPROVED:
                        yield ResultEvent(result=self._denied(
                            session, intent, cls_result, decision,
                            qb_response, t0, extra=hitl_extra,
                        ))
                        return
                    break

            # Step 5: Store intent
            yield _progress("intent_store")
            intent_id = self._store.put(intent)

            # Step 6: PB → tool call
            yield _progress("pb_tool_call")
            tool_schema = self._get_tool_schema(intent["action"])
            pb_user = session.build_pb_user_turn(intent_id, intent["action"], tool_schema)
            pb_system = self._prompts.get("pb")
            pb_response = self._pb.complete(
                system=pb_system, user=pb_user, schema=None, max_retries=1,
            )
            tool_call = pb_response.content_json
            pb_cost = pb_response.cost_usd or 0.0
            total_cost = qb_cost + pb_cost
            total_tokens_in = qb_tokens_in + pb_response.tokens_in
            total_tokens_out = qb_tokens_out + pb_response.tokens_out

            # Step 7: Validate tool call
            yield _progress("tool_validation")
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
                yield ResultEvent(result=TurnResult(
                    success=False, output=f"PB output validation failed: {exc}",
                    outcome=Outcome.PB_SCHEMA_ERROR, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                ))
                return

            # Step 8: QB verifier
            yield _progress("qb_verify")
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
                yield ResultEvent(result=TurnResult(
                    success=False,
                    output=f"Verifier rejected: {vresult.reason}",
                    outcome=Outcome.QB_VERIFIER_REJECTED, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                ))
                return

            # Step 9: mcpd dispatch
            yield _progress("mcpd_dispatch")
            try:
                tool_result = self._mcpd.call(
                    tool_call["tool"], tool_call.get("params"),
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
                yield ResultEvent(result=TurnResult(
                    success=False, output="mcpd timed out — operation did not complete.",
                    outcome=Outcome.TOOL_TIMEOUT, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                ))
                return
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
                yield ResultEvent(result=TurnResult(
                    success=False, output=f"Tool error: {exc}",
                    outcome=Outcome.TOOL_ERROR, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                ))
                return

            # Step 10: COW approval
            if tool_result.requires_cow_approval:
                yield _progress("cow_approval")
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
                    yield ResultEvent(result=self._denied(
                        session, intent, cls_result, decision,
                        qb_response, t0, intent_id=intent_id,
                        extra_cost=pb_cost,
                        extra_tokens_in=pb_response.tokens_in,
                        extra_tokens_out=pb_response.tokens_out,
                        extra=cow_extra,
                    ))
                    return

            # Step 11: QB summarisation (with optional streaming)
            yield _progress("qb_summarize")
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

            # Step 12: Audit
            yield _progress("audit")
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
        pb_response = self._pb.complete(
            system=pb_system,
            user=pb_user,
            schema=None,
            max_retries=1,
        )
        tool_call = pb_response.content_json
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

        # ── Step 9: Dispatch via McpdClient ──────────────────────────────────
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
