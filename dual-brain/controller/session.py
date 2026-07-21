"""SessionState — multi-turn QB conversation memory with INV-2-extended enforcement.

INV-2: build_pb_user_turn() emits ONLY {intent_id, allowed_tool, tool_schema}.
       Never raw user text, never intent fields.
INV-2-extended: add_tool_result_summary() is the ONLY path for mcpd output into
       QB context. The summary is never forwarded to PB.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from .undo import UndoEntry, UndoHistory


# ── V6B Stage 2: ShellContext ─────────────────────────────────────────────
# The Terminal collects the user's shell state each turn (cwd, recent
# commands, current window title) and passes it in the run_turn RPC.
# The Controller renders it as an XML-tagged preamble in front of the
# user's query so QB can resolve ambiguous references like "here", "this
# folder", "the file I was editing" — see prompts/qb_*.txt Context Usage.
# INV-2 stance: fields ARE user data but validated & sanitized here before
# reaching QB — controls stripped, length capped, no unclosed XML tags.

_CONTEXT_STRIP = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Phase 6 Scope B: these used to be authoritative module constants; now
# they are just defaults for code paths that don't know about SessionConfig
# (e.g. bare unit tests). The real ceilings come from
# cfg.session.max_shell_context_chars / max_recent_commands and flow in
# through explicit kwargs. Keep the module constants as documentation of
# the safe defaults (BP-2 backward-compat).
_MAX_CONTEXT_LEN = 512
_MAX_RECENT = 5


def _clean(s: str, limit: int = _MAX_CONTEXT_LEN) -> str:
    """Strip control chars, neutralize XML delimiters, cap length."""
    if not isinstance(s, str):
        return ""
    out = _CONTEXT_STRIP.sub("", s)
    # Prevent the model from seeing a closing </context> inside a value
    # that would let a malicious cwd escape the block. Escape < and >.
    out = out.replace("<", "&lt;").replace(">", "&gt;")
    if len(out) > limit:
        out = out[:limit] + "…"
    return out


@dataclass
class ShellContext:
    """Environmental context captured by the Terminal on each turn.

    Populated by the Terminal on every run_turn RPC. Rendered by the
    Controller into a <context> preamble before the user's <query>.
    See prompts/qb_*.txt for how QB is instructed to use it.
    """
    cwd: str = ""
    recent_commands: list = field(default_factory=list)
    user: str = ""
    active_window: str = ""
    hostname: str = ""

    def render(
        self,
        *,
        max_chars: int = _MAX_CONTEXT_LEN,
        max_recent: int = _MAX_RECENT,
        recent_turns_block: str = "",
        override_cwd: str = "",
    ) -> str:
        """Return the XML preamble string. Empty when no fields are populated.

        ``max_chars`` and ``max_recent`` come from
        ``cfg.session.max_shell_context_chars`` /
        ``cfg.session.max_recent_commands`` (Phase 6 Scope B). Defaults
        preserve pre-Scope-B behavior for callers that don't pass them.

        ``recent_turns_block`` (v6.9 Task #149 shipping-scope 2026-07-17):
        pre-rendered multi-line string with prior-turn user queries +
        tool result summaries. Caller (main.py::_build_qb_input) computes
        it from ``SessionState.render_recent_turns()``. Empty string
        disables it — old behavior preserved.

        ``override_cwd`` (v6.12 Fix D 2026-07-21): when non-empty, use it
        as the ``cwd`` field instead of ``self.cwd``. This wires
        ``SessionState.session_cwd`` (mutated by the nav.cd manifest at
        ``session.py:189-195``) into the QB prompt — nav.cd's whole
        promise is that subsequent turns resolve "here" / bare filenames
        against the last navigated dir, but without this override the
        prompt always got the per-turn shell ``cwd`` from the RPC and
        session_cwd was a dead field.
        """
        parts = ["<context>"]
        any_field = False
        effective_cwd = override_cwd if override_cwd else self.cwd
        if effective_cwd:
            parts.append(f"cwd: {_clean(effective_cwd, max_chars)}")
            any_field = True
        if self.user:
            # user/hostname stay capped at 64 — they're identifiers, not
            # narrative content. Config controls the narrative caps only.
            parts.append(f"user: {_clean(self.user, 64)}")
            any_field = True
        if self.hostname:
            parts.append(f"hostname: {_clean(self.hostname, 64)}")
            any_field = True
        if self.recent_commands:
            cmds = [c for c in self.recent_commands if isinstance(c, str) and c.strip()]
            if cmds:
                parts.append("recent_commands:")
                for c in cmds[-max_recent:]:
                    parts.append(f"  - {_clean(c, max_chars)}")
                any_field = True
        if self.active_window:
            parts.append(f"active_window: {_clean(self.active_window, max_chars)}")
            any_field = True
        if recent_turns_block.strip():
            # Insert as-is — caller has already sanitized + capped.
            parts.append(recent_turns_block.rstrip())
            any_field = True
        parts.append("</context>")
        return "\n".join(parts) if any_field else ""

    @classmethod
    def from_params(
        cls,
        params: Optional[dict],
        *,
        max_recent: int = _MAX_RECENT,
    ) -> "ShellContext":
        """Build from a JSON-RPC context dict. Returns empty context on None.

        ``max_recent`` slices the incoming ``recent_commands`` list — the
        caller passes ``cfg.session.max_recent_commands`` when driven by
        the daemon, or falls through to the module default in tests.
        """
        if not isinstance(params, dict):
            return cls()
        rc = params.get("recent_commands", [])
        if not isinstance(rc, list):
            rc = []
        return cls(
            cwd=str(params.get("cwd", "") or ""),
            recent_commands=[str(x) for x in rc[-max_recent:] if x],
            user=str(params.get("user", "") or ""),
            active_window=str(params.get("active_window", "") or ""),
            hostname=str(params.get("hostname", "") or ""),
        )


@dataclass(frozen=True)
class TurnMemory:
    """v6.8 M7.5 placeholder — per-turn record for follow-up context (F-62).

    Stored REFERENCES, not raw content (§13.3): the audit log holds the
    full text; TurnMemory holds hashes + a redacted summary so the graph
    state can carry it forward without swelling checkpoints or leaking
    PB output through the QB context on replanner turns.

    Fields defined here so the shape is locked in Task #145; population
    happens in Task #149 (M7.5) when the Replanner node lands.
    """

    turn_id: str
    session_id: str
    query_hash: str
    intent_action: str
    result_hash: str
    result_summary: str          # ≤ 200 chars, BP-8 redacted
    outcome: str                 # "executed" | "denied" | "unsupported" | "error"
    tier: int
    timestamp_utc: str           # ISO 8601


@dataclass
class SessionState:
    session_id: str
    backend: str
    cfg: Any                                                    # SessionConfig
    turn_index: int = 0
    _last_activity: float = field(init=False, default=0.0, repr=False)
    _qb_messages: list = field(init=False, default_factory=list, repr=False)
    _accumulated_cost_usd: float = field(init=False, default=0.0, repr=False)
    _turn_timestamps: list = field(init=False, default_factory=list, repr=False)
    _undo_history: UndoHistory = field(init=False, default_factory=UndoHistory, repr=False)
    # V6B Stage 2: latest shell context from the current turn's RPC.
    # Set by daemon._handle_turn_run before entering run_turn_streaming;
    # consumed at the QB call site in main.py to build the <context> preamble.
    shell_context: ShellContext = field(init=False, default_factory=lambda: ShellContext(), repr=False)
    # v6.8 M7.5 placeholder — populated by the Replanner node once the
    # LangGraph adapter lands (Task #149). Stays empty until then;
    # existing code paths that don't read history are unaffected.
    history: list = field(init=False, default_factory=list, repr=False)
    # v6.9 Scope O Layer 2 Part A — persistent per-session working
    # directory mutated by the nav.cd manifest. When empty the QB prompt
    # falls back to shell_context.cwd (the per-turn terminal state).
    # When non-empty it persists across turns and survives daemon
    # restarts only if session_id is stable (fresh session = fresh cwd).
    # F-60 permanent fix — replaces the v6.8 nav-phrase -> fs.list
    # workaround. See controller/manifests/nav.cd.yaml.
    session_cwd: str = field(init=False, default="", repr=False)

    def __post_init__(self) -> None:
        self._last_activity = time.monotonic()
        try:
            undo_cfg = getattr(self.cfg, "undo", None) if self.cfg else None
            max_h = int(getattr(undo_cfg, "max_history", 0)) if undo_cfg else 0
            if max_h > 0:
                self._undo_history = UndoHistory(max_depth=max_h)
        except (TypeError, ValueError):
            pass

    def is_expired(self) -> bool:
        ttl = self.cfg.session_ttl_seconds
        return False if ttl <= 0 else (time.monotonic() - self._last_activity) > ttl

    def is_full(self) -> bool:
        return self.turn_index >= self.cfg.max_turns

    def touch(self) -> None:
        self._last_activity = time.monotonic()
        self.turn_index += 1

    def add_user_message(self, text: str) -> None:
        self._qb_messages.append({"role": "user", "content": text})

    def add_assistant_message(self, text: str) -> None:
        self._qb_messages.append({"role": "assistant", "content": text})

    def add_tool_result_summary(self, summary: str) -> None:
        """INV-2-extended: mcpd output enters QB context as a summary only. Never PB."""
        self._qb_messages.append({
            "role": "user",
            "content": f"[Tool output summary]: {summary}",
        })

    def get_qb_history(self) -> list:
        return [dict(msg) for msg in self._qb_messages]

    def render_recent_turns(self, max_turns: int = 3, max_chars: int = 800) -> str:
        """v6.9 Task #149 shipping-scope (2026-07-17) — render prior turns
        as a `recent_turns:` block for the QB <context> preamble.

        Pairs each user query with its immediately-following tool result
        summary (recorded via ``add_tool_result_summary``). Skips the
        synthetic assistant messages that just carry raw intent JSON —
        those are noise for QB when it's trying to resolve pronouns like
        "it" or "the previous result".

        Format:
            recent_turns:
              T-1 query: what is my IP address
                   result: Your IP is 192.168.64.31
              T-2 query: show my cpu
                   result: The CPU usage is 0.0%

        Ordering: most-recent turn is T-1 (closest to the current query).
        Total block trimmed to ``max_chars`` to bound prompt cost. Empty
        string when no complete (query, result) pair is available yet."""
        if max_turns <= 0 or not self._qb_messages:
            return ""
        # Walk backward pairing user queries with the next tool summary.
        # Only complete pairs (query + result) are useful for pronoun
        # resolution — a half-turn (query without result) tells QB
        # nothing new about what "it" refers to.
        pairs: list[tuple[str, str]] = []
        current_query: Optional[str] = None
        for msg in self._qb_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and content.startswith("[Tool output summary]:"):
                if current_query is not None:
                    summary = content[len("[Tool output summary]:"):].lstrip()
                    pairs.append((current_query, summary))
                    current_query = None
            elif role == "user":
                current_query = content
            # assistant messages (raw intent JSON) — skip
        if not pairs:
            return ""
        # Keep the last max_turns pairs, render T-1 = most recent.
        pairs = pairs[-max_turns:]
        parts = ["recent_turns:"]
        for offset, (query, result) in enumerate(reversed(pairs), start=1):
            parts.append(f"  T-{offset} query: {_clean(query, 200)}")
            parts.append(f"       result: {_clean(result, 200)}")
        block = "\n".join(parts)
        if len(block) > max_chars:
            block = block[: max_chars - 1] + "…"
        return block

    def build_pb_user_turn(
        self,
        intent_id: str,
        allowed_tool: str,
        tool_schema: dict,
        target: str = "",
        content: str = "",
        pb_hint: str = "",
    ) -> str:
        """F-27: PB receives intent_id + tool scaffold + the VALIDATED target
        from the intent. Never raw user text. Per INV-1, the schema-validated
        Intent Object (which includes target) flows to PB; only free-form user
        text is forbidden. Without target PB has to invent params from nothing
        and consistently hallucinates /tmp regardless of what the user asked.

        F-41 QB→PB rich envelope: when QB has extracted the file bytes the user
        wants written (fs.write of a new textual/data file) it stores them in
        ``intent.content``. Pass that through to PB as ``expected_content`` so
        PB can copy it verbatim into ``params.content`` — previously PB had to
        invent CSV/JSON sample data because the user's request never reached it,
        yielding the well-known "column of ones → name,age,…" bug.

        F-41 pb_hint: short natural-language coaching from QB naming the exact
        schema field that carries the semantic payload and any format constraints
        (e.g. "put the content in params.content unchanged"). Both fields are
        optional; legacy intents that carry neither work exactly as before.
        """
        payload = {
            "intent_id": intent_id,
            "allowed_tool": allowed_tool,
            "tool_schema": tool_schema,
        }
        if target:
            payload["target"] = target
        if content:
            payload["expected_content"] = content
        if pb_hint:
            payload["pb_hint"] = pb_hint
        return json.dumps(payload, separators=(",", ":"))

    def add_cost(self, usd: float) -> None:
        self._accumulated_cost_usd += usd

    @property
    def accumulated_cost_usd(self) -> float:
        return self._accumulated_cost_usd

    def check_rate_limit(self, max_per_min: int) -> bool:
        """Return True if within rate limit, False if exceeded."""
        if max_per_min <= 0:
            return True
        now = time.monotonic()
        cutoff = now - 60.0
        self._turn_timestamps = [t for t in self._turn_timestamps if t > cutoff]
        if len(self._turn_timestamps) >= max_per_min:
            return False
        self._turn_timestamps.append(now)
        return True

    @property
    def undo_history(self) -> UndoHistory:
        return self._undo_history

    def record_undo_entry(self, entry: UndoEntry) -> None:
        self._undo_history.record(entry)

    def reset_memory(self) -> None:
        self._qb_messages.clear()
        self._undo_history.clear()

    @classmethod
    def new(cls, backend: str, cfg: Any) -> SessionState:
        return cls(session_id=str(uuid.uuid4()), backend=backend, cfg=cfg)
