"""M7.0.2d (v6.16, 2026-08-06) — bounded PB retry loop with QB-consult rescue.

Wraps the Steps 6-8 chain (`_pb.complete` → `_validate_tool_call` →
`verifier.verify`) in a bounded retry loop. On attempt ≥
`consult_qb_after_attempt`, calls a caller-supplied `qb_repair_fn` to
generate a repair hint that lands in the F-41 `pb_hint` field for the
next attempt. If all attempts fail (or the per-turn cost ceiling is
breached), returns a `PbRetryResult` with `success=False` and a
descriptive `final_reason` — caller emits `Outcome.PB_RETRY_EXHAUSTED`.

Design mirrors:
- `Tier2Reviewer` bounded-attempt loop at `tier2_review.py:64-99` —
  fail-safe terminal branch, `log.warning` per attempt, never re-raises
  into a user-visible crash.
- `VisionGrounder` cost-ceiling pre-check at `gui_agent/vision.py:277-282`
  — typed exception, `est_cost` peek before spending.
- `VerifierConfig.retry_mode` enum at `verifier.py:24-46` — string enum
  with documented values so operators can flip behavior per deployment.

Kept out (explicitly non-goals):
- No changes to PB backend itself — `LlamaCppLocalBackend.complete()` is
  called once per attempt with the same intent.
- No BackendConfig.qb_max_retries changes — that's the intra-call
  schema-retry cap the base backend already owns.
- No mcpd changes — the retry loop sits entirely in the controller.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .verifier import VerifierResult

log = logging.getLogger(__name__)


# ── Config ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PbRetryConfig:
    """Runtime knobs for `PbRetryLoop`.

    `max_attempts`: total attempts including the first. 1 = no retry
    (pre-M7.0.2 behaviour); 3 = default shipping value.

    `consult_qb_after_attempt`: 1-indexed threshold. After attempt N
    fails, if N >= this value, QB is consulted for a repair hint that
    lands in the NEXT attempt's pb_hint. Default 1 = QB coaches from
    attempt 2 onward (attempt 1 uses the intent's original pb_hint).
    Set to a value > max_attempts to disable QB-consult entirely.

    `cost_ceiling_usd_per_turn`: rescue budget. Sum of all PB call
    costs + QB-consult costs during a single turn. Pre-checked
    before each attempt N+1; on breach returns `final_reason=
    "cost_exceeded"` without spending. Matches BP-10 discipline
    (mirror of `GuiVisionConfig.cost_ceiling_usd_per_turn`).

    `retry_mode`:
      - `off`               — no retry (max_attempts effectively 1)
      - `on_verifier_fail`  — retry only on verifier verdict=false
      - `on_any_rejection`  — retry on verifier OR provider errors
                              (default; broadest recovery)
    """
    max_attempts: int = 3
    consult_qb_after_attempt: int = 1
    cost_ceiling_usd_per_turn: float = 0.005
    retry_mode: str = "on_any_rejection"


# ── Result envelope ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PbAttemptRecord:
    """One attempt's outcome. Threaded into audit + Control Center
    Errors page for BP-13 aggregate visibility."""
    attempt: int
    tool_call: Optional[dict]
    verifier_result: Optional[VerifierResult]
    error: Optional[str]  # exception class + message, or None
    cost_usd: float
    tokens_in: int
    tokens_out: int
    latency_ms: float
    pb_hint_used: str  # first 200 chars; empty on attempt 1 unless intent had one


@dataclass(frozen=True)
class PbRetryResult:
    """Aggregate across all attempts. Caller reads `success` +
    (if True) `final_tool_call` + sums into the audit's tokens/cost."""
    success: bool
    final_tool_call: Optional[dict]
    attempts: list[PbAttemptRecord]
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    # "success" | "cost_exceeded" | "max_attempts_exhausted" |
    # "provider_terminal_error" | "retry_disabled"
    final_reason: str
    # For audit / debug: repair hints QB emitted across attempts.
    repair_hints: list[str] = field(default_factory=list)


# ── Loop ────────────────────────────────────────────────────────────────────

class PbRetryLoop:
    """Bounded retry wrapper around PB tool-call generation.

    Callers pass three collaborators:
    - `pb_complete_fn(intent, pb_hint) -> PbCallResult` — invokes PB
      with the current pb_hint; returns tool_call + cost/token
      metadata. Caller (main.py / agent_graph_nodes) owns the
      session.build_pb_user_turn call + PB backend invocation so
      this class stays framework-agnostic (usable from both the
      streaming path and AgentGraph without duplication).
    - `verify_fn(intent, tool_call) -> VerifierResult` — the verifier
      vote for the current attempt. Same signature the caller uses
      today; no wrapping needed.
    - `qb_repair_fn(intent, failed_call, verifier_reason,
      prior_attempts) -> str` — QB-consult that returns a ≤200-char
      pb_hint for the NEXT attempt. On any failure inside qb_repair_fn,
      the caller returns a stub string like "[qb-repair-failed: X]" so
      the loop keeps going with degraded coaching rather than crashing.
    """

    def __init__(
        self,
        cfg: PbRetryConfig,
        pb_complete_fn: Callable[[dict, str], "PbCallResult"],
        verify_fn: Callable[[dict, dict], VerifierResult],
        qb_repair_fn: Callable[[dict, Optional[dict], str, list[PbAttemptRecord]], str],
        logger: Any = None,
    ) -> None:
        if cfg.max_attempts < 1:
            raise ValueError(
                f"PbRetryConfig.max_attempts must be >= 1, got {cfg.max_attempts}"
            )
        if cfg.consult_qb_after_attempt < 1:
            raise ValueError(
                "consult_qb_after_attempt must be >= 1 "
                f"(set >999 to disable QB-consult), got {cfg.consult_qb_after_attempt}"
            )
        if cfg.retry_mode not in ("off", "on_verifier_fail", "on_any_rejection"):
            raise ValueError(f"unknown retry_mode {cfg.retry_mode!r}")
        self.cfg = cfg
        self._pb_complete = pb_complete_fn
        self._verify = verify_fn
        self._qb_repair = qb_repair_fn
        # Optional structured-logger sink (SystemLogger). Falls back to stdlib
        # logging when not provided (unit-test friendliness).
        self._system_logger = logger

    # ── Public entry point ─────────────────────────────────────────────

    def run(self, intent: dict) -> PbRetryResult:
        """Execute up to `max_attempts` PB calls; return the aggregate."""
        # `off` mode: single-attempt behaviour — never retry, never consult.
        effective_max = 1 if self.cfg.retry_mode == "off" else self.cfg.max_attempts

        attempts: list[PbAttemptRecord] = []
        repair_hints: list[str] = []
        current_pb_hint = str(intent.get("pb_hint", "") or "")
        total_cost = 0.0
        total_tokens_in = 0
        total_tokens_out = 0

        for attempt in range(1, effective_max + 1):
            # ── Cost ceiling pre-check (BP-10) ─────────────────────────
            # Peek the accumulated cost BEFORE spending on attempt N. On
            # breach return immediately without a new PB call; caller
            # audits the exhausted turn cleanly.
            if total_cost >= self.cfg.cost_ceiling_usd_per_turn > 0:
                self._log(
                    "warning",
                    f"pb_retry cost ceiling reached at attempt {attempt}: "
                    f"total_cost=${total_cost:.4f} >= "
                    f"ceiling=${self.cfg.cost_ceiling_usd_per_turn:.4f}",
                )
                return PbRetryResult(
                    success=False,
                    final_tool_call=None,
                    attempts=attempts,
                    total_cost_usd=total_cost,
                    total_tokens_in=total_tokens_in,
                    total_tokens_out=total_tokens_out,
                    final_reason="cost_exceeded",
                    repair_hints=repair_hints,
                )

            # ── PB call (single attempt) ───────────────────────────────
            t0 = time.monotonic()
            try:
                pb_result = self._pb_complete(intent, current_pb_hint)
            except Exception as exc:  # noqa: BLE001 — record + maybe retry
                # Provider / schema / transport error surfaces here.
                latency_ms = (time.monotonic() - t0) * 1000
                self._log(
                    "warning",
                    f"pb_retry attempt {attempt}/{effective_max} raised: "
                    f"{type(exc).__name__}: {exc}",
                )
                record = PbAttemptRecord(
                    attempt=attempt,
                    tool_call=None,
                    verifier_result=None,
                    error=f"{type(exc).__name__}: {exc}"[:400],
                    cost_usd=0.0,
                    tokens_in=0,
                    tokens_out=0,
                    latency_ms=latency_ms,
                    pb_hint_used=current_pb_hint[:200],
                )
                attempts.append(record)
                # On provider terminal errors + retry_mode=on_verifier_fail,
                # don't retry — that mode gates retry ONLY on verifier
                # rejections, not provider faults. Match VerifierConfig's
                # semantics (verifier.py::_should_retry_verifier).
                if self.cfg.retry_mode == "on_verifier_fail":
                    return PbRetryResult(
                        success=False,
                        final_tool_call=None,
                        attempts=attempts,
                        total_cost_usd=total_cost,
                        total_tokens_in=total_tokens_in,
                        total_tokens_out=total_tokens_out,
                        final_reason="provider_terminal_error",
                        repair_hints=repair_hints,
                    )
                # on_any_rejection: consult QB before next attempt (if any)
                if attempt < effective_max and attempt >= self.cfg.consult_qb_after_attempt:
                    hint = self._safe_qb_repair(
                        intent, None, record.error or "provider error", attempts,
                    )
                    repair_hints.append(hint)
                    current_pb_hint = hint
                continue

            # ── Cost + token accumulation ──────────────────────────────
            latency_ms = (time.monotonic() - t0) * 1000
            attempt_cost = float(pb_result.cost_usd or 0.0)
            attempt_tokens_in = int(pb_result.tokens_in or 0)
            attempt_tokens_out = int(pb_result.tokens_out or 0)
            total_cost += attempt_cost
            total_tokens_in += attempt_tokens_in
            total_tokens_out += attempt_tokens_out

            # ── Verifier vote ──────────────────────────────────────────
            try:
                vresult = self._verify(intent, pb_result.tool_call)
            except Exception as exc:  # noqa: BLE001
                # Verifier itself failed. Treat as verified=False with
                # a descriptive reason — same shape as the on-provider
                # branch below.
                vresult = VerifierResult(
                    verified=False,
                    reason=f"verifier call failed: {type(exc).__name__}: {exc}"[:400],
                )

            record = PbAttemptRecord(
                attempt=attempt,
                tool_call=pb_result.tool_call,
                verifier_result=vresult,
                error=None,
                cost_usd=attempt_cost,
                tokens_in=attempt_tokens_in,
                tokens_out=attempt_tokens_out,
                latency_ms=latency_ms,
                pb_hint_used=current_pb_hint[:200],
            )
            attempts.append(record)

            if vresult.verified:
                return PbRetryResult(
                    success=True,
                    final_tool_call=pb_result.tool_call,
                    attempts=attempts,
                    total_cost_usd=total_cost,
                    total_tokens_in=total_tokens_in,
                    total_tokens_out=total_tokens_out,
                    final_reason="success",
                    repair_hints=repair_hints,
                )

            # ── Verifier rejected — decide whether to retry ────────────
            self._log(
                "warning",
                f"pb_retry attempt {attempt}/{effective_max} verifier rejected: "
                f"{vresult.reason}",
            )
            if attempt < effective_max and attempt >= self.cfg.consult_qb_after_attempt:
                hint = self._safe_qb_repair(
                    intent, pb_result.tool_call, vresult.reason, attempts,
                )
                repair_hints.append(hint)
                current_pb_hint = hint

        # ── All attempts exhausted ─────────────────────────────────────
        return PbRetryResult(
            success=False,
            final_tool_call=None,
            attempts=attempts,
            total_cost_usd=total_cost,
            total_tokens_in=total_tokens_in,
            total_tokens_out=total_tokens_out,
            final_reason=("retry_disabled" if effective_max == 1
                          else "max_attempts_exhausted"),
            repair_hints=repair_hints,
        )

    # ── Internals ──────────────────────────────────────────────────────

    def _safe_qb_repair(
        self,
        intent: dict,
        failed_tool_call: Optional[dict],
        reason: str,
        prior_attempts: list[PbAttemptRecord],
    ) -> str:
        """Wrap `qb_repair_fn` so its own failures never crash the loop."""
        try:
            hint = self._qb_repair(intent, failed_tool_call, reason, prior_attempts)
            if not isinstance(hint, str):
                return f"[qb-repair-returned-non-string: {type(hint).__name__}]"
            return hint[:200]
        except Exception as exc:  # noqa: BLE001
            self._log(
                "warning",
                f"pb_retry qb_repair failed: {type(exc).__name__}: {exc}",
            )
            return f"[qb-repair-failed: {type(exc).__name__}]"

    def _log(self, level: str, msg: str) -> None:
        """Route diagnostic messages. Prefer SystemLogger when supplied
        (BP-13: exceptions land in Control Center Errors page); fall
        back to stdlib logging otherwise (test-friendly)."""
        if self._system_logger is not None:
            log_fn = getattr(self._system_logger, "log", None)
            if callable(log_fn):
                try:
                    self._system_logger.log(
                        "controller.pb_retry",
                        f"pb_retry_{level}",
                        {"message": msg},
                    )
                    return
                except Exception:  # noqa: BLE001 — don't crash on logging
                    pass
        getattr(log, level, log.info)(msg)


# ── Contract for the PB-call callable ──────────────────────────────────

@dataclass(frozen=True)
class PbCallResult:
    """Return shape for `pb_complete_fn`. Caller adapts its PB backend
    call into this so `PbRetryLoop` stays backend-agnostic."""
    tool_call: dict
    cost_usd: float
    tokens_in: int
    tokens_out: int
