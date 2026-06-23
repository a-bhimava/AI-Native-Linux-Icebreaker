"""Chain-of-thought expander — collapsible card showing CoT steps.

Each step is a sanitized label with a state icon (pending/done/error).
The expander header shows the count of steps and auto-updates as
the daemon streams turn.cot notifications.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ..widgets import _sanitize


class CotStep(Gtk.Box):
    """Single chain-of-thought step with icon and text."""

    _ICONS = {
        "pending": "content-loading-symbolic",
        "done": "object-select-symbolic",
        "error": "dialog-error-symbolic",
    }

    def __init__(self, text: str, state: str = "pending") -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(4)
        self.set_margin_bottom(4)

        self._icon = Gtk.Image.new_from_icon_name(self._ICONS.get(state, self._ICONS["pending"]))
        self._icon.set_pixel_size(16)
        self.append(self._icon)

        self._label = Gtk.Label(label=_sanitize(text))
        self._label.set_xalign(0)
        self._label.set_wrap(True)
        self._label.set_hexpand(True)
        self._label.add_css_class("ib-mono")
        self._label.add_css_class("ib-muted-text")
        self.append(self._label)

    def set_state(self, state: str) -> None:
        icon_name = self._ICONS.get(state, self._ICONS["pending"])
        self._icon.set_from_icon_name(icon_name)


class CotExpander(Gtk.Box):
    """Expandable chain-of-thought container.

    Shows a clickable header "Chain of Thought (N steps)" and a
    collapsible body with individual CotStep rows.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("ib-card")
        self.set_margin_top(4)
        self.set_margin_bottom(4)

        self._steps: list[CotStep] = []
        self._expanded = False

        self._header_btn = Gtk.Button()
        self._header_btn.add_css_class("flat")
        self._header_btn.set_hexpand(True)

        self._header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._header_box.set_margin_start(8)
        self._header_box.set_margin_end(8)

        self._arrow = Gtk.Image.new_from_icon_name("pan-end-symbolic")
        self._arrow.set_pixel_size(16)
        self._header_box.append(self._arrow)

        self._header_label = Gtk.Label(label="Chain of Thought")
        self._header_label.set_xalign(0)
        self._header_label.add_css_class("ib-muted-text")
        self._header_box.append(self._header_label)

        self._header_btn.set_child(self._header_box)
        self._header_btn.connect("clicked", self._toggle)
        self.append(self._header_btn)

        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._body.set_visible(False)
        self.append(self._body)

    def _toggle(self, _btn: Gtk.Button) -> None:
        self._expanded = not self._expanded
        self._body.set_visible(self._expanded)
        icon = "pan-down-symbolic" if self._expanded else "pan-end-symbolic"
        self._arrow.set_from_icon_name(icon)

    def _update_header(self) -> None:
        n = len(self._steps)
        label = f"Chain of Thought ({n} step{'s' if n != 1 else ''})"
        self._header_label.set_label(label)

    def add_step(self, text: str, state: str = "pending") -> CotStep:
        step = CotStep(text, state)
        self._steps.append(step)
        self._body.append(step)
        self._update_header()
        return step

    def complete_last(self) -> None:
        if self._steps:
            self._steps[-1].set_state("done")

    def error_last(self) -> None:
        if self._steps:
            self._steps[-1].set_state("error")

    def clear(self) -> None:
        for step in self._steps:
            self._body.remove(step)
        self._steps.clear()
        self._update_header()

    @property
    def step_count(self) -> int:
        return len(self._steps)
