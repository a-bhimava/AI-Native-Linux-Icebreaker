"""Message row — single conversation bubble (user / assistant / system).

User messages are right-aligned with primary color background.
Assistant messages are left-aligned with card background, and include
optional CotExpander and TierBadge.
System messages are centered with muted styling.

All text goes through BP-3 sanitization before display.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango

from ..widgets import _sanitize
from .cot_expander import CotExpander


class MessageRow(Gtk.Box):
    """Single message bubble in the conversation."""

    def __init__(self, role: str, text: str, **kwargs: object) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.role = role
        self._cot: Optional[CotExpander] = None
        self._label: Optional[Gtk.Label] = None
        self._container: Optional[Gtk.Box] = None
        self._status_box: Optional[Gtk.Box] = None

        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(4)
        self.set_margin_bottom(4)

        if role == "user":
            self._build_user(text)
        elif role == "assistant":
            self._build_assistant(text, **kwargs)
        else:
            self._build_system(text)

    def _build_user(self, text: str) -> None:
        self.set_halign(Gtk.Align.END)

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bubble.add_css_class("ib-elevated")
        bubble.add_css_class("ib-primary")
        bubble.add_css_class("ib-primary-foreground")

        label = Gtk.Label(label=_sanitize(text))
        label.set_xalign(0)
        label.set_wrap(True)
        label.set_max_width_chars(60)
        label.set_margin_start(14)
        label.set_margin_end(14)
        label.set_margin_top(10)
        label.set_margin_bottom(10)
        bubble.append(label)
        self.append(bubble)

    def _build_assistant(self, text: str, **kwargs: object) -> None:
        self.set_halign(Gtk.Align.START)

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        container.set_hexpand(True)

        self._cot = CotExpander()
        self._cot.set_visible(False)
        container.append(self._cot)

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bubble.add_css_class("ib-elevated")
        bubble.set_margin_top(4)
        bubble.set_margin_bottom(4)

        sanitized = _sanitize(text)
        if "```" in sanitized:
            self._build_code_blocks(bubble, sanitized)
        else:
            self._label = Gtk.Label(label=sanitized)
            self._label.set_xalign(0)
            self._label.set_wrap(True)
            self._label.set_max_width_chars(70)
            self._label.set_margin_start(12)
            self._label.set_margin_end(12)
            self._label.set_margin_top(8)
            self._label.set_margin_bottom(8)
            self._label.set_selectable(True)
            bubble.append(self._label)

        container.append(bubble)
        self._container = container

        tier = kwargs.get("tier")
        backend = kwargs.get("backend", "")
        latency_ms = kwargs.get("latency_ms")
        cost = kwargs.get("cost")

        if any(v is not None for v in (tier, latency_ms, cost)):
            self._status_box = self._build_status_line(tier, backend, latency_ms, cost)
            container.append(self._status_box)

        self.append(container)

    def _build_system(self, text: str) -> None:
        self.set_halign(Gtk.Align.CENTER)

        label = Gtk.Label(label=_sanitize(text))
        label.set_xalign(0.5)
        label.set_wrap(True)
        label.set_max_width_chars(50)
        label.add_css_class("ib-muted-text")
        label.set_margin_top(8)
        label.set_margin_bottom(8)
        self.append(label)

    def _build_code_blocks(self, parent: Gtk.Box, text: str) -> None:
        """Split text on ``` fences into prose and monospace blocks."""
        parts = text.split("```")
        for i, part in enumerate(parts):
            part = part.strip()
            if not part:
                continue
            label = Gtk.Label(label=part)
            label.set_xalign(0)
            label.set_wrap(True)
            label.set_max_width_chars(70)
            label.set_selectable(True)
            label.set_margin_start(12)
            label.set_margin_end(12)
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            if i % 2 == 1:
                label.add_css_class("ib-mono")
                label.add_css_class("ib-input")
            parent.append(label)

    def _build_status_line(self, tier: Optional[int], backend: str,
                           latency_ms: Optional[int],
                           cost: Optional[float]) -> Gtk.Box:
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status.set_margin_start(12)
        status.set_margin_top(2)
        status.set_margin_bottom(2)

        parts: list[str] = []
        if tier is not None:
            parts.append(f"Tier {tier}")
        if backend:
            parts.append(backend)
        if latency_ms is not None:
            parts.append(f"{latency_ms}ms")
        if cost is not None:
            parts.append(f"${cost:.4f}")

        label = Gtk.Label(label=" · ".join(parts))
        label.add_css_class("ib-muted-text")
        label.set_xalign(0)
        status.append(label)
        return status

    def set_text(self, text: str) -> None:
        if self._label is not None:
            self._label.set_label(_sanitize(text))

    def update_metadata(self, *, tier: Optional[int] = None,
                        backend: str = "",
                        duration_ms: Optional[int] = None,
                        cost_usd: Optional[float] = None) -> None:
        if self._container is None:
            return
        if self._status_box is not None:
            self._container.remove(self._status_box)
            self._status_box = None
        if any(v is not None for v in (tier, duration_ms, cost_usd)):
            self._status_box = self._build_status_line(
                tier, backend, duration_ms, cost_usd,
            )
            self._container.append(self._status_box)

    @property
    def cot(self) -> Optional[CotExpander]:
        return self._cot
