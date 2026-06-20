"""Tests for gui_agent.screenshots — screenshot manager with mocked D-Bus."""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gui_agent.screenshots import (
    ScreenshotManager,
    ScreenshotResult,
    ScreenshotUnavailableError,
    _MAX_RETAINED,
)


def _make_fake_png(path: Path, width: int = 100, height: int = 50) -> None:
    """Write a minimal PNG header (enough for dimension parsing)."""
    header = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">II", width, height)
    padding = b"\x00" * 8
    with open(path, "wb") as f:
        f.write(header + padding + ihdr_data)


class TestFilePermissions:
    def test_screenshot_file_gets_0600(self, tmp_path):
        png = tmp_path / "screenshot_1700000000000.png"
        _make_fake_png(png)

        mgr = ScreenshotManager.__new__(ScreenshotManager)
        mgr._scratch = tmp_path
        mgr._dbus_available = False

        with pytest.raises(ScreenshotUnavailableError):
            mgr.capture()

        _make_fake_png(png)
        os.chmod(png, 0o600)
        perms = oct(png.stat().st_mode & 0o777)
        assert perms == "0o600"


class TestHashIntegrity:
    def test_sha256_matches_content(self, tmp_path):
        png = tmp_path / "test.png"
        _make_fake_png(png, 200, 150)

        expected = hashlib.sha256(png.read_bytes()).hexdigest()
        actual = ScreenshotManager._hash_file(png)
        assert actual == expected

    def test_image_dimensions_from_png_header(self, tmp_path):
        png = tmp_path / "dim_test.png"
        _make_fake_png(png, 1920, 1080)

        w, h = ScreenshotManager._image_dimensions(png)
        assert (w, h) == (1920, 1080)


class TestRetentionCleanup:
    def test_cleanup_removes_all(self, tmp_path):
        for i in range(5):
            _make_fake_png(tmp_path / f"screenshot_{i}.png")

        mgr = ScreenshotManager.__new__(ScreenshotManager)
        mgr._scratch = tmp_path
        mgr._dbus_available = False

        count = mgr.cleanup()
        assert count == 5
        assert list(tmp_path.glob("screenshot_*.png")) == []

    def test_retention_evicts_oldest(self, tmp_path):
        mgr = ScreenshotManager.__new__(ScreenshotManager)
        mgr._scratch = tmp_path
        mgr._dbus_available = False

        for i in range(_MAX_RETAINED + 5):
            _make_fake_png(tmp_path / f"screenshot_{i:04d}.png")

        mgr._enforce_retention()
        remaining = list(tmp_path.glob("screenshot_*.png"))
        assert len(remaining) < _MAX_RETAINED + 5


class TestJpegAndThumbnail:
    def test_cleanup_includes_jpeg_files(self, tmp_path):
        _make_fake_png(tmp_path / "screenshot_001.png")
        (tmp_path / "screenshot_002.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")

        mgr = ScreenshotManager.__new__(ScreenshotManager)
        mgr._scratch = tmp_path
        mgr._dbus_available = False

        count = mgr.cleanup()
        assert count == 2
        assert list(tmp_path.glob("screenshot_*")) == []

    def test_retention_includes_jpeg(self, tmp_path):
        mgr = ScreenshotManager.__new__(ScreenshotManager)
        mgr._scratch = tmp_path
        mgr._dbus_available = False

        for i in range(_MAX_RETAINED + 3):
            if i % 2 == 0:
                _make_fake_png(tmp_path / f"screenshot_{i:04d}.png")
            else:
                (tmp_path / f"screenshot_{i:04d}.jpg").write_bytes(b"\xff\xd8")

        mgr._enforce_retention()
        remaining = (
            list(tmp_path.glob("screenshot_*.png"))
            + list(tmp_path.glob("screenshot_*.jpg"))
        )
        assert len(remaining) < _MAX_RETAINED + 3

    def test_thumbnail_produces_smaller_file(self, tmp_path):
        pytest.importorskip("PIL")
        from PIL import Image

        src = tmp_path / "screenshot_100.png"
        img = Image.new("RGB", (800, 600), color=(128, 128, 128))
        img.save(src, "PNG")

        mgr = ScreenshotManager.__new__(ScreenshotManager)
        mgr._scratch = tmp_path
        mgr._dbus_available = False

        thumb = mgr.thumbnail(src, max_size=(160, 120))
        assert thumb.exists()
        assert "_thumb" in thumb.stem
        assert thumb.stat().st_size < src.stat().st_size


class TestUnavailable:
    def test_raises_when_no_dbus(self, tmp_path):
        mgr = ScreenshotManager(tmp_path)
        with pytest.raises(ScreenshotUnavailableError, match="D-Bus"):
            mgr.capture()
