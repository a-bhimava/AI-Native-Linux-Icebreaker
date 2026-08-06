"""M7.0.2d (v6.16) — unit tests for PbRetryLoop.

Regression lock for the retry-loop + cost-ceiling + QB-consult
contract. Uses fake PB/verifier/qb_repair callables so tests are
hermetic — no network, no llama-server, no Gemini.
"""
from __future__ import annotations

from typing import Optional

import pytest

from controller.pb_retry import (
    PbAttemptRecord,
    PbCallResult,
    PbRetryConfig,
    PbRetryLoop,
    PbRetryResult,
)
from controller.verifier import VerifierResult


def _intent(**overrides) -> dict:
    base = {
        "intent_id": "test-intent-uuid",
        "action": "fs.write",
        "target": "/tmp/foo",
        "content": "hello",
        "reason": "user_requested",
        "risk_level": "low",
        "pb_hint": "",
    }
    base.update(overrides)
    return base


def _tool_call(tool: str = "fs.write", **params) -> dict:
    return {"tool": tool, "params": params or {"path": "/tmp/foo", "content": "hello"}}


# ── Happy path ──────────────────────────────────────────────────────────────

def test_first_attempt_success_no_consult():
    calls: list[tuple[dict, str]] = []
    verify_calls: list[dict] = []
    repair_calls: list = []

    def pb(intent, pb_hint):
        calls.append((intent, pb_hint))
        return PbCallResult(tool_call=_tool_call(), cost_usd=0.001,
                            tokens_in=100, tokens_out=50)

    def verify(intent, tool_call):
        verify_calls.append(tool_call)
        return VerifierResult(verified=True, reason="ok")

    def repair(intent, failed, reason, prior):
        repair_calls.append(reason)
        return "should never be called"

    loop = PbRetryLoop(PbRetryConfig(), pb, verify, repair)
    result = loop.run(_intent())

    assert result.success is True
    assert result.final_tool_call["tool"] == "fs.write"
    assert result.final_reason == "success"
    assert len(result.attempts) == 1
    assert result.attempts[0].pb_hint_used == ""  # intent had no pb_hint
    assert result.total_cost_usd == pytest.approx(0.001)
    assert result.total_tokens_in == 100
    assert result.total_tokens_out == 50
    assert result.repair_hints == []
    assert len(repair_calls) == 0, "qb_repair must NOT fire on happy path"


def test_first_attempt_success_uses_intent_pb_hint():
    """QB's initial pb_hint (F-41) flows into attempt 1 without QB-consult."""
    seen_hints: list[str] = []

    def pb(intent, pb_hint):
        seen_hints.append(pb_hint)
        return PbCallResult(tool_call=_tool_call(), cost_usd=0.0,
                            tokens_in=1, tokens_out=1)

    def verify(intent, tool_call):
        return VerifierResult(verified=True, reason="ok")

    loop = PbRetryLoop(PbRetryConfig(), pb, verify,
                       qb_repair_fn=lambda *a, **k: "SHOULD NOT FIRE")
    intent = _intent(pb_hint="use content='hello\\n' with trailing newline")
    result = loop.run(intent)

    assert result.success is True
    assert seen_hints[0].startswith("use content=")


# ── Verifier rescue path ────────────────────────────────────────────────────

def test_second_attempt_rescues_verifier_reject():
    """First attempt verifier-rejected → QB coaches → second attempt succeeds."""
    pb_call_count = 0
    seen_hints: list[str] = []
    repair_calls: list = []

    def pb(intent, pb_hint):
        nonlocal pb_call_count
        pb_call_count += 1
        seen_hints.append(pb_hint)
        return PbCallResult(tool_call=_tool_call(path="/tmp/attempt" + str(pb_call_count)),
                            cost_usd=0.001, tokens_in=100, tokens_out=50)

    def verify(intent, tool_call):
        if tool_call["params"]["path"] == "/tmp/attempt1":
            return VerifierResult(verified=False, reason="wrong path (expected /tmp/foo)")
        return VerifierResult(verified=True, reason="ok")

    def repair(intent, failed, reason, prior):
        repair_calls.append((failed, reason, len(prior)))
        return "Use path='/tmp/foo' (from intent.target)."

    loop = PbRetryLoop(PbRetryConfig(), pb, verify, repair)
    result = loop.run(_intent())

    assert result.success is True
    assert len(result.attempts) == 2
    assert result.attempts[0].verifier_result.verified is False
    assert result.attempts[1].verifier_result.verified is True
    assert seen_hints[0] == ""  # initial attempt
    assert "Use path=" in seen_hints[1]  # rescue hint threaded to attempt 2
    assert len(repair_calls) == 1
    assert result.repair_hints == ["Use path='/tmp/foo' (from intent.target)."]
    assert result.total_cost_usd == pytest.approx(0.002)
    assert result.final_reason == "success"


# ── Exhaustion ──────────────────────────────────────────────────────────────

def test_third_attempt_hard_fails_returns_exhausted():
    """All 3 attempts verifier-rejected → PbRetryResult.success=False."""
    def pb(intent, pb_hint):
        return PbCallResult(tool_call=_tool_call(),
                            cost_usd=0.001, tokens_in=50, tokens_out=30)

    def verify(intent, tool_call):
        return VerifierResult(verified=False, reason="always rejects (test)")

    def repair(intent, failed, reason, prior):
        return f"attempt {len(prior)+1} hint"

    loop = PbRetryLoop(PbRetryConfig(max_attempts=3), pb, verify, repair)
    result = loop.run(_intent())

    assert result.success is False
    assert result.final_reason == "max_attempts_exhausted"
    assert result.final_tool_call is None
    assert len(result.attempts) == 3
    assert all(a.verifier_result.verified is False for a in result.attempts)
    assert result.total_cost_usd == pytest.approx(0.003)
    # QB-consult fires after attempts 1 and 2 (consult_qb_after_attempt=1
    # default = coach from attempt 2 onward). Attempt 3 is the last so
    # no consult happens after it. Total: 2 repair hints for 3 attempts.
    assert len(result.repair_hints) == 2


# ── Cost ceiling ────────────────────────────────────────────────────────────

def test_cost_ceiling_blocks_further_attempts():
    """After cost exceeds ceiling, next attempt is refused pre-spend."""
    def pb(intent, pb_hint):
        # Each attempt costs 0.003; ceiling is 0.005 → attempt 2 spends
        # (0.003 -> total 0.003, still under), attempt 3 pre-check sees
        # 0.006 which breaches, refuses to spend attempt 3.
        return PbCallResult(tool_call=_tool_call(),
                            cost_usd=0.003, tokens_in=50, tokens_out=30)

    def verify(intent, tool_call):
        return VerifierResult(verified=False, reason="never verifies")

    loop = PbRetryLoop(
        PbRetryConfig(max_attempts=3, cost_ceiling_usd_per_turn=0.005),
        pb, verify, qb_repair_fn=lambda *a, **k: "hint",
    )
    result = loop.run(_intent())

    assert result.success is False
    assert result.final_reason == "cost_exceeded"
    # Attempts 1 and 2 spent; attempt 3 refused pre-spend.
    assert len(result.attempts) == 2
    assert result.total_cost_usd == pytest.approx(0.006)


def test_cost_ceiling_zero_disables_check():
    """cost_ceiling_usd_per_turn=0 disables the ceiling (opt-out)."""
    def pb(intent, pb_hint):
        return PbCallResult(tool_call=_tool_call(),
                            cost_usd=1.0, tokens_in=1, tokens_out=1)

    def verify(intent, tool_call):
        return VerifierResult(verified=False, reason="reject")

    loop = PbRetryLoop(
        PbRetryConfig(max_attempts=2, cost_ceiling_usd_per_turn=0.0),
        pb, verify, qb_repair_fn=lambda *a, **k: "hint",
    )
    result = loop.run(_intent())

    # Even though cost=1.0 vastly exceeds a nominal ceiling, disabled
    # means both attempts run.
    assert len(result.attempts) == 2
    assert result.final_reason == "max_attempts_exhausted"


# ── Provider errors ─────────────────────────────────────────────────────────

def test_provider_error_recorded_and_retried_by_default():
    """PB provider error on attempt 1 → recorded + retried; retry succeeds."""
    call_count = 0

    def pb(intent, pb_hint):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated PB provider fault")
        return PbCallResult(tool_call=_tool_call(),
                            cost_usd=0.001, tokens_in=10, tokens_out=5)

    def verify(intent, tool_call):
        return VerifierResult(verified=True, reason="ok")

    loop = PbRetryLoop(PbRetryConfig(), pb, verify,
                       qb_repair_fn=lambda *a, **k: "try again")
    result = loop.run(_intent())

    assert result.success is True
    assert len(result.attempts) == 2
    assert result.attempts[0].error is not None
    assert "simulated PB provider fault" in result.attempts[0].error
    assert result.attempts[0].tool_call is None
    assert result.attempts[1].tool_call is not None
    # Cost only from the successful attempt.
    assert result.total_cost_usd == pytest.approx(0.001)


def test_provider_error_terminal_when_mode_is_on_verifier_fail():
    """retry_mode=on_verifier_fail refuses to retry on provider errors."""
    def pb(intent, pb_hint):
        raise RuntimeError("provider fault")

    loop = PbRetryLoop(
        PbRetryConfig(max_attempts=3, retry_mode="on_verifier_fail"),
        pb, verify_fn=lambda i, t: VerifierResult(True, "unused"),
        qb_repair_fn=lambda *a, **k: "hint",
    )
    result = loop.run(_intent())

    assert result.success is False
    assert result.final_reason == "provider_terminal_error"
    assert len(result.attempts) == 1


# ── retry_mode=off ──────────────────────────────────────────────────────────

def test_retry_mode_off_never_retries():
    call_count = 0

    def pb(intent, pb_hint):
        nonlocal call_count
        call_count += 1
        return PbCallResult(tool_call=_tool_call(),
                            cost_usd=0.001, tokens_in=1, tokens_out=1)

    def verify(intent, tool_call):
        return VerifierResult(verified=False, reason="reject")

    loop = PbRetryLoop(
        PbRetryConfig(retry_mode="off", max_attempts=5),
        pb, verify, qb_repair_fn=lambda *a, **k: "hint",
    )
    result = loop.run(_intent())

    assert call_count == 1  # off = effective max_attempts=1
    assert result.final_reason == "retry_disabled"
    assert result.success is False


# ── QB-consult threshold ────────────────────────────────────────────────────

def test_qb_repair_not_called_when_verifier_verified():
    """Happy path: repair_fn never fires."""
    repair_calls = []

    def pb(intent, pb_hint):
        return PbCallResult(tool_call=_tool_call(),
                            cost_usd=0.0, tokens_in=1, tokens_out=1)

    def verify(intent, tool_call):
        return VerifierResult(verified=True, reason="ok")

    def repair(intent, failed, reason, prior):
        repair_calls.append(1)
        return "unused"

    loop = PbRetryLoop(PbRetryConfig(), pb, verify, repair)
    loop.run(_intent())
    assert repair_calls == []


def test_qb_repair_disabled_when_threshold_high():
    """consult_qb_after_attempt=999 effectively disables QB rescue."""
    repair_calls = []

    def pb(intent, pb_hint):
        return PbCallResult(tool_call=_tool_call(),
                            cost_usd=0.0, tokens_in=1, tokens_out=1)

    def verify(intent, tool_call):
        return VerifierResult(verified=False, reason="reject")

    def repair(intent, failed, reason, prior):
        repair_calls.append(1)
        return "unused"

    loop = PbRetryLoop(
        PbRetryConfig(max_attempts=3, consult_qb_after_attempt=999),
        pb, verify, repair,
    )
    loop.run(_intent())
    assert repair_calls == []


def test_qb_repair_failure_does_not_crash_loop():
    """A crashing qb_repair returns a stub hint; loop continues with degraded coaching."""
    call_count = 0

    def pb(intent, pb_hint):
        nonlocal call_count
        call_count += 1
        return PbCallResult(tool_call=_tool_call(),
                            cost_usd=0.001, tokens_in=1, tokens_out=1)

    def verify(intent, tool_call):
        return VerifierResult(verified=False, reason="reject")

    def repair(intent, failed, reason, prior):
        raise RuntimeError("QB is down")

    loop = PbRetryLoop(PbRetryConfig(max_attempts=3), pb, verify, repair)
    result = loop.run(_intent())

    # All 3 attempts ran; repair_hints show the stub failure marker.
    assert call_count == 3
    assert result.final_reason == "max_attempts_exhausted"
    assert len(result.repair_hints) == 2
    assert all(h.startswith("[qb-repair-failed:") for h in result.repair_hints)


# ── Accumulator sanity (guards against pb_cost = ... instead of +=) ────────

def test_attempts_accumulate_cost_and_tokens():
    """Sum across N attempts (would fail if the loop overwrote instead of accumulated)."""
    attempt = [0]
    costs = [0.001, 0.002, 0.003]
    tokens_in = [100, 200, 300]
    tokens_out = [50, 60, 70]

    def pb(intent, pb_hint):
        i = attempt[0]
        attempt[0] += 1
        return PbCallResult(tool_call=_tool_call(),
                            cost_usd=costs[i],
                            tokens_in=tokens_in[i],
                            tokens_out=tokens_out[i])

    def verify(intent, tool_call):
        return VerifierResult(verified=False, reason="reject")

    loop = PbRetryLoop(PbRetryConfig(max_attempts=3), pb, verify,
                       qb_repair_fn=lambda *a, **k: "hint")
    result = loop.run(_intent())

    assert result.total_cost_usd == pytest.approx(sum(costs))
    assert result.total_tokens_in == sum(tokens_in)
    assert result.total_tokens_out == sum(tokens_out)


# ── Config validation ──────────────────────────────────────────────────────

def test_config_rejects_max_attempts_zero():
    with pytest.raises(ValueError, match="max_attempts"):
        PbRetryLoop(
            PbRetryConfig(max_attempts=0),
            pb_complete_fn=lambda *a: PbCallResult({}, 0, 0, 0),
            verify_fn=lambda *a: VerifierResult(True, ""),
            qb_repair_fn=lambda *a: "",
        )


def test_config_rejects_unknown_retry_mode():
    with pytest.raises(ValueError, match="retry_mode"):
        PbRetryLoop(
            PbRetryConfig(retry_mode="nonsense"),
            pb_complete_fn=lambda *a: PbCallResult({}, 0, 0, 0),
            verify_fn=lambda *a: VerifierResult(True, ""),
            qb_repair_fn=lambda *a: "",
        )


# ── QB-repair receives structured facts, not raw text (INV-1 guard) ────────

def test_qb_repair_receives_intent_and_failed_call_only():
    """Verify the repair callable's signature: no raw user text leaks in."""
    seen_args = []

    def pb(intent, pb_hint):
        return PbCallResult(tool_call=_tool_call(),
                            cost_usd=0.0, tokens_in=1, tokens_out=1)

    def verify(intent, tool_call):
        return VerifierResult(verified=False, reason="test rejection")

    def repair(intent, failed_tool_call, reason, prior_attempts):
        seen_args.append({
            "intent_keys": sorted(intent.keys()),
            "failed_tool_call": failed_tool_call,
            "reason": reason,
            "prior_attempts_count": len(prior_attempts),
        })
        return "hint"

    loop = PbRetryLoop(
        PbRetryConfig(max_attempts=2, consult_qb_after_attempt=1),
        pb, verify, repair,
    )
    intent = _intent()
    loop.run(intent)

    assert len(seen_args) == 1
    args = seen_args[0]
    # Intent keys must be the structured schema fields ONLY —
    # no `query` / `raw_user_text` / `user_input` etc.
    forbidden_keys = {"query", "raw_user_text", "user_input", "chat_history"}
    leaked = forbidden_keys & set(args["intent_keys"])
    assert not leaked, f"Raw-text keys leaked into qb_repair intent: {leaked}"
    # Failed tool_call is PB output (JSON-safe by construction).
    assert args["failed_tool_call"] == _tool_call()
    assert args["reason"] == "test rejection"
