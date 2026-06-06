#!/usr/bin/env python3
"""
03_schema_parity.py — every mcpd tool name MUST be accepted by the Intent
Object schema's `action` regex.

Why this exists: in M2.2 we caught the schema rejecting `network.dns.read`
(two dots) because the original action regex only allowed one dot. The fix
landed but there is no automated regression test asserting "every tool
mcpd ships is a valid `action` value." This script is that test —
parameterised over `_mcpd_tools.ALL_TOOLS`.

It also asserts the inverse: a handful of hand-crafted bogus action
strings are rejected. Cheap belt-and-suspenders.

Exit code:
  0  all 22 tool names accepted + all bogus actions rejected
  1  any failure (with a one-line per-tool summary)

Run from anywhere; the script sys.path-mounts dual-brain/ so it works
both from `dbtests/` and from the repo root.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

# Mount dual-brain/ so `from controller.X import Y` works regardless of CWD.
_HERE = Path(__file__).resolve().parent
_DUAL_BRAIN = _HERE.parent
sys.path.insert(0, str(_DUAL_BRAIN))

from controller._mcpd_tools import ALL_TOOLS  # noqa: E402
from controller.intent_schema import IntentValidationError, validate  # noqa: E402


# Match what the schema demands for any valid Intent Object — only `action`
# varies across iterations of this check.
_TEMPLATE = {
    "intent_id": "550e8400-e29b-41d4-a716-446655440000",
    "action": "PLACEHOLDER",
    "target": "",
    "params": {},
    "reason": "user_requested",
    "risk_level": "read_only",
}


# Bogus action strings that MUST be rejected. If any of these slip through,
# the schema regex has been weakened and `system.cpu; rm -rf /` style
# names could reach mcpd.
_BOGUS_ACTIONS = [
    ("empty string",       ""),
    ("no dot",              "bogus"),
    ("uppercase",          "Bad.Tool"),
    ("double dot",         "a..b"),
    ("just dots",          ".."),
    ("trailing dot",       "system.status."),
    ("leading dot",        ".system.status"),
    ("metachar in action", "fs.read;rm"),
    ("space",              "fs read"),
    ("starts with digit",  "1system.status"),
]


# ANSI colours — degrade to plain when stdout isn't a TTY.
def _colour(code: str) -> str:
    return code if sys.stdout.isatty() else ""

GRN = _colour("\033[32m")
RED = _colour("\033[31m")
YLW = _colour("\033[33m")
CYN = _colour("\033[36m")
DIM = _colour("\033[2m")
RST = _colour("\033[0m")


def main() -> int:
    print(f"{CYN}03_schema_parity{RST} — every mcpd tool name accepted by intent schema")
    print(f"{DIM}schemas/intent.json action pattern × {len(ALL_TOOLS)} mcpd tools{RST}")
    print()

    failures: list[str] = []

    # Positive: every mcpd tool name accepted.
    for tool in sorted(ALL_TOOLS):
        candidate = dict(_TEMPLATE)
        candidate["action"] = tool
        try:
            validate(candidate)
            print(f"  {GRN}OK  {RST} {tool}")
        except IntentValidationError as e:
            print(f"  {RED}FAIL{RST} {tool}  ({e.error_type} at {e.field_path}: {e.message})")
            failures.append(f"tool '{tool}' rejected by schema: {e.message}")

    print()

    # Negative: bogus actions must be rejected.
    print(f"{CYN}Negative cases — these MUST be rejected{RST}")
    for label, bogus in _BOGUS_ACTIONS:
        candidate = dict(_TEMPLATE)
        candidate["action"] = bogus
        try:
            validate(candidate)
            print(f"  {RED}FAIL{RST} {label!r}: {bogus!r}  (schema accepted bogus action — regex is too loose)")
            failures.append(f"bogus action {bogus!r} ({label}) was accepted")
        except IntentValidationError:
            print(f"  {GRN}OK  {RST} {label}: {bogus!r}  rejected")

    print()
    if failures:
        print(f"{RED}FAIL{RST}  {len(failures)} parity violation(s)")
        for f in failures:
            print(f"  - {f}")
        return 1

    total = len(ALL_TOOLS) + len(_BOGUS_ACTIONS)
    print(f"{GRN}PASS{RST}  {len(ALL_TOOLS)}/{len(ALL_TOOLS)} mcpd tool names accepted, "
          f"{len(_BOGUS_ACTIONS)}/{len(_BOGUS_ACTIONS)} bogus actions rejected "
          f"({total} checks total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
