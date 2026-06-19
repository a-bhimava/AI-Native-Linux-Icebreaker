"""Template image matching via Pillow (no OpenCV dependency).

Uses Normalized Cross-Correlation (NCC) with a coarse-to-fine strategy:
  1. Downsample source and template by 4x
  2. Slide template over downsampled source, compute NCC at each position
  3. If best NCC ≥ confidence, refine at full resolution in a small neighborhood
  4. Return bounding box + confidence

Used by ``rpa.find_by_image`` when AT-SPI cannot locate elements.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ImageMatchError(Exception):
    """Raised when image matching fails (file not found, corrupt, etc.)."""


@dataclass(frozen=True)
class MatchResult:
    found: bool
    confidence: float
    bbox: tuple[int, int, int, int]  # x, y, width, height
    center: tuple[int, int]


_DOWNSAMPLE_FACTOR = 4
_REFINEMENT_RADIUS = 8


class ImageMatcher:
    """Pillow-based template matcher with configurable confidence threshold."""

    DEFAULT_CONFIDENCE = 0.85

    def __init__(self, confidence: float = DEFAULT_CONFIDENCE) -> None:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence must be 0.0–1.0, got {confidence}")
        self._confidence = confidence

    def match(
        self,
        source_path: str | Path,
        template_path: str | Path,
        *,
        region: Optional[tuple[int, int, int, int]] = None,
    ) -> MatchResult:
        """Find template in source image.

        Args:
            source_path: Full screenshot path.
            template_path: Template image to find.
            region: Optional (x, y, w, h) to restrict search area.

        Returns:
            MatchResult with ``found=True`` if confidence ≥ threshold.
        """
        try:
            from PIL import Image
        except ImportError:
            raise ImageMatchError(
                "Pillow is required for image matching. "
                "Install with: pip install Pillow"
            )

        try:
            source = Image.open(source_path)
        except Exception as exc:
            raise ImageMatchError(f"Cannot open source image: {exc}") from exc

        try:
            template = Image.open(template_path)
        except Exception as exc:
            raise ImageMatchError(f"Cannot open template image: {exc}") from exc

        if region is not None:
            rx, ry, rw, rh = region
            source = source.crop((rx, ry, rx + rw, ry + rh))
        else:
            rx, ry = 0, 0

        src_w, src_h = source.size
        tmpl_w, tmpl_h = template.size

        if tmpl_w > src_w or tmpl_h > src_h:
            return MatchResult(found=False, confidence=0.0, bbox=(0, 0, 0, 0), center=(0, 0))

        src_gray = source.convert("L")
        tmpl_gray = template.convert("L")

        cx, cy, coarse_ncc = self._coarse_search(src_gray, tmpl_gray)

        if coarse_ncc < self._confidence * 0.8:
            return MatchResult(found=False, confidence=coarse_ncc, bbox=(0, 0, 0, 0), center=(0, 0))

        fx, fy, fine_ncc = self._fine_search(src_gray, tmpl_gray, cx, cy)

        abs_x = fx + rx
        abs_y = fy + ry
        center_x = abs_x + tmpl_w // 2
        center_y = abs_y + tmpl_h // 2

        found = fine_ncc >= self._confidence
        return MatchResult(
            found=found,
            confidence=fine_ncc,
            bbox=(abs_x, abs_y, tmpl_w, tmpl_h),
            center=(center_x, center_y),
        )

    def _coarse_search(
        self, src_gray: "Image.Image", tmpl_gray: "Image.Image",
    ) -> tuple[int, int, float]:
        """Downsampled NCC search. Returns (best_x, best_y, best_ncc) in original coords."""
        from PIL import Image

        src_w, src_h = src_gray.size
        tmpl_w, tmpl_h = tmpl_gray.size

        ds = _DOWNSAMPLE_FACTOR
        min_tmpl_dim = min(tmpl_w, tmpl_h)
        while min_tmpl_dim // ds < 8 and ds > 1:
            ds = max(1, ds // 2)

        ds_src = src_gray.resize((max(1, src_w // ds), max(1, src_h // ds)), Image.LANCZOS)
        ds_tmpl = tmpl_gray.resize((max(1, tmpl_w // ds), max(1, tmpl_h // ds)), Image.LANCZOS)

        ds_sw, ds_sh = ds_src.size
        ds_tw, ds_th = ds_tmpl.size

        if ds_tw > ds_sw or ds_th > ds_sh:
            return (0, 0, 0.0)

        src_pixels = list(ds_src.tobytes())
        tmpl_pixels = list(ds_tmpl.tobytes())

        tmpl_mean = sum(tmpl_pixels) / len(tmpl_pixels)
        tmpl_centered = [p - tmpl_mean for p in tmpl_pixels]
        tmpl_norm_sq = sum(v * v for v in tmpl_centered)
        if tmpl_norm_sq < 1e-9:
            return (0, 0, 0.0)

        best_ncc = -1.0
        best_x, best_y = 0, 0

        for sy in range(ds_sh - ds_th + 1):
            for sx in range(ds_sw - ds_tw + 1):
                patch = []
                for ty in range(ds_th):
                    offset = (sy + ty) * ds_sw + sx
                    patch.extend(src_pixels[offset:offset + ds_tw])

                patch_mean = sum(patch) / len(patch)
                num = 0.0
                patch_norm_sq = 0.0
                for i, pv in enumerate(patch):
                    centered = pv - patch_mean
                    num += centered * tmpl_centered[i]
                    patch_norm_sq += centered * centered

                denom = math.sqrt(patch_norm_sq * tmpl_norm_sq)
                if denom < 1e-9:
                    continue
                ncc = num / denom

                if ncc > best_ncc:
                    best_ncc = ncc
                    best_x = sx
                    best_y = sy

        return (best_x * ds, best_y * ds, best_ncc)

    def _fine_search(
        self,
        src_gray: "Image.Image",
        tmpl_gray: "Image.Image",
        coarse_x: int,
        coarse_y: int,
    ) -> tuple[int, int, float]:
        """Full-resolution NCC in a small neighborhood around coarse match."""
        src_w, src_h = src_gray.size
        tmpl_w, tmpl_h = tmpl_gray.size

        src_pixels = list(src_gray.tobytes())
        tmpl_pixels = list(tmpl_gray.tobytes())

        tmpl_mean = sum(tmpl_pixels) / len(tmpl_pixels)
        tmpl_centered = [p - tmpl_mean for p in tmpl_pixels]
        tmpl_norm_sq = sum(v * v for v in tmpl_centered)
        if tmpl_norm_sq < 1e-9:
            return (coarse_x, coarse_y, 0.0)

        r = _REFINEMENT_RADIUS
        x_lo = max(0, coarse_x - r)
        y_lo = max(0, coarse_y - r)
        x_hi = min(src_w - tmpl_w, coarse_x + r)
        y_hi = min(src_h - tmpl_h, coarse_y + r)

        best_ncc = -1.0
        best_x, best_y = coarse_x, coarse_y

        for sy in range(y_lo, y_hi + 1):
            for sx in range(x_lo, x_hi + 1):
                patch = []
                for ty in range(tmpl_h):
                    offset = (sy + ty) * src_w + sx
                    patch.extend(src_pixels[offset:offset + tmpl_w])

                patch_mean = sum(patch) / len(patch)
                num = 0.0
                patch_norm_sq = 0.0
                for i, pv in enumerate(patch):
                    centered = pv - patch_mean
                    num += centered * tmpl_centered[i]
                    patch_norm_sq += centered * centered

                denom = math.sqrt(patch_norm_sq * tmpl_norm_sq)
                if denom < 1e-9:
                    continue
                ncc = num / denom

                if ncc > best_ncc:
                    best_ncc = ncc
                    best_x = sx
                    best_y = sy

        return (best_x, best_y, best_ncc)
