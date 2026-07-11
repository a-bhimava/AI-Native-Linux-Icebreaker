#!/usr/bin/env python3
"""Phase 6 Scope F — live corpus runner (guest-side).

Reads ``intent_corpus.json`` from stdin, connects to the local
Icebreaker daemon, runs each row's query via ``turn.run`` and compares
the daemon's response against ``expected_outcome``. Prints one JSON line
per row to stdout (streamed, so a long sweep gets progressively visible
from the driver's ``ssh`` tail).

Exit code = number of rows whose outcome did NOT match. 0 = clean.

Called by ``ci.sh`` G24 (see the plan file, F3):

    scp intent_corpus.json     $HOST:/tmp/
    scp ib_run_corpus.py        $HOST:/tmp/
    ssh $HOST 'python3 /tmp/ib_run_corpus.py \\
               --sock /run/icebreaker/controller.sock \\
               < /tmp/intent_corpus.json' \\
        | tee /tmp/g24_output.jsonl

The runner is deliberately independent of ``pytest``, so a broken venv
on the guest does NOT block the sweep — only the daemon needs to be up.

BP-3 note. This script never prints untrusted model / audit strings to
a terminal without escaping — it prints structured JSON only. That way
a hostile audit row can't paint an approval prompt into the operator's
terminal.

**Skipping semantics.** Rows the runner refuses to POST live:

* Adversarial rows (category=adversarial) — never run against a real
  guest. The offline test proves they're routed to a safe fallback;
  running them live would spend real API budget on adversarial input.
* ``qb_mock_action`` rows (phantom-UNSUPPORTED) — the mocked action
  can't happen against a real Gemini; they're purely offline routing
  tests.
* Rows whose ``expected_action`` is ``system.unsupported`` — running
  them is only useful if we want to prove the friendly-error path;
  keeping them out saves cost on every G24 run. The offline suite
  covers routing.

Reading the output:

    {"id": "broad-os.pkg.query.001",
     "status": "ok",           # ok | outcome_mismatch | error | skipped
     "expected_outcome": "executed",
     "actual_outcome": "executed",
     "success": true,
     "elapsed_ms": 4321.2,
     "reason": null}           # null on success; short string on failure

The G24 gate parses this stream. See ``ci.sh``.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path


# The runner runs on the guest, from /tmp — add the venv's site-packages
# so we can import `controller.client`. The path matches the deploy layout
# documented in CLAUDE.md § Deployment & Packaging Gotchas.
_VENV_SITE = Path("/opt/icebreaker/venv/lib/python3.12/site-packages")
if _VENV_SITE.exists() and str(_VENV_SITE) not in sys.path:
    sys.path.insert(0, str(_VENV_SITE))


def _load_corpus_from_stdin() -> list[dict]:
    data = json.loads(sys.stdin.read())
    return data.get("rows", [])


def _should_skip(row: dict) -> tuple[bool, str]:
    """Return (skip?, reason)."""
    cat = row.get("category", "")
    if cat == "adversarial":
        return True, "adversarial — offline-only"
    if "qb_mock_action" in row:
        return True, "phantom action — offline-only"
    if row.get("expected_action") == "system.unsupported":
        return True, "unsupported-routing — offline-only"
    return False, ""


def _run_row(client, row: dict) -> dict:
    """Send one turn, return a JSONL-ready dict."""
    row_id = row.get("id", "<no-id>")
    query = row.get("query", "")
    context = row.get("context")
    expected = row.get("expected_outcome", "")
    t0 = time.monotonic()
    try:
        envelope = client.run_turn(query, context=context)
    except Exception as exc:
        elapsed = (time.monotonic() - t0) * 1000
        return {
            "id": row_id,
            "status": "error",
            "expected_outcome": expected,
            "actual_outcome": None,
            "success": False,
            "elapsed_ms": round(elapsed, 1),
            "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    elapsed = (time.monotonic() - t0) * 1000
    # ``DaemonClient.send_request`` returns the raw JSON-RPC envelope:
    #   {"jsonrpc": "2.0", "id": ..., "result": {...outcome, success, ...}}
    #     OR
    #   {"jsonrpc": "2.0", "id": ..., "error": {"code": ..., "message": ...}}
    # Unwrap either shape into a caller-friendly (actual, success, err) tuple.
    if isinstance(envelope, dict) and "result" in envelope and isinstance(envelope["result"], dict):
        result = envelope["result"]
        actual = result.get("outcome")
        success = result.get("success")
        err_msg = None
    elif isinstance(envelope, dict) and "error" in envelope:
        actual = None
        success = False
        err_msg = envelope["error"].get("message", "unknown JSON-RPC error")[:300]
    else:
        actual = None
        success = False
        err_msg = f"unexpected daemon response shape: {type(envelope).__name__}"
    matched = actual == expected
    if err_msg is not None and not matched:
        # Daemon raised — surface the message alongside the outcome mismatch
        # so the operator sees WHY the daemon rejected (F-43/F-52 shape etc.).
        reason = f"expected={expected!r} actual={actual!r} daemon_error={err_msg!r}"
    elif matched:
        reason = None
    else:
        reason = f"expected={expected!r} actual={actual!r}"
    return {
        "id": row_id,
        "status": "ok" if matched else ("error" if err_msg else "outcome_mismatch"),
        "expected_outcome": expected,
        "actual_outcome": actual,
        "success": success,
        "elapsed_ms": round(elapsed, 1),
        "reason": reason,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--sock",
        default="/run/icebreaker/controller.sock",
        help="Path to the Icebreaker daemon UNIX socket "
             "(default: /run/icebreaker/controller.sock)",
    )
    ap.add_argument(
        "--only-category",
        action="append",
        default=None,
        help="Only run rows whose category matches (repeatable). "
             "Skip logic still applies to adversarial / qb_mock_action.",
    )
    ap.add_argument(
        "--turn-timeout",
        type=float,
        default=600.0,
        help="Per-turn timeout in seconds (default: 600). Matches the "
             "daemon's cfg.run.turn_timeout_seconds ceiling.",
    )
    args = ap.parse_args()

    if not Path(args.sock).exists():
        print(
            json.dumps({
                "id": "<runner>",
                "status": "error",
                "reason": f"daemon socket not found at {args.sock}",
            }),
            flush=True,
        )
        return 1

    try:
        from controller.client import DaemonClient
    except ImportError as exc:
        print(
            json.dumps({
                "id": "<runner>",
                "status": "error",
                "reason": (
                    f"cannot import controller.client: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }),
            flush=True,
        )
        return 1

    # BP-2 backward-compat: the DaemonClient constructor gained
    # `turn_timeout_seconds` / `reader_recv_timeout_seconds` /
    # `max_reconnect_delay_seconds` in Phase 6 Scope B. G24 must work
    # against ISOs that predate Scope B — the whole point is verifying
    # ALREADY-SHIPPED artifacts, not the current tree. Introspect and
    # pass only what the target's signature accepts.
    import inspect
    _sig = inspect.signature(DaemonClient.__init__)
    _client_kwargs: dict = {}
    if "turn_timeout_seconds" in _sig.parameters:
        _client_kwargs["turn_timeout_seconds"] = args.turn_timeout

    try:
        rows = _load_corpus_from_stdin()
    except json.JSONDecodeError as exc:
        print(
            json.dumps({
                "id": "<runner>",
                "status": "error",
                "reason": f"corpus JSON parse failed: {exc}",
            }),
            flush=True,
        )
        return 1

    if not rows:
        print(
            json.dumps({
                "id": "<runner>",
                "status": "error",
                "reason": "corpus is empty",
            }),
            flush=True,
        )
        return 1

    client = DaemonClient(args.sock, **_client_kwargs)
    try:
        client.connect()
    except (OSError, ConnectionError) as exc:
        print(
            json.dumps({
                "id": "<runner>",
                "status": "error",
                "reason": (
                    f"connect to {args.sock}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }),
            flush=True,
        )
        return 1

    failed = 0
    only_cats = set(args.only_category) if args.only_category else None
    try:
        for row in rows:
            if only_cats and row.get("category") not in only_cats:
                continue
            skip, reason = _should_skip(row)
            if skip:
                print(
                    json.dumps({
                        "id": row.get("id", "<no-id>"),
                        "status": "skipped",
                        "reason": reason,
                    }),
                    flush=True,
                )
                continue
            out = _run_row(client, row)
            print(json.dumps(out), flush=True)
            if out["status"] != "ok":
                failed += 1
    finally:
        client.close()
    return failed


if __name__ == "__main__":
    sys.exit(main())
