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
    )

    def __init__(self, sock_path: str) -> None:
        self._sock_path = sock_path
        self._transport: Transport | None = None
        self._reader_thread: threading.Thread | None = None
        self._pending: dict[str, tuple[threading.Event, list]] = {}
        self._pending_lock = threading.Lock()
        self._closed = False

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._sock_path)
        self._transport = UnixSocketTransport(sock)
        self._closed = False
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

    def run_turn(self, user_input: str) -> dict:
        return self.send_request("turn.run", {"input": user_input}, timeout=120.0)

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
                raw = self._transport.recv(timeout=1.0)
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
            except Exception:
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
        delay = 1.0
        max_delay = 30.0
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
