"""v6.8 Task #146 Step 2b — opt-in live provider smoke tests.

D5 decision: end-to-end tests that hit REAL providers through the real
LiteLLM adapter inside the compiled AgentGraph. Purpose: catch
provider-surface surprises the mocked tests can't see (auth changes,
response-format enforcement, token-counting quirks, reasoning-token
overhead).

Opt-in per provider: each row skips unless its dedicated env var is
set (ICEBREAKER_LIVE_GEMINI_KEY, ICEBREAKER_LIVE_ANTHROPIC_KEY, or
ICEBREAKER_LIVE_OPENAI_KEY). Users run one, two, or all three
depending on which keys they have. Not GEMINI_API_KEY etc. — a
purpose-specific env var so CI's default provider keys (if any)
don't accidentally trigger paid calls. Same pattern as G24
(Scope F live sweep).

Cost budget: max_tokens=50000 (well under each provider's output
ceiling). Actual per-run cost is pennies because we emit ~100 tokens
of intent JSON; the cap only exists to eliminate the truncation
class of bug. Max theoretical: Gemini ~\$0.015, Anthropic Haiku
~\$0.20, OpenAI Mini ~\$0.15.

Live-run learning (2026-07-13): Gemini 2.5-flash consumes reasoning
tokens against max_tokens; a 4-field JSON needed >100 tokens. Setting
50k eliminates the class of bug across all three providers regardless
of their reasoning-token semantics.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ── Provider registry ────────────────────────────────────────────────
#
# Each row: (name, env_var, model_id, backend_module, backend_class_name)
# Backend module/class are strings so pytest.mark.parametrize's id
# generation stays clean.

_LIVE_PROVIDERS = [
    (
        "gemini",
        "ICEBREAKER_LIVE_GEMINI_KEY",
        "gemini-2.5-flash",
        "controller.backends.gemini_backend",
        "GeminiBackend",
    ),
    (
        "anthropic",
        "ICEBREAKER_LIVE_ANTHROPIC_KEY",
        "claude-haiku-4-5",
        "controller.backends.anthropic_backend",
        "AnthropicBackend",
    ),
    (
        "openai",
        "ICEBREAKER_LIVE_OPENAI_KEY",
        "gpt-5-mini",
        "controller.backends.openai_backend",
        "OpenAIBackend",
    ),
]


@pytest.mark.parametrize(
    "provider_name,env_var,model_id,backend_mod,backend_cls_name",
    _LIVE_PROVIDERS,
    ids=[row[0] for row in _LIVE_PROVIDERS],
)
def test_live_provider_end_to_end_smoke(
    provider_name,
    env_var,
    model_id,
    backend_mod,
    backend_cls_name,
):
    """Real provider call through LiteLLM through the compiled AgentGraph.

    Asserts: outcome=executed, tier is set, mcpd_result_hash non-empty.
    PB + mcpd are mocked — the point is to exercise the QB provider
    path end-to-end, not to boot mcpd.
    """
    key = os.environ.get(env_var, "")
    if not key:
        pytest.skip(
            f"opt-in — set {env_var} to run the {provider_name} smoke test"
        )

    import importlib
    from controller.agent_graph import AgentGraph
    from controller.backends import SecretRef
    from controller.config import AgentGraphConfig

    backend_cls = getattr(importlib.import_module(backend_mod), backend_cls_name)

    # Wrap the key in a SecretRef the way each backend expects. The env
    # var name IS the SecretRef target — SecretRef.reveal() reads from
    # os.environ, so we don't need to duplicate the value here.
    qb_cfg = SimpleNamespace(
        model=model_id,
        # 50k eliminates the truncation-class of bug across all three
        # providers regardless of reasoning-token semantics (Gemini 2.5,
        # Claude 4.x extended thinking, OpenAI o-series). Gemini 2.5-flash
        # supports 64k output; Claude 4.5 supports 64k; OpenAI 5-mini
        # supports 128k. Actual per-run cost is pennies — a 4-field JSON
        # intent is <100 tokens.
        max_tokens=50000,
        timeout_seconds=30,
        api_key=SecretRef(env_var),
    )
    qb = backend_cls(qb_cfg)

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
    intent_store.put.return_value = f"live-intent-{provider_name}"

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
            verifier=SimpleNamespace(
                tier_floor=2, retry_mode="on_call_failed_only"
            ),
        ),
        intent_store=intent_store,
    )
    try:
        results = list(ag.run("show system status", f"live-session-{provider_name}"))
        out = results[0]
        assert out["outcome"] == "executed", (
            f"[{provider_name}] expected executed, got {out}"
        )
        assert out["tier"] == 0, f"[{provider_name}] tier not classified"
        assert out["mcpd_result_hash"], (
            f"[{provider_name}] mcpd never fired — QB likely returned invalid intent"
        )
    finally:
        ag.close()


# ── v6.8 Task #147 — Bridge path live test ───────────────────────────


@pytest.mark.skipif(
    not os.environ.get("ICEBREAKER_LIVE_GEMINI_KEY"),
    reason="opt-in — set ICEBREAKER_LIVE_GEMINI_KEY to run",
)
def test_live_bridge_end_to_end_gemini():
    """Task #147: prove Controller.run_turn_streaming routes through
    the bridge when cfg.agent_graph.enabled=True. Uses real Gemini so
    the QB call really runs; PB + mcpd mocked; asserts we get a
    ProgressEvent + CotEvent stream ending in ResultEvent EXECUTED.

    This is the ONE test that proves the whole Task #147 wiring works
    end-to-end. Everything else is mocked."""
    from unittest.mock import MagicMock

    from controller.agent_graph import AgentGraph
    from controller.agent_graph_bridge import run_via_agent_graph
    from controller.audit import Outcome
    from controller.backends import SecretRef
    from controller.backends.gemini_backend import GeminiBackend
    from controller.config import AgentGraphConfig
    from controller.turn_events import CotEvent, ProgressEvent, ResultEvent

    qb_cfg = SimpleNamespace(
        model="gemini-2.5-flash",
        max_tokens=50000,
        timeout_seconds=30,
        api_key=SecretRef("ICEBREAKER_LIVE_GEMINI_KEY"),
    )
    qb = GeminiBackend(qb_cfg)

    session_state = MagicMock()
    session_state.session_id = "live-bridge-session"
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
    intent_store.put.return_value = "live-bridge-intent"

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
            verifier=SimpleNamespace(
                tier_floor=2, retry_mode="on_call_failed_only"
            ),
        ),
        intent_store=intent_store,
    )
    presenter = MagicMock()  # Tier-0 fast path never invokes it.

    try:
        events = list(run_via_agent_graph(
            ag, presenter, "show system status", session_state,
            backend="gemini",
        ))
        types = [type(e) for e in events]
        assert ProgressEvent in types, "bridge must emit ProgressEvent"
        assert CotEvent in types, "bridge must emit CotEvent"
        assert types.count(ResultEvent) == 1
        result_event = next(e for e in events if isinstance(e, ResultEvent))
        assert result_event.result.outcome == Outcome.EXECUTED
        assert result_event.result.success is True
        presenter.show_prompt.assert_not_called()
    finally:
        ag.close()
