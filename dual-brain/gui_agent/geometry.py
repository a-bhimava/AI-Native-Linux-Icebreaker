"""Monitor layout + HiDPI translation for Fix V.

The VLM (Gemini vision) returns coordinates in the pixel space of the
image it received — which is the screenshot's PHYSICAL resolution.
Xdotool + AT-SPI operate in the X server's LOGICAL coordinate space.
On a HiDPI display those diverge (Retina 2x, Wayland fractional
scaling, etc.). Without translation every click would land at the
wrong pixel.

Design:
- ``MonitorLayout.detect()`` parses ``xrandr --query`` output to build
  a list of ``Monitor(name, origin_x, origin_y, width, height, scale)``.
- ``logical_to_physical(x, y)`` and ``physical_to_logical(x, y)``
  translate between the two coord spaces.
- ``find_monitor(x, y)`` picks the monitor a logical coord lands on.
- Detection is cached 30s (monitors don't move often; the cost of
  spawning ``xrandr`` on every click is significant on VMs).
- Falls back to single-monitor 1x scale if xrandr is missing or
  parsing fails — the module is fail-safe by construction.

Configuration:
- ``[gui.geometry] hidpi_scale_override = 0`` — 0 means "auto-detect
  via xrandr". Any positive integer forces that scale on every
  monitor, useful when xrandr misreports (e.g. some VM guests).
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Monitor:
    """One physical/logical display."""

    name: str
    origin_x: int   # logical
    origin_y: int   # logical
    width: int      # logical
    height: int     # logical
    scale: float    # physical_px / logical_px (2.0 on Retina)

    def contains(self, x: int, y: int) -> bool:
        """Whether logical (x, y) lands on this monitor."""
        return (self.origin_x <= x < self.origin_x + self.width
                and self.origin_y <= y < self.origin_y + self.height)


_XRANDR_LINE_RE = re.compile(
    # "eDP-1 connected primary 3840x2160+0+0 (normal ..." etc.
    # We match: name connected [primary] WxH+X+Y
    r"^(?P<name>\S+)\s+connected(?:\s+primary)?"
    r"\s+(?P<w>\d+)x(?P<h>\d+)\+(?P<x>\d+)\+(?P<y>\d+)"
)


class MonitorLayout:
    """Cached monitor layout with HiDPI-aware coord translation."""

    _CACHE: Optional["MonitorLayout"] = None
    _CACHE_TIME: float = 0.0
    _CACHE_TTL: float = 30.0

    def __init__(
        self,
        monitors: tuple[Monitor, ...],
        scale_override: int = 0,
    ) -> None:
        # Empty tuple is not a valid layout — always at least the fallback.
        self._monitors: tuple[Monitor, ...] = monitors or (_FALLBACK_MONITOR,)
        self._scale_override = scale_override

    # ── public API ─────────────────────────────────────────────────

    @classmethod
    def detect(cls, scale_override: int = 0, force_refresh: bool = False) -> "MonitorLayout":
        """Return a MonitorLayout, using the cache if fresh."""
        now = time.monotonic()
        if (not force_refresh and cls._CACHE is not None
                and now - cls._CACHE_TIME < cls._CACHE_TTL):
            return cls._CACHE
        layout = cls._detect_uncached(scale_override)
        cls._CACHE = layout
        cls._CACHE_TIME = now
        return layout

    @classmethod
    def invalidate_cache(cls) -> None:
        cls._CACHE = None
        cls._CACHE_TIME = 0.0

    @property
    def monitors(self) -> tuple[Monitor, ...]:
        return self._monitors

    def find_monitor(self, x: int, y: int) -> Monitor:
        """Return the monitor that (logical) (x, y) lands on. Defaults to
        the first (primary) monitor if the coord is off-screen — we still
        try to click, on the theory that a wrapping window manager or a
        stale VLM parse is better than silently dropping the action."""
        for m in self._monitors:
            if m.contains(x, y):
                return m
        return self._monitors[0]

    def logical_to_physical(self, x: int, y: int) -> tuple[int, int]:
        """Translate a logical coord to physical pixels within its monitor.

        Physical coord is what the VLM produces (screenshots are captured
        at physical resolution). Logical coord is what xdotool consumes.
        """
        m = self.find_monitor(x, y)
        # Offset within monitor, scaled by the monitor's scale factor.
        px = m.origin_x + int((x - m.origin_x) * m.scale)
        py = m.origin_y + int((y - m.origin_y) * m.scale)
        return (px, py)

    def physical_to_logical(self, x: int, y: int) -> tuple[int, int]:
        """Translate a physical coord (e.g. from the VLM) back to the
        logical coord xdotool needs."""
        # We don't know which monitor the physical coord falls on without
        # first mapping back through scale. Try each monitor: if the
        # physical / scale + origin lands inside it, that's the one.
        for m in self._monitors:
            # Physical inside this monitor's physical range?
            phys_x_end = m.origin_x + int(m.width * m.scale)
            phys_y_end = m.origin_y + int(m.height * m.scale)
            if (m.origin_x <= x < phys_x_end
                    and m.origin_y <= y < phys_y_end):
                lx = m.origin_x + int((x - m.origin_x) / m.scale)
                ly = m.origin_y + int((y - m.origin_y) / m.scale)
                return (lx, ly)
        # Fallback: assume primary monitor's scale.
        m = self._monitors[0]
        return (int(x / m.scale), int(y / m.scale))

    # ── internals ──────────────────────────────────────────────────

    @classmethod
    def _detect_uncached(cls, scale_override: int) -> "MonitorLayout":
        try:
            output = subprocess.check_output(
                ["xrandr", "--query"],
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                text=True,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
            _log.info("xrandr unavailable (%s) — using single-monitor 1x fallback", exc)
            return cls((_FALLBACK_MONITOR,), scale_override)

        monitors = _parse_xrandr(output, scale_override)
        if not monitors:
            _log.info("xrandr returned no connected monitors — using fallback")
            return cls((_FALLBACK_MONITOR,), scale_override)
        return cls(tuple(monitors), scale_override)


# ── Parsing ─────────────────────────────────────────────────────────────

_FALLBACK_MONITOR = Monitor(
    name="fallback",
    origin_x=0,
    origin_y=0,
    width=1920,
    height=1080,
    scale=1.0,
)


def _parse_xrandr(output: str, scale_override: int = 0) -> list[Monitor]:
    """Extract Monitor objects from ``xrandr --query`` output.

    We infer HiDPI scale by comparing the reported LOGICAL resolution
    (WxH+X+Y in the connected line) against the currently-active MODE
    (first * or + line after it, typically the physical resolution).
    Ratio > 1.0 → scaled display.

    ``scale_override`` (> 0) forces the same scale on every monitor,
    for cases where xrandr misreports (e.g. some VM guests).
    """
    monitors: list[Monitor] = []
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        m = _XRANDR_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        logical_w = int(m["w"])
        logical_h = int(m["h"])

        # Look ahead for the active mode line (starts with whitespace,
        # contains a res like "3840x2160", has * or + marker).
        physical_w = logical_w
        physical_h = logical_h
        j = i + 1
        while j < len(lines) and lines[j].startswith(("   ", "\t")):
            mode_match = re.match(r"^\s+(\d+)x(\d+)", lines[j])
            marker_match = re.search(r"[*+]", lines[j])
            if mode_match and marker_match:
                physical_w = int(mode_match.group(1))
                physical_h = int(mode_match.group(2))
                break
            j += 1

        if scale_override > 0:
            scale = float(scale_override)
        elif logical_w > 0 and physical_w > logical_w:
            scale = physical_w / logical_w
        else:
            scale = 1.0

        monitors.append(Monitor(
            name=m["name"],
            origin_x=int(m["x"]),
            origin_y=int(m["y"]),
            width=logical_w,
            height=logical_h,
            scale=scale,
        ))
        i = j
    return monitors


# ── Self-test ───────────────────────────────────────────────────────────

def _self_test() -> int:
    """``python -m gui_agent.geometry --self-test``."""
    layout = MonitorLayout.detect()
    print(f"geometry OK: {len(layout.monitors)} monitor(s) detected", flush=True)
    for m in layout.monitors:
        print(f"  {m.name}: logical {m.width}x{m.height}+{m.origin_x}+{m.origin_y} "
              f"scale={m.scale:.2f}", flush=True)
    return 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print("usage: python -m gui_agent.geometry --self-test",
          file=__import__("sys").stderr)
    sys.exit(2)
