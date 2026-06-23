"""Shared GTK4 widget library for Icebreaker desktop components.

Every widget applies Icebreaker design-token CSS classes (``ib-*``) so
the look and feel stays consistent across all GUI windows.

Widgets (imported lazily — require PyGObject at runtime):
  - ``TierBadge``       — risk tier pill (T0 green / T1 amber / T2 red)
  - ``StatusDot``       — pulsing connection-status indicator
  - ``SanitizedLabel``  — label that strips ANSI/C0/C1 control chars (BP-3)
  - ``LockoutButton``   — approve button with INV-6 countdown lockout
  - ``KeyValueRow``     — two-column label: value row for detail views

The ``_sanitize`` function is importable without PyGObject for testing.
"""

from __future__ import annotations

import re
import time
from typing import Callable, Optional

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?\x07")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _sanitize(text: str) -> str:
    """Strip ANSI escapes and C0/C1 control characters (BP-3)."""
    text = _ANSI_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    return text.replace("\r", "")


def _ensure_gtk():
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
    return Gtk, GLib


# ---------------------------------------------------------------------------
# GTK widget classes — defined at first import of the containing module.
# We defer class bodies so that `from gui.widgets import _sanitize` works
# even without PyGObject installed (needed for headless test environments).
# ---------------------------------------------------------------------------

_widget_classes_loaded = False


def _load_widget_classes() -> None:
    global _widget_classes_loaded, TierBadge, StatusDot, SanitizedLabel, LockoutButton, KeyValueRow
    if _widget_classes_loaded:
        return

    Gtk, GLib = _ensure_gtk()

    class _TierBadge(Gtk.Label):
        _TIER_CLASSES = {0: "ib-tier-0", 1: "ib-tier-1", 2: "ib-tier-2"}

        def __init__(self, tier: int = 0) -> None:
            super().__init__()
            self.set_tier(tier)

        def set_tier(self, tier: int) -> None:
            for cls in self._TIER_CLASSES.values():
                self.remove_css_class(cls)
            self.set_text(f"T{tier}")
            css_cls = self._TIER_CLASSES.get(tier, "ib-tier-2")
            self.add_css_class(css_cls)
            self.add_css_class("ib-badge")

    class _StatusDot(Gtk.DrawingArea):
        def __init__(self, connected: bool = False) -> None:
            super().__init__()
            self._connected = connected
            self.set_size_request(12, 12)
            self.set_draw_func(self._draw)

        def set_connected(self, connected: bool) -> None:
            self._connected = connected
            self.queue_draw()

        def _draw(self, area: object, cr: object, width: int, height: int) -> None:
            color = (0.34, 0.74, 0.34) if self._connected else (0.6, 0.6, 0.6)
            cr.arc(width / 2, height / 2, min(width, height) / 2 - 1, 0, 6.283)
            cr.set_source_rgb(*color)
            cr.fill()

    class _SanitizedLabel(Gtk.Label):
        def __init__(self, text: str = "", **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.set_sanitized_text(text)

        def set_sanitized_text(self, text: str) -> None:
            self.set_text(_sanitize(text))

        def set_sanitized_markup(self, markup: str) -> None:
            self.set_markup(_sanitize(markup))

    class _LockoutButton(Gtk.Button):
        def __init__(
            self,
            label: str = "Approve",
            lockout_seconds: int = 3,
            on_click: Optional[Callable[[], None]] = None,
        ) -> None:
            super().__init__()
            self._base_label = label
            self._lockout = lockout_seconds
            self._on_click = on_click
            self._start_mono: float = 0.0
            self.set_sensitive(False)
            self.add_css_class("ib-primary-button")
            if on_click is not None:
                self.connect("clicked", lambda _btn: on_click())

        def start_lockout(self) -> None:
            self._start_mono = time.monotonic()
            self.set_sensitive(False)
            self._tick()

        def _tick(self) -> bool:
            elapsed = time.monotonic() - self._start_mono
            remaining = self._lockout - elapsed
            if remaining <= 0:
                self.set_label(self._base_label)
                self.set_sensitive(True)
                return False
            self.set_label(f"{self._base_label} ({int(remaining) + 1}s)")
            GLib.timeout_add(250, self._tick)
            return False

    class _KeyValueRow(Gtk.Box):
        def __init__(self, key: str, value: str, key_width: int = 120) -> None:
            super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self._key_label = Gtk.Label(label=f"{key}:")
            self._key_label.set_xalign(0)
            self._key_label.set_size_request(key_width, -1)
            self._key_label.add_css_class("ib-muted-text")

            self._value_label = _SanitizedLabel(text=value)
            self._value_label.set_xalign(0)
            self._value_label.set_wrap(True)
            self._value_label.set_hexpand(True)

            self.append(self._key_label)
            self.append(self._value_label)

        def set_value(self, value: str) -> None:
            self._value_label.set_sanitized_text(value)

    TierBadge = _TierBadge
    StatusDot = _StatusDot
    SanitizedLabel = _SanitizedLabel
    LockoutButton = _LockoutButton
    KeyValueRow = _KeyValueRow
    _widget_classes_loaded = True
