"""v6.8 Task #146 Step 2b — opt-in live Gemini smoke test.

D5 decision: one end-to-end test that hits a real provider through
the real LiteLLM adapter inside the compiled AgentGraph. Purpose: catch
provider-surface surprises the mocked tests can't see (auth changes,
response-format enforcement, ContextWindowExceededError shape shifts).

Opt-in: skipped unless ICEBREAKER_LIVE_GEMINI_KEY is set — not
GEMINI_API_KEY, so CI's default provider keys (if any) don't
accidentally trigger a paid call. Same env-gate pattern as G24
(Scope F live sweep).

Cost budget: max_tokens=50000. Max theoretical cost per run is
$0.015 (Gemini 2.5-flash output @ $0.30/1M) but actual is trivial
because Gemini only emits what's needed — a 4-field intent JSON is
<100 tokens. The cap is just a ceiling to prevent runaway generation.

Live-run learning (2026-07-13): Gemini 2.5-flash consumes reasoning
tokens against max_tokens. My initial 100-then-512 was cost-anxious
and still risked truncation if the model decided to think longer.
50k eliminates the class of bug entirely (Gemini 2.5-flash supports
64k output ceiling) at zero real cost.

Production QB defaults in catalogue.toml should also use 4k-8k
minimums for Gemini 2.5 — the shipped 256 default was tuned for
Gemini 2.0 and is too tight for the reasoning-token model.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_LIVE_KEY = os.environ.get("ICEBREAKER_LIVE_GEMINI_KEY", "")


@pytest.mark.skipif(
    not _LIVE_KEY,
    reason="opt-in live test — set ICEBREAKER_LIVE_GEMINI_KEY to run",
)
def test_live_gemini_end_to_end_smoke():
    """Real Gemini call through the LiteLLM adapter through the compiled
    AgentGraph. Asserts: outcome=executed, tier is set, mcpd_result_hash
    non-empty. PB + mcpd are still mocked — the point is to exercise the
    QB provider path end-to-end, not to boot mcpd."""

    from controller.agent_graph import AgentGraph
    from controller.backends import SecretRef
    from controller.backends.gemini_backend import GeminiBackend
    from controller.config import AgentGraphConfig
    from controller.session_store import SessionStore

    # Wrap the live key in a SecretRef the way GeminiBackend expects.
    # We set the env var here explicitly so SecretRef can resolve it —
    # the test's ICEBREAKER_LIVE_GEMINI_KEY becomes the value.
    os.environ["ICEBREAKER_LIVE_GEMINI_KEY"] = _LIVE_KEY

    qb_cfg = SimpleNamespace(
        model="gemini-2.5-flash",
        # Gemini 2.5 uses reasoning tokens against max_tokens. Cap at
        # 50k (well under the 64k output ceiling) to eliminate the
        # truncation-class of bug without inducing real cost — Gemini
        # only emits what it needs. Max theoretical: $0.015/run.
        max_tokens=50000,
        timeout_seconds=30,
        api_key=SecretRef("ICEBREAKER_LIVE_GEMINI_KEY"),
    )
    qb = GeminiBackend(qb_cfg)

    session_state = MagicMock()
    session_state.build_pb_user_turn.return_value = "pb-user-turn"
    session_store = MagicMock()
    session_store.get.return_value = session_state

    pb = MagicMock()
    pb.complete.return_value = SimpleNamespace(
        content_json={"tool": "system.status", "params": {}}
    )
    mcpd = MagicMock()
    mcpd.call.return_value = SimpleNamespace(stdout="all systems nominal")

    prompts = MagicMock()
    prompts.get.return_value = (
        "You are an intent parser. Emit a JSON object with fields "
        "action, target, reason, risk_level. For 'show system status' "
        'emit {"action": "system.status", "target": "", '
        '"reason": "user_requested", "risk_level": "read_only"}.'
    )

    intent_store = MagicMock()
    intent_store.put.return_value = "live-intent-id"

    ag = AgentGraph(
        cfg=AgentGraphConfig(
            enabled=True, checkpointer_path=":memory:",
            checkpointer_retention_days=30, strict_msgpack=True,
        ),
        session_store=session_store,
        qb_backend=qb,
        pb_backend=pb,
        mcpd_client=mcpd,
        audit_log=MagicMock(),
        risk_classify=MagicMock(return_value=SimpleNamespace(tier=0)),
        verifier=MagicMock(),
        prompts=prompts,
        intent_schema={"type": "object"},
        controller_cfg=SimpleNamespace(
            run=SimpleNamespace(tier0_fast_path=True),
            verifier=SimpleNamespace(tier_floor=2, retry_mode="on_call_failed_only"),
        ),
        intent_store=intent_store,
    )
    try:
        results = list(ag.run("show system status", "live-session-1"))
        out = results[0]
        # The QB call actually happened — outcome is executed (fast path
        # bypasses PB) and mcpd_result_hash is populated.
        assert out["outcome"] == "executed", f"got {out}"
        assert out["tier"] == 0
        assert out["mcpd_result_hash"]
        # Real cost: tokens > 0 would need audit-line access; the fact
        # that we got a valid intent back proves LiteLLM ↔ Gemini works
        # end-to-end.
    finally:
        ag.close()
