"""Controller — 14-step orchestration pipeline.

Wires QB, PB, mcpd, HITL, audit, and session into a single run_turn() call.
All dependencies are injected so the class is fully testable without live servers.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .audit import AuditFields, AuditLog, Outcome, make_entry
from .hitl import Decision, HitlPresenter, HitlPrompt
from .intent_schema import IntentValidationError, validate
from .intent_store import IntentStore
from .mcpd_client import JsonRpcError, McpdClient, McpdProcessError, McpdTimeoutError
from .risk_classifier import classify

_SUMMARISE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verified": {"type": "boolean"},
        "reason":   {"type": "string"},
    },
    "required": ["verified", "reason"],
    "additionalProperties": False,
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
    ) -> None:
        self._cfg = cfg
        self._qb = qb_backend
        self._pb = pb_backend
        self._mcpd = mcpd_client
        self._audit = audit_log
        self._store = store
        self._prompts = prompt_loader
        self._schemas_dir = self._resolve_schemas_dir()

    def backend_name(self) -> str:
        return self._cfg.qb.name

    def run_turn(self, user_input: str, session: Any) -> TurnResult:
        t0 = time.monotonic()
        try:
            return self._run_turn_inner(user_input, session, t0)
        except Exception as exc:
            duration = (time.monotonic() - t0) * 1000
            try:
                self._audit.write_fields(self._make_error_fields(session, str(exc), duration))
            except Exception:
                pass
            return TurnResult(
                success=False,
                output=f"Internal error: {type(exc).__name__}: {exc}",
                outcome=Outcome.BRAIN_ERROR,
                backend=session.backend,
                duration_ms=duration,
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
            schema=None,
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

        # ── Step 3: Risk classification ───────────────────────────────────────
        cls_result = classify(intent)

        # ── Step 4: HITL gate (Tier 3 only) ──────────────────────────────────
        if cls_result.requires_hitl:
            decision = HitlPrompt(
                intent, cls_result,
                backend=session.backend,
                lockout_seconds=self._cfg.hitl.lockout_seconds,
                timeout_seconds=self._cfg.hitl.timeout_seconds,
                presenter=self._build_presenter(),
            ).ask()
            if decision != Decision.APPROVED:
                return self._denied(session, intent, cls_result, decision, qb_response, t0)

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
                intent_id=intent["intent_id"], action=intent["action"],
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
        verification = self._qb_verify(intent, tool_call)
        if not verification.get("verified", False):
            duration = (time.monotonic() - t0) * 1000
            self._audit.write_fields(AuditFields(
                session_id=session.session_id, turn_index=session.turn_index,
                intent_id=intent["intent_id"], action=intent["action"],
                target=intent["target"], tier=int(cls_result.tier),
                reason=intent["reason"], risk_level=intent["risk_level"],
                outcome=Outcome.QB_VERIFIER_REJECTED, duration_ms=duration,
                backend=session.backend, model=self._cfg.qb.model,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                cost_estimate_usd=total_cost,
            ))
            return TurnResult(
                success=False,
                output=f"Verifier rejected: {verification.get('reason', 'mismatch')}",
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
                intent_id=intent["intent_id"], action=intent["action"],
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
                intent_id=intent["intent_id"], action=intent["action"],
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
            decision = HitlPrompt(
                intent, cls_result,
                backend=session.backend,
                cow_summary=cow_preview_text,
                lockout_seconds=self._cfg.hitl.lockout_seconds,
                timeout_seconds=self._cfg.hitl.timeout_seconds,
                presenter=self._build_presenter(),
            ).ask()
            if decision != Decision.APPROVED:
                return self._denied(session, intent, cls_result, decision,
                                    qb_response, t0, extra_cost=pb_cost,
                                    extra_tokens_in=pb_response.tokens_in,
                                    extra_tokens_out=pb_response.tokens_out)

        # ── Step 11: QB summarisation (INV-2-extended) ───────────────────────
        raw_output = json.dumps(tool_result.result)
        truncated = "\n".join(raw_output.splitlines()[:self._cfg.session.max_tool_output_lines])
        summary = self._qb_summarise(truncated, intent)
        session.add_tool_result_summary(summary)

        # ── Step 12: Audit ────────────────────────────────────────────────────
        duration = (time.monotonic() - t0) * 1000
        self._audit.write_fields(AuditFields(
            session_id=session.session_id, turn_index=session.turn_index,
            intent_id=intent["intent_id"], action=intent["action"],
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

    def _qb_verify(self, intent: dict, tool_call: dict) -> dict:
        verifier_system = self._prompts.get("qb_verifier")
        user_msg = json.dumps(
            {"intent": intent, "tool_call": tool_call},
            separators=(",", ":"),
        )
        try:
            resp = self._qb.complete(
                system=verifier_system,
                user=user_msg,
                schema=_VERIFY_SCHEMA,
                max_retries=1,
            )
            return resp.content_json
        except Exception:
            return {"verified": False, "reason": "verifier call failed"}

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
        return ""

    def _build_presenter(self) -> Optional[HitlPresenter]:
        return None  # TerminalPresenter is the default in HitlPrompt

    def _denied(
        self, session: Any, intent: dict, cls_result: Any, decision: Decision,
        qb_response: Any, t0: float,
        extra_cost: float = 0.0, extra_tokens_in: int = 0, extra_tokens_out: int = 0,
    ) -> TurnResult:
        outcome = decision.to_outcome() or Outcome.HITL_DENIED
        duration = (time.monotonic() - t0) * 1000
        cost = (qb_response.cost_usd or 0.0) + extra_cost
        self._audit.write_fields(AuditFields(
            session_id=session.session_id, turn_index=session.turn_index,
            intent_id=intent["intent_id"], action=intent["action"],
            target=intent["target"], tier=int(cls_result.tier),
            reason=intent["reason"], risk_level=intent["risk_level"],
            outcome=outcome, duration_ms=duration,
            backend=session.backend, model=self._cfg.qb.model,
            tokens_in=qb_response.tokens_in + extra_tokens_in,
            tokens_out=qb_response.tokens_out + extra_tokens_out,
            cost_estimate_usd=cost,
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
