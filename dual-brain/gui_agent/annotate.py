"""Annotated-screenshot rendering for Fix V.

F-51 marker: F-104-annotate (per incremental/GROUND_TRUTH.md F-104).

Turns a raw screenshot + a parsed element list into a PNG the user
sees on their desktop *before* any grounded action fires. Non-blocking
recognition-over-recall UX inspired by Anthropic Computer Use previews,
Chrome permission chips (kind-colored borders), and Notion's hover
previews.

Design highlights:
- **Kind-colored borders.** Button=red, input=blue, link=green,
  icon=gray, others=purple. User pattern-matches "the red one" faster
  than reading captions.
- **Target highlight.** When ``target_id`` is set, that element gets
  a thick outline + a filled red arrow anchored to its centroid, and
  every other element's region is dimmed 40% so the eye is drawn to
  the target.
- **Confidence signal.** Elements with confidence < 0.7 get a
  dashed border and a small "?" glyph — honest surface of model
  uncertainty (BP-6: model uncertainty is data, not to be hidden).
- **Watermark.** Corner tag with product name + timestamp + monitor
  scale — so a preview PNG can never be confused for real UI
  (anti-spoofing, BP-3 spirit).
- **Numeric labels.** Each element gets its ``id`` prefixed to the
  caption for the LLM-pick + user-say-"click #7" flow.

Zero new deps — Pillow is the vision-module dep already.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from gui_agent.vision import ElementBox

_log = logging.getLogger(__name__)


# ── Palette (kind → RGB) ────────────────────────────────────────────────

_KIND_COLOR: dict[str, tuple[int, int, int]] = {
    "button":   (255,  59,  48),   # iOS red
    "input":    ( 10, 132, 255),   # iOS blue
    "link":     ( 52, 199,  89),   # iOS green
    "icon":     (142, 142, 147),   # iOS gray
    "text":     (255, 204,   0),   # iOS yellow
    "list":     (175,  82, 222),   # iOS purple
    "menu":     (255, 149,   0),   # iOS orange
    "tab":      ( 90, 200, 250),   # iOS teal
    "checkbox": ( 88,  86, 214),   # iOS indigo
    "radio":    ( 88,  86, 214),
    "slider":   (255,  45,  85),   # iOS pink
    "other":    (199, 199, 204),   # iOS light gray
}

_TARGET_COLOR = (255,  59,  48)   # Distinct red for the target arrow/box
_LABEL_TEXT_COLOR = (255, 255, 255)
_LABEL_SHADOW_COLOR = (0, 0, 0)
_WATERMARK_COLOR = (200, 200, 200)
_DIM_OVERLAY = (0, 0, 0, 100)     # 40% dim on non-target regions

_LOW_CONFIDENCE_THRESHOLD = 0.7

# Retention: 50 preview PNGs, oldest evicted (matches ScreenshotManager).
_PREVIEW_DIR = Path("/tmp/icebreaker-gui")
_PREVIEW_RETENTION = 50


# ── Result shape ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AnnotateResult:
    path: Path
    sha256: str
    width: int
    height: int
    element_count: int
    target_id: Optional[int]
    timestamp: float


# ── Public API ──────────────────────────────────────────────────────────

def render_annotated(
    image: Image.Image,
    elements: list[ElementBox],
    target_id: Optional[int] = None,
    preview_dir: Path = _PREVIEW_DIR,
    watermark_note: str = "",
) -> AnnotateResult:
    """Render an annotated PNG suitable for `notify-send --icon=...`.

    Parameters
    ----------
    image : PIL.Image
        The raw screenshot (RGB or RGBA).
    elements : list[ElementBox]
        The VLM-parsed element list from ``vision.VisionGrounder.parse_screen``.
    target_id : int, optional
        If set, that element gets the "about to click" highlight —
        thick red border, filled arrow anchored to centroid, and every
        other region is dimmed to draw the eye.
    preview_dir : Path
        Where to write the PNG. Defaults to ``/tmp/icebreaker-gui``.
    watermark_note : str
        Short text appended to the watermark (e.g. app name, prompt).
    """
    preview_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Convert to RGBA for compositing (draw overlays on separate layers).
    if image.mode != "RGBA":
        base = image.convert("RGBA")
    else:
        base = image.copy()

    width, height = base.size

    # Optional dim layer for non-target regions.
    if target_id is not None and _element_by_id(elements, target_id) is not None:
        base = _apply_target_dim(base, elements, target_id)

    # Draw every element's box + label on a transparent overlay layer.
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_small = _load_font(size=13)
    font_target = _load_font(size=16, bold=True)

    for e in elements:
        _draw_element_box(draw, e, is_target=(e.id == target_id),
                          font_small=font_small, font_target=font_target)

    # If target is set, add the red arrow last so it sits on top.
    if target_id is not None:
        target = _element_by_id(elements, target_id)
        if target is not None:
            _draw_target_arrow(draw, target, image_size=(width, height))

    # Corner watermark.
    _draw_watermark(draw, width, height, watermark_note,
                    element_count=len(elements))

    composed = Image.alpha_composite(base, overlay).convert("RGB")
    png_bytes = _image_to_png_bytes(composed)
    sha = hashlib.sha256(png_bytes).hexdigest()

    out_path = preview_dir / f"preview-{sha[:16]}.png"
    out_path.write_bytes(png_bytes)
    out_path.chmod(0o600)

    _prune_old_previews(preview_dir)

    return AnnotateResult(
        path=out_path,
        sha256=sha,
        width=width,
        height=height,
        element_count=len(elements),
        target_id=target_id,
        timestamp=time.time(),
    )


# ── Rendering helpers ───────────────────────────────────────────────────

def _draw_element_box(
    draw: ImageDraw.ImageDraw,
    element: ElementBox,
    *,
    is_target: bool,
    font_small: ImageFont.ImageFont,
    font_target: ImageFont.ImageFont,
) -> None:
    x, y, w, h = element.box
    color = _KIND_COLOR.get(element.kind, _KIND_COLOR["other"])
    low_conf = element.confidence < _LOW_CONFIDENCE_THRESHOLD

    # Box border. Target = 4px solid; low-confidence = 2px dashed;
    # normal = 2px solid.
    if is_target:
        _draw_rect(draw, x, y, w, h, outline=_TARGET_COLOR, width=4)
    elif low_conf:
        _draw_dashed_rect(draw, x, y, w, h, outline=color, width=2, dash=6)
    else:
        _draw_rect(draw, x, y, w, h, outline=color, width=2)

    # Label: "#id caption" — id in a small filled chip anchored to
    # the box's top-left; caption text next to it with a soft shadow.
    label_id = f"#{element.id}"
    label_caption = element.caption[:40] + ("…" if len(element.caption) > 40 else "")
    if low_conf and not is_target:
        label_caption = f"{label_caption} ?"

    font = font_target if is_target else font_small
    _draw_label_chip(
        draw, x=x, y=y,
        chip_text=label_id,
        chip_bg=_TARGET_COLOR if is_target else color,
        caption_text=label_caption,
        font=font,
    )


def _draw_target_arrow(
    draw: ImageDraw.ImageDraw,
    target: ElementBox,
    image_size: tuple[int, int],
) -> None:
    """Filled red arrow anchored to the target's centroid, pointing at it
    from ~80px away on whichever axis has room."""
    width, height = image_size
    cx, cy = target.centroid
    # Anchor 80px above the target if there's room, else below/left/right.
    if cy > 90:
        tail_x, tail_y = cx, cy - 80
    elif cy < height - 90:
        tail_x, tail_y = cx, cy + 80
    elif cx > 90:
        tail_x, tail_y = cx - 80, cy
    else:
        tail_x, tail_y = cx + 80, cy

    # Shaft (line + small tip).
    draw.line([(tail_x, tail_y), (cx, cy)],
              fill=_TARGET_COLOR, width=5)
    # Arrowhead — a small filled triangle at (cx, cy).
    ah = 12
    if tail_y < cy:                  # arrow pointing down
        pts = [(cx, cy), (cx - ah, cy - ah), (cx + ah, cy - ah)]
    elif tail_y > cy:                # arrow pointing up
        pts = [(cx, cy), (cx - ah, cy + ah), (cx + ah, cy + ah)]
    elif tail_x < cx:                # arrow pointing right
        pts = [(cx, cy), (cx - ah, cy - ah), (cx - ah, cy + ah)]
    else:                            # arrow pointing left
        pts = [(cx, cy), (cx + ah, cy - ah), (cx + ah, cy + ah)]
    draw.polygon(pts, fill=_TARGET_COLOR)


def _draw_watermark(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    note: str,
    element_count: int,
) -> None:
    """Bottom-right corner tag so PNG can't be mistaken for real UI."""
    ts = time.strftime("%H:%M:%S", time.localtime())
    text = f"Icebreaker vision · {ts} · {element_count} elements"
    if note:
        text += f" · {note[:60]}"
    font = _load_font(size=11)

    # Measure text — Pillow API changed across versions, so handle both.
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    margin = 8
    x = width - tw - margin - 4
    y = height - th - margin - 4

    # Semi-transparent background pill.
    pad = 4
    draw.rectangle(
        (x - pad, y - pad, x + tw + pad, y + th + pad),
        fill=(0, 0, 0, 180),
    )
    draw.text((x, y), text, fill=_WATERMARK_COLOR, font=font)


def _draw_label_chip(
    draw: ImageDraw.ImageDraw,
    x: int, y: int,
    chip_text: str, chip_bg: tuple,
    caption_text: str,
    font: ImageFont.ImageFont,
) -> None:
    """Draw a filled ID chip + caption text with a subtle shadow.
    Placement: chip anchored to the box's top-left corner; caption to
    the right of the chip. If the box is at y=0 (against top edge),
    flip the label INSIDE the box so it doesn't get cropped off-screen.
    """
    label_y = y - 20 if y > 22 else y + 2   # above box, or inside if no room

    # Chip
    chip_bbox = draw.textbbox((0, 0), chip_text, font=font)
    cw = chip_bbox[2] - chip_bbox[0]
    ch = chip_bbox[3] - chip_bbox[1]
    chip_pad_x, chip_pad_y = 5, 2
    draw.rectangle(
        (x, label_y,
         x + cw + 2 * chip_pad_x, label_y + ch + 2 * chip_pad_y),
        fill=(*chip_bg, 220),
    )
    draw.text((x + chip_pad_x, label_y + chip_pad_y),
              chip_text, fill=_LABEL_TEXT_COLOR, font=font)

    # Caption — 6px to the right of chip end, with 1px black shadow for contrast.
    cap_x = x + cw + 2 * chip_pad_x + 6
    cap_y = label_y + chip_pad_y
    draw.text((cap_x + 1, cap_y + 1), caption_text,
              fill=(*_LABEL_SHADOW_COLOR, 220), font=font)
    draw.text((cap_x, cap_y), caption_text,
              fill=(*_LABEL_TEXT_COLOR, 240), font=font)


def _draw_rect(
    draw: ImageDraw.ImageDraw,
    x: int, y: int, w: int, h: int,
    outline: tuple,
    width: int,
) -> None:
    draw.rectangle((x, y, x + w, y + h), outline=outline, width=width)


def _draw_dashed_rect(
    draw: ImageDraw.ImageDraw,
    x: int, y: int, w: int, h: int,
    outline: tuple,
    width: int,
    dash: int,
) -> None:
    """Dashed rectangle border — low-confidence element signal."""
    # Top + bottom
    for dx in range(0, w, dash * 2):
        draw.line([(x + dx, y), (min(x + dx + dash, x + w), y)],
                  fill=outline, width=width)
        draw.line([(x + dx, y + h), (min(x + dx + dash, x + w), y + h)],
                  fill=outline, width=width)
    # Left + right
    for dy in range(0, h, dash * 2):
        draw.line([(x, y + dy), (x, min(y + dy + dash, y + h))],
                  fill=outline, width=width)
        draw.line([(x + w, y + dy), (x + w, min(y + dy + dash, y + h))],
                  fill=outline, width=width)


def _apply_target_dim(
    image: Image.Image,
    elements: list[ElementBox],
    target_id: int,
) -> Image.Image:
    """Dim non-target regions 40% so the target box pops.

    Approach: fully dim overlay, then "cut out" the target rectangle
    with an alpha mask so it shows through at full brightness.
    """
    dim = Image.new("RGBA", image.size, _DIM_OVERLAY)
    mask = Image.new("L", image.size, 255)  # 255 = fully dim
    mask_draw = ImageDraw.Draw(mask)

    target = _element_by_id(elements, target_id)
    if target is not None:
        tx, ty, tw, th = target.box
        # Add 8px breathing room so the border isn't dimmed.
        pad = 8
        mask_draw.rectangle(
            (max(0, tx - pad), max(0, ty - pad),
             min(image.size[0], tx + tw + pad),
             min(image.size[1], ty + th + pad)),
            fill=0,   # 0 = no dim (transparent hole in the overlay)
        )

    dim.putalpha(mask.point(lambda v: int(v * _DIM_OVERLAY[3] / 255)))
    return Image.alpha_composite(image, dim)


def _element_by_id(elements: list[ElementBox], target_id: int) -> Optional[ElementBox]:
    for e in elements:
        if e.id == target_id:
            return e
    return None


def _image_to_png_bytes(image: Image.Image) -> bytes:
    import io
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


# ── Font loading ────────────────────────────────────────────────────────

_FONT_CACHE: dict[tuple[int, bool], ImageFont.ImageFont] = {}


def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Load DejaVu Sans (ships in fonts-dejavu, in the base ISO), fall
    back to Pillow's default bitmap font if it's missing."""
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",   # macOS dev
        "/Library/Fonts/Arial.ttf",              # macOS dev fallback
    ]
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size=size)
            _FONT_CACHE[key] = font
            return font
        except (OSError, IOError):
            continue
    # Absolute last resort — Pillow's built-in 8pt bitmap.
    font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


# ── Retention ───────────────────────────────────────────────────────────

def _prune_old_previews(preview_dir: Path) -> None:
    """Keep the ``_PREVIEW_RETENTION`` newest preview-*.png files."""
    try:
        files = sorted(
            preview_dir.glob("preview-*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for old in files[_PREVIEW_RETENTION:]:
        try:
            old.unlink()
        except OSError as exc:
            _log.debug("could not prune %s: %s", old, exc)


# ── Self-test ───────────────────────────────────────────────────────────

def _self_test() -> int:
    """``python -m gui_agent.annotate --self-test`` — render a synthetic
    2-element annotated PNG and print the path."""
    from gui_agent.vision import ElementBox as _EB
    img = Image.new("RGB", (800, 600), color=(50, 50, 60))
    elements = [
        _EB(id=0, box=(40, 40, 200, 40),
            caption="Sign in", kind="button", confidence=0.95),
        _EB(id=1, box=(40, 120, 300, 30),
            caption="Email address", kind="input", confidence=0.60),
    ]
    result = render_annotated(img, elements, target_id=0,
                              watermark_note="self-test")
    print(f"annotate OK: {result.path} ({result.width}x{result.height}, "
          f"{result.element_count} elements, target={result.target_id})",
          flush=True)
    return 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print("usage: python -m gui_agent.annotate --self-test",
          file=__import__("sys").stderr)
    sys.exit(2)
