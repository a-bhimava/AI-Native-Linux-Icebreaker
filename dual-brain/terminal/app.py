"""AiTerminalApp — split-pane Textual TUI for the Icebreaker Controller.

Layout (ADR-18, ADR-19):
    Vertical(
        InputBar,               # top — mode indicator + text input
        Horizontal(             # main split
            ExecutionPanel,     # left — shell subprocess output
            CompanionPanel,     # right — CoT cards / interpretation
        ),
        StatusBar,              # bottom — keybinding legend
    )
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any


def _hdiag(msg: str) -> None:
    """v6.12 HITL-DIAG hotfix: write to /tmp/hitl-diag.log. Bypasses
    Python logging so lines survive Textual's stderr/ANSI output
    collision. Best-effort — never raises."""
    try:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [terminal pid={os.getpid()}] {msg}\n"
        with open("/tmp/hitl-diag.log", "a") as f:
            f.write(line)
    except Exception:
        pass


# Phase 6 Scope A.P2: module logger used by every event handler + UI
# swallow site. Textual's dev tools pick this up under
# ``~/.textual/log/`` and journalctl captures it when the terminal is
# spawned by systemd. Debug level keeps production quiet while making
# "why did this event vanish" diagnosable.
log = logging.getLogger(__name__)

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Label, Static

from .companion import CompanionPanel
from .execution import ExecutionPanel
from .input_bar import InputBar
from .input_router import InputRouter, RouteTarget

_CSS_PATH = Path(__file__).parent / "styles.tcss"

# Minimum layout CSS injected when styles.tcss is not present at the installed
# path (e.g. package-data not included in the venv build). This guarantees the
# TUI widgets are always visible — avoids the blank-screen Bug #3.
_FALLBACK_CSS = """
Screen { background: #111112; color: #c0c0c0; }
InputBar { dock: top; height: 3; background: #111111;
           border-bottom: solid #222222; padding: 0 1; }
StatusBar { dock: bottom; height: 1; background: #111111;
            color: #888888; padding: 0 1; }
StatusBar .key-hint { color: #e78952; text-style: bold; }
#main-split { height: 1fr; }
ExecutionPanel { width: 3fr; min-width: 30;
                 border-right: solid #222222; background: #111112; }
ExecutionPanel #shell-output { background: #111112; color: #c0c0c0;
                                scrollbar-size: 1 1; }
CompanionPanel { width: 2fr; min-width: 24;
                 background: #111112; padding: 0 1; }
CompanionPanel #companion-header { height: 1; color: #888888;
                                   text-style: bold; }
CompanionPanel #cot-container { height: 1fr; overflow-y: auto;
                                 scrollbar-size: 1 1; }
CompanionPanel.-hidden { display: none; }
.cot-card { height: auto; margin: 0 0 1 0; padding: 0 1;
            background: #111111; border-left: tall #222222; }
.card-heading { height: 1; color: #c0c0c0; }
.card-body { height: auto; color: #888888; }
InputBar #mode-label { width: 8; color: #888888; text-style: bold;
                       padding: 0 1; content-align: center middle; }
InputBar #input-field { background: #111112; color: #c0c0c0;
                        border: round #222222; padding: 0 1; }
"""

_MIN_COMPANION_WIDTH = 80


class StatusBar(Static):
    """Bottom status bar with keybinding hints."""

    DEFAULT_CSS = ""

    def compose(self) -> ComposeResult:
        yield Label(
            " F2 NL Mode │ F3 Panel │ F9 Resize │ F10 Quit ",
            classes="key-hint",
        )


class AiTerminalApp(App):
    """Split-pane AI Terminal."""

    # Use the on-disk TCSS when available; fall back to the embedded constant
    # so widgets are always visible even if package-data wasn't installed.
    if _CSS_PATH.exists():
        CSS_PATH = str(_CSS_PATH)
    else:
        CSS = _FALLBACK_CSS

    TITLE = "Icebreaker AI Terminal"

    BINDINGS = [
        Binding("f2", "toggle_nl_mode", "Toggle NL mode", show=False),
        Binding("f3", "toggle_companion", "Toggle companion panel"),
        Binding("f10", "quit", "Quit"),
        # v6.12 HITL-DIAG hotfix: always-available approve/deny keybindings.
        # Works even when the HitlModal fails to render. Combined with the
        # fallback banner in _on_hitl_prompt, the user always has a visible
        # prompt AND a way to answer it.
        Binding("ctrl+a", "hitl_approve", "Approve pending HITL", show=True),
        Binding("ctrl+d", "hitl_deny", "Deny pending HITL", show=True),
    ]

    def __init__(
        self,
        daemon_client: Any = None,
        startup_warning: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._companion_visible = True
        self._no_color = bool(os.environ.get("NO_COLOR"))
        self._router = InputRouter()
        self._daemon_client = daemon_client
        self._startup_warning = startup_warning

    @property
    def router(self) -> InputRouter:
        return self._router

    def compose(self) -> ComposeResult:
        yield InputBar()
        with Horizontal(id="main-split"):
            yield ExecutionPanel()
            yield CompanionPanel()
        yield StatusBar()

    def on_mount(self) -> None:
        if self._no_color:
            self.screen.add_class("-no-color")
        self._check_companion_width()
        self._wire_daemon_callbacks()
        # Surface any daemon startup error inside the TUI output panel so the
        # user sees it rather than a silent blank window or unexpected exit.
        if self._startup_warning:
            try:
                from rich.text import Text
                out = self.query_one(ExecutionPanel).query_one("#shell-output")
                out.write(Text(
                    f"[WARN] {self._startup_warning}",
                    style="bold #f87171",
                ))
            except Exception as exc:
                # F-53 Scope A.P2: startup UI not ready yet — the warning
                # can't be rendered. Fall through so the terminal still
                # starts; log so operators diagnosing "warning never
                # appeared" see the render error.
                log.debug("tui.startup_warning render failed: %s: %s",
                          type(exc).__name__, exc)

    def _wire_daemon_callbacks(self) -> None:
        client = self._daemon_client
        if client is None or not hasattr(client, "on"):
            return
        if hasattr(client, "set_app"):
            client.set_app(self)
        client.on("cot", self._on_cot_event)
        client.on("token", self._on_token_event)
        client.on("progress", self._on_progress_event)
        client.on("gui", self._on_gui_event)
        client.on("rpa", self._on_rpa_event)
        client.on("info", self._on_info_event)
        # v6.10 Task #152 (F-61 fix): wire HITL prompt + lockout so the
        # daemon-forwarded prompts actually render an overlay instead
        # of silently dropping the event. Prior versions had the client
        # firing the event but no listener attached — Tier ≥ 1 intents
        # timed out after 30s with no user-visible prompt.
        client.on("hitl_prompt", self._on_hitl_prompt)
        client.on("hitl_lockout", self._on_hitl_lockout)

    def _on_cot_event(self, params: dict) -> None:
        try:
            companion = self.query_one(CompanionPanel)
            companion.handle_cot(
                step_index=params.get("step_index", 0),
                step_name=params.get("step_name", ""),
                step_state=params.get("step_state", "pending"),
                heading=params.get("heading", ""),
                body=params.get("body", ""),
                data=params.get("data"),
            )
        except Exception as exc:
            # F-53 Scope A.P2: CoT event dropped. Debug-log so operators
            # investigating "step never appeared in the interpretation
            # pane" have a lead. Common cause: companion torn down mid-
            # turn during teardown.
            log.debug("tui.event.cot dropped: %s: %s",
                      type(exc).__name__, exc)

    def _on_token_event(self, params: dict) -> None:
        if not params.get("final"):
            return
        text = params.get("accumulated", "")
        if not text:
            return
        try:
            from rich.text import Text
            out = self.query_one(ExecutionPanel).query_one("#shell-output")
            out.write(Text(text, style="#c0c0c0"))
        except Exception as exc:
            # F-53 Scope A.P2: final token dropped. Users see a "response
            # never appeared" symptom; debug-log gives operators the trail.
            log.debug("tui.event.token dropped: %s: %s",
                      type(exc).__name__, exc)

    def _on_progress_event(self, params: dict) -> None:
        pass

    def _on_gui_event(self, params: dict) -> None:
        try:
            companion = self.query_one(CompanionPanel)
            companion.handle_gui(params)
        except Exception as exc:
            # F-53 Scope A.P2: GUI event drop mirror of _on_cot_event.
            log.debug("tui.event.gui dropped: %s: %s",
                      type(exc).__name__, exc)

    def _on_rpa_event(self, params: dict) -> None:
        try:
            companion = self.query_one(CompanionPanel)
            companion.handle_rpa(params)
        except Exception as exc:
            # F-53 Scope A.P2: RPA event drop mirror of _on_cot_event.
            log.debug("tui.event.rpa dropped: %s: %s",
                      type(exc).__name__, exc)

    def _on_info_event(self, params: dict) -> None:
        msg = params.get("message", "")
        if not msg:
            return
        try:
            from rich.text import Text
            out = self.query_one(ExecutionPanel).query_one("#shell-output")
            out.write(Text(f"[info] {msg}", style="#888888"))
        except Exception as exc:
            # F-53 Scope A.P2: info banner dropped. Debug-log surface.
            log.debug("tui.event.info dropped: %s: %s",
                      type(exc).__name__, exc)

    # ── HITL (v6.10 Task #152 / F-61 fix) ──────────────────────────────

    def _on_hitl_prompt(self, params: dict) -> None:
        """Daemon-forwarded HITL prompt → open the modal on the UI
        thread. Called from the DaemonClient reader thread; must
        marshal via ``call_from_thread`` so Textual widgets stay
        touched only from the app's event loop.

        The daemon side (ForwardingPresenter) is blocked on an
        Event.wait() with a per-turn timeout; if the user does nothing
        here the daemon times out on its own. That means we don't need
        to invent a client-side timeout — pushing the modal is enough.

        v6.12 HITL-DIAG hotfix: log every hop TO A DEDICATED FILE
        (/tmp/hitl-diag.log) so lines survive Textual's ANSI output
        collision on stderr. Also render a visible text banner in
        the LEFT pane so the user SEES a pending prompt even if the
        modal push fails silently."""
        _hdiag(f"HITL-DIAG _on_hitl_prompt: received prompt notification: {params}")
        from .hitl_modal import HitlModal

        # Copy params — the modal keeps a snapshot.
        snapshot = dict(params or {})
        # Stash for the keyboard fallback in case the modal doesn't render.
        self._pending_hitl_snapshot = snapshot

        def _emit_fallback_banner() -> None:
            """Print a visible banner into the LEFT pane. Runs on UI thread."""
            try:
                action = str(snapshot.get("action", "?"))
                target = str(snapshot.get("target", ""))
                tier = int(snapshot.get("tier", 0) or 0)
                banner = (
                    f"[bold yellow][HITL PENDING][/bold yellow] "
                    f"{action} {target} (Tier {tier}) — press "
                    f"[bold]Ctrl+A[/bold] to approve, [bold]Ctrl+D[/bold] to deny"
                )
                # left_log is the RichLog on the left pane; write() renders inline.
                for attr in ("_left_log", "left_log", "_output_log", "output_log"):
                    ll = getattr(self, attr, None)
                    if ll is not None and hasattr(ll, "write"):
                        ll.write(banner)
                        _hdiag(f"HITL-DIAG fallback banner written to {attr}")
                        return
                _hdiag("HITL-DIAG fallback banner: no left pane widget found")
            except Exception as exc:  # noqa: BLE001
                _hdiag(f"HITL-DIAG fallback banner failed: {type(exc).__name__}: {exc}")

        def _open() -> None:
            _hdiag("HITL-DIAG _on_hitl_prompt._open: on UI thread, about to push modal")
            try:
                self.push_screen(HitlModal(snapshot), self._handle_hitl_decision)
                _hdiag("HITL-DIAG _on_hitl_prompt._open: push_screen returned OK")
            except Exception as exc:  # noqa: BLE001 F-53
                _hdiag(f"HITL-DIAG _on_hitl_prompt._open: push_screen FAILED {type(exc).__name__}: {exc}")
            # Always emit the fallback banner too — cheap and visible.
            _emit_fallback_banner()

        # v3 hotfix (2026-07-22): Textual's call_from_thread REFUSES if
        # we're already on the app's event loop thread (RuntimeError:
        # "'call_from_thread' method must run in a different thread from
        # the app"). TextualDaemonClient._fire dispatches on the app
        # thread — so this handler already IS on the correct thread and
        # we should call _open directly. But if we're EVER called from a
        # background thread (future refactor), call_from_thread is right.
        # Try call_from_thread first; on the "same thread" RuntimeError,
        # call _open directly.
        try:
            self.call_from_thread(_open)
            _hdiag("HITL-DIAG _on_hitl_prompt: call_from_thread dispatched successfully")
        except RuntimeError as exc:
            msg = str(exc)
            if "different thread" in msg or "must run" in msg:
                _hdiag(f"HITL-DIAG _on_hitl_prompt: already on app thread ({msg!r}); calling _open() directly")
                try:
                    _open()
                except Exception as exc2:  # noqa: BLE001
                    _hdiag(f"HITL-DIAG _on_hitl_prompt: direct _open() FAILED {type(exc2).__name__}: {exc2}")
            else:
                _hdiag(f"HITL-DIAG _on_hitl_prompt: call_from_thread RuntimeError (unexpected shape): {msg!r}")
        except Exception as exc:  # noqa: BLE001 F-53
            _hdiag(f"HITL-DIAG _on_hitl_prompt: call_from_thread FAILED {type(exc).__name__}: {exc}")

    # v6.12 HITL-DIAG hotfix: bind Ctrl+A / Ctrl+D as always-available approve/deny
    # keys that fire regardless of modal state. Provides a resilient fallback UX
    # if HitlModal fails to render for any reason.
    def action_hitl_approve(self) -> None:
        _hdiag("HITL-DIAG action_hitl_approve keybinding fired")
        self._handle_hitl_decision("approved")

    def action_hitl_deny(self) -> None:
        _hdiag("HITL-DIAG action_hitl_deny keybinding fired")
        self._handle_hitl_decision("denied")

    def _on_hitl_lockout(self, params: dict) -> None:
        """Daemon acknowledged the lockout window. No-op on the client:
        the modal computes its own lockout deadline on construction
        from ``lockout_seconds`` so the countdown stays accurate even
        if this notification arrives after the modal opens. Kept as a
        listener so the event isn't silently dropped."""
        try:
            log.debug("hitl.lockout received: %s", params)
        except Exception:  # noqa: BLE001 — logger swallow
            pass

    def _handle_hitl_decision(self, decision: str | None) -> None:
        """Dispatched when the HitlModal.dismiss() fires. Sends the
        decision back to the daemon via ``client.respond_hitl()`` on a
        worker thread — the RPC call is blocking (JSON-RPC request)
        and MUST NOT run on the UI thread."""
        _hdiag(f"HITL-DIAG _handle_hitl_decision: called with decision={decision!r}")
        if not decision:
            _hdiag("HITL-DIAG _handle_hitl_decision: empty decision, returning early (modal dismissed without answer)")
            return
        client = self._daemon_client
        if client is None:
            _hdiag("HITL-DIAG _handle_hitl_decision: no client attached!")
            return

        def _send() -> None:
            try:
                _hdiag(f"HITL-DIAG _handle_hitl_decision._send: calling client.respond_hitl({decision!r})")
                client.respond_hitl(decision)
                _hdiag("HITL-DIAG _handle_hitl_decision._send: respond_hitl returned OK")
            except Exception as exc:  # noqa: BLE001 F-53
                _hdiag(f"HITL-DIAG _handle_hitl_decision._send: respond_hitl FAILED {type(exc).__name__}: {exc}")

        try:
            self.run_worker(_send, thread=True)
        except Exception as exc:  # noqa: BLE001 F-53
            log.warning("hitl.respond dispatch failed: %s: %s",
                        type(exc).__name__, exc)

    def on_resize(self) -> None:
        self._check_companion_width()

    def _check_companion_width(self) -> None:
        """Auto-hide companion panel when terminal is too narrow."""
        if self.size.width < _MIN_COMPANION_WIDTH:
            if self._companion_visible:
                self._companion_visible = False
                self._set_companion_hidden(True)
        else:
            if not self._companion_visible:
                self._companion_visible = True
                self._set_companion_hidden(False)

    def _set_companion_hidden(self, hidden: bool) -> None:
        try:
            panel = self.query_one(CompanionPanel)
            panel.set_class(hidden, "-hidden")
        except Exception as exc:
            # F-53 Scope A.P2: companion panel not present (torn down or
            # not yet mounted). Fall through; log so a real widget-lookup
            # regression is visible instead of a silent no-op layout.
            log.debug("tui.companion.set_hidden failed: %s: %s",
                      type(exc).__name__, exc)

    def action_toggle_companion(self) -> None:
        self._companion_visible = not self._companion_visible
        self._set_companion_hidden(not self._companion_visible)

    def action_toggle_nl_mode(self) -> None:
        """F2: toggle sticky NL mode."""
        is_nl = self._router.toggle_sticky()
        input_bar = self.query_one(InputBar)
        input_bar.nl_mode = is_nl

    async def on_input_bar_submitted(self, event: InputBar.Submitted) -> None:
        """Route submitted text via the InputRouter."""
        target, text = self._router.route(event.value)
        input_bar = self.query_one(InputBar)

        # F-50 (2026-07-09): if the InputBar visually indicates NL mode but
        # the router's state didn't route to NL (sticky toggle race, or F2
        # binding didn't propagate before the user hit Enter), force NL
        # routing. Users otherwise report "I pressed F2, NL is shown, my
        # query still went to shell — I have to type # every time".
        if input_bar.nl_mode and target != RouteTarget.NL:
            target = RouteTarget.NL
            # Strip any prefix so the daemon sees the same shape it does
            # via the `#`-triggered one-shot path.
            text = event.value.lstrip("#").lstrip()

        if self._router.oneshot_pending:
            pass
        if not self._router.sticky_nl:
            input_bar.nl_mode = False

        if target == RouteTarget.NL:
            await self._run_nl_turn(text)
        else:
            await self._run_shell(text)

    async def _run_shell(self, command: str) -> None:
        input_bar = self.query_one(InputBar)
        execution = self.query_one(ExecutionPanel)
        input_bar.set_working(True)
        try:
            await execution.run_command(command)
        finally:
            input_bar.set_working(False)

    async def _run_nl_turn(self, text: str) -> None:
        """Send NL text to the daemon and render results in companion panel."""
        input_bar = self.query_one(InputBar)
        companion = self.query_one(CompanionPanel)
        execution = self.query_one(ExecutionPanel)

        if self._daemon_client is None:
            from rich.text import Text
            log = execution.query_one("#shell-output")
            log.write(Text(
                "No daemon connection — NL mode requires --terminal with a running daemon.",
                style="bold #f87171",
            ))
            return

        input_bar.set_working(True)
        companion.clear()

        # V6B Stage 2: snapshot shell state (cwd, recent commands, user)
        # from the ExecutionPanel so QB can resolve "here" / "this folder"
        # / relative paths against the actual environment.
        try:
            ctx = execution.shell_context()
        except Exception as exc:
            # F-53 Scope A.P2: shell context capture failed. The turn
            # proceeds without a <context> preamble (QB just loses
            # cwd/recent-commands hints); log so operators tuning the
            # context path see why their hints didn't reach QB.
            log.debug("tui.shell_context capture failed: %s: %s",
                      type(exc).__name__, exc)
            ctx = None

        def _do_turn() -> dict:
            # F-50 belt-and-suspenders: daemon accepts raw NL text, but
            # older ib_trigger.bash paths may still route via `#` detection.
            # Prepend if the text is bare so the same string can flow through
            # both entry points. Idempotent.
            payload = text if text.startswith("#") else f"# {text}"
            return self._daemon_client.run_turn(payload, context=ctx)

        try:
            resp = await asyncio.to_thread(_do_turn)
        except Exception as exc:
            from rich.text import Text
            log = execution.query_one("#shell-output")
            log.write(Text(f"NL error: {exc}", style="bold #f87171"))
            return
        finally:
            input_bar.set_working(False)

        if "result" in resp:
            result = resp["result"]
            companion.handle_result(result)
            output = result.get("output", "")
            if output:
                from rich.text import Text
                log = execution.query_one("#shell-output")
                log.write(Text(output, style="#c0c0c0"))
        elif "error" in resp:
            from rich.text import Text
            log = execution.query_one("#shell-output")
            msg = resp["error"].get("message", "unknown error")
            log.write(Text(f"Error: {msg}", style="bold #f87171"))


def run(daemon_client: Any = None, startup_warning: str | None = None) -> None:
    """Entry point for `python -m terminal`."""
    app = AiTerminalApp(daemon_client=daemon_client, startup_warning=startup_warning)
    app.run()
