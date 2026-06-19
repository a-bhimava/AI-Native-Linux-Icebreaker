"""Generic AT-SPI fallback API for apps without dedicated wrappers.

Always returns ``supports() = True`` — this is the catch-all. Uses
``AtSpiClient`` for all interactions, translating high-level action
names into AT-SPI tree lookups.
"""

from __future__ import annotations

from typing import Any

from .base import AppApi
from .registry import register_app_api
from ..atspi import (
    AtSpiClient,
    AtSpiUnavailableError,
    ElementNotFoundError,
    ElementTooSmallError,
)


_GENERIC_ACTIONS = frozenset({
    "click", "type", "select",
    "find_element", "get_window_list", "get_element_tree",
})


@register_app_api("generic_atspi")
class GenericAtSpiApi(AppApi):
    """Fallback API using raw AT-SPI for any application."""

    def __init__(self) -> None:
        self._client = AtSpiClient()

    @property
    def available(self) -> bool:
        return self._client.available

    def supports(self, window_title: str) -> bool:
        return True

    def capabilities(self) -> list[str]:
        return sorted(_GENERIC_ACTIONS)

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action not in _GENERIC_ACTIONS:
            return {
                "success": False,
                "error": f"Action {action!r} not supported. "
                         f"Available: {sorted(_GENERIC_ACTIONS)}",
            }

        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return {"success": False, "error": f"No handler for {action!r}"}

        try:
            return handler(params)
        except (AtSpiUnavailableError, ElementNotFoundError, ElementTooSmallError) as exc:
            return {"success": False, "error": str(exc)}

    def _do_click(self, params: dict) -> dict:
        elem = self._client.find_element(
            params["window"], params["role"], params["name"],
        )
        result = self._client.click(elem)
        return {"success": result.success, "error": result.error}

    def _do_type(self, params: dict) -> dict:
        elem = self._client.find_element(
            params["window"], params["role"], params["name"],
        )
        result = self._client.type_text(elem, params["text"])
        return {"success": result.success, "error": result.error}

    def _do_select(self, params: dict) -> dict:
        elem = self._client.find_element(
            params["window"], params["role"], params["name"],
        )
        result = self._client.select(elem, params["value"])
        return {"success": result.success, "error": result.error}

    def _do_find_element(self, params: dict) -> dict:
        elem = self._client.find_element(
            params["window"], params["role"], params["name"],
        )
        return {
            "success": True,
            "path": elem.path,
            "role": elem.role,
            "name": elem.name,
            "position": list(elem.position),
            "size": list(elem.size),
            "states": sorted(elem.states),
            "text": elem.text,
        }

    def _do_get_window_list(self, params: dict) -> dict:
        windows = self._client.get_window_list()
        return {
            "success": True,
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

    def _do_get_element_tree(self, params: dict) -> dict:
        max_depth = params.get("max_depth", 3)
        tree = self._client.get_element_tree(params["window"], max_depth=max_depth)
        return {"success": True, "tree": tree}
