#!/usr/bin/env python3
"""Fake RPA Bridge shim for unit tests.

Same pattern as ``gui_agent/tests/fake_gui_agent.py`` — behaviour is
selected by the ``FAKE_RPA_BRIDGE_MODE`` environment variable:

  normal        — Happy path. Responds to all RPA methods with realistic results.
  timeout       — rpa.execute_workflow returns partial results with timed_out=True.
  keyword_error — 3rd keyword in any workflow fails.
  sandbox_fail  — Exits with code 1 immediately (simulates sandbox failure).
"""

from __future__ import annotations

import json
import os
import sys
import time

MODE = os.environ.get("FAKE_RPA_BRIDGE_MODE", "normal")


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _make_keyword_results(keywords: list, *, fail_at: int = -1, timeout_at: int = -1) -> list:
    results = []
    for i, kw in enumerate(keywords):
        if i == timeout_at:
            results.append({
                "index": i, "name": kw.get("name", ""),
                "status": "timeout", "elapsed_ms": 30000.0,
                "screenshot_hash": "0" * 64, "error": "workflow timeout exceeded",
            })
            break
        if i == fail_at:
            results.append({
                "index": i, "name": kw.get("name", ""),
                "status": "fail", "elapsed_ms": 50.0,
                "screenshot_hash": "0" * 64, "error": "Element not found",
            })
            continue
        results.append({
            "index": i, "name": kw.get("name", ""),
            "status": "pass", "elapsed_ms": 100.0 * (i + 1),
            "screenshot_hash": f"{'0' * 60}{i:04d}", "error": "",
        })
    return results


def main() -> int:
    if MODE == "sandbox_fail":
        print("RPA Bridge sandbox failed: simulated", file=sys.stderr)
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

        if method == "rpa.ping":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "status": "ok",
                "robot_framework_available": True,
                "uinput_available": False,
            }})
        elif method == "rpa.execute_workflow":
            keywords = params.get("keywords", [])

            if MODE == "timeout":
                kr = _make_keyword_results(keywords, timeout_at=min(2, len(keywords) - 1))
                _send({"jsonrpc": "2.0", "id": req_id, "result": {
                    "success": False,
                    "timed_out": True,
                    "keywords_executed": len(kr),
                    "keywords_total": len(keywords),
                    "elapsed_ms": 30000.0,
                    "keyword_results": kr,
                }})
            elif MODE == "keyword_error":
                fail_idx = min(2, len(keywords) - 1)
                kr = _make_keyword_results(keywords, fail_at=fail_idx)
                _send({"jsonrpc": "2.0", "id": req_id, "result": {
                    "success": False,
                    "timed_out": False,
                    "keywords_executed": len(kr),
                    "keywords_total": len(keywords),
                    "elapsed_ms": 500.0,
                    "keyword_results": kr,
                }})
            else:
                kr = _make_keyword_results(keywords)
                _send({"jsonrpc": "2.0", "id": req_id, "result": {
                    "success": True,
                    "timed_out": False,
                    "keywords_executed": len(kr),
                    "keywords_total": len(keywords),
                    "elapsed_ms": sum(r["elapsed_ms"] for r in kr),
                    "keyword_results": kr,
                }})
        elif method == "rpa.find_by_image":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "found": True,
                "confidence": 0.92,
                "bbox": [100, 200, 50, 30],
                "center": [125, 215],
                "screenshot_hash": "0" * 64,
            }})
        elif method == "rpa.list_workflows":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "workflows": [],
            }})
        else:
            _send({"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32601, "message": f"Unknown method: {method}",
            }})

    return 0


if __name__ == "__main__":
    sys.exit(main())
