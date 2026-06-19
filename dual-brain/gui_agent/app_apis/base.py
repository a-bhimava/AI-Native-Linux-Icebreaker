"""AppApi abstract base class for application-specific automation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AppApi(ABC):
    """Base class for application-specific GUI automation APIs.

    Concrete subclasses wrap native interfaces (D-Bus, UNO, etc.)
    to provide structured operations instead of raw AT-SPI clicks.
    """

    @abstractmethod
    def supports(self, window_title: str) -> bool:
        """Return True if this API can handle the given window."""

    @abstractmethod
    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute an action and return a structured result dict."""

    @abstractmethod
    def capabilities(self) -> list[str]:
        """Return the list of action names this API supports."""
