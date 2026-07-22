"""v6.8 M7.2 — Tier-0 fast path.

For read-only intents (Tier 0), the intent → mcp_tool_call mapping is
deterministic: PB's grammar-constrained decoding does not add information
that isn't already in the QB-emitted Intent. Skipping PB removes a
~1-2 second HTTP round-trip per turn and lets 'list Downloads' feel like
'list Downloads', not 'list Downloads … after some thought'.

Security preservation:
- INV-1 unchanged: PB is the intermediary for Tier 1+. Fast path only
  applies to Tier 0 (sandbox-contained, cannot mutate state).
- INV-2 preserved: the constructed tool_call still goes through
  main._validate_tool_call() against the mcpd tool schema before
  dispatch. Fast path does NOT bypass validation.
- INV-5 preserved: mcpd's Landlock + Seccomp still fires on every call.
- Verifier bypass: already handled by M7.1 tier_floor for Tier 0/1.

Design:
- TIER0_FAST_PATH_TOOLS is a mapping of action → intent-to-params function.
- Only the subset of Tier-0 tools with an unambiguous intent → tool_call
  translation is included. Tools whose parameters aren't in a single
  intent field fall through to the normal PB path (safe default).
- The classifier owns the tier label; this module trusts it and does
  not re-classify.
- Callers use try_fast_path(intent, tier) to opt in; the caller decides
  whether the config flag is on.

Contract: see docs/IMPLEMENTATION_PLAN_v6.8_2026-07-13.md §4.3 M7.2.
"""

from __future__ import annotations

from typing import Callable, Optional


# Intent → params dict for Tier-0 tools. Each callable takes the validated
# intent dict and returns the params dict shape that mcpd expects for that
# tool. Tools are grouped by shape.
TIER0_FAST_PATH_TOOLS: dict[str, Callable[[dict], dict]] = {
    # ── No-args tools: params = {} ────────────────────────────────────
    "system.status":    lambda intent: {},
    "system.uptime":    lambda intent: {},
    "system.cpu":       lambda intent: {},
    "system.memory":    lambda intent: {},
    "system.disk":      lambda intent: {},
    "process.list":     lambda intent: {},
    "network.status":   lambda intent: {},
    # ── Path-based: intent.target -> params.path ──────────────────────
    "fs.list":          lambda intent: {"path": intent.get("target", "")},
    "fs.read":          lambda intent: {"path": intent.get("target", "")},
    "fs.stat":          lambda intent: {"path": intent.get("target", "")},
    # v6.9 Scope O Layer 2 Part A: manifest-served tools that live
    # inside the controller (no PB round-trip needed to translate).
    # nav.cd = update session cwd; Tier 0 (no OS mutation).
    "nav.cd":           lambda intent: {"path": intent.get("target", "")},
    # ── Named-target string args ──────────────────────────────────────
    "service.logs":     lambda intent: {"service": intent.get("target", "")},
    "network.dns.read": lambda intent: {"hostname": intent.get("target", "")},
    "package.query":    lambda intent: {"name": intent.get("target", "")},
    # ── Int-target (pid) — validated below to avoid ValueError leak ──
    "process.inspect":  lambda intent: {"pid": _target_as_pid(intent)},
    # ── v6.12 Fix I follow-up: Tier-0 GUI Agent + RPA Bridge tools ──
    # These are read-only Phase 6T tools that PB was never trained on
    # (Qwen fine-tuning corpus was fs.* / system.* only). Without
    # fast-path handling, PB free-forms and produces garbage params
    # like {"host": "8.8.8.8"} for gui.ping — the tool then rejects
    # via handle_request's schema validation. Fast-path skips PB
    # entirely and constructs the correct empty / target-based
    # params directly.
    "gui.ping":              lambda intent: {},
    "rpa.ping":              lambda intent: {},
    "gui.get_window_list":   lambda intent: {},
    "gui.screenshot":        lambda intent: (
        {"window": intent.get("target", "")} if intent.get("target") else {}
    ),
    "gui.get_element_tree":  lambda intent: {"window": intent.get("target", "")},
    "gui.find_element":      lambda intent: {"window": intent.get("target", "")},
    "rpa.list_workflows":    lambda intent: {},
    "rpa.find_by_image":     lambda intent: {"template": intent.get("target", "")},
}


def _target_as_pid(intent: dict) -> int:
    """Coerce intent.target to an int PID; empty/invalid target → 0
    (the resulting tool_call fails schema validation, main.py surfaces
    the error to the user via the normal Outcome.PB_SCHEMA_ERROR path).
    Never raises — fast path must be silent on translation failures."""
    raw = str(intent.get("target", "")).strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


def try_fast_path(intent: dict, tier: int) -> Optional[dict]:
    """Return the constructed tool_call dict if this intent qualifies
    for the Tier-0 fast path, else None (caller must fall through to PB).

    Qualification criteria (all must hold):
    1. tier == 0 (Tier.READ_ONLY). Sandbox contains blast radius.
    2. intent['action'] is in TIER0_FAST_PATH_TOOLS. Tools with
       ambiguous parameter shapes fall through — safer.
    3. intent has a well-formed 'action' field. Missing action means
       something upstream is broken; do not paper over it.

    The returned tool_call has the shape mcpd expects:
        {"tool": "<action>", "params": {...}}

    The caller MUST run this through main._validate_tool_call() before
    dispatching. This module never bypasses INV-2.
    """
    action = intent.get("action")
    if not action or not isinstance(action, str):
        return None

    # v6.12 Fix J (F-90 2026-07-22): fs.write content passthrough —
    # bypass PB for ALL tiers when intent has verbatim content bytes.
    # QB (Gemini) generates the bytes into `intent.content` per the
    # v6.61 F-41 passthrough pattern; PB (local Qwen) has no work to
    # do except re-emit those same bytes inside its output JSON, which
    # exceeds PB's output token budget on any non-trivial content and
    # deterministically truncates ("BrainTruncationError: JSON decode
    # failure: unterminated object"). Live-verified on UTM Stage E:
    # `# create a html page…` failed after 78s.
    #
    # Safety invariants preserved:
    #  - INV-2 (schema): the constructed tool_call still runs through
    #    validate_resolved_intent in agent_graph_nodes.py before
    #    executor marks it valid.
    #  - INV-6 (COW): tier ≥ 2 fs.write still routes through hitl_gate
    #    → mcpd, and mcpd's fs.write handler still applies COW.
    #  - INV-8 (audit): unchanged — audit_writer_node fires at end.
    #
    # General principle (extends R16): PB should only be called when
    # it has real work to do. When the tool_call is derivable from
    # intent + a shape mapping, skip PB — cheaper AND more reliable.
    if action == "fs.write" and str(intent.get("content", "") or ""):
        return {
            "tool": "fs.write",
            "params": {
                "path": intent.get("target", ""),
                "content": intent["content"],
            },
        }

    if tier != 0:
        return None
    mapper = TIER0_FAST_PATH_TOOLS.get(action)
    if mapper is None:
        return None
    try:
        params = mapper(intent)
    except Exception:  # noqa: BLE001 — mapper failures fall through
        # Any unexpected mapper failure → fall through to PB. Fast path
        # must never surface an error to the user; PB handles it.
        return None
    return {"tool": action, "params": params}
