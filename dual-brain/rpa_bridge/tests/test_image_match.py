"""Tests for ImageMatcher — NCC template matching (PR #28; NumPy backend added)."""

from __future__ import annotations

import time

import pytest

PIL = pytest.importorskip("PIL")

from PIL import Image

from rpa_bridge.image_match import (
    ImageMatcher,
    ImageMatchError,
    MatchResult,
    _numpy_available,
)

_BACKEND_PARAMS = [
    "pillow",
    pytest.param(
        "numpy",
        marks=pytest.mark.skipif(not _numpy_available(), reason="numpy not installed"),
    ),
]


def _create_solid_image(path, width, height, color):
    """Create a solid-color test image."""
    img = Image.new("RGB", (width, height), color)
    img.save(path)
    return path


def _create_image_with_patch(path, width, height, bg_color, patch_color, patch_rect):
    """Create an image with a distinctive rectangular patch."""
    img = Image.new("RGB", (width, height), bg_color)
    px, py, pw, ph = patch_rect
    for y in range(py, py + ph):
        for x in range(px, px + pw):
            img.putpixel((x, y), patch_color)
    img.save(path)
    return path


def _create_distinctive_patch(path, width, height, bg_color, offset_x, offset_y):
    """Create an image with a distinctive multi-shade pattern for NCC matching."""
    img = Image.new("RGB", (width, height), bg_color)
    pw, ph = 40, 40
    for y in range(ph):
        for x in range(pw):
            shade = int(50 + 150 * (x / pw))
            img.putpixel((offset_x + x, offset_y + y), (shade, shade, shade))
    img.save(path)
    return path


def _create_template_from_pattern(path, width, height):
    """Create a template with the same gradient pattern."""
    img = Image.new("RGB", (width, height), (0, 0, 0))
    for y in range(height):
        for x in range(width):
            shade = int(50 + 150 * (x / width))
            img.putpixel((x, y), (shade, shade, shade))
    img.save(path)
    return path


class TestImageMatcher:
    @pytest.mark.parametrize("backend", _BACKEND_PARAMS)
    def test_finds_template_in_source(self, tmp_path, backend):
        source_path = _create_distinctive_patch(
            tmp_path / "source.png", 200, 200,
            bg_color=(200, 200, 200),
            offset_x=80, offset_y=60,
        )
        template_path = _create_template_from_pattern(
            tmp_path / "template.png", 40, 40,
        )

        matcher = ImageMatcher(confidence=0.7, backend=backend)
        result = matcher.match(source_path, template_path)

        assert result.found is True
        assert result.confidence >= 0.7
        assert abs(result.bbox[0] - 80) <= 16
        assert abs(result.bbox[1] - 60) <= 16
        assert result.bbox[2] == 40
        assert result.bbox[3] == 40

    @pytest.mark.parametrize("backend", _BACKEND_PARAMS)
    def test_below_confidence_returns_not_found(self, tmp_path, backend):
        source_path = _create_solid_image(
            tmp_path / "source.png", 200, 200, (100, 100, 100),
        )
        template_path = _create_template_from_pattern(
            tmp_path / "template.png", 40, 40,
        )

        matcher = ImageMatcher(confidence=0.99, backend=backend)
        result = matcher.match(source_path, template_path)

        assert result.found is False

    @pytest.mark.parametrize("backend", _BACKEND_PARAMS)
    def test_template_larger_than_source_returns_not_found(self, tmp_path, backend):
        source_path = _create_solid_image(tmp_path / "small.png", 20, 20, (100, 100, 100))
        template_path = _create_solid_image(tmp_path / "big.png", 100, 100, (100, 100, 100))

        matcher = ImageMatcher(confidence=0.5, backend=backend)
        result = matcher.match(source_path, template_path)
        assert result.found is False

    def test_invalid_source_raises(self, tmp_path):
        template_path = _create_solid_image(tmp_path / "t.png", 10, 10, (0, 0, 0))
        matcher = ImageMatcher()
        with pytest.raises(ImageMatchError, match="Cannot open source"):
            matcher.match("/nonexistent/path.png", template_path)

    def test_invalid_confidence_raises(self):
        with pytest.raises(ValueError, match="confidence"):
            ImageMatcher(confidence=1.5)

    @pytest.mark.parametrize("backend", _BACKEND_PARAMS)
    def test_region_restricts_search(self, tmp_path, backend):
        source_path = _create_distinctive_patch(
            tmp_path / "source.png", 200, 200,
            bg_color=(200, 200, 200),
            offset_x=10, offset_y=10,
        )
        template_path = _create_template_from_pattern(
            tmp_path / "template.png", 40, 40,
        )

        matcher = ImageMatcher(confidence=0.7, backend=backend)
        result = matcher.match(
            source_path, template_path,
            region=(120, 120, 80, 80),
        )
        assert result.found is False

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="backend"):
            ImageMatcher(backend="opencv")


@pytest.mark.skipif(not _numpy_available(), reason="numpy not installed")
class TestNumpyBackend:
    """BP-12 differential + latency guards for the NumPy fast-NCC backend."""

    def test_differential_numpy_vs_pillow(self, tmp_path):
        """Both backends must agree on found/bbox, confidence within 0.02."""
        source_path = _create_distinctive_patch(
            tmp_path / "source.png", 200, 200,
            bg_color=(200, 200, 200),
            offset_x=80, offset_y=60,
        )
        template_path = _create_template_from_pattern(
            tmp_path / "template.png", 40, 40,
        )

        pillow = ImageMatcher(confidence=0.7, backend="pillow").match(
            source_path, template_path,
        )
        numpy_r = ImageMatcher(confidence=0.7, backend="numpy").match(
            source_path, template_path,
        )

        assert numpy_r.found == pillow.found is True
        assert abs(numpy_r.bbox[0] - pillow.bbox[0]) <= 2
        assert abs(numpy_r.bbox[1] - pillow.bbox[1]) <= 2
        assert numpy_r.bbox[2:] == pillow.bbox[2:]
        assert abs(numpy_r.confidence - pillow.confidence) <= 0.02

    def test_latency_full_screenshot(self, tmp_path):
        """Regression guard: a full 1920x1080 match must stay fast.

        The pure-Python path takes tens of seconds on this input; the
        NumPy path is ~tens of milliseconds. The 2 s budget leaves ample
        headroom for slow/contended CI machines while still catching any
        regression back to a Python-loop implementation (>10x margin).
        """
        import numpy as np

        rng = np.random.default_rng(42)
        src = rng.integers(0, 256, size=(1080, 1920), dtype=np.uint8)
        source_img = Image.fromarray(src, mode="L")
        template_img = source_img.crop((900, 500, 1020, 540))  # 120x40

        source_path = tmp_path / "screen.png"
        template_path = tmp_path / "tmpl.png"
        source_img.save(source_path)
        template_img.save(template_path)

        matcher = ImageMatcher(confidence=0.85, backend="numpy")
        matcher.match(source_path, template_path)  # warm-up (FFT plan, page cache)

        start = time.monotonic()
        result = matcher.match(source_path, template_path)
        elapsed = time.monotonic() - start

        assert result.found is True
        assert result.bbox == (900, 500, 120, 40)
        assert elapsed < 2.0, f"NumPy NCC took {elapsed:.3f}s (budget 2s)"
