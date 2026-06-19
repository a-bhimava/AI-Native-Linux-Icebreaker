"""App API package — pluggable application-specific automation APIs.

Import concrete APIs here for self-registration via
``@register_app_api(app_name)``. ``get_app_api()`` dispatches by
window title; ``GenericAtSpiApi`` is the fallback.
"""

from .registry import get_app_api, list_app_apis, register_app_api
from .base import AppApi
from . import libreoffice as _lo  # noqa: F401 — self-registration
from . import generic_atspi as _gen  # noqa: F401 — self-registration

__all__ = [
    "AppApi",
    "get_app_api",
    "list_app_apis",
    "register_app_api",
]
