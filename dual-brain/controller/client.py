"""DaemonClient + ClientRepl — thin client for the Icebreaker Controller daemon.

Connects to a running daemon over AF_UNIX. The client handles terminal UI:
rendering progress, streaming tokens, and driving the HITL approval gate
locally. The daemon owns the expensive resources (QB, PB, mcpd, audit).

Threading model:
  - Main thread: REPL loop (readline → send request → wait for response)
  - Reader thread: continuously reads from transport, routes responses to
    waiting callers via per-ID Events, routes notifications to callbacks
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import DaemonConfig
from .hitl import Decision, HitlDisplayData, HitlPrompt, TerminalPresenter
from .protocol import (
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    is_notification,
    is_request,
    is_response,
    parse_message,
)
from .risk_classifier import Tier
from .transport import Transport, TransportClosed, UnixSocketTransport


class DaemonClient:
    """Connects to a running daemon over AF_UNIX. Base class for notification handling."""

    __slots__ = (
        "_sock_path", "_transport", "_reader_thread",
        "_pending", "_pending_lock", "_closed",
        # Phase 6 Scope A.P1: rate-limit warning about malformed JSON in
        # the reader loop — one message per connection, not per bad packet.
        "_malformed_warned",
        # Phase 6 Scope B: three previously-hardcoded transport knobs.
        # Constructor accepts explicit values (from cfg.run / cfg.daemon
        # at the top-level caller); defaults preserve pre-Scope-B behavior
        # for callers that don't pass them (backward compatibility).
        "_turn_timeout_seconds", "_reader_recv_timeout_seconds",
        "_max_reconnect_delay_seconds",
    )

    def __init__(
        self,
        sock_path: str,
        *,
        turn_timeout_seconds: float = 600.0,
        reader_recv_timeout_seconds: float = 1.0,
        max_reconnect_delay_seconds: float = 30.0,
    ) -> None:
        self._sock_path = sock_path
        self._transport: Transport | None = None
        self._reader_thread: threading.Thread | None = None
        self._pending: dict[str, tuple[threading.Event, list]] = {}
        self._pending_lock = threading.Lock()
        self._closed = False
        self._malformed_warned = False
        self._turn_timeout_seconds = turn_timeout_seconds
        self._reader_recv_timeout_seconds = reader_recv_timeout_seconds
        self._max_reconnect_delay_seconds = max_reconnect_delay_seconds

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._sock_path)
        self._transport = UnixSocketTransport(sock)
        self._closed = False
        # Reset the per-connection warning flag so operators see one
        # warning per fresh connection, not one per client-lifetime.
        self._malformed_warned = False
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="client-reader",
        )
        self._reader_thread.start()

    def close(self) -> None:
        self._closed = True
        if self._transport is not None:
            self._transport.close()
        with self._pending_lock:
            for event, _ in self._pending.values():
                event.set()
            self._pending.clear()

    @property
    def is_connected(self) -> bool:
        return (
            self._transport is not None
            and self._transport.is_open()
            and not self._closed
        )

    # ── Request/response ────────────────────────────────────────────────

    def send_request(
        self, method: str, params: dict | None = None, timeout: float = 30.0,
    ) -> dict:
        if self._transport is None or not self._transport.is_open():
            raise TransportClosed("not connected")
        req = JsonRpcRequest(method=method, params=params or {})
        event = threading.Event()
        result_box: list = []
        with self._pending_lock:
            self._pending[req.id] = (event, result_box)
        self._transport.send(req.to_bytes())
        if not event.wait(timeout=timeout):
            with self._pending_lock:
                self._pending.pop(req.id, None)
            raise TimeoutError(f"no response for {method} within {timeout}s")
        with self._pending_lock:
            self._pending.pop(req.id, None)
        if not result_box:
            raise TransportClosed("connection closed while waiting for response")
        return result_box[0]

    # ── Public API ──────────────────────────────────────────────────────

    def run_turn(
        self,
        user_input: str,
        context: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        # F-25: 600 s so a single turn under emulated x86 (Rosetta 2) has
        # room. Native x86-64 / arm64 completes in seconds; harmless slack.
        # V6B Stage 2: optional context (cwd, recent_commands, active_window)
        # captured by the caller (Terminal / ib_run.py) — rendered by the
        # Controller into a <context> preamble in front of the user query
        # so QB can resolve ambiguous references like "here" or "this folder".
        # Phase 6 Scope B: was a hardcoded 600.0. Now driven by
        # cfg.run.turn_timeout_seconds, passed at construction.
        # v6.9 Bug A (2026-07-17): optional per-call timeout override so
        # the plain shell client (ib_run.py) can use a shorter deadline
        # than the interactive TUI. None → cfg default. Explicit wins.
        params: dict = {"input": user_input}
        if context is not None:
            params["context"] = context
        effective_timeout = (
            timeout if timeout is not None else self._turn_timeout_seconds
        )
        return self.send_request(
            "turn.run", params, timeout=effective_timeout,
        )

    def run_offline_command(
        self,
        command: str,
        context: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Run an explicitly selected fixed local command without QB.

        The daemon rejects unsupported input rather than rerouting it to a
        cloud backend. This is the only client API suitable for the `/ice`
        affordance.
        """
        params: dict = {"command": command}
        if context is not None:
            params["context"] = context
        effective_timeout = timeout if timeout is not None else self._turn_timeout_seconds
        return self.send_request("offline.run", params, timeout=effective_timeout)

    def new_session(self, backend: str | None = None) -> dict:
        params = {"backend": backend} if backend else {}
        return self.send_request("session.new", params)

    def reset_session(self) -> dict:
        return self.send_request("session.reset")

    def status(self) -> dict:
        return self.send_request("daemon.status")

    def request_shutdown(self) -> dict:
        return self.send_request("daemon.shutdown")

    def respond_hitl(self, decision: str) -> dict:
        return self.send_request("hitl.respond", {"decision": decision})

    # ── Reader thread ───────────────────────────────────────────────────

    def _reader_loop(self) -> None:
        assert self._transport is not None
        while not self._closed:
            try:
                # Phase 6 Scope B: was a hardcoded 1.0. Now driven by
                # cfg.daemon.reader_recv_timeout_seconds.
                raw = self._transport.recv(
                    timeout=self._reader_recv_timeout_seconds,
                )
            except TransportClosed:
                if self._closed:
                    break
                self._on_info({"message": "Connection to daemon lost. Reconnecting..."})
                if not self._reconnect_with_backoff():
                    break
                self._on_info({"message": "Reconnected to daemon"})
                continue
            if raw is None:
                continue
            try:
                msg = parse_message(raw)
            except Exception as exc:
                # F-53 Scope A.P1: previously swallowed silently — a torn
                # framing byte or truncated JSON would cascade into "nothing
                # is happening" symptoms with zero diagnostic. Warn ONCE per
                # connection with the exception type + first bytes so the
                # operator can look upstream (protocol version mismatch,
                # transport corruption, etc.). Rate-limited via the flag on
                # __slots__ so a flooded reader doesn't spam stderr.
                if not self._malformed_warned:
                    self._malformed_warned = True
                    excerpt = raw[:80] if isinstance(raw, (bytes, str)) else repr(raw)[:80]
                    print(
                        f"WARN: client reader: dropped malformed message: "
                        f"{type(exc).__name__}: {exc} (first 80 bytes: {excerpt!r}). "
                        f"Further parse errors on this connection will be silent.",
                        file=sys.stderr,
                    )
                continue

            if is_response(msg):
                msg_id = msg.get("id", "")
                with self._pending_lock:
                    entry = self._pending.get(msg_id)
                if entry is not None:
                    event, result_box = entry
                    result_box.append(msg)
                    event.set()
            elif is_notification(msg):
                self._dispatch_notification(msg)

    def _reconnect_with_backoff(self) -> bool:
        # Phase 6 Scope B: max_delay was a hardcoded 30.0. Now driven by
        # cfg.daemon.max_reconnect_delay_seconds.
        delay = 1.0
        max_delay = self._max_reconnect_delay_seconds
        while not self._closed:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self._sock_path)
                self._transport = UnixSocketTransport(sock)
                return True
            except (OSError, ConnectionRefusedError):
                time.sleep(delay)
                delay = min(delay * 2, max_delay)
        return False

    def _dispatch_notification(self, msg: dict) -> None:
        method = msg.get("method", "")
        params = msg.get("params", {})
        handlers = {
            "turn.progress": self._on_progress,
            "turn.token": self._on_token,
            "turn.info": self._on_info,
            "turn.cot": self._on_cot,
            "turn.gui": self._on_gui,
            "turn.rpa": self._on_rpa,
            "hitl.prompt": self._on_hitl_prompt,
            "hitl.lockout": self._on_hitl_lockout,
        }
        handler = handlers.get(method)
        if handler is not None:
            handler(params)

    # ── Notification callbacks (override in subclass) ───────────────────

    def _on_progress(self, params: dict) -> None:
        pass

    def _on_token(self, params: dict) -> None:
        pass

    def _on_info(self, params: dict) -> None:
        pass

    def _on_cot(self, params: dict) -> None:
        pass

    def _on_gui(self, params: dict) -> None:
        pass

    def _on_rpa(self, params: dict) -> None:
        pass

    def _on_hitl_prompt(self, params: dict) -> None:
        pass

    def _on_hitl_lockout(self, params: dict) -> None:
        pass


class ClientRepl(DaemonClient):
    """Interactive REPL that connects to the daemon and renders output locally."""

    __slots__ = ("_keymap",)

    def __init__(self, sock_path: str, keymap: Any = None) -> None:
        super().__init__(sock_path)
        self._keymap = keymap

    def _on_progress(self, params: dict) -> None:
        label = params.get("step_label", "")
        if label:
            sys.stdout.write(f"\r  {label}")
            sys.stdout.flush()

    def _on_token(self, params: dict) -> None:
        token = params.get("token", "")
        sys.stdout.write(token)
        sys.stdout.flush()

    def _on_info(self, params: dict) -> None:
        message = params.get("message", "")
        sys.stdout.write(message)
        sys.stdout.flush()

    def _on_cot(self, params: dict) -> None:
        state = params.get("step_state", "")
        heading = params.get("heading", "")
        body = params.get("body", "")
        _STATE_GLYPHS = {"active": ">", "done": "+", "failed": "!"}
        glyph = _STATE_GLYPHS.get(state, "?")
        detail = f" — {body}" if body else ""
        sys.stderr.write(f"  [{glyph}] {heading}{detail}\n")
        sys.stderr.flush()

    def _on_gui(self, params: dict) -> None:
        phase = params.get("phase", "")
        action = params.get("action", "")
        window = params.get("window_title", "")
        element = params.get("element_name", "")
        error = params.get("error", "")
        _PHASE_GLYPHS = {"preview": "?", "executing": ">", "complete": "+"}
        glyph = _PHASE_GLYPHS.get(phase, "~")
        parts = [f"[{glyph}] GUI {action}"]
        if window:
            parts.append(f"in {window!r}")
        if element:
            parts.append(f"on {element!r}")
        if error:
            parts.append(f"— {error}")
        sys.stderr.write(f"  {'  '.join(parts)}\n")
        sys.stderr.flush()

    def _on_rpa(self, params: dict) -> None:
        phase = params.get("phase", "")
        workflow = params.get("workflow_name", "")
        kw_idx = params.get("keyword_index", 0)
        kw_total = params.get("keyword_total", 0)
        current_kw = params.get("current_keyword", "")
        kw_status = params.get("keyword_status", "")
        qb_on_track = params.get("qb_on_track", True)
        qb_concern = params.get("qb_concern", "")
        error = params.get("error", "")
        _PHASE_GLYPHS = {
            "preview": "?", "executing": ">", "step": ".",
            "paused": "!", "complete": "+",
        }
        glyph = _PHASE_GLYPHS.get(phase, "~")
        parts = [f"[{glyph}] RPA {workflow}"]
        if kw_total:
            parts.append(f"[{kw_idx}/{kw_total}]")
        if current_kw:
            parts.append(current_kw)
        if kw_status:
            parts.append(f"-> {kw_status}")
        if not qb_on_track and qb_concern:
            parts.append(f"QB: {qb_concern}")
        if error:
            parts.append(f"-- {error}")
        sys.stderr.write(f"  {'  '.join(parts)}\n")
        sys.stderr.flush()

    def _on_hitl_prompt(self, params: dict) -> None:
        data = HitlDisplayData(
            action=params.get("action", ""),
            target=params.get("target", ""),
            tier=Tier(params.get("tier", 1)),
            risk_level=params.get("risk_level", "medium"),
            reversible=params.get("reversible", True),
            backend=params.get("backend", ""),
            reason=params.get("reason", ""),
            blocked_pattern=params.get("blocked_pattern"),
            cow_summary=params.get("cow_summary"),
        )
        presenter = TerminalPresenter(keymap=self._keymap)
        presenter.show_prompt(data)

    def _on_hitl_lockout(self, params: dict) -> None:
        seconds = params.get("seconds", 3)
        presenter = TerminalPresenter(keymap=self._keymap)
        decision = presenter.read_decision(timeout_seconds=seconds + 30)
        if decision is not None:
            try:
                self.respond_hitl(decision.value)
            except (TransportClosed, TimeoutError):
                pass

    def run(self) -> None:
        self.connect()
        try:
            status = self.status()
            result = status.get("result", {})
            print(
                f"Connected to daemon (pid={result.get('pid')}, "
                f"backend={result.get('backend')})"
            )
            while self.is_connected:
                try:
                    user_input = input("icebreaker> ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break

                stripped = user_input.strip()
                if not stripped:
                    continue
                if stripped in ("/exit", "/quit"):
                    break
                if stripped == "/status":
                    resp = self.status()
                    print(json.dumps(resp.get("result", {}), indent=2))
                    continue
                if stripped == "/reset":
                    resp = self.reset_session()
                    print("Session reset." if "result" in resp else f"Error: {resp}")
                    continue
                if stripped == "/shutdown":
                    self.request_shutdown()
                    print("Shutdown requested.")
                    break

                try:
                    resp = self.run_turn(stripped)
                except TimeoutError:
                    print("\nTurn timed out.", file=sys.stderr)
                    continue
                except TransportClosed:
                    print("\nConnection lost.", file=sys.stderr)
                    break

                if "result" in resp:
                    r = resp["result"]
                    output = r.get("output", "")
                    if output:
                        print(f"\n{output}")
                elif "error" in resp:
                    print(f"\nError: {resp['error'].get('message', 'unknown')}")
        finally:
            self.close()
