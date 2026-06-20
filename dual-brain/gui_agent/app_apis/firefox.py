"""Firefox app API — Chrome DevTools Protocol over WebSocket.

Provides structured browser automation for Firefox (and Iceweasel) via
CDP without relying on AT-SPI element tree traversal. Uses ``websockets``
for the CDP transport when available; degrades gracefully otherwise.

All returned data is structured dicts, never raw protocol objects.
Security: no ``eval``, no ``Runtime.evaluate``, no arbitrary JS execution.
Only structured DOM and Input domain methods are used.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlparse

from .base import AppApi
from .registry import register_app_api


_FF_TITLE_PATTERNS = (
    re.compile(r".*Firefox", re.I),
    re.compile(r".*Iceweasel", re.I),
    re.compile(r".*Mozilla\s+Firefox", re.I),
)

_ACTIONS = frozenset({
    "navigate",
    "screenshot_tab",
    "get_dom_element",
    "click",
    "type_in_form",
})

_ALLOWED_SCHEMES = frozenset({"http", "https"})


@register_app_api("firefox")
class FirefoxApi(AppApi):
    """Firefox automation via Chrome DevTools Protocol (CDP).

    Lazy-imports ``websockets`` to avoid hard dependency. When the library
    is unavailable or CDP connection fails, ``execute()`` returns
    structured error dicts with an ``atspi`` fallback hint.
    """

    def __init__(self, cdp_port: int = 9222) -> None:
        self._ws_mod = None
        self._available = False
        self._init_error: str = ""
        self._cdp_port = cdp_port
        self._msg_id = 0

        try:
            import websockets  # noqa: F401
            self._ws_mod = websockets
            self._available = True
        except ImportError as exc:
            self._init_error = str(exc)

    @property
    def available(self) -> bool:
        return self._available

    def supports(self, window_title: str) -> bool:
        return any(pat.match(window_title) for pat in _FF_TITLE_PATTERNS)

    def capabilities(self) -> list[str]:
        return sorted(_ACTIONS)

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._available:
            return {
                "success": False,
                "error": f"websockets not available: {self._init_error}. "
                         "Install websockets (pip install websockets).",
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
            return {
                "success": False,
                "error": str(exc),
                "fallback": "atspi",
            }

    def _validate_url(self, url: str) -> str | None:
        if not url or not url.strip():
            return "url is required and must not be empty"
        parsed = urlparse(url)
        if parsed.scheme not in _ALLOWED_SCHEMES:
            return (
                f"Scheme {parsed.scheme!r} is not allowed. "
                f"Only {sorted(_ALLOWED_SCHEMES)} are permitted."
            )
        return None

    def _get_ws_url(self) -> str:
        import urllib.request
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{self._cdp_port}/json/version",
            timeout=5,
        )
        info = json.loads(resp.read())
        return info["webSocketDebuggerUrl"]

    async def _send_cdp_async(
        self, ws_url: str, method: str, params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        async with self._ws_mod.connect(ws_url) as ws:
            await ws.send(json.dumps(msg))
            while True:
                raw = await ws.recv()
                response = json.loads(raw)
                if response.get("id") == self._msg_id:
                    if "error" in response:
                        raise RuntimeError(
                            f"CDP error: {response['error'].get('message', response['error'])}"
                        )
                    return response.get("result", {})

    def _send_cdp(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ws_url = self._get_ws_url()
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(
            self._send_cdp_async(ws_url, method, params)
        )

    def _do_navigate(self, params: dict) -> dict:
        url = params.get("url", "")
        err = self._validate_url(url)
        if err is not None:
            return {"success": False, "error": err}
        result = self._send_cdp("Page.navigate", {"url": url})
        return {"success": True, "frameId": result.get("frameId", "")}

    def _do_screenshot_tab(self, params: dict) -> dict:
        result = self._send_cdp("Page.captureScreenshot", {})
        return {"success": True, "data": result.get("data", "")}

    def _do_get_dom_element(self, params: dict) -> dict:
        selector = params.get("selector", "")
        if not selector:
            return {"success": False, "error": "selector is required"}
        doc = self._send_cdp("DOM.getDocument", {})
        root_node_id = doc["root"]["nodeId"]
        result = self._send_cdp(
            "DOM.querySelector",
            {"nodeId": root_node_id, "selector": selector},
        )
        node_id = result.get("nodeId", 0)
        if node_id == 0:
            return {
                "success": False,
                "error": f"No element found for selector {selector!r}",
            }
        return {"success": True, "nodeId": node_id}

    def _do_click(self, params: dict) -> dict:
        selector = params.get("selector", "")
        if not selector:
            return {"success": False, "error": "selector is required"}
        doc = self._send_cdp("DOM.getDocument", {})
        root_node_id = doc["root"]["nodeId"]
        result = self._send_cdp(
            "DOM.querySelector",
            {"nodeId": root_node_id, "selector": selector},
        )
        node_id = result.get("nodeId", 0)
        if node_id == 0:
            return {
                "success": False,
                "error": f"No element found for selector {selector!r}",
            }
        box = self._send_cdp("DOM.getBoxModel", {"nodeId": node_id})
        content = box["model"]["content"]
        x = (content[0] + content[2] + content[4] + content[6]) / 4.0
        y = (content[1] + content[3] + content[5] + content[7]) / 4.0
        self._send_cdp("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1,
        })
        self._send_cdp("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1,
        })
        return {"success": True, "x": x, "y": y}

    def _do_type_in_form(self, params: dict) -> dict:
        selector = params.get("selector", "")
        if not selector:
            return {"success": False, "error": "selector is required"}
        text = params.get("text", "")
        if not text:
            return {"success": False, "error": "text is required"}
        doc = self._send_cdp("DOM.getDocument", {})
        root_node_id = doc["root"]["nodeId"]
        result = self._send_cdp(
            "DOM.querySelector",
            {"nodeId": root_node_id, "selector": selector},
        )
        node_id = result.get("nodeId", 0)
        if node_id == 0:
            return {
                "success": False,
                "error": f"No element found for selector {selector!r}",
            }
        self._send_cdp("DOM.focus", {"nodeId": node_id})
        self._send_cdp("Input.insertText", {"text": text})
        return {"success": True, "selector": selector, "length": len(text)}
