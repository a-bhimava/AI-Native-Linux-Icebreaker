"""AT-SPI client — accessibility tree interaction for GUI automation.

Uses ``pyatspi`` (GObject introspection bindings) via lazy import.
Gracefully degrades on systems without ``pyatspi`` — all methods
return structured errors instead of crashing.

Element locators use semantic roles and accessible names (ADR-12),
not pixel coordinates. This ensures the GUI Agent works with any
AT-SPI-compliant application.

All GUI-sourced strings (window titles, element names) are sanitized
before returning to the caller (BP-3).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional


class AtSpiUnavailableError(Exception):
    """pyatspi or AT-SPI registry not available."""


class ElementNotFoundError(Exception):
    """Element could not be located in the accessibility tree.

    The ``reason`` field explains WHY for error recovery:
      - ``no_a11y_tree``: window has no accessible children
      - ``wrong_role``: no element with the requested role
      - ``wrong_name``: role matched but name did not
      - ``multiple_matches``: ambiguous — multiple elements match
      - ``window_not_found``: target window not in window list
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class ElementTooSmallError(Exception):
    """Element dimensions are below the minimum safe size (10x10px)."""


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_C0_C1 = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f\x80-\x9f]")

_MIN_ELEMENT_SIZE = 10


def _sanitize_gui_string(s: str, *, max_len: int = 256) -> str:
    """Sanitize GUI-sourced strings (BP-3). Strips ANSI, C0/C1 control chars."""
    if not isinstance(s, str):
        s = str(s)
    s = _ANSI_ESCAPE.sub("", s)
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = _C0_C1.sub("", s)
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s


@dataclass(frozen=True)
class ElementDescriptor:
    path: str
    role: str
    name: str
    position: tuple[int, int]
    size: tuple[int, int]
    states: frozenset[str]
    text: str = ""


@dataclass(frozen=True)
class WindowDescriptor:
    title: str
    app_name: str
    pid: int
    geometry: tuple[int, int, int, int]


@dataclass(frozen=True)
class ActionResult:
    success: bool
    element_state_after: dict = field(default_factory=dict)
    error: str | None = None


class AtSpiClient:
    """AT-SPI accessibility tree client with lazy import."""

    _MAX_TREE_ELEMENTS = 500
    _DEFAULT_CACHE_TTL = 2.0

    def __init__(self, *, window_cache_ttl: float = _DEFAULT_CACHE_TTL) -> None:
        self._atspi = None
        self._available = False
        self._window_cache: list[WindowDescriptor] | None = None
        self._window_cache_time: float = 0.0
        self._window_cache_ttl = window_cache_ttl

        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi
            self._atspi = Atspi
            self._available = True
        except (ImportError, ValueError) as exc:
            self._init_error = str(exc)

    @property
    def available(self) -> bool:
        return self._available

    def get_window_list(self) -> list[WindowDescriptor]:
        """Return all visible windows. Cached for 2 seconds."""
        if not self._available:
            return []

        now = time.monotonic()
        if (
            self._window_cache is not None
            and (now - self._window_cache_time) < self._window_cache_ttl
        ):
            return self._window_cache

        Atspi = self._atspi
        desktop = Atspi.get_desktop(0)
        windows: list[WindowDescriptor] = []

        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            app_name = _sanitize_gui_string(app.get_name() or "")
            pid = app.get_process_id()

            for j in range(app.get_child_count()):
                win = app.get_child_at_index(j)
                if win is None:
                    continue
                title = _sanitize_gui_string(win.get_name() or "")
                try:
                    ext = win.get_extents(Atspi.CoordType.SCREEN)
                    geom = (ext.x, ext.y, ext.width, ext.height)
                except Exception:
                    geom = (0, 0, 0, 0)

                windows.append(WindowDescriptor(
                    title=title,
                    app_name=app_name,
                    pid=pid,
                    geometry=geom,
                ))

        self._window_cache = windows
        self._window_cache_time = now
        return windows

    def find_element(
        self, window: str, role: str, name: str,
    ) -> ElementDescriptor:
        """Find an element in the accessibility tree by window, role, and name.

        Raises ``ElementNotFoundError`` with a specific ``reason`` field
        explaining why the element could not be found.
        """
        if not self._available:
            raise AtSpiUnavailableError(
                f"pyatspi not available: {getattr(self, '_init_error', 'unknown')}. "
                "Install with: sudo apt install python3-pyatspi gir1.2-atspi-2.0"
            )

        win_acc = self._find_window(window)
        if win_acc is None:
            raise ElementNotFoundError(
                f"Window {window!r} not found. Available windows: "
                + ", ".join(w.title for w in self.get_window_list()),
                reason="window_not_found",
            )

        if win_acc.get_child_count() == 0:
            raise ElementNotFoundError(
                f"Window {window!r} has no accessibility tree. "
                "The application may not support AT-SPI, or accessibility "
                "may need to be enabled in system settings.",
                reason="no_a11y_tree",
            )

        fast = self._find_element_fast(win_acc, role, name)
        if fast is not None:
            return fast

        matches = self._search_tree(win_acc, role, name, max_elements=self._MAX_TREE_ELEMENTS)

        if not matches:
            role_matches = self._search_tree(win_acc, role, None, max_elements=self._MAX_TREE_ELEMENTS)
            if not role_matches:
                raise ElementNotFoundError(
                    f"No elements with role={role!r} found in window {window!r}. "
                    "Try a different role (button, text, menu-item, etc.).",
                    reason="wrong_role",
                )
            available_names = [_sanitize_gui_string(m.name) for m in role_matches[:10]]
            raise ElementNotFoundError(
                f"Found {len(role_matches)} elements with role={role!r} but none "
                f"named {name!r}. Available names: {available_names}",
                reason="wrong_name",
            )

        if len(matches) > 1:
            raise ElementNotFoundError(
                f"Ambiguous: {len(matches)} elements match role={role!r} "
                f"name={name!r}. Refine the selector.",
                reason="multiple_matches",
            )

        return matches[0]

    def click(self, element: ElementDescriptor) -> ActionResult:
        """Click an element. Verifies size >= 10x10px first."""
        if not self._available:
            return ActionResult(success=False, error="pyatspi not available")

        w, h = element.size
        if w < _MIN_ELEMENT_SIZE or h < _MIN_ELEMENT_SIZE:
            raise ElementTooSmallError(
                f"Element {element.name!r} is {w}x{h}px — below minimum "
                f"safe size ({_MIN_ELEMENT_SIZE}x{_MIN_ELEMENT_SIZE}px). "
                "This may indicate a hidden or collapsed element."
            )

        acc = self._resolve_element(element)
        if acc is None:
            return ActionResult(success=False, error="Element no longer in tree")

        try:
            action_iface = acc.get_action_iface()
            if action_iface is None:
                return ActionResult(success=False, error="Element does not support actions")
            action_iface.do_action(0)
            return ActionResult(success=True)
        except Exception as exc:
            return ActionResult(success=False, error=str(exc))

    def type_text(self, element: ElementDescriptor, text: str) -> ActionResult:
        """Type text into an element."""
        if not self._available:
            return ActionResult(success=False, error="pyatspi not available")

        acc = self._resolve_element(element)
        if acc is None:
            return ActionResult(success=False, error="Element no longer in tree")

        try:
            edit_iface = acc.get_editable_text_iface()
            if edit_iface is None:
                return ActionResult(success=False, error="Element is not editable")
            edit_iface.insert_text(len(acc.get_text_iface().get_text(0, -1)), text, len(text))
            return ActionResult(success=True)
        except Exception as exc:
            return ActionResult(success=False, error=str(exc))

    def select(self, element: ElementDescriptor, value: str) -> ActionResult:
        """Select a value from an element."""
        if not self._available:
            return ActionResult(success=False, error="pyatspi not available")

        acc = self._resolve_element(element)
        if acc is None:
            return ActionResult(success=False, error="Element no longer in tree")

        try:
            sel_iface = acc.get_selection_iface()
            if sel_iface is None:
                return ActionResult(success=False, error="Element does not support selection")
            for i in range(acc.get_child_count()):
                child = acc.get_child_at_index(i)
                if child and child.get_name() == value:
                    sel_iface.select_child(i)
                    return ActionResult(success=True)
            return ActionResult(success=False, error=f"Value {value!r} not found in options")
        except Exception as exc:
            return ActionResult(success=False, error=str(exc))

    def get_element_tree(
        self, window: str, max_depth: int = 3,
    ) -> list[dict]:
        """Return a simplified tree representation, capped at max_depth."""
        if not self._available:
            return []

        win_acc = self._find_window(window)
        if win_acc is None:
            return []

        result: list[dict] = []
        count = [0]

        def _walk(acc: Any, depth: int) -> dict | None:
            if depth > max_depth or count[0] >= self._MAX_TREE_ELEMENTS:
                return None
            count[0] += 1
            node = {
                "role": str(acc.get_role_name()),
                "name": _sanitize_gui_string(acc.get_name() or ""),
            }
            children = []
            for i in range(acc.get_child_count()):
                child = acc.get_child_at_index(i)
                if child is None:
                    continue
                child_node = _walk(child, depth + 1)
                if child_node is not None:
                    children.append(child_node)
            if children:
                node["children"] = children
            return node

        root = _walk(win_acc, 0)
        if root is not None:
            result.append(root)
        return result

    def verify_element(self, element: ElementDescriptor) -> ElementDescriptor | None:
        """Re-verify that an element still exists in the tree."""
        if not self._available:
            return None
        try:
            parts = element.path.split("/")
            if len(parts) >= 2:
                window = parts[1] if len(parts) > 1 else ""
                return self.find_element(window, element.role, element.name)
        except (ElementNotFoundError, AtSpiUnavailableError):
            pass
        return None

    def _find_element_fast(
        self, root: Any, role: str, name: str,
    ) -> ElementDescriptor | None:
        """BFS that returns immediately on first match, skipping full tree walk."""
        Atspi = self._atspi
        queue = [(root, f"/{_sanitize_gui_string(root.get_name() or '')}")]
        visited = 0

        while queue and visited < self._MAX_TREE_ELEMENTS:
            acc, path = queue.pop(0)
            visited += 1
            acc_role = str(acc.get_role_name())
            acc_name = acc.get_name() or ""

            if acc_role == role and acc_name == name:
                try:
                    ext = acc.get_extents(Atspi.CoordType.SCREEN)
                    pos = (ext.x, ext.y)
                    size = (ext.width, ext.height)
                except Exception:
                    pos = (0, 0)
                    size = (0, 0)

                states: frozenset[str] = frozenset()
                try:
                    st = acc.get_state_set()
                    state_names = []
                    for s in [
                        Atspi.StateType.ENABLED, Atspi.StateType.VISIBLE,
                        Atspi.StateType.FOCUSABLE, Atspi.StateType.FOCUSED,
                    ]:
                        if st.contains(s):
                            state_names.append(s.value_nick)
                    states = frozenset(state_names)
                except Exception:
                    pass

                text = ""
                try:
                    ti = acc.get_text_iface()
                    if ti:
                        text = ti.get_text(0, min(ti.get_character_count(), 256))
                except Exception:
                    pass

                return ElementDescriptor(
                    path=f"{path}/{_sanitize_gui_string(acc_name)}",
                    role=acc_role,
                    name=_sanitize_gui_string(acc_name),
                    position=pos,
                    size=size,
                    states=states,
                    text=_sanitize_gui_string(text),
                )

            for i in range(acc.get_child_count()):
                child = acc.get_child_at_index(i)
                if child is not None:
                    child_name = child.get_name() or ""
                    queue.append((child, f"{path}/{_sanitize_gui_string(child_name)}"))

        return None

    def _find_window(self, title: str) -> Any:
        """Find a window accessible object by title."""
        Atspi = self._atspi
        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            for j in range(app.get_child_count()):
                win = app.get_child_at_index(j)
                if win is None:
                    continue
                win_title = win.get_name() or ""
                if title in win_title:
                    return win
        return None

    def _search_tree(
        self, root: Any, role: str, name: str | None,
        *, max_elements: int = 500,
    ) -> list[ElementDescriptor]:
        """Depth-first search for matching elements."""
        Atspi = self._atspi
        matches: list[ElementDescriptor] = []
        count = [0]

        def _walk(acc: Any, path: str) -> None:
            if count[0] >= max_elements:
                return
            count[0] += 1

            acc_role = str(acc.get_role_name())
            acc_name = acc.get_name() or ""

            if acc_role == role and (name is None or acc_name == name):
                try:
                    ext = acc.get_extents(Atspi.CoordType.SCREEN)
                    pos = (ext.x, ext.y)
                    size = (ext.width, ext.height)
                except Exception:
                    pos = (0, 0)
                    size = (0, 0)

                states: frozenset[str] = frozenset()
                try:
                    st = acc.get_state_set()
                    state_names = []
                    for s in [
                        Atspi.StateType.ENABLED,
                        Atspi.StateType.VISIBLE,
                        Atspi.StateType.FOCUSABLE,
                        Atspi.StateType.FOCUSED,
                    ]:
                        if st.contains(s):
                            state_names.append(s.value_nick)
                    states = frozenset(state_names)
                except Exception:
                    pass

                text = ""
                try:
                    ti = acc.get_text_iface()
                    if ti:
                        text = ti.get_text(0, min(ti.get_character_count(), 256))
                except Exception:
                    pass

                element_path = f"{path}/{_sanitize_gui_string(acc_name)}"
                matches.append(ElementDescriptor(
                    path=element_path,
                    role=acc_role,
                    name=_sanitize_gui_string(acc_name),
                    position=pos,
                    size=size,
                    states=states,
                    text=_sanitize_gui_string(text),
                ))

            for i in range(acc.get_child_count()):
                child = acc.get_child_at_index(i)
                if child is not None:
                    child_name = child.get_name() or ""
                    _walk(child, f"{path}/{_sanitize_gui_string(child_name)}")

        root_name = root.get_name() or ""
        _walk(root, f"/{_sanitize_gui_string(root_name)}")
        return matches

    def _resolve_element(self, element: ElementDescriptor) -> Any:
        """Resolve an ElementDescriptor back to an AT-SPI accessible object."""
        parts = element.path.strip("/").split("/")
        if not parts:
            return None
        window_title = parts[0]
        win = self._find_window(window_title)
        if win is None:
            return None

        matches = self._search_tree(win, element.role, element.name, max_elements=self._MAX_TREE_ELEMENTS)
        return matches[0] if matches else None
