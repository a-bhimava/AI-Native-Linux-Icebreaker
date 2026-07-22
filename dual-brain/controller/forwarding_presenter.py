"""ForwardingPresenter — serializes HITL prompts over JSON-RPC to a remote client.

Self-contained: implements HitlPresenter ABC directly with no dependency on the
presenter registry (which lives in a separate PR). Called from the daemon's turn
worker thread; responses arrive from the connection handler thread.

v6.12 HITL-DIAG (2026-07-22): every hop of the HITL flow now logs INFO to the
controller journal so the exact broken step is visible via
``journalctl -u icebreaker-controller | grep HITL-DIAG``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .hitl import Decision, HitlDisplayData, HitlPresenter
from .protocol import JsonRpcNotification
from .transport import Transport, TransportClosed

_log = logging.getLogger("controller.forwarding_presenter")


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
        _log.info(
            "HITL-DIAG show_prompt: action=%s tier=%d transport_open=%s",
            data.action, int(data.tier), self._transport.is_open(),
        )
        try:
            self._transport.send(notif.to_bytes())
            _log.info("HITL-DIAG show_prompt: notification sent to client")
        except TransportClosed:
            _log.warning("HITL-DIAG show_prompt: transport CLOSED — client gone; deny will follow")

    def lockout(self, seconds: int) -> None:
        notif = JsonRpcNotification("hitl.lockout", {"seconds": seconds})
        try:
            self._transport.send(notif.to_bytes())
        except TransportClosed:
            pass
        time.sleep(seconds)

    def read_decision(self, timeout_seconds: int) -> Decision:
        _log.info("HITL-DIAG read_decision: BEGIN timeout=%ds", timeout_seconds)
        self._decision_event.clear()
        with self._lock:
            self._pending_decision = None

        if not self._transport.is_open():
            self._last_key_class = "transport_closed"
            _log.warning("HITL-DIAG read_decision: transport CLOSED at entry → DENIED (immediate)")
            return Decision.DENIED

        t_wait_start = time.monotonic()
        got_event = self._decision_event.wait(timeout=timeout_seconds)
        elapsed = time.monotonic() - t_wait_start
        _log.info("HITL-DIAG read_decision: wait returned got_event=%s elapsed=%.1fs", got_event, elapsed)

        if not got_event:
            self._last_key_class = "timeout"
            _log.warning("HITL-DIAG read_decision: TIMEOUT after %.1fs — client never called hitl.respond", elapsed)
            return Decision.TIMEOUT

        with self._lock:
            decision = self._pending_decision
        if decision is None:
            self._last_key_class = "transport_closed"
            _log.warning("HITL-DIAG read_decision: event set but pending_decision is None → DENIED")
            return Decision.DENIED
        self._last_key_class = "remote"
        _log.info("HITL-DIAG read_decision: returning decision=%s", decision.value)
        return decision

    def receive_decision(self, decision: Decision) -> None:
        """Called by the connection handler thread when hitl.respond arrives."""
        _log.info("HITL-DIAG receive_decision: client sent decision=%s", decision.value)
        with self._lock:
            self._pending_decision = decision
        self._decision_event.set()
