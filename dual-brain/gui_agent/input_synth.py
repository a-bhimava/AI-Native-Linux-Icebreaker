"""Keyboard + mouse synthesis via ``xdotool`` for Fix V.

F-51 marker: F-103-input-synth (per incremental/GROUND_TRUTH.md F-103).

Wraps ``xdotool`` in a narrow, allowlisted, timeout-bounded API. The
GuiAgent (see ``agent.py``) calls these from its ``_handle_click_at_coords``,
``_handle_type_at_coords``, ``_handle_drag`` etc. handlers.

Design:
- **No shell=True.** Every subprocess.run() takes an explicit argv list.
- **Every mouse fn** translates logical coords through
  ``MonitorLayout.logical_to_physical`` first, so the VLM-provided
  physical coord lands at the right screen pixel on HiDPI.
- **Every argument is validated:** button ∈ {left, right, middle};
  direction ∈ {up, down, left, right}; text ≤ 4096 chars; key combos
  parsed + each token checked against an explicit allowlist
  (no ``xdotool key "$(rm -rf /)"`` injection).
- **Every call is bounded** (5s subprocess timeout) so a hung xdotool
  can't wedge the controller.
- Returns ``ActionResult(success, error, latency_ms)`` — mirrors the
  shape ``atspi.py`` produces so callers can't tell whether the click
  went through the a11y tree or the pixel path.

Security note: xdotool is trusted because it runs as the `icebreaker`
user under the existing X session — same privilege boundary as the
seat's other userland processes. The sandbox in ``sandbox.py`` (V.6)
adds seccomp/Landlock to bound the blast radius further.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from gui_agent.geometry import MonitorLayout

_log = logging.getLogger(__name__)


# ── Errors & result shape ───────────────────────────────────────────────

class InputSynthError(Exception):
    """xdotool wrapper couldn't complete the requested action."""


class InputSynthUnavailable(InputSynthError):
    """xdotool is not installed on PATH."""


class InputSynthValidationError(InputSynthError):
    """Argument failed the allowlist / bounds check."""


@dataclass(frozen=True)
class ActionResult:
    success: bool
    error: str = ""
    latency_ms: int = 0
    physical_coords: Optional[tuple[int, int]] = None
    logical_coords: Optional[tuple[int, int]] = None
    extra: dict = field(default_factory=dict)


# ── Allowlists ──────────────────────────────────────────────────────────

_XDOTOOL_BIN = "xdotool"

_BUTTON_TO_NUMBER: dict[str, int] = {
    "left": 1,
    "middle": 2,
    "right": 3,
}

# xdotool wheel-scroll uses buttons 4/5 (vertical) and 6/7 (horizontal).
_SCROLL_TO_BUTTON: dict[str, int] = {
    "up": 4,
    "down": 5,
    "left": 6,
    "right": 7,
}

# Every key that may appear in press_key/key_sequence combos. Adding a
# new key means adding it here — the allowlist is the whole point of
# rejecting arbitrary xdotool key strings.
_ALLOWED_KEY_TOKENS: frozenset[str] = frozenset({
    # Modifiers
    "ctrl", "control", "shift", "alt", "meta", "super", "cmd", "command",
    # Letters (single char handled by regex below)
    # Digits (single char handled by regex below)
    # Common named keys
    "return", "enter", "escape", "esc", "tab", "space", "backspace",
    "delete", "insert", "home", "end", "pageup", "pagedown",
    "up", "down", "left", "right",
    "prior", "next",
    # Function keys
    *(f"f{i}" for i in range(1, 25)),
    # Punctuation names xdotool accepts
    "minus", "equal", "plus", "underscore",
    "bracketleft", "bracketright", "braceleft", "braceright",
    "semicolon", "colon", "apostrophe", "quotedbl", "grave", "asciitilde",
    "comma", "period", "slash", "backslash", "question", "at",
    "numbersign", "dollar", "percent", "asciicircum", "ampersand",
    "asterisk", "parenleft", "parenright", "exclam",
})

_MAX_TEXT_LEN = 4096
_MAX_KEY_COMBO_TOKENS = 6           # ctrl+shift+alt+F12 → 4 tokens; 6 is generous
_MAX_KEY_SEQUENCE_ITEMS = 32
_MAX_WAYPOINTS = 8
_SUBPROCESS_TIMEOUT = 5.0

_KEY_TOKEN_RE = re.compile(r"^[A-Za-z0-9]$|^[A-Za-z][A-Za-z0-9]{1,15}$")

_COORD_MIN = 0
_COORD_MAX = 32768


# ── Availability ────────────────────────────────────────────────────────

def check_available() -> bool:
    """Return True if xdotool is on PATH."""
    try:
        subprocess.run(
            [_XDOTOOL_BIN, "--version"],
            check=True, capture_output=True, timeout=1.0,
        )
        return True
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False


# ── Coord + argument validation ────────────────────────────────────────

def _validate_coord(x: int, y: int, label: str = "coord") -> None:
    if not (isinstance(x, int) and isinstance(y, int)):
        raise InputSynthValidationError(f"{label}: x, y must be int, got {type(x).__name__}, {type(y).__name__}")
    if not (_COORD_MIN <= x <= _COORD_MAX and _COORD_MIN <= y <= _COORD_MAX):
        raise InputSynthValidationError(
            f"{label}: out of bounds ({x}, {y}) — expected {_COORD_MIN}..{_COORD_MAX}"
        )


def _validate_button(button: str) -> int:
    if button not in _BUTTON_TO_NUMBER:
        raise InputSynthValidationError(
            f"button: {button!r} — expected one of {sorted(_BUTTON_TO_NUMBER)}"
        )
    return _BUTTON_TO_NUMBER[button]


def _validate_scroll_direction(direction: str) -> int:
    if direction not in _SCROLL_TO_BUTTON:
        raise InputSynthValidationError(
            f"scroll direction: {direction!r} — expected one of {sorted(_SCROLL_TO_BUTTON)}"
        )
    return _SCROLL_TO_BUTTON[direction]


def _validate_key_combo(combo: str) -> str:
    """Validate a single key combo like 'ctrl+s' or 'F12'.

    Returns the combo in the canonical form xdotool wants (unchanged,
    just verified safe).
    """
    if not isinstance(combo, str) or not combo:
        raise InputSynthValidationError(f"key combo: must be non-empty str, got {combo!r}")
    if len(combo) > 64:
        raise InputSynthValidationError(f"key combo: too long ({len(combo)} > 64)")
    parts = combo.split("+")
    if len(parts) > _MAX_KEY_COMBO_TOKENS:
        raise InputSynthValidationError(
            f"key combo: too many tokens ({len(parts)} > {_MAX_KEY_COMBO_TOKENS})"
        )
    for token in parts:
        if not token:
            raise InputSynthValidationError(f"key combo: empty token in {combo!r}")
        if not _KEY_TOKEN_RE.match(token):
            raise InputSynthValidationError(
                f"key combo: token {token!r} contains disallowed chars"
            )
        # Single alnum char is fine as-is; multi-char must be in allowlist.
        if len(token) > 1 and token.lower() not in _ALLOWED_KEY_TOKENS:
            raise InputSynthValidationError(
                f"key combo: token {token!r} not in allowlist"
            )
    return combo


def _validate_text(text: str) -> str:
    if not isinstance(text, str):
        raise InputSynthValidationError(f"text: must be str, got {type(text).__name__}")
    if len(text) > _MAX_TEXT_LEN:
        raise InputSynthValidationError(
            f"text: too long ({len(text)} > {_MAX_TEXT_LEN})"
        )
    # C0/C1 control chars other than \n \t \r are stripped for safety
    # (BP-3 — never let untrusted text redraw a terminal). Emoji + full
    # Unicode text is fine — xdotool passes it through.
    allowed_controls = {"\n", "\t", "\r"}
    if any(ord(c) < 0x20 and c not in allowed_controls for c in text):
        raise InputSynthValidationError("text: contains control characters")
    if any(0x7f <= ord(c) < 0xa0 for c in text):
        raise InputSynthValidationError("text: contains C1 control characters")
    return text


# ── xdotool invocation ────────────────────────────────────────────────

def _run_xdotool(argv: list[str], op: str) -> ActionResult:
    """Run xdotool with a bounded timeout. Return ActionResult."""
    t_start = time.monotonic()
    try:
        proc = subprocess.run(
            [_XDOTOOL_BIN, *argv],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        raise InputSynthUnavailable("xdotool not installed on PATH") from None
    except subprocess.TimeoutExpired:
        return ActionResult(
            success=False,
            error=f"{op}: xdotool timed out after {_SUBPROCESS_TIMEOUT}s",
            latency_ms=int((time.monotonic() - t_start) * 1000),
        )
    except OSError as exc:
        return ActionResult(
            success=False,
            error=f"{op}: OSError: {exc}",
            latency_ms=int((time.monotonic() - t_start) * 1000),
        )
    latency_ms = int((time.monotonic() - t_start) * 1000)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[:500].strip()
        return ActionResult(
            success=False,
            error=f"{op}: xdotool rc={proc.returncode}: {stderr}",
            latency_ms=latency_ms,
        )
    return ActionResult(success=True, latency_ms=latency_ms)


def _translate(
    x: int, y: int,
    layout: Optional[MonitorLayout] = None,
) -> tuple[int, int, int, int]:
    """Translate logical→physical (or pass through if no layout).

    Returns (logical_x, logical_y, physical_x, physical_y) so ActionResult
    can record both for audit."""
    if layout is None:
        return (x, y, x, y)
    px, py = layout.logical_to_physical(x, y)
    return (x, y, px, py)


# ── Public API ────────────────────────────────────────────────────────

def click(
    x: int, y: int,
    button: str = "left",
    count: int = 1,
    layout: Optional[MonitorLayout] = None,
) -> ActionResult:
    """Click ``count`` times at physical (x, y). count ∈ {1, 2, 3}."""
    _validate_coord(x, y, "click")
    btn = _validate_button(button)
    if count not in (1, 2, 3):
        raise InputSynthValidationError(f"count: must be 1, 2, or 3, got {count}")
    lx, ly, px, py = _translate(x, y, layout)
    result = _run_xdotool(
        ["mousemove", "--sync", str(px), str(py),
         "click", "--repeat", str(count), str(btn)],
        op="click",
    )
    return ActionResult(
        success=result.success, error=result.error, latency_ms=result.latency_ms,
        physical_coords=(px, py), logical_coords=(lx, ly),
        extra={"button": button, "count": count},
    )


def right_click(x: int, y: int, layout: Optional[MonitorLayout] = None) -> ActionResult:
    return click(x, y, button="right", count=1, layout=layout)


def double_click(x: int, y: int, button: str = "left",
                 layout: Optional[MonitorLayout] = None) -> ActionResult:
    return click(x, y, button=button, count=2, layout=layout)


def hover(x: int, y: int, layout: Optional[MonitorLayout] = None) -> ActionResult:
    """Move mouse cursor without clicking. Useful to reveal tooltips /
    hover-state UI."""
    _validate_coord(x, y, "hover")
    lx, ly, px, py = _translate(x, y, layout)
    result = _run_xdotool(
        ["mousemove", "--sync", str(px), str(py)],
        op="hover",
    )
    return ActionResult(
        success=result.success, error=result.error, latency_ms=result.latency_ms,
        physical_coords=(px, py), logical_coords=(lx, ly),
    )


def type_text(
    text: str,
    delay_ms: int = 12,
    layout: Optional[MonitorLayout] = None,  # unused; consistent signature
) -> ActionResult:
    """Type ``text`` into whichever element currently has focus. Use
    ``type_at_coords`` if you need to focus first."""
    text = _validate_text(text)
    if not (0 <= delay_ms <= 5000):
        raise InputSynthValidationError(f"delay_ms: out of range (0..5000): {delay_ms}")
    return _run_xdotool(
        ["type", "--delay", str(delay_ms), "--", text],
        op="type",
    )


def type_at_coords(
    x: int, y: int,
    text: str,
    delay_ms: int = 12,
    layout: Optional[MonitorLayout] = None,
) -> ActionResult:
    """Click (x, y) to focus, then type ``text``."""
    click_r = click(x, y, button="left", count=1, layout=layout)
    if not click_r.success:
        return click_r
    type_r = type_text(text, delay_ms=delay_ms)
    return ActionResult(
        success=type_r.success,
        error=type_r.error,
        latency_ms=click_r.latency_ms + type_r.latency_ms,
        physical_coords=click_r.physical_coords,
        logical_coords=click_r.logical_coords,
        extra={"chars_typed": len(text)},
    )


def drag(
    x1: int, y1: int,
    x2: int, y2: int,
    button: str = "left",
    hold_ms: int = 100,
    waypoints: Optional[Sequence[tuple[int, int]]] = None,
    layout: Optional[MonitorLayout] = None,
) -> ActionResult:
    """Drag from (x1, y1) to (x2, y2) with an optional list of intermediate
    points for gesture-style drags."""
    _validate_coord(x1, y1, "drag src")
    _validate_coord(x2, y2, "drag dst")
    btn = _validate_button(button)
    if not (0 <= hold_ms <= 5000):
        raise InputSynthValidationError(f"hold_ms: out of range (0..5000): {hold_ms}")
    wp = list(waypoints or [])
    if len(wp) > _MAX_WAYPOINTS:
        raise InputSynthValidationError(
            f"waypoints: too many ({len(wp)} > {_MAX_WAYPOINTS})"
        )
    for i, (wx, wy) in enumerate(wp):
        _validate_coord(wx, wy, f"drag waypoint {i}")

    lx1, ly1, px1, py1 = _translate(x1, y1, layout)
    lx2, ly2, px2, py2 = _translate(x2, y2, layout)

    argv: list[str] = ["mousemove", "--sync", str(px1), str(py1),
                       "mousedown", str(btn)]
    for wx, wy in wp:
        _, _, wpx, wpy = _translate(wx, wy, layout)
        argv.extend(["mousemove", "--sync", str(wpx), str(wpy)])
    argv.extend(["mousemove", "--sync", str(px2), str(py2),
                 "sleep", f"{hold_ms / 1000:.3f}",
                 "mouseup", str(btn)])

    result = _run_xdotool(argv, op="drag")
    return ActionResult(
        success=result.success, error=result.error, latency_ms=result.latency_ms,
        physical_coords=(px2, py2), logical_coords=(lx2, ly2),
        extra={"from": (px1, py1), "waypoints": len(wp), "button": button},
    )


def scroll(
    x: int, y: int,
    direction: str,
    amount: int,
    layout: Optional[MonitorLayout] = None,
) -> ActionResult:
    """Scroll ``amount`` clicks at (x, y) in the given direction."""
    _validate_coord(x, y, "scroll")
    btn = _validate_scroll_direction(direction)
    if not (1 <= amount <= 100):
        raise InputSynthValidationError(f"amount: must be 1..100, got {amount}")
    lx, ly, px, py = _translate(x, y, layout)
    result = _run_xdotool(
        ["mousemove", "--sync", str(px), str(py),
         "click", "--repeat", str(amount), str(btn)],
        op="scroll",
    )
    return ActionResult(
        success=result.success, error=result.error, latency_ms=result.latency_ms,
        physical_coords=(px, py), logical_coords=(lx, ly),
        extra={"direction": direction, "amount": amount},
    )


def press_key(combo: str) -> ActionResult:
    """Press one key or key combo, e.g. ``"ctrl+s"``, ``"F12"``, ``"escape"``.

    Combos are ``+``-separated; each token is validated against an
    allowlist. No arbitrary strings reach xdotool."""
    combo = _validate_key_combo(combo)
    return _run_xdotool(["key", "--", combo], op="press_key")


def key_sequence(items: Sequence[object]) -> ActionResult:
    """Atomically execute a mixed sequence of key combos and typed text.

    Each item is either:
      - ``str`` with ``+`` in it (or single named key) → treated as a
        key combo, validated then pressed
      - other ``str`` → treated as typed text, validated then typed
      - ``{"type": "key", "combo": "ctrl+s"}`` or
        ``{"type": "text", "text": "hello"}`` → explicit form

    Returns the first failure or aggregate success.
    """
    if not isinstance(items, (list, tuple)) or not items:
        raise InputSynthValidationError("items: must be non-empty list")
    if len(items) > _MAX_KEY_SEQUENCE_ITEMS:
        raise InputSynthValidationError(
            f"items: too many ({len(items)} > {_MAX_KEY_SEQUENCE_ITEMS})"
        )

    t_start = time.monotonic()
    for i, item in enumerate(items):
        kind, payload = _classify_sequence_item(item, i)
        if kind == "key":
            r = press_key(payload)
        else:  # text
            r = type_text(payload)
        if not r.success:
            return ActionResult(
                success=False,
                error=f"item {i} ({kind}): {r.error}",
                latency_ms=int((time.monotonic() - t_start) * 1000),
                extra={"failed_at": i, "items_total": len(items)},
            )
    return ActionResult(
        success=True,
        latency_ms=int((time.monotonic() - t_start) * 1000),
        extra={"items_executed": len(items)},
    )


def _classify_sequence_item(item: object, index: int) -> tuple[str, str]:
    """Return ("key"|"text", payload_string). Raises on invalid."""
    if isinstance(item, dict):
        kind = item.get("type")
        if kind == "key":
            combo = item.get("combo")
            if not isinstance(combo, str):
                raise InputSynthValidationError(f"item {index}: key requires 'combo' str")
            return ("key", combo)
        if kind == "text":
            text = item.get("text")
            if not isinstance(text, str):
                raise InputSynthValidationError(f"item {index}: text requires 'text' str")
            return ("text", text)
        raise InputSynthValidationError(
            f"item {index}: dict must have type='key' or 'text', got {kind!r}"
        )
    if isinstance(item, str):
        # Heuristic: if it contains + AND all + -separated tokens look
        # like key names, treat as key combo. Otherwise typed text.
        if "+" in item:
            try:
                _validate_key_combo(item)
                return ("key", item)
            except InputSynthValidationError:
                pass
        # Single named key (e.g. "escape", "F12") → treat as key
        if item.lower() in _ALLOWED_KEY_TOKENS or _KEY_TOKEN_RE.match(item) and len(item) <= 15 and not item.isspace():
            # Ambiguous — single-word strings like "hello" are also
            # allowlist misses so they'd fall through to text below.
            # But e.g. "escape" IS in the allowlist and should be a key.
            if item.lower() in _ALLOWED_KEY_TOKENS:
                return ("key", item)
        return ("text", item)
    raise InputSynthValidationError(
        f"item {index}: must be str or dict, got {type(item).__name__}"
    )


# ── Self-test ─────────────────────────────────────────────────────────

def _self_test() -> int:
    """``python -m gui_agent.input_synth --self-test`` — verify the
    module imports + validates + can find xdotool if installed. Does
    NOT actually synthesize input (would move the user's mouse)."""
    if not check_available():
        print("input_synth: xdotool NOT installed — actuation will fail at runtime",
              flush=True)
    # Exercise the validators — should NOT raise.
    _validate_coord(100, 200, "self-test")
    _validate_button("left")
    _validate_scroll_direction("up")
    _validate_key_combo("ctrl+shift+s")
    _validate_text("hello world 🌍")
    # And SHOULD raise on injection.
    for bad in ("$(rm -rf /)", "ctrl+`whoami`", "  ", "toolong" * 20):
        try:
            _validate_key_combo(bad)
        except InputSynthValidationError:
            pass
        else:
            print(f"input_synth: SELF-TEST FAILED — allowed bad combo {bad!r}",
                  flush=True)
            return 1
    print("input_synth OK: validators pass, allowlist rejects injections", flush=True)
    return 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print("usage: python -m gui_agent.input_synth --self-test",
          file=__import__("sys").stderr)
    sys.exit(2)
