"""User-owned appearance preferences shared by Icebreaker desktop apps.

This module deliberately owns only presentation preferences.  It never
launches desktop tools or changes controller/mcpd configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


_CONFIG_PATH = Path.home() / ".config" / "icebreaker" / "appearance.toml"
_PROFILES = frozenset({"classic", "frosted"})


@dataclass(frozen=True)
class AppearanceSettings:
    profile: str = "frosted"
    dock_enabled: bool = True
    notifications_enabled: bool = False
    overview_enabled: bool = False
    compositor_enabled: bool = False

    def __post_init__(self) -> None:
        if self.profile not in _PROFILES:
            raise ValueError(f"unknown appearance profile: {self.profile}")


def load_appearance(path: Path = _CONFIG_PATH) -> AppearanceSettings:
    """Return safe defaults for missing, malformed, or unknown preferences."""
    try:
        raw = tomllib.loads(path.read_text())
        profile = raw.get("profile", "frosted")
        if profile not in _PROFILES:
            return AppearanceSettings()
        values = {
            "profile": profile,
            "dock_enabled": raw.get("dock_enabled", True),
            "notifications_enabled": raw.get("notifications_enabled", False),
            "overview_enabled": raw.get("overview_enabled", False),
            "compositor_enabled": raw.get("compositor_enabled", False),
        }
        if not all(isinstance(value, bool) for key, value in values.items() if key != "profile"):
            return AppearanceSettings()
        return AppearanceSettings(**values)
    except (OSError, tomllib.TOMLDecodeError):
        return AppearanceSettings()


def save_appearance(settings: AppearanceSettings, path: Path = _CONFIG_PATH) -> None:
    """Persist canonical values only; callers cannot inject shell/config syntax."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                f'profile = "{settings.profile}"',
                f"dock_enabled = {str(settings.dock_enabled).lower()}",
                f"notifications_enabled = {str(settings.notifications_enabled).lower()}",
                f"overview_enabled = {str(settings.overview_enabled).lower()}",
                f"compositor_enabled = {str(settings.compositor_enabled).lower()}",
                "",
            )
        )
    )
