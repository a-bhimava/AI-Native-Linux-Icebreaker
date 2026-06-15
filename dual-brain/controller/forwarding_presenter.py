"""ForwardingPresenter — serializes HITL prompts over JSON-RPC to a remote client.

Self-contained: implements HitlPresenter ABC directly with no dependency on the
presenter registry (which lives in a separate PR). Called from the daemon's turn
worker thread; responses arrive from the connection handler thread.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from .hitl import Decision, HitlDisplayData, HitlPresenter
from .protocol import JsonRpcNotification
from .transport import Transport, TransportClosed


class ForwardingPresenter(HitlPresenter):
    """Forwards HITL approval prompts to a remote client over JSON-RPC.

    Thread safety: the pipeline worker thread calls show_prompt/lockout/
    read_decision. The connection handler thread calls receive_decision
    when the client sends hitl.respond. A threading.Event bridges them.
    """

    __slots__ = (
        "_transport", "_decision_event", "_pending_decision",
        "_last_key_class", "_lock",
    )

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._decision_event = threading.Event()
        self._pending_decision: Decision | None = None
        self._last_key_class: str = ""
        self._lock = threading.Lock()

    @property
    def last_key_class(self) -> str:
        return self._last_key_class

    def pre_check(self) -> Optional[Decision]:
        if not self._transport.is_open():
            self._last_key_class = "transport_closed"
            return Decision.DENIED
        return None

    def show_prompt(self, data: HitlDisplayData) -> None:
        payload = {
            "action": data.action,
            "target": data.target,
            "tier": int(data.tier),
            "risk_level": data.risk_level,
            "reversible": data.reversible,
            "backend": data.backend,
            "reason": data.reason,
            "blocked_pattern": data.blocked_pattern,
            "cow_summary": data.cow_summary,
        }
        notif = JsonRpcNotification("hitl.prompt", payload)
        try:
            self._transport.send(notif.to_bytes())
        except TransportClosed:
            pass

    def lockout(self, seconds: int) -> None:
        notif = JsonRpcNotification("hitl.lockout", {"seconds": seconds})
        try:
            self._transport.send(notif.to_bytes())
        except TransportClosed:
            pass
        time.sleep(seconds)

    def read_decision(self, timeout_seconds: int) -> Decision:
        self._decision_event.clear()
        with self._lock:
            self._pending_decision = None

        if not self._transport.is_open():
            self._last_key_class = "transport_closed"
            return Decision.DENIED

        if not self._decision_event.wait(timeout=timeout_seconds):
            self._last_key_class = "timeout"
            return Decision.TIMEOUT

        with self._lock:
            decision = self._pending_decision
        if decision is None:
            self._last_key_class = "transport_closed"
            return Decision.DENIED
        self._last_key_class = "remote"
        return decision

    def receive_decision(self, decision: Decision) -> None:
        """Called by the connection handler thread when hitl.respond arrives."""
        with self._lock:
            self._pending_decision = decision
        self._decision_event.set()
