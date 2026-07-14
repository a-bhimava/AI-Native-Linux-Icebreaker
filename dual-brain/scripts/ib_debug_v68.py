#!/usr/bin/env python3
"""ib_debug_v68.py — v6.8 pipeline stage probe.

Walks every stage of the new v6.8 pipeline in order (config → deps →
adapter → session → planner → tier gate → executor → mcpd → bridge)
and prints the first stage that fails. Intentionally minimal: this is
"which stage broke," not "why."

Usage:
  python3 ib_debug_v68.py                      # dry probe (mocks QB/PB/mcpd)
  python3 ib_debug_v68.py --live-gemini        # probe with real QB call
                                                 (needs ICEBREAKER_LIVE_GEMINI_KEY)

Exit code = index of the first failing stage (0 = all green).
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import traceback
from types import SimpleNamespace
from unittest.mock import MagicMock

# Make `controller.*` importable both from a source checkout (parent
# dir of scripts/) and from the installed venv (/opt/icebreaker/venv/
# lib/pythonX.Y/site-packages/).
_HERE = pathlib.Path(__file__).resolve().parent
_PARENT = _HERE.parent
if (_PARENT / "controller" / "__init__.py").exists():
    sys.path.insert(0, str(_PARENT))
for _cand in ("/opt/icebreaker/venv/lib/python3.12/site-packages",
              "/opt/icebreaker/venv/lib/python3.11/site-packages"):
    if pathlib.Path(_cand).exists() and _cand not in sys.path:
        sys.path.insert(0, _cand)


STAGES: list[tuple[str, str]] = []
_FAILED_AT: int | None = None


def _stage(idx: int, name: str, fn):
    global _FAILED_AT
    if _FAILED_AT is not None:
        print(f"  [{idx:>2}] {name}  SKIP (earlier stage failed)")
        return None
    try:
        result = fn()
        detail = f"  →  {result}" if result is not None else ""
        print(f"  [{idx:>2}] {name}  OK{detail}")
        return result
    except Exception as exc:  # noqa: BLE001 — this IS the diagnostic
        print(f"  [{idx:>2}] {name}  FAIL — {type(exc).__name__}: {exc}")
        if os.environ.get("IB_DEBUG_V68_TRACE"):
            traceback.print_exc()
        _FAILED_AT = idx
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live-gemini", action="store_true",
        help="use a real Gemini QB call (needs ICEBREAKER_LIVE_GEMINI_KEY)",
    )
    args = parser.parse_args()

    print("v6.8 pipeline stage probe")
    print("=" * 50)

    # Stage 1 — config surface
    def s1():
        from controller.config import AgentGraphConfig, VerifierConfig, RunConfig
        cfg = AgentGraphConfig()
        assert hasattr(cfg, "enabled")
        assert hasattr(cfg, "checkpointer_path")
        vcfg = VerifierConfig()
        assert hasattr(vcfg, "tier_floor")
        rcfg = RunConfig()
        assert hasattr(rcfg, "tier0_fast_path")
        return {"agent_graph.enabled": cfg.enabled,
                "verifier.tier_floor": vcfg.tier_floor,
                "run.tier0_fast_path": rcfg.tier0_fast_path}

    c1 = _stage(1, "config dataclasses (agent_graph, verifier, run)", s1)

    # Stage 2 — LangGraph + msgpack allowlist import
    def s2():
        import importlib.metadata as md
        import langgraph  # noqa: F401
        from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: F401
        return md.version("langgraph")

    v = _stage(2, "langgraph + SqliteSaver import", s2)

    # Stage 3 — LiteLLM import
    def s3():
        import importlib.metadata as md
        import litellm  # noqa: F401
        from controller.backends._litellm_shared import call_via_litellm  # noqa: F401
        return md.version("litellm")

    lv = _stage(3, "litellm + _litellm_shared import", s3)

    # Stage 4 — SessionStore create/get
    def s4():
        from controller.session_store import SessionStore
        from controller.config import SessionConfig
        store = SessionStore()
        s = store.create(backend="gemini", cfg=SessionConfig())
        got = store.get(s.session_id)
        assert got is s, "session_store.get != store.create"
        store.remove(s.session_id)
        return s.session_id[:8] + "..."

    _stage(4, "SessionStore create/get/remove", s4)

    # Stage 5 — Plan schema loads + normalize accepts bare intent
    def s5():
        import json, pathlib
        p = pathlib.Path(__file__).parent.parent / "controller" / "schemas" / "plan.json"
        schema = json.loads(p.read_text())
        assert "properties" in schema and "plan" in schema["properties"]
        from controller.plan_executor import normalize_planner_output
        bare = {"action": "system.status", "target": "",
                "reason": "test", "risk_level": "low"}
        norm = normalize_planner_output(bare)
        assert isinstance(norm, list) and len(norm) == 1
        assert norm[0]["action"] == "system.status"
        return "bare intent → 1-step plan"

    _stage(5, "plan.json + normalize_planner_output", s5)

    # Stage 6 — marker substitution
    def s6():
        from controller.plan_executor import resolve_step_markers
        step = {"action": "fs.write", "target": "/tmp/$STEP_0_STDOUT",
                "content": "line: $STEP_0_STDOUT"}
        resolved = resolve_step_markers(step, {0: "192.168.1.1"})
        assert resolved["target"] == "/tmp/192.168.1.1", resolved
        assert resolved["content"] == "line: 192.168.1.1", resolved
        return "$STEP_0_STDOUT → 192.168.1.1"

    _stage(6, "resolve_step_markers ($STEP_N_STDOUT)", s6)

    # Stage 7 — Tier-0 fast path recognizes a known tool
    def s7():
        from controller.tier0_fast_path import TIER0_FAST_PATH_TOOLS
        assert "system.status" in TIER0_FAST_PATH_TOOLS
        assert "fs.list" in TIER0_FAST_PATH_TOOLS
        return f"{len(TIER0_FAST_PATH_TOOLS)} tools registered"

    _stage(7, "tier0_fast_path tool registry", s7)

    # Stage 8 — verifier tier_floor skip helper
    def s8():
        from controller.verifier import should_skip_verifier
        from controller.config import VerifierConfig
        vcfg = VerifierConfig(tier_floor=2, retry_mode="on_call_failed_only")
        skip0 = should_skip_verifier(vcfg, tier=0)
        skip2 = should_skip_verifier(vcfg, tier=2)
        assert skip0 is True and skip2 is False, (skip0, skip2)
        return "tier 0 skipped, tier 2 gated"

    _stage(8, "verifier.should_skip_verifier (M7.1)", s8)

    # Stage 9 — AgentGraph constructs + compiles
    def s9():
        from controller.agent_graph import AgentGraph
        from controller.config import AgentGraphConfig
        qb = MagicMock()
        pb = MagicMock()
        mcpd = MagicMock()
        ss = MagicMock()
        prompts = MagicMock()
        prompts.get.return_value = "stub"
        intent_store = MagicMock()
        intent_store.put.return_value = "iid"
        ag = AgentGraph(
            cfg=AgentGraphConfig(enabled=True, checkpointer_path=":memory:",
                                 checkpointer_retention_days=30,
                                 strict_msgpack=True),
            session_store=ss, qb_backend=qb, pb_backend=pb,
            mcpd_client=mcpd, audit_log=MagicMock(),
            risk_classify=MagicMock(return_value=SimpleNamespace(tier=0)),
            verifier=MagicMock(), prompts=prompts,
            intent_schema={"type": "object"},
            controller_cfg=SimpleNamespace(
                run=SimpleNamespace(tier0_fast_path=True),
                verifier=SimpleNamespace(tier_floor=2,
                                         retry_mode="on_call_failed_only"),
            ),
            intent_store=intent_store,
        )
        assert ag._graph is not None
        ag.close()
        return "compiled + closed clean"

    _stage(9, "AgentGraph construct + compile + close", s9)

    # Stage 10 — bridge translates outcome → events
    def s10():
        from controller.agent_graph_bridge import translate_outcome_to_events
        from controller.audit import Outcome
        from controller.turn_events import ResultEvent
        outcome = {
            "outcome": "executed", "session_id": "s", "turn_id": "t",
            "intent_id": "i", "tier": 0,
            "tool_call_hash": "hash", "mcpd_result_hash": "rhash",
            "error_kind": None, "error_reason": None,
        }
        events = translate_outcome_to_events(outcome, t0=0.0, backend="gemini")
        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert len(result_events) == 1
        assert result_events[0].result.outcome == Outcome.EXECUTED
        return "outcome → ResultEvent(EXECUTED)"

    _stage(10, "agent_graph_bridge.translate_outcome_to_events", s10)

    # Stage 11 — live QB call through the graph (opt-in)
    if args.live_gemini:
        def s11():
            key = os.environ.get("ICEBREAKER_LIVE_GEMINI_KEY", "")
            if not key:
                raise RuntimeError("ICEBREAKER_LIVE_GEMINI_KEY not set")
            from controller.agent_graph import AgentGraph
            from controller.backends import SecretRef
            from controller.backends.gemini_backend import GeminiBackend
            from controller.config import AgentGraphConfig

            qb_cfg = SimpleNamespace(
                model="gemini-2.5-flash", max_tokens=50000,
                timeout_seconds=30,
                api_key=SecretRef("ICEBREAKER_LIVE_GEMINI_KEY"),
            )
            qb = GeminiBackend(qb_cfg)
            pb = MagicMock()
            mcpd = MagicMock()
            mcpd.call.return_value = SimpleNamespace(stdout="nominal")
            ss_state = MagicMock()
            ss_state.build_pb_user_turn.return_value = "u"
            ss = MagicMock()
            ss.get.return_value = ss_state
            prompts = MagicMock()
            prompts.get.return_value = (
                "Emit JSON: {\"action\":\"system.status\",\"target\":\"\","
                "\"reason\":\"user_requested\",\"risk_level\":\"low\"}"
            )
            istore = MagicMock()
            istore.put.return_value = "iid"

            ag = AgentGraph(
                cfg=AgentGraphConfig(
                    enabled=True, checkpointer_path=":memory:",
                    checkpointer_retention_days=30, strict_msgpack=True),
                session_store=ss, qb_backend=qb, pb_backend=pb,
                mcpd_client=mcpd, audit_log=MagicMock(),
                risk_classify=MagicMock(return_value=SimpleNamespace(tier=0)),
                verifier=MagicMock(), prompts=prompts,
                intent_schema={"type": "object"},
                controller_cfg=SimpleNamespace(
                    run=SimpleNamespace(tier0_fast_path=True),
                    verifier=SimpleNamespace(tier_floor=2,
                                             retry_mode="on_call_failed_only"),
                ),
                intent_store=istore,
            )
            try:
                out = list(ag.run("show system status", "probe-sess"))[0]
                assert out["outcome"] == "executed", out
                assert mcpd.call.call_count == 1
                return f"outcome={out['outcome']} tier={out['tier']}"
            finally:
                ag.close()

        _stage(11, "live gemini → planner → tier0 → mcpd", s11)

    print("=" * 50)
    if _FAILED_AT is None:
        print("all stages OK")
        return 0
    print(f"first failure at stage {_FAILED_AT}")
    return _FAILED_AT


if __name__ == "__main__":
    sys.exit(main())
