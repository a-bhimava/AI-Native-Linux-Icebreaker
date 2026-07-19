"""Template image matching via NumPy fast-NCC, with a Pillow fallback.

Preferred backend ("numpy"): exact Normalized Cross-Correlation over the
full image in one pass, using FFT cross-correlation for the numerator and
integral images for the per-window normalization (Lewis' fast NCC). This
is orders of magnitude faster than the sliding-window Python loop and
searches every position, so it cannot miss a match the way a downsampled
coarse pass can.

Fallback backend ("pillow"): the original pure-Python coarse-to-fine NCC:
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

_BACKENDS = ("auto", "numpy", "pillow")


def _numpy_available() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _next_fast_len(n: int) -> int:
    """Smallest 5-smooth number >= n (numpy's FFT is fast only for radix 2/3/5;
    a prime-sized axis — e.g. 2039 for a 1920px screenshot — is orders of
    magnitude slower)."""
    while True:
        m = n
        for p in (2, 3, 5):
            while m % p == 0:
                m //= p
        if m == 1:
            return n
        n += 1


class ImageMatcher:
    """NCC template matcher with configurable confidence threshold and backend."""

    DEFAULT_CONFIDENCE = 0.85

    def __init__(
        self,
        confidence: float = DEFAULT_CONFIDENCE,
        *,
        backend: str = "auto",
    ) -> None:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence must be 0.0–1.0, got {confidence}")
        if backend not in _BACKENDS:
            raise ValueError(f"backend must be one of {_BACKENDS}, got {backend!r}")
        if backend == "numpy" and not _numpy_available():
            raise ImageMatchError(
                "NumPy backend requested but numpy is not installed. "
                "Install with: pip install numpy"
            )
        self._confidence = confidence
        self._backend = backend

    @property
    def active_backend(self) -> str:
        """The backend that ``match()`` will actually use."""
        if self._backend == "auto":
            return "numpy" if _numpy_available() else "pillow"
        return self._backend

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

        if self.active_backend == "numpy":
            fx, fy, fine_ncc = self._search_numpy(src_gray, tmpl_gray)
        else:
            cx, cy, coarse_ncc = self._coarse_search(src_gray, tmpl_gray)

            if coarse_ncc < self._confidence * 0.8:
                return MatchResult(
                    found=False, confidence=coarse_ncc, bbox=(0, 0, 0, 0), center=(0, 0),
                )

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

    def _search_numpy(
        self, src_gray: "Image.Image", tmpl_gray: "Image.Image",
    ) -> tuple[int, int, float]:
        """Exact full-image NCC via FFT + integral images (Lewis' fast NCC).

        Returns (best_x, best_y, best_ncc) at full resolution. Unlike the
        Pillow coarse-to-fine path this evaluates every window position,
        so the returned NCC is the true global maximum.
        """
        import numpy as np

        src = np.asarray(src_gray, dtype=np.float64)
        tmpl = np.asarray(tmpl_gray, dtype=np.float64)
        sh, sw = src.shape
        th, tw = tmpl.shape

        tmpl_centered = tmpl - tmpl.mean()
        tmpl_norm_sq = float((tmpl_centered * tmpl_centered).sum())
        if tmpl_norm_sq < 1e-9:
            return (0, 0, 0.0)

        out_h = sh - th + 1
        out_w = sw - tw + 1

        # Numerator: sum over each window of src * (tmpl - mean(tmpl)).
        # Because sum(tmpl_centered) == 0, subtracting the window mean from
        # src changes nothing — this IS the centered cross-correlation.
        fshape = (_next_fast_len(sh + th - 1), _next_fast_len(sw + tw - 1))
        fsrc = np.fft.rfft2(src, fshape)
        ftmpl = np.fft.rfft2(tmpl_centered[::-1, ::-1], fshape)
        corr_full = np.fft.irfft2(fsrc * ftmpl, fshape)
        corr = corr_full[th - 1:th - 1 + out_h, tw - 1:tw - 1 + out_w]

        # Denominator: per-window variance via integral images.
        n = float(th * tw)
        padded = np.pad(src, ((1, 0), (1, 0)))
        padded_sq = np.pad(src * src, ((1, 0), (1, 0)))
        ii = padded.cumsum(axis=0).cumsum(axis=1)
        ii2 = padded_sq.cumsum(axis=0).cumsum(axis=1)
        win_sum = ii[th:, tw:] - ii[:-th, tw:] - ii[th:, :-tw] + ii[:-th, :-tw]
        win_sum_sq = ii2[th:, tw:] - ii2[:-th, tw:] - ii2[th:, :-tw] + ii2[:-th, :-tw]
        win_var = win_sum_sq - (win_sum * win_sum) / n
        np.maximum(win_var, 0.0, out=win_var)

        denom = np.sqrt(win_var * tmpl_norm_sq)
        with np.errstate(divide="ignore", invalid="ignore"):
            ncc = np.where(denom > 1e-9, corr / np.maximum(denom, 1e-12), -1.0)

        flat_idx = int(np.argmax(ncc))
        best_y, best_x = divmod(flat_idx, out_w)
        best_ncc = float(min(1.0, max(-1.0, ncc[best_y, best_x])))
        return (int(best_x), int(best_y), best_ncc)

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
