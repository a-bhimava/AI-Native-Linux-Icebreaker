#!/usr/bin/env python3
"""ib_run_v68.py — interactive runner for the v6.8 pipeline.

Type a query, watch every graph event fire in real time with the actual
values (intent JSON, tier, tool call, mcpd stdout, final outcome). PB
and mcpd are STUBBED so you can run this on any dev machine without
booting the full stack — the point is to see what the QB planner emits
and how the graph routes it.

Usage:
  export ICEBREAKER_LIVE_GEMINI_KEY=<key>       # or ANTHROPIC / OPENAI
  python3 ib_run_v68.py                          # interactive prompt
  python3 ib_run_v68.py "list /tmp"              # one-shot
  python3 ib_run_v68.py --backend anthropic "get network status and save to notes.txt"

What you'll see (per turn):
  [planner]       raw QB output:  {"action":"fs.list","target":"/tmp",...}
  [normalize]     plan has 1 step
  [risk]          tier=0
  [executor]      step 1/1  action=fs.list  target=/tmp
  [mcpd]          stdout=<stubbed>
  [responder]     outcome=executed
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

_HERE = pathlib.Path(__file__).resolve().parent
_PARENT = _HERE.parent
if (_PARENT / "controller" / "__init__.py").exists():
    sys.path.insert(0, str(_PARENT))


BACKENDS = {
    "gemini": (
        "ICEBREAKER_LIVE_GEMINI_KEY",
        "gemini-2.5-flash",
        "controller.backends.gemini_backend",
        "GeminiBackend",
    ),
    "anthropic": (
        "ICEBREAKER_LIVE_ANTHROPIC_KEY",
        "claude-haiku-4-5",
        "controller.backends.anthropic_backend",
        "AnthropicBackend",
    ),
    "openai": (
        "ICEBREAKER_LIVE_OPENAI_KEY",
        "gpt-5-mini",
        "controller.backends.openai_backend",
        "OpenAIBackend",
    ),
}


PROMPT_TEMPLATE = """\
You translate a user's natural-language request into a JSON Intent for a
Linux OS agent. Only these actions are supported: system.status,
fs.list, fs.read, fs.write, network.status, service.logs.

Simple single-action request → emit a bare Intent object:
  {"action": "<one of the above>", "target": "<path or empty>",
   "reason": "user_requested", "risk_level": "low"}

Compound request (two actions where step 2 uses step 1's output) →
emit a plan wrapper:
  {"plan": [
     {"action": "network.status", "target": "",
      "reason": "user_requested", "risk_level": "low"},
     {"action": "fs.write", "target": "/tmp/notes.txt",
      "content": "IP: $STEP_0_STDOUT",
      "reason": "user_requested", "risk_level": "low"}
  ]}

Nav phrases ("take me to X" / "go to X" / "cd X") → fs.list <resolved
path>. If X starts with ~, resolve to /home/user + rest.

Emit ONLY the JSON. No prose, no fences.
"""


def _make_backend(name: str):
    env_var, model_id, mod_name, cls_name = BACKENDS[name]
    key = os.environ.get(env_var, "")
    if not key:
        raise RuntimeError(f"{env_var} not set — export it to run {name}")

    import importlib
    from controller.backends import SecretRef

    backend_cls = getattr(importlib.import_module(mod_name), cls_name)
    cfg = SimpleNamespace(
        model=model_id, max_tokens=50000, timeout_seconds=30,
        api_key=SecretRef(env_var),
    )
    return backend_cls(cfg), model_id


class _InstrumentedMcpd:
    """Stubs mcpd but prints the actual tool call the graph asked for."""
    def __init__(self, stubbed_stdout: dict[str, str] | None = None):
        self._i = 0
        self._stubs = stubbed_stdout or {
            "network.status": "10.0.0.42",
            "system.status": "load: 0.12; mem: 34%",
            "fs.list": "file1.txt  file2.txt  README.md",
            "fs.read": "<stubbed file contents>",
            "fs.write": "written",
            "service.logs": "<stubbed logs>",
        }
        self.call = self._call
        self.call_count = 0

    def _call(self, method: str = "", params: dict | None = None, **kwargs):
        tool = method or kwargs.get("tool", "")
        params = params or {}
        self._i += 1
        self.call_count += 1
        target = params.get("path") or params.get("target") or ""
        content = params.get("content", "")
        print(f"  [mcpd]        step {self._i}  tool={tool}")
        if target:
            print(f"                 target={target}")
        if content:
            snippet = content[:80].replace("\n", " ")
            print(f"                 content={snippet!r}")
        stdout = self._stubs.get(tool, "<stubbed>")
        print(f"                 stdout={stdout!r}")
        return SimpleNamespace(stdout=stdout)


def _run_one(query: str, backend_name: str) -> None:
    from controller.agent_graph import AgentGraph
    from controller.config import AgentGraphConfig

    print(f"\n─── query: {query!r}  (backend={backend_name}) ───")

    qb, model_id = _make_backend(backend_name)
    print(f"  [config]      model={model_id}")

    pb = MagicMock()
    pb.complete.return_value = SimpleNamespace(
        content_json={"tool": "system.status", "params": {}}
    )
    mcpd = _InstrumentedMcpd()

    session_state = MagicMock()
    session_state.build_pb_user_turn.return_value = "u"
    session_store = MagicMock()
    session_store.get.return_value = session_state

    prompts = MagicMock()
    prompts.get.return_value = PROMPT_TEMPLATE

    intent_store = MagicMock()
    _counter = {"n": 0}
    def _put(*a, **kw):
        _counter["n"] += 1
        return f"intent-{_counter['n']}"
    intent_store.put.side_effect = _put

    # Instrument the QB backend via a proxy (backend has __slots__).
    class _TracedQb:
        def __init__(self, inner): self._inner = inner
        def __getattr__(self, name): return getattr(self._inner, name)
        def complete(self, *a, **kw):
            result = self._inner.complete(*a, **kw)
            raw = getattr(result, "content_json", None) or getattr(result, "content", None)
            print("  [planner]     raw QB output:")
            try:
                print("                " + json.dumps(raw, indent=2).replace("\n", "\n                "))
            except (TypeError, ValueError):
                print(f"                {raw!r}")
            return result
        def stream_complete(self, *a, **kw):
            return self._inner.stream_complete(*a, **kw)
    qb = _TracedQb(qb)

    def _classify(intent):
        # Tier-0 for the shipped read-only tools; tier-2 for fs.write.
        action = (intent or {}).get("action", "")
        tier = 2 if action == "fs.write" else 0
        print(f"  [risk]        action={action}  tier={tier}")
        return SimpleNamespace(tier=tier)

    ag = AgentGraph(
        cfg=AgentGraphConfig(
            enabled=True, checkpointer_path=":memory:",
            checkpointer_retention_days=30, strict_msgpack=True,
        ),
        session_store=session_store,
        qb_backend=qb, pb_backend=pb, mcpd_client=mcpd,
        audit_log=MagicMock(),
        risk_classify=_classify,
        verifier=MagicMock(),
        prompts=prompts,
        intent_schema={"type": "object"},
        controller_cfg=SimpleNamespace(
            run=SimpleNamespace(tier0_fast_path=True),
            verifier=SimpleNamespace(
                tier_floor=2, retry_mode="on_call_failed_only",
            ),
        ),
        intent_store=intent_store,
    )

    try:
        results = list(ag.run(query, f"session-{backend_name}"))
        out = results[0]
        print(f"  [responder]   outcome={out['outcome']}  tier={out['tier']}")
        if out.get("error_reason"):
            print(f"                error_kind={out.get('error_kind')}  reason={out['error_reason']}")
        print(f"  [summary]     mcpd fired {mcpd.call_count} time(s)")
    finally:
        ag.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="gemini", choices=list(BACKENDS))
    parser.add_argument("query", nargs="?", default=None,
                        help="one-shot query; omit for interactive REPL")
    args = parser.parse_args()

    if args.query:
        _run_one(args.query, args.backend)
        return 0

    print(f"ib_run_v68 — interactive (backend={args.backend}). Ctrl-D to exit.")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not q:
            continue
        try:
            _run_one(q, args.backend)
        except Exception as exc:  # noqa: BLE001
            print(f"  [error] {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
