"""HitlModal — in-TUI approval overlay (v6.10 Task #152 / F-61 fix).

Renders the Textual ModalScreen shown when the daemon forwards a
``hitl.prompt`` notification. Before this widget existed, the AI
Terminal client received the notification, fired an ``hitl_prompt``
event on the DaemonClient, but nothing was subscribed — so the daemon
timed out after 30s and every Tier ≥ 1 intent silently died with
``hitl_timeout``.

Threading contract:
- Constructed on the DaemonClient reader thread (call point is
  ``AiTerminalApp._on_hitl_prompt``).
- Pushed to the app via ``call_from_thread(push_screen, modal, cb)``
  so the widget touches Textual state on the UI thread only.
- Key bindings dispatch synchronously to ``self.dismiss(decision)``.
- Decision callback dispatches ``client.respond_hitl(decision)`` on a
  worker thread — the send is blocking (JSON-RPC request) and MUST NOT
  run on the UI thread.

INV-6 lockout:
- ``lockout_seconds`` (default 3) after ``show_prompt`` the APPROVE
  key is disabled. A monotonic timer re-enables it. Overriding here
  matches the daemon-side ``ForwardingPresenter.lockout()`` behavior.
- ESC hard-reserved to DENY (matches keymap contract).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


log = logging.getLogger(__name__)


# Tier → (label, color) for the badge shown in the header.
_TIER_STYLE = {
    0: ("Tier 0", "#5e8787"),   # teal — auto-execute, should not reach here
    1: ("Tier 1", "#faca97"),   # golden — write in $HOME
    2: ("Tier 2", "#faca97"),   # golden — system write
    3: ("Tier 3", "#f87171"),   # red — destructive / outside home
}


class HitlModal(ModalScreen[str]):
    """Full-screen modal that shows the HITL prompt + captures the
    decision keypress. Returns the decision string via ``dismiss()``.

    Decision strings match ``controller.hitl.Decision.value``:
        "approved" | "denied" | "modify" | "explain" | "trust"

    The timeout branch does not dismiss with "timeout" — that path is
    owned by the daemon-side ``ForwardingPresenter`` which decides
    Decision.TIMEOUT when its event.wait() expires. If the user does
    nothing here we just don't send a response; daemon times out on
    its own after ``timeout_seconds``.
    """

    # v6 hotfix (2026-07-22): AUTO_FOCUS points at a hidden focusable
    # Button in the modal so ModalScreen has a widget to focus on
    # mount. Without a focusable child, `app.focused` stays None and
    # key events don't route to the modal's Bindings (proven by v5
    # diagnostic showing both BEFORE and AFTER app.focused=None).
    AUTO_FOCUS = "#hitl-focus-anchor"

    # Shrink the focus anchor to 1×1 (essentially invisible) — it's a
    # focus target only, NOT display:none which would remove it from
    # the widget tree and defeat the focus purpose.
    DEFAULT_CSS = """
    #hitl-focus-anchor {
        width: 1;
        height: 1;
        min-width: 1;
        min-height: 1;
        padding: 0;
        margin: 0;
        border: none;
        background: transparent;
        color: transparent;
    }
    """

    # ESC is hard-reserved to DENY per keymap contract (SEC-1).
    # Number keys + letters cover the standard keymap (Approve, Deny,
    # Modify, Explain, Trust). "y" and "n" get free ergonomic bonuses.
    BINDINGS = [
        Binding("escape", "deny", "Deny", show=True),
        Binding("1", "approve_if_unlocked", "Approve", show=True),
        Binding("a", "approve_if_unlocked", "Approve", show=False),
        Binding("y", "approve_if_unlocked", "Approve", show=False),
        Binding("2", "deny", "Deny", show=True),
        Binding("d", "deny", "Deny", show=False),
        Binding("n", "deny", "Deny", show=False),
        Binding("3", "modify", "Modify", show=True),
        Binding("m", "modify", "Modify", show=False),
        Binding("4", "explain", "Explain", show=True),
        Binding("e", "explain", "Explain", show=False),
        Binding("5", "trust", "Trust", show=True),
        Binding("t", "trust", "Trust", show=False),
    ]

    def __init__(
        self,
        params: dict,
        lockout_seconds: int = 3,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._params = dict(params or {})
        self._lockout_seconds = max(0, int(lockout_seconds))
        self._locked_until = time.monotonic() + self._lockout_seconds
        # Countdown label mutated by a Textual set_interval so the user
        # sees seconds ticking down instead of a static number.
        self._countdown_label: Optional[Label] = None

    # ── Compose ────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        action = str(self._params.get("action", "") or "?")
        target = str(self._params.get("target", "") or "")
        tier = int(self._params.get("tier", 2) or 2)
        risk_level = str(self._params.get("risk_level", "") or "")
        reason = str(self._params.get("reason", "") or "")
        cow_summary = self._params.get("cow_summary") or ""
        blocked = str(self._params.get("blocked_pattern") or "")

        tier_label, tier_color = _TIER_STYLE.get(tier, (f"Tier {tier}", "#faca97"))

        with Vertical(id="hitl-modal-root"):
            # v6 hotfix: invisible focusable anchor so ModalScreen has
            # a widget to focus on mount, allowing key events to route
            # to the modal's BINDINGS via the widget→screen bubble path.
            yield Button("", id="hitl-focus-anchor")
            yield Label(
                f"[b {tier_color}]{tier_label}[/]  [b]{action}[/]",
                id="hitl-header",
            )
            if target:
                yield Static(f"target: {target}", id="hitl-target")
            if risk_level:
                yield Static(f"risk_level: {risk_level}", id="hitl-risk")
            if reason:
                yield Static(f"reason: {reason}", id="hitl-reason")
            if blocked:
                yield Static(f"[b #f87171]blocked pattern:[/] {blocked}",
                             id="hitl-blocked")

            if cow_summary:
                with VerticalScroll(id="hitl-cow-scroll"):
                    yield Label("[b]COW dry-run preview:[/]",
                                id="hitl-cow-label")
                    yield Static(cow_summary, id="hitl-cow-body")

            self._countdown_label = Label(
                self._countdown_text(),
                id="hitl-countdown",
            )
            yield self._countdown_label

            yield Label(
                "  [b #5e8787]A[/]/1/y approve   "
                "[b #f87171]D[/]/2/n/Esc deny   "
                "[b]E[/]/4 explain   "
                "[b]M[/]/3 modify   "
                "[b]T[/]/5 trust",
                id="hitl-keys",
            )

    # ── Lifecycle + tick ───────────────────────────────────────────────

    def on_mount(self) -> None:
        # Tick every 250 ms while locked — enough to look responsive.
        self.set_interval(0.25, self._tick_countdown)
        # v7 (2026-07-22): DO NOT call self.app.set_focus() here.
        # ModalScreen.AUTO_FOCUS above already focuses the hidden anchor
        # Button, which is the only thing that makes key events dispatch
        # through the modal's BINDINGS. v5's set_focus(None) + set_focus(self)
        # ran AFTER AUTO_FOCUS and destroyed focus because Screen has
        # can_focus=False, silently dropping focus back to None — that's
        # why v6 log showed `BEFORE: app.focused=Button(...)` then
        # `AFTER: app.focused=None` then keys reached on_key but Bindings
        # never fired. Just log what AUTO_FOCUS produced and leave it.
        try:
            import os as _os, time as _time
            with open("/tmp/hitl-diag.log", "a") as _f:
                _now = None
                try:
                    _now = self.app.focused
                except Exception:
                    pass
                _f.write(
                    f"{_time.strftime('%Y-%m-%d %H:%M:%S')} [modal pid={_os.getpid()}] "
                    f"HITL-DIAG HitlModal.on_mount AUTO_FOCUS produced: "
                    f"app.focused={_now!r}\n"
                )
        except Exception:
            pass

    def on_key(self, event) -> None:  # type: ignore[override]
        """v8 (2026-07-23): handle keys DIRECTLY here instead of going
        through class-level BINDINGS. v7 log proved keys reach this
        handler (`HITL-DIAG HitlModal.on_key: key='a'`) with focus on
        the AUTO_FOCUS anchor Button, but no action_* fired — Textual's
        BINDINGS dispatch doesn't reach the modal for reasons that
        aren't worth debugging further when we can just call
        self.dismiss(decision) here and be done with it."""
        try:
            import os as _os, time as _time
            with open("/tmp/hitl-diag.log", "a") as _f:
                _f.write(f"{_time.strftime('%Y-%m-%d %H:%M:%S')} [modal pid={_os.getpid()}] "
                         f"HITL-DIAG HitlModal.on_key: key={event.key!r} name={event.name!r}\n")
        except Exception:
            pass

        k = event.key
        # Approve family: check INV-6 lockout first.
        if k in ("a", "y", "1"):
            if time.monotonic() < self._locked_until:
                # Silently swallow during the 3s lockout — countdown
                # label already tells the user why.
                event.stop()
                return
            try:
                event.stop()
            except Exception:
                pass
            self.dismiss("approved")
            return
        # Deny family (Esc reserved per SEC-1).
        if k in ("d", "n", "2", "escape", "ctrl+c", "ctrl+d"):
            try:
                event.stop()
            except Exception:
                pass
            self.dismiss("denied")
            return
        if k in ("m", "3"):
            try:
                event.stop()
            except Exception:
                pass
            self.dismiss("modify")
            return
        if k in ("e", "4"):
            try:
                event.stop()
            except Exception:
                pass
            self.dismiss("explain")
            return
        if k in ("t", "5"):
            try:
                event.stop()
            except Exception:
                pass
            self.dismiss("trust")
            return

    def _countdown_text(self) -> str:
        remaining = int(max(0, self._locked_until - time.monotonic()))
        if remaining > 0:
            return (f"[#faca97]Approve unlocks in {remaining}s "
                    "(INV-6 lockout)[/]")
        return "[#5e8787]Approve ready[/]"

    def _tick_countdown(self) -> None:
        if self._countdown_label is None:
            return
        try:
            self._countdown_label.update(self._countdown_text())
        except Exception as exc:  # noqa: BLE001 F-53
            log.debug("hitl.countdown update failed: %s: %s",
                      type(exc).__name__, exc)

    # ── Actions ────────────────────────────────────────────────────────

    def action_approve_if_unlocked(self) -> None:
        if time.monotonic() < self._locked_until:
            # Ignore — visual feedback via countdown; don't dismiss.
            return
        self.dismiss("approved")

    def action_deny(self) -> None:
        self.dismiss("denied")

    def action_modify(self) -> None:
        self.dismiss("modify")

    def action_explain(self) -> None:
        self.dismiss("explain")

    def action_trust(self) -> None:
        self.dismiss("trust")

    # ── Keymap safety net ──────────────────────────────────────────────
    # (on_key is defined earlier in the class as part of v4 hotfix
    # with diagnostic logging. The class-level BINDINGS still dispatch
    # action_* handlers for defined keys.)
