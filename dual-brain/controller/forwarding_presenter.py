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
import os
import threading
import time
from typing import Optional

from .hitl import Decision, HitlDisplayData, HitlPresenter
from .protocol import JsonRpcNotification
from .transport import Transport, TransportClosed

_log = logging.getLogger("controller.forwarding_presenter")


_HDIAG_PATH = "/tmp/hitl-diag.log"


def _hdiag(msg: str) -> None:
    """Write a HITL-DIAG line to /tmp/hitl-diag.log with timestamp.
    Bypasses the Python logging module entirely so the line survives
    stderr redirects, Textual's ANSI output, systemd journal filters,
    or anything else that eats stderr.

    v10.2 (2026-07-23): daemon runs as root/icebreaker system user;
    terminal client runs as icebreaker desktop user. If the daemon
    creates the file first with a restrictive umask, the terminal
    cannot append and all client-side HITL-DIAG lines vanish silently
    — this masked the entire key-routing debug for an evening. Fix:
    chmod 0666 after open so both users can append. Best-effort —
    never raises."""
    try:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [presenter pid={os.getpid()}] {msg}\n"
        existed = os.path.exists(_HDIAG_PATH)
        try:
            with open(_HDIAG_PATH, "a") as f:
                f.write(line)
            if not existed:
                try:
                    os.chmod(_HDIAG_PATH, 0o666)
                except Exception:
                    pass
        except PermissionError:
            try:
                os.chmod(_HDIAG_PATH, 0o666)
                with open(_HDIAG_PATH, "a") as f:
                    f.write(line)
            except Exception:
                pass
    except Exception:
        pass
    try:
        _log.info(msg)
    except Exception:
        pass


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
        _hdiag(
            f"HITL-DIAG show_prompt: action={data.action} tier={int(data.tier)} "
            f"transport_open={self._transport.is_open()}"
        )
        try:
            self._transport.send(notif.to_bytes())
            _hdiag("HITL-DIAG show_prompt: notification sent to client")
        except TransportClosed:
            _hdiag("HITL-DIAG show_prompt: transport CLOSED — client gone; deny will follow")

    def lockout(self, seconds: int) -> None:
        notif = JsonRpcNotification("hitl.lockout", {"seconds": seconds})
        try:
            self._transport.send(notif.to_bytes())
        except TransportClosed:
            pass
        time.sleep(seconds)

    def read_decision(self, timeout_seconds: int) -> Decision:
        _hdiag(f"HITL-DIAG read_decision: BEGIN timeout={timeout_seconds}s")
        self._decision_event.clear()
        with self._lock:
            self._pending_decision = None

        if not self._transport.is_open():
            self._last_key_class = "transport_closed"
            _hdiag("HITL-DIAG read_decision: transport CLOSED at entry → DENIED (immediate)")
            return Decision.DENIED

        t_wait_start = time.monotonic()
        got_event = self._decision_event.wait(timeout=timeout_seconds)
        elapsed = time.monotonic() - t_wait_start
        _hdiag(f"HITL-DIAG read_decision: wait returned got_event={got_event} elapsed={elapsed:.1f}s")

        if not got_event:
            self._last_key_class = "timeout"
            _hdiag(f"HITL-DIAG read_decision: TIMEOUT after {elapsed:.1f}s — client never called hitl.respond")
            return Decision.TIMEOUT

        with self._lock:
            decision = self._pending_decision
        if decision is None:
            self._last_key_class = "transport_closed"
            _hdiag("HITL-DIAG read_decision: event set but pending_decision is None → DENIED")
            return Decision.DENIED
        self._last_key_class = "remote"
        _hdiag(f"HITL-DIAG read_decision: returning decision={decision.value}")
        return decision

    def receive_decision(self, decision: Decision) -> None:
        """Called by the connection handler thread when hitl.respond arrives."""
        _hdiag(f"HITL-DIAG receive_decision: client sent decision={decision.value}")
        with self._lock:
            self._pending_decision = decision
        self._decision_event.set()
