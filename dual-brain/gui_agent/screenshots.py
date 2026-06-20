"""D-Bus screenshot capture for the GUI Agent.

Tries ``org.freedesktop.portal.Screenshot`` (Wayland-native, no consent
dialog when ``interactive: false``) first, falls back to
``org.gnome.Shell.Screenshot``.

Security (INV-8 / BP-3):
  - Screenshot files get 0o600 permissions (owner-only)
  - SHA-256 hash recorded in audit, never pixels
  - Retention capped at 50 files; oldest evicted on overflow
  - All filenames sanitized before use
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ScreenshotUnavailableError(Exception):
    """Neither portal nor GNOME Shell screenshot interface is available."""


@dataclass(frozen=True)
class ScreenshotResult:
    path: Path
    sha256: str
    width: int
    height: int
    timestamp: float


_MAX_RETAINED = 50


class ScreenshotManager:
    """D-Bus screenshot capture with portal + GNOME Shell fallback.

    Uses lazy GObject import so tests run without D-Bus.
    """

    def __init__(self, scratch_dir: str | Path) -> None:
        self._scratch = Path(scratch_dir)
        self._scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._dbus_available = False
        self._bus = None
        self._portal_proxy = None
        self._gnome_proxy = None

        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
            self._gio = Gio
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._dbus_available = True
        except (ImportError, ValueError, Exception):
            pass

    @property
    def available(self) -> bool:
        return self._dbus_available

    def capture(
        self,
        window_title: str = "",
        *,
        format: str = "png",
        jpeg_quality: int = 85,
    ) -> ScreenshotResult:
        """Capture a screenshot. Empty window_title = full screen.

        Raises ``ScreenshotUnavailableError`` if no D-Bus method works.
        """
        if not self._dbus_available:
            raise ScreenshotUnavailableError(
                "D-Bus session bus not available. "
                "Screenshots require a running D-Bus session."
            )

        self._enforce_retention()

        ts = time.time()
        png_name = f"screenshot_{int(ts * 1000)}.png"
        dest = self._scratch / png_name

        path = self._try_portal(dest, window_title)
        if path is None:
            path = self._try_gnome_shell(dest, window_title)
        if path is None:
            raise ScreenshotUnavailableError(
                "Screenshot capture failed. Neither "
                "org.freedesktop.portal.Screenshot nor "
                "org.gnome.Shell.Screenshot responded. "
                "Ensure you are running a GNOME-based desktop session."
            )

        if format == "jpeg":
            path = self._convert_to_jpeg(path, jpeg_quality)

        os.chmod(path, 0o600)

        sha = self._hash_file(path)
        w, h = self._image_dimensions(path)

        return ScreenshotResult(
            path=path,
            sha256=sha,
            width=w,
            height=h,
            timestamp=ts,
        )

    def cleanup(self) -> int:
        """Remove all screenshots from scratch dir. Returns count removed."""
        count = 0
        for pattern in ("screenshot_*.png", "screenshot_*.jpg"):
            for f in self._scratch.glob(pattern):
                try:
                    f.unlink()
                    count += 1
                except OSError:
                    pass
        return count

    def _try_portal(self, dest: Path, window_title: str) -> Path | None:
        """Try org.freedesktop.portal.Screenshot (Wayland)."""
        try:
            Gio = self._gio
            result = self._bus.call_sync(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Screenshot",
                "Screenshot",
                Gio.Variant("(sa{sv})", (
                    "",
                    {"interactive": Gio.Variant("b", False)},
                )),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            if result is None:
                return None

            response = result.unpack()
            if isinstance(response, tuple) and len(response) > 0:
                uri = str(response[0])
                if uri.startswith("file://"):
                    src = Path(uri[7:])
                    if src.exists():
                        if src != dest:
                            import shutil
                            shutil.move(str(src), str(dest))
                        return dest
        except Exception:
            pass
        return None

    def _try_gnome_shell(self, dest: Path, window_title: str) -> Path | None:
        """Try org.gnome.Shell.Screenshot (X11/Wayland fallback)."""
        try:
            Gio = self._gio
            if window_title:
                result = self._bus.call_sync(
                    "org.gnome.Shell.Screenshot",
                    "/org/gnome/Shell/Screenshot",
                    "org.gnome.Shell.Screenshot",
                    "ScreenshotWindow",
                    Gio.Variant("(bbbs)", (True, False, False, str(dest))),
                    None,
                    Gio.DBusCallFlags.NONE,
                    5000,
                    None,
                )
            else:
                result = self._bus.call_sync(
                    "org.gnome.Shell.Screenshot",
                    "/org/gnome/Shell/Screenshot",
                    "org.gnome.Shell.Screenshot",
                    "Screenshot",
                    Gio.Variant("(bbs)", (False, False, str(dest))),
                    None,
                    Gio.DBusCallFlags.NONE,
                    5000,
                    None,
                )

            if result is not None:
                unpacked = result.unpack()
                success = unpacked[0] if isinstance(unpacked, tuple) else unpacked
                if success and dest.exists():
                    return dest
        except Exception:
            pass
        return None

    def _convert_to_jpeg(self, png_path: Path, quality: int) -> Path:
        """Convert a PNG screenshot to JPEG. Removes the original."""
        from PIL import Image
        jpg_path = png_path.with_suffix(".jpg")
        with Image.open(png_path) as img:
            img = img.convert("RGB")
            img.save(jpg_path, "JPEG", quality=quality)
        png_path.unlink()
        return jpg_path

    def thumbnail(
        self, path: str | Path, *, max_size: tuple[int, int] = (160, 120),
    ) -> Path:
        """Generate a thumbnail. Returns the thumbnail path."""
        from PIL import Image
        src = Path(path)
        thumb_path = src.with_stem(src.stem + "_thumb")
        with Image.open(src) as img:
            img.thumbnail(max_size)
            img.save(thumb_path, img.format or "PNG")
        os.chmod(thumb_path, 0o600)
        return thumb_path

    def _enforce_retention(self) -> None:
        """Evict oldest screenshots if over the retention limit."""
        files = sorted(
            list(self._scratch.glob("screenshot_*.png"))
            + list(self._scratch.glob("screenshot_*.jpg")),
            key=lambda f: f.stat().st_mtime,
        )
        while len(files) >= _MAX_RETAINED:
            oldest = files.pop(0)
            try:
                oldest.unlink()
            except OSError:
                pass

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _image_dimensions(path: Path) -> tuple[int, int]:
        """Read image dimensions from header (PNG IHDR or Pillow fallback)."""
        try:
            with open(path, "rb") as f:
                header = f.read(24)
                if len(header) >= 24 and header[:8] == b"\x89PNG\r\n\x1a\n":
                    w = int.from_bytes(header[16:20], "big")
                    h = int.from_bytes(header[20:24], "big")
                    return (w, h)
            if path.suffix.lower() in (".jpg", ".jpeg"):
                from PIL import Image
                with Image.open(path) as img:
                    return img.size
        except Exception:
            pass
        return (0, 0)
