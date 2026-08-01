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
    # Fix V (v6.15) raw pixel/keyboard tools
    GUI_CLICK_AT_COORDS,
    GUI_TYPE_AT_COORDS,
    GUI_DRAG,
    GUI_SCROLL,
    GUI_HOVER,
    GUI_PRESS_KEY,
    GUI_KEY_SEQUENCE,
    # Fix V (v6.15) parse tool (V.4c)
    GUI_PARSE_SCREEN,
    validate_gui_params,
)
from .app_apis.registry import get_app_api
from .screenshots import ScreenshotManager, ScreenshotUnavailableError
# Fix V (v6.15) — xdotool wrappers + HiDPI translation. Lazy-imported
# below only when a coord/keyboard tool is actually invoked so a
# controller without X11 (e.g. headless CI) can still run.


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


def _try_notify_send(title: str, body: str, icon_path: str) -> bool:
    """Fire `notify-send --icon=<png> title body`. Best-effort — returns
    False on any failure so the caller can record notify_sent=False in
    the audit chain without raising. Used by V.4c parse_screen so the
    user sees the annotated preview on their desktop BEFORE any
    grounded action's ask prompt lands.

    Precedent: scripts/ib_bundle.py:635-650 uses the same pattern.
    """
    import subprocess as _sp
    try:
        proc = _sp.run(
            ["notify-send", "--urgency=low",
             f"--icon={icon_path}", title, body],
            capture_output=True, timeout=2.0, check=False,
        )
        return proc.returncode == 0
    except (FileNotFoundError, _sp.SubprocessError, OSError):
        return False


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
        config: Any = None,
    ) -> None:
        # v6.10 P6 (F-73): accept the whole GuiConfig so timeouts +
        # retention + prefer_app_api are honored from user config. Prior
        # to this the Controller only threaded ``scratch_dir``; TOML
        # knobs `gui.a11y_timeout_ms`, `gui.screenshot_retention`, and
        # even `gui.prefer_app_api` were dead — the Agent constructor
        # never saw them. When ``config`` is provided, its fields
        # override the legacy positional/keyword args. When absent, the
        # legacy defaults kick in (backward compat for tests + callers
        # that don't have a GuiConfig handy).
        if config is not None:
            scratch_dir = getattr(config, "screenshot_dir", scratch_dir)
            prefer_app_api = getattr(config, "prefer_app_api", prefer_app_api)
            self._screenshot_retention = int(
                getattr(config, "screenshot_retention", 50)
            )
            self._a11y_timeout_ms = int(
                getattr(config, "a11y_timeout_ms", 5000)
            )
        else:
            self._screenshot_retention = 50
            self._a11y_timeout_ms = 5000
        self._scratch = Path(scratch_dir)
        self._atspi = AtSpiClient()
        self._screenshots = ScreenshotManager(
            self._scratch, retention=self._screenshot_retention,
        )
        self._prefer_app_api = prefer_app_api

        # Fix V (v6.15) — vision grounder (created lazily on first
        # parse_screen / grounded_* call). Config passthrough happens
        # via _get_vision() below.
        self._vision = None  # type: ignore[assignment]
        self._vision_config = self._extract_vision_config(config)

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
        # ── Fix V (v6.15) parse tool ──
        elif method == GUI_PARSE_SCREEN:
            return self._handle_parse_screen(params)
        # ── Fix V (v6.15) raw pixel/keyboard tools ──
        elif method == GUI_CLICK_AT_COORDS:
            return self._handle_click_at_coords(params)
        elif method == GUI_TYPE_AT_COORDS:
            return self._handle_type_at_coords(params)
        elif method == GUI_DRAG:
            return self._handle_drag(params)
        elif method == GUI_SCROLL:
            return self._handle_scroll(params)
        elif method == GUI_HOVER:
            return self._handle_hover(params)
        elif method == GUI_PRESS_KEY:
            return self._handle_press_key(params)
        elif method == GUI_KEY_SEQUENCE:
            return self._handle_key_sequence(params)
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

    # ══════════════════════════════════════════════════════════════════
    # Fix V (v6.15) — parse_screen (V.4c)
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_vision_config(config: Any) -> dict:
        """Pull the [gui.vision] TOML section out of the GuiConfig, if
        present. Fall back to a permissive default so agent works
        standalone in tests. Config wiring proper lands in V.6."""
        if config is None:
            return {}
        # Support either a namespace with .vision attribute or a
        # dict-like config.vision entry.
        vision = getattr(config, "vision", None)
        if vision is None and isinstance(config, dict):
            vision = config.get("vision") or config.get("gui.vision")
        if vision is None:
            return {}
        if hasattr(vision, "__dict__"):
            return {k: v for k, v in vars(vision).items() if not k.startswith("_")}
        if isinstance(vision, dict):
            return dict(vision)
        return {}

    def _get_vision(self):
        """Lazy-construct the VisionGrounder. Import stays local so the
        agent module works when `litellm` isn't installed (unit tests
        pass a mocked completion instead)."""
        if self._vision is not None:
            return self._vision
        from .vision import VisionGrounder
        # Sensible defaults if config didn't provide anything (V.6 wires
        # the real controller.toml section).
        cfg = {
            "enabled": True,
            "backend": "gemini/gemini-2.5-flash",
            "cost_ceiling_usd_per_turn": 0.01,
            "cache_ttl_seconds": 2.0,
            "retry_on_malformed_json": 1,
        }
        cfg.update(self._vision_config)
        self._vision = VisionGrounder(config=cfg)
        return self._vision

    def _handle_parse_screen(self, params: dict) -> dict:
        """Screenshot → VLM parse → annotated PNG + notify-send.

        Returns:
            {
                "elements": [{id, box, caption, kind, confidence}, ...],
                "preview_path": "/tmp/icebreaker-gui/preview-<sha>.png",
                "screenshot_sha256": "...",
                "vlm_latency_ms": int,
                "vlm_cost_usd": float,
                "backend_used": "gemini/gemini-2.5-flash",
                "cached": bool,
                "cost_this_turn_usd": float,
                "notify_sent": bool,
            }
        """
        # 1. Capture the screenshot via the existing manager (reuses
        #    portal→GNOME fallback + SHA-256 + 0o600 retention).
        window = params.get("window", "")
        try:
            shot = self._screenshots.capture(window)
        except ScreenshotUnavailableError as exc:
            return {"error": str(exc), "reason": "screenshot_unavailable"}

        # 2. Parse via VisionGrounder. Handles cache, retry, fallback,
        #    cost ceiling internally.
        try:
            vision = self._get_vision()
        except ImportError as exc:
            return {"error": f"vision unavailable: {exc}",
                    "reason": "vision_import_failed"}

        try:
            parse = vision.parse_screen(shot.path,
                                        prompt_hint=params.get("prompt_hint", ""))
        except Exception as exc:  # noqa: BLE001
            from .vision import (
                VisionCostCeilingExceeded, VisionDisabled,
                VisionAllBackendsFailed, VisionMalformedResponse,
            )
            reason = {
                VisionCostCeilingExceeded: "vision_cost_ceiling_exceeded",
                VisionDisabled: "vision_disabled",
                VisionAllBackendsFailed: "vision_all_backends_failed",
                VisionMalformedResponse: "vision_malformed_response",
            }.get(type(exc), "vision_unexpected_error")
            return {"error": str(exc), "reason": reason}

        # 3. Render an annotated preview PNG (V.3a).
        preview_path = None
        try:
            from PIL import Image
            from .annotate import render_annotated
            img = Image.open(shot.path).convert("RGBA")
            watermark = window[:40] if window else "screen"
            ann = render_annotated(
                image=img, elements=list(parse.elements),
                target_id=None,   # parse_screen alone has no target yet
                watermark_note=watermark,
            )
            preview_path = str(ann.path)
        except Exception as exc:  # noqa: BLE001 — annotate is best-effort
            preview_path = f"annotate-failed: {type(exc).__name__}: {exc}"

        # 4. Fire notify-send so the user sees the preview on their
        #    desktop BEFORE any subsequent grounded_click ask prompt.
        notify_sent = False
        if preview_path and preview_path.startswith("/"):
            notify_sent = _try_notify_send(
                title="Icebreaker",
                body=f"Parsed {len(parse.elements)} element(s) — "
                     f"grounded action may follow",
                icon_path=preview_path,
            )

        return {
            "elements": [e.to_dict() for e in parse.elements],
            "preview_path": preview_path,
            "screenshot_sha256": parse.screenshot_sha256,
            "vlm_latency_ms": parse.vlm_latency_ms,
            "vlm_cost_usd": parse.vlm_cost_usd,
            "backend_used": parse.backend_used,
            "cached": parse.cached,
            "cost_this_turn_usd": vision.turn_cost_usd(),
            "notify_sent": notify_sent,
        }

    # ══════════════════════════════════════════════════════════════════
    # Fix V (v6.15) — raw pixel + keyboard handlers
    # ══════════════════════════════════════════════════════════════════
    #
    # These wrap `gui_agent.input_synth` (V.2b) — thin adapters that
    # convert the input_synth ActionResult shape into this module's
    # existing `{success, error, ...}` return convention so callers
    # written for the AT-SPI handlers (get_window_list, click, etc.)
    # don't need special-case parsing for the coord-based results.
    #
    # HiDPI translation: every mouse fn takes a MonitorLayout so the
    # VLM-provided PHYSICAL coord lands at the right pixel on Retina
    # displays. The layout is cached 30s inside MonitorLayout itself
    # (see gui_agent/geometry.py) — we call detect() per handler for
    # freshness without hurting perf.

    def _handle_click_at_coords(self, params: dict) -> dict:
        return self._synth_wrap(
            op="click_at_coords",
            fn=lambda synth, layout: synth.click(
                x=params["x"], y=params["y"],
                button=params.get("button", "left"),
                count=params.get("count", 1),
                layout=layout,
            ),
        )

    def _handle_type_at_coords(self, params: dict) -> dict:
        return self._synth_wrap(
            op="type_at_coords",
            fn=lambda synth, layout: synth.type_at_coords(
                x=params["x"], y=params["y"],
                text=params["text"],
                delay_ms=params.get("delay_ms", 12),
                layout=layout,
            ),
        )

    def _handle_drag(self, params: dict) -> dict:
        waypoints_raw = params.get("waypoints") or []
        # Schema guarantees each is [x, y] — convert to tuples for input_synth.
        waypoints = [(w[0], w[1]) for w in waypoints_raw]
        return self._synth_wrap(
            op="drag",
            fn=lambda synth, layout: synth.drag(
                x1=params["x1"], y1=params["y1"],
                x2=params["x2"], y2=params["y2"],
                button=params.get("button", "left"),
                hold_ms=params.get("hold_ms", 100),
                waypoints=waypoints,
                layout=layout,
            ),
        )

    def _handle_scroll(self, params: dict) -> dict:
        return self._synth_wrap(
            op="scroll",
            fn=lambda synth, layout: synth.scroll(
                x=params["x"], y=params["y"],
                direction=params["direction"],
                amount=params["amount"],
                layout=layout,
            ),
        )

    def _handle_hover(self, params: dict) -> dict:
        return self._synth_wrap(
            op="hover",
            fn=lambda synth, layout: synth.hover(
                x=params["x"], y=params["y"],
                layout=layout,
            ),
        )

    def _handle_press_key(self, params: dict) -> dict:
        return self._synth_wrap(
            op="press_key",
            fn=lambda synth, layout: synth.press_key(combo=params["combo"]),
            needs_layout=False,
        )

    def _handle_key_sequence(self, params: dict) -> dict:
        return self._synth_wrap(
            op="key_sequence",
            fn=lambda synth, layout: synth.key_sequence(items=params["items"]),
            needs_layout=False,
        )

    def _synth_wrap(self, op: str, fn, needs_layout: bool = True) -> dict:
        """Common wrapper: lazy-import input_synth + geometry, run `fn`,
        convert ActionResult to the `{success, error, ...}` dict shape.

        Errors that would break every future call (xdotool missing,
        validation error) surface with a `reason` field so callers can
        distinguish them from operation-specific failures.
        """
        try:
            from . import input_synth as synth
            from .geometry import MonitorLayout
        except ImportError as exc:
            return {"success": False,
                    "error": f"input_synth unavailable: {exc}",
                    "reason": "input_synth_import_failed"}

        layout = MonitorLayout.detect() if needs_layout else None
        try:
            result = fn(synth, layout)
        except synth.InputSynthUnavailable as exc:
            return {"success": False, "error": str(exc),
                    "reason": "xdotool_missing"}
        except synth.InputSynthValidationError as exc:
            return {"success": False, "error": str(exc),
                    "reason": "validation_error"}
        except Exception as exc:  # noqa: BLE001 — never propagate to the RPC loop
            return {"success": False,
                    "error": f"{op}: unexpected {type(exc).__name__}: {exc}",
                    "reason": "internal_error"}

        # ActionResult → dict. Preserve every field for audit + debug.
        d: dict = {
            "success": result.success,
            "error": result.error,
            "latency_ms": result.latency_ms,
        }
        if result.physical_coords is not None:
            d["physical_coords"] = list(result.physical_coords)
        if result.logical_coords is not None:
            d["logical_coords"] = list(result.logical_coords)
        if result.extra:
            d["extra"] = result.extra
        return d

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
            except Exception as exc:  # noqa: BLE001
                # F-53 Scope A.P3: JSON-RPC top-level dispatch —
                # every unhandled error becomes an INTERNAL_ERROR
                # response carrying `str(exc)` back to the caller.
                # The Controller's daemon logs the resulting error
                # reply; nothing is truly swallowed.
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
