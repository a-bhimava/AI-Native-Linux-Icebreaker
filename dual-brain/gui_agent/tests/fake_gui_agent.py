#!/usr/bin/env python3
"""Fake GUI Agent shim for unit tests.

Same pattern as ``controller/tests/fake_mcpd.py`` — behaviour is
selected by the ``FAKE_GUI_AGENT_MODE`` environment variable:

  normal        — Happy path. Responds to all GUI methods.
  no_atspi      — gui.ping reports atspi_available=false.
  sandbox_fail  — Exits with code 1 immediately (simulates sandbox failure).
"""

from __future__ import annotations

import json
import os
import sys

MODE = os.environ.get("FAKE_GUI_AGENT_MODE", "normal")


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    if MODE == "sandbox_fail":
        print("GUI Agent sandbox failed: simulated", file=sys.stderr)
        return 1

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue

        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {}) or {}

        if method == "gui.ping":
            atspi = MODE != "no_atspi"
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "status": "ok",
                "atspi_available": atspi,
                "screenshots_available": True,
            }})
        elif method == "gui.get_window_list":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "windows": [
                    {"title": "Test Window", "app_name": "test-app", "pid": 1234, "geometry": [0, 0, 800, 600]},
                ],
            }})
        elif method == "gui.find_element":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "path": f"/{params.get('window', '')}/{params.get('name', '')}",
                "role": params.get("role", ""),
                "name": params.get("name", ""),
                "position": [100, 200],
                "size": [80, 30],
                "states": ["enabled", "visible"],
                "text": "",
            }})
        elif method in ("gui.click", "gui.type", "gui.select"):
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"success": True, "error": None}})
        elif method == "gui.screenshot":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "path": "/tmp/icebreaker-gui/fake.png",
                "sha256": "0" * 64,
                "width": 1920,
                "height": 1080,
                "timestamp": 1700000000.0,
            }})
        elif method == "gui.get_element_tree":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "tree": [{"role": "frame", "name": "root", "children": []}],
            }})
        else:
            _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}})

    return 0


if __name__ == "__main__":
    sys.exit(main())
