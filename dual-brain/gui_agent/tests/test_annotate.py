"""Tests for gui_agent.annotate — annotated screenshot rendering."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from PIL import Image

from gui_agent.annotate import (
    _KIND_COLOR,
    _LOW_CONFIDENCE_THRESHOLD,
    _PREVIEW_RETENTION,
    AnnotateResult,
    _element_by_id,
    _prune_old_previews,
    render_annotated,
)
from gui_agent.vision import ElementBox


@pytest.fixture
def tmp_preview_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _img(w: int = 400, h: int = 300) -> Image.Image:
    return Image.new("RGB", (w, h), color=(30, 30, 40))


def _element(id_: int = 0, box=(20, 20, 80, 30), caption="Sign in",
             kind="button", confidence=0.95) -> ElementBox:
    return ElementBox(id=id_, box=box, caption=caption,
                      kind=kind, confidence=confidence)


# ═══ Happy path ═══════════════════════════════════════════════════════


def test_render_annotated_returns_result_and_writes_png(tmp_preview_dir):
    img = _img()
    elements = [_element(id_=0, kind="button")]
    result = render_annotated(img, elements, preview_dir=tmp_preview_dir)
    assert isinstance(result, AnnotateResult)
    assert result.path.is_file()
    assert result.path.suffix == ".png"
    assert result.width == 400
    assert result.height == 300
    assert result.element_count == 1
    assert result.target_id is None
    assert len(result.sha256) == 64


def test_output_file_has_0o600_perms(tmp_preview_dir):
    result = render_annotated(_img(), [_element()], preview_dir=tmp_preview_dir)
    mode = result.path.stat().st_mode & 0o777
    assert mode == 0o600


def test_preview_filename_encodes_sha_prefix(tmp_preview_dir):
    result = render_annotated(_img(), [_element()], preview_dir=tmp_preview_dir)
    assert result.path.name.startswith("preview-")
    # First 16 chars of the sha go in the filename.
    assert result.sha256[:16] in result.path.name


def test_result_output_is_valid_png(tmp_preview_dir):
    result = render_annotated(_img(), [_element()], preview_dir=tmp_preview_dir)
    reopened = Image.open(result.path)
    assert reopened.format == "PNG"
    assert reopened.size == (400, 300)


# ═══ Target highlight ═════════════════════════════════════════════════


def test_target_id_recorded_in_result(tmp_preview_dir):
    elements = [_element(id_=0), _element(id_=1, box=(200, 100, 60, 40),
                                          caption="Cancel", kind="button")]
    result = render_annotated(image=_img(), elements=elements,
                              target_id=1, preview_dir=tmp_preview_dir)
    assert result.target_id == 1


def test_missing_target_id_still_renders(tmp_preview_dir):
    """target_id that doesn't match any element → render succeeds
    without arrow/dim, target_id in result reflects what caller asked."""
    elements = [_element(id_=0)]
    result = render_annotated(_img(), elements, target_id=99,
                              preview_dir=tmp_preview_dir)
    assert result.target_id == 99
    assert result.path.is_file()


def test_element_by_id_helper():
    a = _element(id_=0)
    b = _element(id_=5)
    assert _element_by_id([a, b], 5) is b
    assert _element_by_id([a, b], 99) is None
    assert _element_by_id([], 0) is None


# ═══ Multiple elements + kinds ═══════════════════════════════════════


def test_many_elements_render_without_error(tmp_preview_dir):
    elements = [
        ElementBox(id=i, box=(10 + i * 15, 10 + i * 10, 40, 25),
                   caption=f"e{i}", kind=list(_KIND_COLOR.keys())[i % len(_KIND_COLOR)],
                   confidence=0.9)
        for i in range(20)
    ]
    result = render_annotated(_img(1600, 900), elements,
                              preview_dir=tmp_preview_dir)
    assert result.element_count == 20


def test_all_kinds_render(tmp_preview_dir):
    """Every kind in _KIND_COLOR should render without KeyError."""
    elements = [
        ElementBox(id=i, box=(10, 10 + i * 25, 50, 20),
                   caption=k, kind=k, confidence=0.9)
        for i, k in enumerate(_KIND_COLOR.keys())
    ]
    result = render_annotated(_img(400, 400), elements,
                              preview_dir=tmp_preview_dir)
    assert result.element_count == len(_KIND_COLOR)


def test_unknown_kind_uses_other_color(tmp_preview_dir):
    """Unknown kind → falls back to _KIND_COLOR['other'], no crash."""
    e = ElementBox(id=0, box=(10, 10, 50, 20),
                   caption="weird", kind="wingdings", confidence=0.9)
    result = render_annotated(_img(), [e], preview_dir=tmp_preview_dir)
    assert result.path.is_file()


# ═══ Confidence signal ═══════════════════════════════════════════════


def test_low_confidence_element_renders_dashed(tmp_preview_dir):
    """Low-confidence element (< threshold) uses dashed border. Renders
    without error — visual verification is a live test, but at minimum
    the code path must not raise."""
    e = _element(confidence=_LOW_CONFIDENCE_THRESHOLD - 0.1)
    result = render_annotated(_img(), [e], preview_dir=tmp_preview_dir)
    assert result.path.is_file()


def test_high_confidence_element_renders_solid(tmp_preview_dir):
    e = _element(confidence=0.99)
    result = render_annotated(_img(), [e], preview_dir=tmp_preview_dir)
    assert result.path.is_file()


# ═══ Edge cases ═══════════════════════════════════════════════════════


def test_element_at_top_edge_flips_label_inside(tmp_preview_dir):
    """Element at y=0 has no room for label above; label should go inside."""
    e = _element(box=(20, 0, 100, 40))
    result = render_annotated(_img(), [e], preview_dir=tmp_preview_dir)
    assert result.path.is_file()


def test_empty_element_list_still_writes_watermark(tmp_preview_dir):
    result = render_annotated(_img(), elements=[], preview_dir=tmp_preview_dir)
    assert result.element_count == 0
    assert result.path.is_file()


def test_watermark_note_included(tmp_preview_dir):
    result = render_annotated(_img(), [_element()],
                              watermark_note="slack",
                              preview_dir=tmp_preview_dir)
    assert result.path.is_file()


def test_caption_truncated_at_40_chars_no_crash(tmp_preview_dir):
    e = _element(caption="x" * 500)
    result = render_annotated(_img(), [e], preview_dir=tmp_preview_dir)
    assert result.path.is_file()


def test_element_larger_than_image_no_crash(tmp_preview_dir):
    """Off-canvas coords shouldn't crash — Pillow just clips."""
    e = _element(box=(300, 200, 5000, 3000))
    result = render_annotated(_img(400, 300), [e],
                              preview_dir=tmp_preview_dir)
    assert result.path.is_file()


def test_rgba_input_preserved(tmp_preview_dir):
    img = Image.new("RGBA", (200, 200), color=(50, 50, 50, 255))
    result = render_annotated(img, [_element()], preview_dir=tmp_preview_dir)
    assert result.path.is_file()


# ═══ Retention ═══════════════════════════════════════════════════════


def test_prune_keeps_only_retention_count(tmp_preview_dir):
    """After N+5 renders, only _PREVIEW_RETENTION files remain."""
    for i in range(_PREVIEW_RETENTION + 5):
        img = _img()
        # Different caption → different sha → distinct filename.
        e = _element(caption=f"e{i}")
        render_annotated(img, [e], preview_dir=tmp_preview_dir)
    surviving = sorted(tmp_preview_dir.glob("preview-*.png"))
    assert len(surviving) == _PREVIEW_RETENTION


def test_prune_missing_dir_does_not_raise(tmp_preview_dir):
    """Pruning a nonexistent dir → silent no-op (defensive)."""
    missing = tmp_preview_dir / "does-not-exist"
    # Should not raise.
    _prune_old_previews(missing)
