"""GNOME Files (Nautilus) app API — D-Bus FileOperations2 interface.

Provides structured folder/file operations without relying on AT-SPI
element tree traversal. Uses the ``org.gnome.Nautilus.FileOperations2``
D-Bus interface when available; degrades gracefully otherwise.

All returned data is structured dicts, never raw GLib objects.
"""

from __future__ import annotations

import re
from typing import Any

from .base import AppApi
from .registry import register_app_api


_NAUTILUS_TITLE_PATTERNS = (
    re.compile(r".*\bFiles\b", re.I),
    re.compile(r".*\bNautilus\b", re.I),
    re.compile(r".*\bHome\s*-", re.I),
    re.compile(r".*\borg\.gnome\.Nautilus\b", re.I),
)

_ACTIONS = frozenset({"open_folder", "rename", "select_file"})

_DBUS_NAME = "org.gnome.Nautilus"
_DBUS_PATH = "/org/gnome/Nautilus/FileOperations2"
_DBUS_IFACE = "org.gnome.Nautilus.FileOperations2"


def _validate_absolute_path(path: str) -> dict[str, Any] | None:
    if not path:
        return {"success": False, "error": "path is required"}
    if not path.startswith("/"):
        return {"success": False, "error": f"path must be absolute: {path!r}"}
    return None


@register_app_api("gnome_files")
class GnomeFilesApi(AppApi):
    """GNOME Files automation via D-Bus FileOperations2.

    Lazy-imports ``gi.repository.Gio`` to avoid hard dependency. When
    GI bindings are unavailable, ``execute()`` returns structured error
    dicts with an AT-SPI fallback hint.
    """

    def __init__(self) -> None:
        self._gio = None
        self._glib = None
        self._available = False
        self._init_error: str = ""

        try:
            from gi.repository import Gio, GLib  # type: ignore[import-untyped]
            self._gio = Gio
            self._glib = GLib
            self._available = True
        except (ImportError, ValueError) as exc:
            self._init_error = str(exc)

    @property
    def available(self) -> bool:
        return self._available

    def supports(self, window_title: str) -> bool:
        return any(pat.match(window_title) for pat in _NAUTILUS_TITLE_PATTERNS)

    def capabilities(self) -> list[str]:
        return sorted(_ACTIONS)

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._available:
            return {
                "success": False,
                "error": f"GI bindings not available: {self._init_error}. "
                         "Install gir1.2-gio-2.0 / python3-gi.",
                "fallback": "atspi",
            }

        if action not in _ACTIONS:
            return {
                "success": False,
                "error": f"Action {action!r} not supported. "
                         f"Available: {sorted(_ACTIONS)}",
            }

        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return {
                "success": False,
                "error": f"Handler for {action!r} not implemented",
            }

        try:
            return handler(params)
        except Exception as exc:
            return {"success": False, "error": str(exc), "fallback": "atspi"}

    def _get_proxy(self) -> Any:
        Gio = self._gio
        return Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            _DBUS_NAME,
            _DBUS_PATH,
            _DBUS_IFACE,
            None,
        )

    def _do_open_folder(self, params: dict[str, Any]) -> dict[str, Any]:
        path: str = params.get("path", "")
        err = _validate_absolute_path(path)
        if err is not None:
            return err
        GLib = self._glib
        proxy = self._get_proxy()
        uri = f"file://{path}"
        proxy.call_sync(
            "ShowFolders",
            GLib.Variant("(assa{sv})", ([uri], "", {})),
            0,
            -1,
            None,
        )
        return {"success": True, "path": path}

    def _do_select_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path: str = params.get("path", "")
        err = _validate_absolute_path(path)
        if err is not None:
            return err
        GLib = self._glib
        proxy = self._get_proxy()
        uri = f"file://{path}"
        proxy.call_sync(
            "ShowItems",
            GLib.Variant("(assa{sv})", ([uri], "", {})),
            0,
            -1,
            None,
        )
        return {"success": True, "path": path}

    def _do_rename(self, params: dict[str, Any]) -> dict[str, Any]:
        path: str = params.get("path", "")
        err = _validate_absolute_path(path)
        if err is not None:
            return err
        new_name: str = params.get("new_name", "")
        if not new_name:
            return {"success": False, "error": "new_name is required"}
        GLib = self._glib
        proxy = self._get_proxy()
        uri = f"file://{path}"
        proxy.call_sync(
            "RenameFile",
            GLib.Variant("(ssa{sv})", (uri, new_name, {})),
            0,
            -1,
            None,
        )
        return {"success": True, "path": path, "new_name": new_name}
