"""Phase 6 Scope C — preset_registry regression tests.

Cover:
  * Ship catalogue loads without errors + returns providers in the
    canonical order (gemini, anthropic, openai, local).
  * User overlay REPLACES a provider's presets (never concatenates)
    — the deep-merge trap the Scope C research flagged.
  * Malformed status is rejected loudly.
  * `preset_ids()` and `backend_order()` match the shape
    `models_page` expected pre-Scope-C.
"""

from __future__ import annotations

import pathlib
import tempfile
import textwrap

import pytest

from controller.preset_registry import (
    PresetRegistryError,
    backend_order,
    load_registry,
    preset_ids,
)


_MIN_CATALOGUE = textwrap.dedent("""\
    schema_version = "2"

    [[model]]
    id = "test-local"
    display_name = "Test Local"
    role = "qb"
    family = "test"
    file_name = "test.gguf"
    parameter_count_b = 0.5
    quantization = "Q4_K_M"
    size_bytes = 100
    license = "MIT"
    min_ram_mb = 100
    min_disk_mb = 100
    tags = []
    description = "test"

    [model.source]
    type = "url"
    url = "https://example.com/test.gguf"

    [[preset]]
    id = "gemini-2.5-flash"
    provider = "gemini"
    display_name = "Gemini 2.5 Flash"
    context_window = 1048576
    status = "recommended"

    [[preset]]
    id = "gemini-2.5-pro"
    provider = "gemini"
    display_name = "Gemini 2.5 Pro"
    context_window = 1048576
    status = "recommended"

    [[preset]]
    id = "claude-sonnet-4-6"
    provider = "anthropic"
    display_name = "Claude Sonnet 4.6"
    context_window = 200000
    status = "recommended"
""")


def _write(tmp_dir: pathlib.Path, name: str, content: str) -> pathlib.Path:
    p = tmp_dir / name
    p.write_text(content)
    return p


# ── Load path ────────────────────────────────────────────────────────────


def test_ship_catalogue_loads() -> None:
    """The catalogue.toml that ships must load without errors — this
    catches accidental TOML syntax breakage in CI before it hits users."""
    providers = load_registry()
    assert isinstance(providers, tuple)
    # Every catalogue-shipped preset has a non-empty id.
    for provider in providers:
        for preset in provider.presets:
            assert preset.id, f"empty id in {provider.key}"


def test_ship_catalogue_provider_order_is_canonical() -> None:
    """UI ordering: gemini before anthropic before openai before local.
    Changing this would silently reshuffle the Models dropdown."""
    providers = load_registry()
    keys = [p.key for p in providers]
    order = [k for k in ("gemini", "anthropic", "openai", "local") if k in keys]
    assert keys[: len(order)] == order


def test_load_registry_from_synthetic_catalogue(tmp_path) -> None:
    path = _write(tmp_path, "cat.toml", _MIN_CATALOGUE)
    providers = load_registry(system_path=path, user_path=tmp_path / "no-user")

    keys = [p.key for p in providers]
    assert keys == ["gemini", "anthropic"]
    assert [p.id for p in providers[0].presets] == [
        "gemini-2.5-flash", "gemini-2.5-pro",
    ]
    assert providers[1].presets[0].id == "claude-sonnet-4-6"


# ── Overlay merge — REPLACE semantics ────────────────────────────────────


def test_user_overlay_replaces_provider_presets(tmp_path) -> None:
    """The Scope C research flag: arrays REPLACE, never concatenate.
    An overlay that ships `provider = "gemini"` presets drops the
    catalogue's Gemini presets entirely rather than merging."""
    system = _write(tmp_path, "cat.toml", _MIN_CATALOGUE)
    overlay = _write(tmp_path, "presets.toml", textwrap.dedent("""\
        [[preset]]
        id = "gemini-3.0-flash-preview"
        provider = "gemini"
        display_name = "Gemini 3.0 Flash (preview)"
        context_window = 1048576
        status = "experimental"
    """))

    providers = load_registry(system_path=system, user_path=overlay)
    gemini = next(p for p in providers if p.key == "gemini")

    # Anthropic (unchanged in overlay) still present.
    anthropic = next(p for p in providers if p.key == "anthropic")
    assert anthropic.presets[0].id == "claude-sonnet-4-6"

    # Gemini: overlay REPLACED the catalogue's two presets with one.
    assert [p.id for p in gemini.presets] == ["gemini-3.0-flash-preview"]


def test_overlay_that_does_not_mention_provider_leaves_it_alone(tmp_path) -> None:
    """An overlay that only touches Anthropic must NOT affect Gemini's
    presets — the merge is per-provider."""
    system = _write(tmp_path, "cat.toml", _MIN_CATALOGUE)
    overlay = _write(tmp_path, "presets.toml", textwrap.dedent("""\
        [[preset]]
        id = "claude-experimental"
        provider = "anthropic"
        display_name = "Claude Experimental"
        context_window = 200000
        status = "experimental"
    """))

    providers = load_registry(system_path=system, user_path=overlay)
    gemini = next(p for p in providers if p.key == "gemini")

    # Gemini presets from catalogue survived.
    assert [p.id for p in gemini.presets] == [
        "gemini-2.5-flash", "gemini-2.5-pro",
    ]
    anthropic = next(p for p in providers if p.key == "anthropic")
    assert [p.id for p in anthropic.presets] == ["claude-experimental"]


# ── Validation ───────────────────────────────────────────────────────────


def test_invalid_status_rejected(tmp_path) -> None:
    """A typo like `status = "recommend"` must fail at load — otherwise
    it would silently fall through to no-badge in the GUI."""
    bad = _write(tmp_path, "cat.toml", textwrap.dedent("""\
        schema_version = "2"

        [[model]]
        id = "test-local"
        display_name = "Test"
        role = "qb"
        family = "test"
        file_name = "test.gguf"
        parameter_count_b = 0.5
        quantization = "Q4_K_M"
        size_bytes = 100
        license = "MIT"
        min_ram_mb = 100
        min_disk_mb = 100
        tags = []
        description = "test"

        [model.source]
        type = "url"
        url = "https://example.com/test.gguf"

        [[preset]]
        id = "gemini-broken"
        provider = "gemini"
        display_name = "Broken"
        context_window = 1000
        status = "recommend"
    """))
    with pytest.raises(PresetRegistryError) as exc_info:
        load_registry(system_path=bad, user_path=tmp_path / "none")
    assert "recommend" in str(exc_info.value)


def test_missing_required_field_rejected(tmp_path) -> None:
    """Missing `id` or `provider` fails loudly rather than producing
    a broken Preset instance."""
    bad = _write(tmp_path, "cat.toml", textwrap.dedent("""\
        schema_version = "2"

        [[model]]
        id = "test-local"
        display_name = "Test"
        role = "qb"
        family = "test"
        file_name = "test.gguf"
        parameter_count_b = 0.5
        quantization = "Q4_K_M"
        size_bytes = 100
        license = "MIT"
        min_ram_mb = 100
        min_disk_mb = 100
        tags = []
        description = "test"

        [model.source]
        type = "url"
        url = "https://example.com/test.gguf"

        [[preset]]
        display_name = "Missing ID and provider"
        context_window = 1000
        status = "recommended"
    """))
    with pytest.raises(PresetRegistryError) as exc_info:
        load_registry(system_path=bad, user_path=tmp_path / "none")
    assert "missing required field" in str(exc_info.value)


# ── Convenience shapes for models_page ───────────────────────────────────


def test_preset_ids_matches_models_page_shape(tmp_path) -> None:
    """Pre-Scope-C the page did `_MODEL_PRESETS["gemini"]` and got
    `list[str]`. Ensure the new registry returns the same shape."""
    path = _write(tmp_path, "cat.toml", _MIN_CATALOGUE)
    providers = load_registry(system_path=path, user_path=tmp_path / "none")
    assert preset_ids(providers, "gemini") == [
        "gemini-2.5-flash", "gemini-2.5-pro",
    ]
    assert preset_ids(providers, "openai") == []  # empty is fine


def test_backend_order_matches_models_page_shape(tmp_path) -> None:
    """Pre-Scope-C `_BACKENDS: list[tuple[str, str]]` — same shape."""
    path = _write(tmp_path, "cat.toml", _MIN_CATALOGUE)
    providers = load_registry(system_path=path, user_path=tmp_path / "none")
    order = backend_order(providers)
    assert isinstance(order, list)
    assert order[0] == ("gemini", "Gemini (cloud)")
    assert order[1] == ("anthropic", "Anthropic Claude (cloud)")
