"""GuiAgent — sandboxed GUI automation subprocess.

Spawned by the Controller daemon (ADR-11). Communicates over AF_UNIX
JSON-RPC, same framing as ``controller.protocol``.

Lifecycle:
  1. Controller calls ``GuiAgent.spawn()``
  2. Child applies Landlock + Seccomp (INV-5)
  3. Child opens AT-SPI + D-Bus connections
  4. Child enters JSON-RPC request loop on stdin/stdout
  5. On stdin EOF, child exits cleanly

Environment scrubbing (BP-8): only safe locale/term vars plus display
vars (DISPLAY, WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS, XDG_RUNTIME_DIR)
are inherited.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from controller.protocol import (
    JsonRpcRequest,
    JsonRpcResponse,
    JSONRPC_VERSION,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    make_error,
    parse_message,
)

from .atspi import (
    AtSpiClient,
    AtSpiUnavailableError,
    ElementNotFoundError,
    ElementTooSmallError,
)
from .protocol import (
    ALL_GUI_METHODS,
    GUI_PING,
    GUI_SCREENSHOT,
    GUI_FIND_ELEMENT,
    GUI_GET_WINDOW_LIST,
    GUI_GET_ELEMENT_TREE,
    GUI_CLICK,
    GUI_TYPE,
    GUI_SELECT,
    validate_gui_params,
)
from .app_apis.registry import get_app_api
from .screenshots import ScreenshotManager, ScreenshotUnavailableError


_SAFE_ENV_VARS: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL",
    "LC_ALL", "LC_CTYPE", "LANG", "TZ",
    "TERM", "COLORTERM",
})

_GUI_ENV_VARS: frozenset[str] = frozenset({
    "DISPLAY", "WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
})

_DEFAULT_SCRATCH_DIR = "/tmp/icebreaker-gui"
_PER_HANDLER_TIMEOUT = 10.0


def _scrubbed_gui_env(
    *,
    extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Build a scrubbed environment for the GUI Agent subprocess (BP-8)."""
    env = {
        k: v for k, v in os.environ.items()
        if k in _SAFE_ENV_VARS or k in _GUI_ENV_VARS
    }
    if extra:
        env.update(extra)
    return env


class GuiAgent:
    """GUI Agent process — AT-SPI + D-Bus screenshot automation.

    Use ``GuiAgent.main()`` as the subprocess entry point (via ``__main__.py``).
    Use ``GuiAgent.spawn()`` from the Controller to launch the subprocess.
    """

    def __init__(
        self,
        scratch_dir: str | Path = _DEFAULT_SCRATCH_DIR,
        *,
        prefer_app_api: bool = True,
    ) -> None:
        self._scratch = Path(scratch_dir)
        self._atspi = AtSpiClient()
        self._screenshots = ScreenshotManager(self._scratch)
        self._prefer_app_api = prefer_app_api

    def handle_request(self, method: str, params: dict) -> dict:
        """Dispatch a GUI method call. Returns a result dict."""
        try:
            validate_gui_params(method, params)
        except Exception as exc:
            raise _InvalidParams(str(exc)) from exc

        if method == GUI_PING:
            return self._handle_ping()
        elif method == GUI_SCREENSHOT:
            return self._handle_screenshot(params)
        elif method == GUI_FIND_ELEMENT:
            return self._handle_find_element(params)
        elif method == GUI_GET_WINDOW_LIST:
            return self._handle_get_window_list()
        elif method == GUI_GET_ELEMENT_TREE:
            return self._handle_get_element_tree(params)
        elif method == GUI_CLICK:
            return self._handle_click(params)
        elif method == GUI_TYPE:
            return self._handle_type(params)
        elif method == GUI_SELECT:
            return self._handle_select(params)
        else:
            raise _MethodNotFound(method)

    def _handle_ping(self) -> dict:
        return {
            "status": "ok",
            "atspi_available": self._atspi.available,
            "screenshots_available": self._screenshots.available,
        }

    def _handle_screenshot(self, params: dict) -> dict:
        window = params.get("window", "")
        try:
            result = self._screenshots.capture(window)
            return {
                "path": str(result.path),
                "sha256": result.sha256,
                "width": result.width,
                "height": result.height,
                "timestamp": result.timestamp,
            }
        except ScreenshotUnavailableError as exc:
            return {"error": str(exc), "available": False}

    def _handle_find_element(self, params: dict) -> dict:
        try:
            elem = self._atspi.find_element(
                params["window"], params["role"], params["name"],
            )
            return {
                "path": elem.path,
                "role": elem.role,
                "name": elem.name,
                "position": list(elem.position),
                "size": list(elem.size),
                "states": sorted(elem.states),
                "text": elem.text,
            }
        except AtSpiUnavailableError as exc:
            return {"error": str(exc), "reason": "atspi_unavailable"}
        except ElementNotFoundError as exc:
            return {"error": str(exc), "reason": exc.reason}

    def _handle_get_window_list(self) -> dict:
        windows = self._atspi.get_window_list()
        return {
            "windows": [
                {
                    "title": w.title,
                    "app_name": w.app_name,
                    "pid": w.pid,
                    "geometry": list(w.geometry),
                }
                for w in windows
            ],
        }

    def _handle_get_element_tree(self, params: dict) -> dict:
        max_depth = params.get("max_depth", 3)
        tree = self._atspi.get_element_tree(params["window"], max_depth=max_depth)
        return {"tree": tree}

    def _try_app_api(self, action: str, params: dict) -> dict | None:
        """Try the registered app API for the target window. Returns None to fall back."""
        if not self._prefer_app_api:
            return None
        window = params.get("window", "")
        if not window:
            return None
        api = get_app_api(window)
        if api is None:
            return None
        result = api.execute(action, params)
        if result.get("fallback") == "atspi":
            return None
        result["api_used"] = type(api).__name__
        return result

    def _handle_click(self, params: dict) -> dict:
        app_result = self._try_app_api("click", params)
        if app_result is not None:
            return app_result
        try:
            elem = self._atspi.find_element(
                params["window"], params["role"], params["name"],
            )
            result = self._atspi.click(elem)
            return {"success": result.success, "error": result.error}
        except ElementNotFoundError as exc:
            return {"success": False, "error": str(exc), "reason": exc.reason}
        except (AtSpiUnavailableError, ElementTooSmallError) as exc:
            reason = "atspi_unavailable" if isinstance(exc, AtSpiUnavailableError) else "element_too_small"
            return {"success": False, "error": str(exc), "reason": reason}

    def _handle_type(self, params: dict) -> dict:
        app_result = self._try_app_api("type", params)
        if app_result is not None:
            return app_result
        try:
            elem = self._atspi.find_element(
                params["window"], params["role"], params["name"],
            )
            result = self._atspi.type_text(elem, params["text"])
            return {"success": result.success, "error": result.error}
        except ElementNotFoundError as exc:
            return {"success": False, "error": str(exc), "reason": exc.reason}
        except AtSpiUnavailableError as exc:
            return {"success": False, "error": str(exc), "reason": "atspi_unavailable"}

    def _handle_select(self, params: dict) -> dict:
        app_result = self._try_app_api("select", params)
        if app_result is not None:
            return app_result
        try:
            elem = self._atspi.find_element(
                params["window"], params["role"], params["name"],
            )
            result = self._atspi.select(elem, params["value"])
            return {"success": result.success, "error": result.error}
        except ElementNotFoundError as exc:
            return {"success": False, "error": str(exc), "reason": exc.reason}
        except AtSpiUnavailableError as exc:
            return {"success": False, "error": str(exc), "reason": "atspi_unavailable"}

    def _run_loop(self) -> int:
        """Read JSON-RPC requests from stdin, write responses to stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                resp = make_error("null", -32700, "Parse error")
                sys.stdout.write(resp.to_bytes().decode("utf-8"))
                sys.stdout.flush()
                continue

            req_id = msg.get("id", "null")
            method = msg.get("method", "")

            if method not in ALL_GUI_METHODS:
                resp = make_error(str(req_id), METHOD_NOT_FOUND, f"Unknown method: {method}")
                sys.stdout.write(resp.to_bytes().decode("utf-8"))
                sys.stdout.flush()
                continue

            try:
                result = self.handle_request(method, msg.get("params", {}))
                resp = JsonRpcResponse(id=str(req_id), result=result)
            except _InvalidParams as exc:
                resp = make_error(str(req_id), INVALID_PARAMS, str(exc))
            except _MethodNotFound as exc:
                resp = make_error(str(req_id), METHOD_NOT_FOUND, str(exc))
            except Exception as exc:
                resp = make_error(str(req_id), INTERNAL_ERROR, str(exc))

            sys.stdout.write(resp.to_bytes().decode("utf-8"))
            sys.stdout.flush()

        return 0

    @classmethod
    def main(cls) -> int:
        """Subprocess entry point. Applies sandbox, then enters request loop.

        On non-Linux or sandbox failure, exits with code 1 and a
        diagnostic message on stderr.
        """
        scratch = os.environ.get("ICEBREAKER_GUI_SCRATCH", _DEFAULT_SCRATCH_DIR)

        if os.environ.get("ICEBREAKER_GUI_SKIP_SANDBOX") != "1":
            try:
                from .sandbox import apply_gui_sandbox, SandboxError
                apply_gui_sandbox(
                    home_dir=os.path.expanduser("~"),
                    scratch_dir=scratch,
                )
            except SandboxError as exc:
                print(f"GUI Agent sandbox failed: {exc}", file=sys.stderr)
                return 1

        prefer_app_api = os.environ.get("ICEBREAKER_GUI_PREFER_APP_API", "1") != "0"
        agent = cls(scratch_dir=scratch, prefer_app_api=prefer_app_api)
        return agent._run_loop()

    @classmethod
    def spawn_env(cls, *, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Return the scrubbed environment for spawning the GUI Agent subprocess."""
        return _scrubbed_gui_env(extra=extra)


class _InvalidParams(Exception):
    pass


class _MethodNotFound(Exception):
    pass
