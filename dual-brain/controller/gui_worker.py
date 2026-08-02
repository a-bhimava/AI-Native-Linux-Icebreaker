"""v6.12 Fix I+ — subprocess worker for gui.*/rpa.* dispatch.

Isolates GuiAgent + RpaBridge calls in a fresh Python process so
pyatspi's GLib initialization runs in a context with its own mainloop.
The daemon's turn-worker thread has no GMainLoop; invoking pyatspi
from there aborts the daemon with SIGTRAP inside libglib-2.0 (v6.12
UTM Stage E finding — daemon crash on `# list my open windows`).

## Fix V.6f (2026-08-02) — sandbox application (P0-2 remediation)

Pre-V.6f: this worker imported GuiAgent/RpaBridge and called
handle_request() with NO sandbox — Landlock + Seccomp were never
applied on the OC edition's primary dispatch path. INV-5 was bypassed.
The codebase-ct-scan agent surfaced this as a P0 ship-blocker: Fix V
now execs xdotool synthesizing keyboard/mouse events; running that
unsandboxed is much worse than the pre-Fix V baseline.

Post-V.6f: after imports (so /usr/lib mmaps land pre-Landlock) and
before agent.handle_request, we call apply_gui_sandbox() or
apply_rpa_sandbox() based on kind. On non-Linux (dev on macOS),
SandboxError is caught + logged + swallowed — the dev flow shouldn't
require Linux. The env-var ICEBREAKER_GUI_SKIP_SANDBOX=1 skips
unconditionally for unit tests.

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

Invoked by ``agent_graph_nodes.py::_dispatch_in_subprocess`` +
``controller.mcp_gui_server::_handle_tools_call`` via
``[sys.executable, "-m", "controller.gui_worker"]``.
"""

from __future__ import annotations

import io
import json
import os
import sys
import traceback
from types import SimpleNamespace
from typing import Any


_SKIP_SANDBOX_ENV = "ICEBREAKER_GUI_SKIP_SANDBOX"


def _apply_sandbox_for(kind: str, config: dict) -> None:
    """V.6f (2026-08-02): apply the right sandbox for the dispatch kind.

    Callers who want to skip (unit tests, macOS dev flow) set
    ``ICEBREAKER_GUI_SKIP_SANDBOX=1`` in the env. Otherwise:
    - kind='gui' → apply_gui_sandbox
    - kind='rpa' → apply_rpa_sandbox
    - non-Linux → SandboxError raised by sandbox module; log + swallow
    - any other unexpected failure → log + swallow (do NOT crash the
      worker — the tool call should still proceed and produce a
      structured error, not a silent exit)
    """
    if os.environ.get(_SKIP_SANDBOX_ENV):
        print(
            f"gui_worker: skipping sandbox ({_SKIP_SANDBOX_ENV} set)",
            file=sys.stderr, flush=True,
        )
        return

    # Resolve paths from config → fall back to defaults matching the
    # dataclass fields in controller/config.py Gui/RpaConfig.
    home_dir = os.environ.get("HOME") or os.path.expanduser("~")
    if kind == "gui":
        scratch_dir = (config or {}).get(
            "screenshot_dir", "/tmp/icebreaker-gui",
        )
    elif kind == "rpa":
        scratch_dir = (config or {}).get(
            "scratch_dir", "/tmp/icebreaker-rpa",
        )
    else:
        # Unknown kind — dispatch will raise below with a clearer
        # message. Skip the sandbox rather than raise a second error.
        return

    try:
        if kind == "gui":
            from gui_agent.sandbox import apply_gui_sandbox, SandboxError
            apply_gui_sandbox(home_dir=home_dir, scratch_dir=scratch_dir)
        else:
            from rpa_bridge.sandbox import apply_rpa_sandbox, SandboxError
            apply_rpa_sandbox(home_dir=home_dir, scratch_dir=scratch_dir)
    except SandboxError as exc:  # noqa: N806
        print(
            f"gui_worker: SandboxError (kind={kind}): {exc}. "
            f"Continuing WITHOUT sandbox (INV-5 relaxed). "
            f"On Linux this indicates a Landlock/seccomp regression.",
            file=sys.stderr, flush=True,
        )
    except Exception as exc:  # noqa: BLE001 — never crash the worker
        print(
            f"gui_worker: unexpected sandbox failure "
            f"({type(exc).__name__}: {exc}). Continuing without sandbox.",
            file=sys.stderr, flush=True,
        )


def _dispatch(kind: str, tool: str, params: dict, config: dict) -> dict:
    """Import + instantiate the right agent, run handle_request, return
    the raw dict result. Raises on any failure — caller wraps in the
    ok=false envelope.

    V.6f (2026-08-02): sandbox is applied AFTER the agent import (so
    libc/libdbus/pyatspi are already mmap'd) but BEFORE
    handle_request is called (so the actual tool dispatch runs under
    the restricted view).
    """
    cfg_ns = SimpleNamespace(**(config or {}))
    if kind == "gui":
        from gui_agent.agent import GuiAgent
        # Import first (pre-sandbox mmaps), then apply sandbox, then
        # instantiate — GuiAgent.__init__ constructs AtSpiClient +
        # ScreenshotManager but doesn't do the D-Bus connection yet.
        _apply_sandbox_for(kind, config)
        agent: Any = GuiAgent(config=cfg_ns) if config else GuiAgent()
    elif kind == "rpa":
        from rpa_bridge.bridge import RpaBridge
        _apply_sandbox_for(kind, config)
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
