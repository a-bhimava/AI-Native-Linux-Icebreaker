"""SessionState — multi-turn QB conversation memory with INV-2-extended enforcement.

INV-2: build_pb_user_turn() emits ONLY {intent_id, allowed_tool, tool_schema}.
       Never raw user text, never intent fields.
INV-2-extended: add_tool_result_summary() is the ONLY path for mcpd output into
       QB context. The summary is never forwarded to PB.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from .undo import UndoEntry, UndoHistory


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

    def build_pb_user_turn(
        self,
        intent_id: str,
        allowed_tool: str,
        tool_schema: dict,
        target: str = "",
    ) -> str:
        """F-27: PB receives intent_id + tool scaffold + the VALIDATED target
        from the intent. Never raw user text. Per INV-1, the schema-validated
        Intent Object (which includes target) flows to PB; only free-form user
        text is forbidden. Without target PB has to invent params from nothing
        and consistently hallucinates /tmp regardless of what the user asked."""
        payload = {
            "intent_id": intent_id,
            "allowed_tool": allowed_tool,
            "tool_schema": tool_schema,
        }
        if target:
            payload["target"] = target
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
