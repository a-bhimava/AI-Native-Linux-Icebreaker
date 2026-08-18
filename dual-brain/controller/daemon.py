"""Daemon — persistent background service for the Icebreaker Controller.

Owns the expensive resources (QB, PB, mcpd, audit). Thin clients connect
over AF_UNIX and drive turns via JSON-RPC 2.0. The daemon enforces
authentication (SO_PEERCRED UID match), connection limits, and server-side
HITL lockout (BP-4/INV-6).

Threading model:
  - Main thread: accept loop (select-based, 1 s timeout for shutdown check)
  - Connection handler thread (1 per connection): reads JSON-RPC, dispatches
  - Turn worker thread (1 per active turn): runs pipeline, sends notifications
"""

from __future__ import annotations

import logging
import os
import select
import signal
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import DaemonConfig
from .forwarding_presenter import ForwardingPresenter
from .hitl import Decision
from .main import Controller, TurnResult
from .mcpd_client import McpdClient
from .protocol import (
    AUTH_REJECTED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JsonRpcNotification,
    JsonRpcResponse,
    make_error,
    parse_message,
    is_request,
)
from .session import SessionState
from .transport import Transport, TransportClosed, UnixSocketTransport
from .turn_events import (
    CotEvent,
    ErrorEvent,
    GuiEvent,
    InfoEvent,
    ProgressEvent,
    ResultEvent,
    RpaEvent,
    TokenEvent,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """OS identity bound to a daemon connection via SO_PEERCRED."""

    uid: int
    username: str
    home: str


@dataclass
class DaemonSession:
    """Per-connection session state."""

    session_state: SessionState
    transport: Transport
    principal: Principal | None = None
    controller: Controller | None = None
    mcpd_client: McpdClient | None = None
    connected_at: float = field(default_factory=time.monotonic)
    active_presenter: ForwardingPresenter | None = None
    _turn_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class Daemon:
    """Persistent background daemon — AF_UNIX server with JSON-RPC dispatch."""

    __slots__ = (
        "_cfg", "_controller", "_controller_cfg", "_audit",
        "_server_sock", "_sessions", "_session_lock",
        "_shutdown_event", "_accept_thread",
        # v6.8 Task #145: SessionStore keyed by session_id — enables
        # by-ID lookup for LangGraph nodes (Task #146+).
        "_session_store",
        "_principal_scoped_mcpd",
    )

    def __init__(
        self,
        daemon_cfg: DaemonConfig,
        controller: Controller,
        controller_cfg: Any,
        audit_log: Any,
    ) -> None:
        self._cfg = daemon_cfg
        self._controller = controller
        self._controller_cfg = controller_cfg
        self._audit = audit_log
        self._server_sock: socket.socket | None = None
        self._sessions: list[DaemonSession] = []
        self._session_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._accept_thread: threading.Thread | None = None
        # ``is True`` is intentional: permissive truthiness here would turn
        # an incomplete/mocked legacy config into production identity mode.
        self._principal_scoped_mcpd = (
            getattr(getattr(controller_cfg, "run", None), "principal_scoped_mcpd", False) is True
        )
        if self._principal_scoped_mcpd:
            # The bootstrap client was necessary to construct the legacy
            # Controller.  Production sessions must never reuse it: it has no
            # authenticated home/uid binding.
            controller._mcpd.close()
        # v6.8 Task #145: session_id-keyed registry. The connection list
        # (_sessions) stays for transport lifecycle; SessionStore mirrors
        # the SessionState instances by session_id for LangGraph nodes.
        from .session_store import SessionStore
        self._session_store = SessionStore()

    # ── Socket lifecycle ────────────────────────────────────────────────

    def _create_socket(self) -> socket.socket:
        sock_path = Path(self._cfg.socket_path).expanduser()
        sock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if sock_path.exists():
            sock_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        os.chmod(str(sock_path), 0o660)
        try:
            import grp
            gid = grp.getgrnam(self._cfg.socket_group).gr_gid
            os.chown(str(sock_path), -1, gid)
        except (KeyError, PermissionError):
            pass
        sock.listen(4)
        sock.setblocking(False)
        return sock

    def _write_pid_file(self) -> None:
        pid_path = Path(self._cfg.pid_file).expanduser()
        pid_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = pid_path.with_suffix(".tmp")
        tmp.write_text(str(os.getpid()), encoding="utf-8")
        tmp.rename(pid_path)

    def _remove_pid_file(self) -> None:
        pid_path = Path(self._cfg.pid_file).expanduser()
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass

    def _check_stale_pid(self) -> None:
        pid_path = Path(self._cfg.pid_file).expanduser()
        if not pid_path.exists():
            return
        try:
            old_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid_path.unlink(missing_ok=True)
            return
        try:
            os.kill(old_pid, 0)
        except ProcessLookupError:
            pid_path.unlink(missing_ok=True)
        except PermissionError:
            raise RuntimeError(
                f"PID file {pid_path} exists and process {old_pid} is alive "
                "(owned by another user)"
            )
        else:
            raise RuntimeError(
                f"Daemon already running (PID {old_pid}, file {pid_path})"
            )

    # ── Authentication ──────────────────────────────────────────────────

    def _authenticate(self, transport: Transport) -> bool:
        peer = transport.peer_uid
        if peer is None:
            return False
        if peer == os.getuid():
            return True
        try:
            import grp
            import pwd
            peer_entry = pwd.getpwuid(peer)
            group_gid = grp.getgrnam(self._cfg.socket_group).gr_gid
            peer_groups = os.getgrouplist(peer_entry.pw_name, peer_entry.pw_gid)
            return group_gid in peer_groups
        except (KeyError, PermissionError, OSError):
            return False

    @staticmethod
    def _principal_for(transport: Transport) -> Principal | None:
        """Resolve the kernel-authenticated peer into a usable home root."""
        peer = transport.peer_uid
        if peer is None:
            return None
        try:
            import pwd

            entry = pwd.getpwuid(peer)
            home = Path(entry.pw_dir).resolve(strict=False)
        except (KeyError, OSError, ValueError):
            return None
        if not home.is_absolute() or str(home) in ("/", "/nonexistent"):
            return None
        return Principal(uid=peer, username=entry.pw_name, home=str(home))

    def _controller_for_principal(self, principal: Principal) -> tuple[Controller, McpdClient]:
        """Spawn mcpd under the SO_PEERCRED identity, never as the daemon.

        mcpd receives just this account's home as both HOME and its dynamic
        Landlock/userspace read root.  The Python subprocess API performs the
        uid/gid transition without a shell or arbitrary command string.
        """
        try:
            import pwd
            entry = pwd.getpwuid(principal.uid)
            groups = tuple(sorted(set(os.getgrouplist(entry.pw_name, entry.pw_gid))))
        except (KeyError, OSError):
            raise RuntimeError("authenticated peer no longer has a usable OS account")
        env = {
            "HOME": principal.home,
            "USER": principal.username,
            "LOGNAME": principal.username,
            "MCPD_FS_READ_ROOTS": principal.home,
        }
        client = McpdClient.spawn(
            self._controller._cfg.run.mcpd_binary,
            default_timeout=self._controller._cfg.run.mcpd_timeout_seconds,
            extra_env=env,
            run_as_uid=principal.uid,
            run_as_gid=entry.pw_gid,
            run_as_groups=groups,
        )
        return self._controller.with_mcpd_client(client), client

    # ── Connection gate ─────────────────────────────────────────────────

    def _connection_gate(self) -> bool:
        with self._session_lock:
            active = sum(1 for s in self._sessions if s.transport.is_open())
            return active < self._cfg.max_connections

    # ── Accept loop ─────────────────────────────────────────────────────

    def _accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._shutdown_event.is_set():
            try:
                ready, _, _ = select.select(
                    [self._server_sock], [], [], 1.0,
                )
            except (ValueError, OSError):
                break
            if not ready:
                continue
            try:
                conn, _ = self._server_sock.accept()
            except OSError:
                if self._shutdown_event.is_set():
                    break
                continue

            conn.setblocking(True)
            transport = UnixSocketTransport(conn)

            if not self._authenticate(transport):
                log.warning("rejected connection: UID mismatch")
                try:
                    resp = make_error(
                        "0", AUTH_REJECTED, "authentication failed: UID mismatch"
                    )
                    transport.send(resp.to_bytes())
                except TransportClosed:
                    pass
                transport.close()
                continue

            principal = self._principal_for(transport)
            if principal is None:
                log.warning("rejected connection: no usable peer principal")
                transport.close()
                continue

            if not self._connection_gate():
                log.info("rejected connection: max_connections reached")
                try:
                    resp = make_error(
                        "0", AUTH_REJECTED,
                        f"max_connections ({self._cfg.max_connections}) reached"
                    )
                    transport.send(resp.to_bytes())
                except TransportClosed:
                    pass
                transport.close()
                continue

            # v6.8 Task #145: register via SessionStore so LangGraph
            # nodes can look up by session_id. The DaemonSession still
            # owns transport lifecycle.
            session_state = self._session_store.create(
                self._controller_cfg.qb.name,
                self._controller_cfg.session,
            )
            session_controller = self._controller
            session_mcpd = None
            if self._principal_scoped_mcpd:
                try:
                    session_controller, session_mcpd = self._controller_for_principal(principal)
                except Exception as exc:
                    log.warning("rejected connection: unable to scope mcpd for uid %s: %s", principal.uid, exc)
                    transport.close()
                    continue

            ds = DaemonSession(
                session_state=session_state,
                transport=transport,
                principal=principal,
                controller=session_controller,
                mcpd_client=session_mcpd,
            )
            with self._session_lock:
                self._sessions.append(ds)

            handler = threading.Thread(
                target=self._connection_loop,
                args=(ds,),
                daemon=True,
                name=f"conn-{session_state.session_id[:8]}",
            )
            handler.start()

    # ── Per-connection handler ──────────────────────────────────────────

    def _connection_loop(self, session: DaemonSession) -> None:
        try:
            while not self._shutdown_event.is_set():
                try:
                    raw = session.transport.recv(timeout=1.0)
                except TransportClosed:
                    break
                if raw is None:
                    continue

                try:
                    msg = parse_message(raw)
                except Exception as exc:
                    # F-53 Scope A.P1: previously swallowed silently — the
                    # client got a "malformed JSON" error but the daemon
                    # journal recorded nothing. Log the type + first bytes
                    # so operators diagnosing wire-format regressions
                    # (protocol version mismatch, framing corruption) can
                    # grep journalctl. Debug level to avoid flooding on a
                    # spammy client; escalate to warning in future if we
                    # see repeated hits.
                    excerpt = raw[:80] if isinstance(raw, (bytes, str)) else repr(raw)[:80]
                    log.debug(
                        "parse_message failed: %s: %s (first 80 bytes: %r)",
                        type(exc).__name__, exc, excerpt,
                    )
                    try:
                        resp = make_error("0", PARSE_ERROR, "malformed JSON")
                        session.transport.send(resp.to_bytes())
                    except TransportClosed:
                        # Legitimate swallow: torn connection while
                        # reporting an earlier parse failure. Nothing more
                        # to do — the outer loop's `while not shutdown`
                        # exits cleanly on the next iteration.
                        break  # noqa: BLE001-not-applicable-here
                    continue

                if not is_request(msg):
                    continue

                method = msg.get("method", "")
                msg_id = msg.get("id", "0")

                handler = self._METHODS.get(method)
                if handler is None:
                    try:
                        resp = make_error(
                            msg_id, METHOD_NOT_FOUND, f"unknown method: {method}"
                        )
                        session.transport.send(resp.to_bytes())
                    except TransportClosed:
                        break
                    continue

                try:
                    getattr(self, handler)(session, msg)
                except TransportClosed:
                    break
                except Exception as exc:
                    log.exception("handler error for %s", method)
                    try:
                        resp = make_error(
                            msg_id, INTERNAL_ERROR, str(exc)
                        )
                        session.transport.send(resp.to_bytes())
                    except TransportClosed:
                        break
        finally:
            session.transport.close()
            if session.mcpd_client is not None:
                session.mcpd_client.close()
            with self._session_lock:
                try:
                    self._sessions.remove(session)
                except ValueError:
                    pass
            # v6.8 Task #145: also drop from SessionStore. The store
            # can be a leak vector otherwise — new connections keep
            # calling create() which registers fresh entries.
            self._session_store.remove(session.session_state.session_id)

    # ── JSON-RPC dispatch table ─────────────────────────────────────────

    _METHODS = {
        "turn.run":        "_handle_turn_run",
        # v6.17 M7.6a-1b: OC edition submit_intent path — receives a
        # pre-formed intent object from opencode/Gemini via the iceui
        # MCP server's submit_intent tool, skips Step 1 qb_intent, and
        # drives the rest of the pipeline (COW, HITL, PB retry, verifier,
        # dispatch, summarise). See F-111 in GROUND_TRUTH.md § 7.
        "intent.run":      "_handle_intent_run",
        "session.new":     "_handle_session_new",
        "session.reset":   "_handle_session_reset",
        "hitl.respond":    "_handle_hitl_respond",
        "daemon.status":   "_handle_daemon_status",
        "daemon.shutdown": "_handle_daemon_shutdown",
    }

    def _handle_turn_run(self, session: DaemonSession, msg: dict) -> None:
        msg_id = msg.get("id", "0")
        params = msg.get("params", {})
        user_input = params.get("input")
        if not isinstance(user_input, str) or not user_input.strip():
            resp = make_error(msg_id, INVALID_PARAMS, "missing 'input' string")
            session.transport.send(resp.to_bytes())
            return

        # V6B Stage 2: optional context from the Terminal (cwd, recent
        # commands, active_window). Sanitized in ShellContext.from_params;
        # rendered into the QB preamble at the call site in main.py.
        # Phase 6 Scope B: max_recent slice is driven by config.
        from .session import ShellContext
        session.session_state.shell_context = ShellContext.from_params(
            params.get("context"),
            max_recent=int((session.controller or self._controller)._cfg.session.max_recent_commands),
        )

        if not session._turn_lock.acquire(blocking=False):
            resp = make_error(msg_id, INTERNAL_ERROR, "turn already in progress")
            session.transport.send(resp.to_bytes())
            return

        offline_cfg = getattr(self._controller_cfg, "offline_command_lane", None)
        offline_enabled = bool(getattr(offline_cfg, "enabled", False))
        match = None
        if offline_enabled and session.principal is not None:
            from .direct_command import parse_direct_command

            match = parse_direct_command(
                user_input,
                cwd=session.session_state.shell_context.cwd,
                home=session.principal.home,
            )

        def _event_iter():
            if match is not None:
                return (session.controller or self._controller).run_turn_from_intent(
                    match.intent,
                    session.session_state,
                    source="offline_direct",
                    force_pb=True,
                    offline_direct=True,
                )
            return (session.controller or self._controller).run_turn_streaming(
                user_input, session.session_state,
            )

        self._start_pipeline_worker(session, msg_id, _event_iter, name="turn-worker")

    # v6.17 M7.6a-1b: intent.run handler — OC edition submit_intent path.
    # Mirrors _handle_turn_run but drives Controller.run_turn_from_intent
    # with a pre-formed intent object (skips Step 1 qb_intent). Reuses
    # the _start_pipeline_worker streaming machinery so event handling,
    # notification wiring, and error paths stay identical to turn.run.
    def _handle_intent_run(self, session: DaemonSession, msg: dict) -> None:
        msg_id = msg.get("id", "0")
        params = msg.get("params", {})
        intent = params.get("intent")
        session_id = params.get("session_id")

        # Params validation — Controller re-validates the intent against
        # intent.json + risk-classifies + verifies; this is defense in
        # depth at the transport boundary.
        if not isinstance(intent, dict) or not intent:
            resp = make_error(msg_id, INVALID_PARAMS,
                              "missing 'intent' object (non-empty dict required)")
            session.transport.send(resp.to_bytes())
            return
        if not isinstance(session_id, str) or not session_id:
            resp = make_error(msg_id, INVALID_PARAMS,
                              "missing 'session_id' string")
            session.transport.send(resp.to_bytes())
            return

        # v0 session policy: use the connection-scoped session
        # (1:1 with each MCP tool call — opencode reconnects per call).
        # session_id from params is accepted + validated but not yet
        # used for cross-connection session lookup — that's reserved
        # for a future sub-commit if opencode needs multi-turn session
        # continuity (session context, cwd tracking across turns).

        # Guard: Controller must have implemented run_turn_from_intent
        # (lands in M7.6a-1c). Until then, return a clear error rather
        # than crashing the worker thread.
        controller = session.controller or self._controller
        if not hasattr(controller, "run_turn_from_intent"):
            resp = make_error(msg_id, INTERNAL_ERROR,
                              "Controller.run_turn_from_intent not implemented "
                              "(M7.6a-1c not yet shipped)")
            session.transport.send(resp.to_bytes())
            return

        if not session._turn_lock.acquire(blocking=False):
            resp = make_error(msg_id, INTERNAL_ERROR, "turn already in progress")
            session.transport.send(resp.to_bytes())
            return

        def _event_iter():
            return controller.run_turn_from_intent(
                intent, session.session_state,
            )

        self._start_pipeline_worker(session, msg_id, _event_iter, name="intent-worker")

    def _start_pipeline_worker(
        self,
        session: DaemonSession,
        msg_id: Any,
        event_iter_factory: Callable[[], Any],
        *,
        name: str,
    ) -> None:
        """v6.17 M7.6a-1b: shared streaming worker used by turn.run and
        intent.run. Runs the controller pipeline on a worker thread and
        streams events back as JSON-RPC notifications; the final
        ResultEvent becomes the JSON-RPC response.

        Caller must have acquired session._turn_lock before calling this
        (both handlers do). Worker releases it in the finally block.
        """
        def _worker() -> None:
            try:
                presenter = ForwardingPresenter(session.transport)
                session.active_presenter = presenter

                def _factory() -> ForwardingPresenter:
                    return presenter

                controller = session.controller or self._controller
                controller._presenter_factory = _factory

                for event in event_iter_factory():
                    try:
                        if isinstance(event, ProgressEvent):
                            notif = JsonRpcNotification("turn.progress", {
                                "step_name": event.step_name,
                                "step_label": event.step_label,
                                "step_index": event.step_index,
                                "total_steps": event.total_steps,
                                "elapsed_ms": event.elapsed_ms,
                            })
                            session.transport.send(notif.to_bytes())
                        elif isinstance(event, TokenEvent):
                            notif = JsonRpcNotification("turn.token", {
                                "token": event.token,
                                "accumulated": event.accumulated,
                                "final": event.final,
                            })
                            session.transport.send(notif.to_bytes())
                        elif isinstance(event, InfoEvent):
                            notif = JsonRpcNotification("turn.info", {
                                "message": event.message,
                            })
                            session.transport.send(notif.to_bytes())
                        elif isinstance(event, CotEvent):
                            notif = JsonRpcNotification("turn.cot", {
                                "step_index": event.step_index,
                                "step_name": event.step_name,
                                "step_state": event.step_state,
                                "heading": event.heading,
                                "body": event.body,
                                "data": event.data,
                                "timestamp_ms": event.timestamp_ms,
                            })
                            session.transport.send(notif.to_bytes())
                        elif isinstance(event, GuiEvent):
                            notif = JsonRpcNotification("turn.gui", {
                                "phase": event.phase,
                                "action": event.action,
                                "window_title": event.window_title,
                                "app_name": event.app_name,
                                "element_role": event.element_role,
                                "element_name": event.element_name,
                                "screenshot_before_hash": event.screenshot_before_hash,
                                "screenshot_after_hash": event.screenshot_after_hash,
                                "predicted_outcome": event.predicted_outcome,
                                "error": event.error,
                                "timestamp_ms": event.timestamp_ms,
                            })
                            session.transport.send(notif.to_bytes())
                        elif isinstance(event, RpaEvent):
                            notif = JsonRpcNotification("turn.rpa", {
                                "phase": event.phase,
                                "workflow_name": event.workflow_name,
                                "keyword_index": event.keyword_index,
                                "keyword_total": event.keyword_total,
                                "current_keyword": event.current_keyword,
                                "keyword_status": event.keyword_status,
                                "timeout_remaining_ms": event.timeout_remaining_ms,
                                "screenshot_hash": event.screenshot_hash,
                                "qb_on_track": event.qb_on_track,
                                "qb_concern": event.qb_concern,
                                "error": event.error,
                                "timestamp_ms": event.timestamp_ms,
                            })
                            session.transport.send(notif.to_bytes())
                        elif isinstance(event, ResultEvent):
                            r = event.result
                            # v6.12 Fix G (F-87 2026-07-21): expose the
                            # daemon's SessionState.session_cwd on every
                            # turn's response so the shell trigger can
                            # sync bash $PWD when nav.cd fires. nav.cd's
                            # session_op impl mutates the SessionState's
                            # session_cwd field directly; we snapshot it
                            # here on the way out. Zero cost for non-nav
                            # turns — just an empty string.
                            _scwd = ""
                            try:
                                _scwd = str(
                                    getattr(session.session_state, "session_cwd", "") or ""
                                )
                            except Exception:  # noqa: BLE001
                                pass
                            resp = JsonRpcResponse(id=msg_id, result={
                                "success": r.success,
                                "output": r.output,
                                "outcome": r.outcome.value if hasattr(r.outcome, "value") else str(r.outcome),
                                "tier": r.tier,
                                "backend": r.backend,
                                "duration_ms": r.duration_ms,
                                "cost_usd": r.cost_usd,
                                "tokens_in": r.tokens_in,
                                "tokens_out": r.tokens_out,
                                "session_cwd": _scwd,
                            })
                            session.transport.send(resp.to_bytes())
                        elif isinstance(event, ErrorEvent):
                            resp = make_error(msg_id, INTERNAL_ERROR, event.message)
                            session.transport.send(resp.to_bytes())
                    except TransportClosed:
                        return
            except TransportClosed:
                pass
            except Exception as exc:
                log.exception("turn worker error")
                try:
                    resp = make_error(msg_id, INTERNAL_ERROR, str(exc))
                    session.transport.send(resp.to_bytes())
                except TransportClosed:
                    pass
            finally:
                session.active_presenter = None
                controller._presenter_factory = None
                session._turn_lock.release()

        t = threading.Thread(target=_worker, daemon=True, name=name)
        t.start()

    def _handle_session_new(self, session: DaemonSession, msg: dict) -> None:
        msg_id = msg.get("id", "0")
        params = msg.get("params", {})
        backend = params.get("backend", self._controller_cfg.qb.name)
        # v6.8 Task #145: SessionStore registration for by-id lookup.
        # Old session (if any) stays in the store until the connection
        # closes and _connection_loop removes it — LangGraph nodes may
        # still be checkpoint-resuming against it.
        new_state = self._session_store.create(backend, self._controller_cfg.session)
        session.session_state = new_state
        resp = JsonRpcResponse(id=msg_id, result={
            "session_id": new_state.session_id,
            "backend": new_state.backend,
        })
        session.transport.send(resp.to_bytes())

    def _handle_session_reset(self, session: DaemonSession, msg: dict) -> None:
        msg_id = msg.get("id", "0")
        session.session_state.reset_memory()
        resp = JsonRpcResponse(id=msg_id, result={"ok": True})
        session.transport.send(resp.to_bytes())

    def _handle_hitl_respond(self, session: DaemonSession, msg: dict) -> None:
        msg_id = msg.get("id", "0")
        presenter = session.active_presenter
        if presenter is None:
            resp = make_error(msg_id, INVALID_PARAMS, "no pending HITL prompt")
            session.transport.send(resp.to_bytes())
            return

        params = msg.get("params", {})
        decision_str = params.get("decision", "")
        try:
            decision = Decision(decision_str)
        except ValueError:
            resp = make_error(
                msg_id, INVALID_PARAMS, f"invalid decision: {decision_str!r}"
            )
            session.transport.send(resp.to_bytes())
            return

        presenter.receive_decision(decision)
        resp = JsonRpcResponse(id=msg_id, result={"ok": True})
        session.transport.send(resp.to_bytes())

    def _handle_daemon_status(self, session: DaemonSession, msg: dict) -> None:
        msg_id = msg.get("id", "0")
        ss = session.session_state
        resp = JsonRpcResponse(id=msg_id, result={
            "pid": os.getpid(),
            "session_id": ss.session_id,
            "turn_index": ss.turn_index,
            "backend": ss.backend,
            "status": "ready",
        })
        session.transport.send(resp.to_bytes())

    def _handle_daemon_shutdown(self, session: DaemonSession, msg: dict) -> None:
        msg_id = msg.get("id", "0")
        resp = JsonRpcResponse(id=msg_id, result={"ok": True})
        session.transport.send(resp.to_bytes())
        self._shutdown_event.set()

    # ── Public API ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Create socket, write PID, run accept loop (blocks)."""
        self._check_stale_pid()
        self._server_sock = self._create_socket()
        self._write_pid_file()
        log.info(
            "daemon started (pid=%d, socket=%s)",
            os.getpid(), self._cfg.socket_path,
        )
        try:
            self._accept_loop()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Drain connections, close socket, remove PID file."""
        self._shutdown_event.set()
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None

        with self._session_lock:
            for s in self._sessions:
                s.transport.close()
            self._sessions.clear()

        self._remove_pid_file()

        sock_path = Path(self._cfg.socket_path).expanduser()
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        log.info("daemon shut down")

    @property
    def socket_path(self) -> str:
        return str(Path(self._cfg.socket_path).expanduser())

    @property
    def is_running(self) -> bool:
        return not self._shutdown_event.is_set()

    @property
    def active_sessions(self) -> int:
        with self._session_lock:
            return sum(1 for s in self._sessions if s.transport.is_open())
