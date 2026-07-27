"""v6.12 Fix I+ — subprocess worker for gui.*/rpa.* dispatch.

Isolates GuiAgent + RpaBridge calls in a fresh Python process so
pyatspi's GLib initialization runs in a context with its own mainloop.
The daemon's turn-worker thread has no GMainLoop; invoking pyatspi
from there aborts the daemon with SIGTRAP inside libglib-2.0 (v6.12
UTM Stage E finding — daemon crash on `# list my open windows`).

Protocol (stdin → stdout, one JSON line each):

  stdin:  {
    "kind":   "gui" | "rpa",
    "tool":   "gui.ping" | "rpa.list_workflows" | ...,
    "params": {...},
    "config": {...}   # SimpleNamespace-serializable field dict
  }

  stdout: {"ok": bool, "result": {...}, "error": "<str if ok=false>"}

Exit code is 0 on ALL paths — the parent parses the JSON envelope for
success/failure. Uncaught exceptions still print the envelope with
ok=false + traceback string before exiting. Parent enforces a 10s
wall-clock timeout via subprocess.run(..., timeout=10).

Invoked by ``agent_graph_nodes.py::_dispatch_in_subprocess`` via
``[sys.executable, "-m", "controller.gui_worker"]``.
"""

from __future__ import annotations

import io
import json
import sys
import traceback
from types import SimpleNamespace
from typing import Any


def _dispatch(kind: str, tool: str, params: dict, config: dict) -> dict:
    """Import + instantiate the right agent, run handle_request, return
    the raw dict result. Raises on any failure — caller wraps in the
    ok=false envelope.
    """
    cfg_ns = SimpleNamespace(**(config or {}))
    if kind == "gui":
        from gui_agent.agent import GuiAgent
        agent: Any = GuiAgent(config=cfg_ns) if config else GuiAgent()
    elif kind == "rpa":
        from rpa_bridge.bridge import RpaBridge
        agent = RpaBridge(config=cfg_ns) if config else RpaBridge()
    else:
        raise ValueError(f"gui_worker: unknown kind {kind!r} (want 'gui' or 'rpa')")

    result = agent.handle_request(tool, params or {})
    if not isinstance(result, dict):
        result = {"result": result}
    return result


def main() -> int:
    # F-101 Prereq 2 (2026-07-27): RpaBridge._emit_notification writes
    # rpa.keyword_progress / rpa.no_effect / rpa.step_screenshot_error
    # JSON-RPC notifications directly to sys.stdout during
    # handle_request. Without redirection, those lines land in the
    # parent's stdout buffer BEFORE our envelope, corrupting the
    # single-blob json.loads(stdout) the parent does. Redirect
    # sys.stdout to an in-memory buffer during dispatch and write the
    # envelope to the SAVED real stdout after. Notifications are
    # discarded — the final RpaBridge return dict carries the same
    # progress information in `keyword_results[]` (per bridge.py's
    # handle_execute_workflow shape).
    real_stdout = sys.stdout
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            envelope = {"ok": False, "error": "gui_worker: empty stdin"}
            print(json.dumps(envelope), file=real_stdout, flush=True)
            return 0
        req = json.loads(raw)
        kind = str(req.get("kind", ""))
        tool = str(req.get("tool", ""))
        params = req.get("params") or {}
        config = req.get("config") or {}
        # Swap stdout for a discard buffer for the dispatch call so
        # RpaBridge notifications don't reach the parent.
        sys.stdout = io.StringIO()
        try:
            result = _dispatch(kind, tool, params, config)
        finally:
            sys.stdout = real_stdout
        envelope = {"ok": True, "result": result}
    except Exception as exc:  # noqa: BLE001 — worker MUST NOT propagate
        sys.stdout = real_stdout  # ensure restore even on early raise
        envelope = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-2000:],
        }
    # Always print exactly one JSON line to the REAL stdout so parent's
    # json.loads on stdout works. Any stderr from imported modules is
    # captured by parent's capture_output but not parsed.
    print(json.dumps(envelope), file=real_stdout, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
