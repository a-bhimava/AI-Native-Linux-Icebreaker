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
from textual.widgets import Label, Static


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

    def on_key(self, event: events.Key) -> None:
        """Catch any keypress that isn't in BINDINGS so the modal never
        becomes a black hole. Everything unhandled is a no-op (the
        user has to press one of the documented decision keys)."""
        if event.key in {"ctrl+c", "ctrl+d"}:
            # Explicit deny path — treat kill-shortcut as "get me out".
            self.dismiss("denied")
