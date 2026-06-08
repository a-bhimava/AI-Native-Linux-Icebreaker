"""Tests for ``controller.model_registry``.

14 tests, fully offline: catalogue parsing, lookup, role filter,
hardware filter, resolve search-path fallback, sha256 verification,
ModelNotInstalledError, ChecksumError, install with mocked HF download,
recommend on simulated 16 GB vs 4 GB.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

import pytest

from controller.model_registry import (
    ALLOW_LIST_LICENSES,
    CatalogueError,
    ChecksumError,
    HardwareProbe,
    ModelEntry,
    ModelNotInstalledError,
    ModelRegistry,
    ModelRegistryError,
    UnknownModelError,
    recommend_for_hardware,
)


# ─── Helpers ──────────────────────────────────────────────────────────────


def _write_catalogue(path: Path, schema_version: str = "1") -> None:
    """Write a 4-entry test catalogue: 2 QB, 1 PB, 1 draft."""
    path.write_text(
        f'''
schema_version = "{schema_version}"

[[model]]
id = "tiny-qb"
display_name = "Tiny QB"
role = "qb"
family = "test"
file_name = "tiny-qb.gguf"
parameter_count_b = 0.5
quantization = "Q4_K_M"
size_bytes = 1000
license = "Apache-2.0"
min_ram_mb = 500
min_disk_mb = 100
tags = ["fast", "low-ram"]
description = "tiny QB for tests"
[model.source]
type = "huggingface"
repo = "test/tiny-qb-gguf"
file = "tiny-qb.gguf"
[model.throughput_estimates]
m4 = 100

[[model]]
id = "balanced-qb"
display_name = "Balanced QB"
role = "qb"
family = "test"
file_name = "balanced-qb.gguf"
parameter_count_b = 1.5
quantization = "Q4_K_M"
size_bytes = 2000
license = "Apache-2.0"
min_ram_mb = 2000
min_disk_mb = 500
tags = ["default", "balanced"]
description = "balanced QB for tests"
[model.source]
type = "huggingface"
repo = "test/balanced-qb-gguf"
file = "balanced-qb.gguf"

[[model]]
id = "test-pb"
display_name = "Test PB"
role = "pb"
family = "test"
file_name = "test-pb.gguf"
parameter_count_b = 1.5
quantization = "Q4_K_M"
size_bytes = 2000
license = "Apache-2.0"
min_ram_mb = 2000
min_disk_mb = 500
tags = ["default"]
description = "PB for tests"
[model.source]
type = "huggingface"
repo = "test/test-pb-gguf"
file = "test-pb.gguf"

[[model]]
id = "test-draft"
display_name = "Test Draft"
role = "draft"
family = "test"
file_name = "test-draft.gguf"
parameter_count_b = 0.5
quantization = "Q4_K_M"
size_bytes = 500
license = "Apache-2.0"
min_ram_mb = 500
min_disk_mb = 100
tags = ["draft"]
description = "draft for tests"
[model.source]
type = "huggingface"
repo = "test/test-draft-gguf"
file = "test-draft.gguf"
'''
    )


def _create_fake_gguf(path: Path, content: bytes = b"FAKE-GGUF-DATA") -> str:
    """Write a tiny file and return its expected sha256."""
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _write_checksums(path: Path, entries: dict[str, str]) -> None:
    """Write sha256sum-format file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(f"{sha}  {name}" for name, sha in sorted(entries.items()))
    path.write_text(lines + "\n")


def _make_registry(
    tmp_path: Path,
    *,
    extra_search_dirs: list[Path] | None = None,
    with_checksums: dict[str, str] | None = None,
) -> ModelRegistry:
    catalogue = tmp_path / "catalogue.toml"
    _write_catalogue(catalogue)
    models_dir = tmp_path / "models"
    models_dir.mkdir(exist_ok=True)
    search_dirs = [models_dir] + (extra_search_dirs or [])
    checksums = tmp_path / "checksums.sha256"
    if with_checksums is not None:
        _write_checksums(checksums, with_checksums)
    return ModelRegistry(
        catalogue_path=catalogue,
        search_dirs=search_dirs,
        checksums_file=checksums,
    )


# ─── 1: catalogue loads and validates ──────────────────────────────────────


def test_catalogue_loads_and_validates(tmp_path):
    reg = _make_registry(tmp_path)
    assert len(reg.all_entries()) == 4
    ids = {e.id for e in reg.all_entries()}
    assert ids == {"tiny-qb", "balanced-qb", "test-pb", "test-draft"}


def test_catalogue_rejects_unknown_schema_version(tmp_path):
    catalogue = tmp_path / "catalogue.toml"
    _write_catalogue(catalogue, schema_version="999")
    with pytest.raises(CatalogueError, match="schema_version"):
        ModelRegistry(catalogue_path=catalogue, search_dirs=[tmp_path])


def test_catalogue_rejects_duplicate_ids(tmp_path):
    catalogue = tmp_path / "catalogue.toml"
    catalogue.write_text(
        '''
schema_version = "1"
[[model]]
id = "dup"
display_name = "X"
role = "qb"
file_name = "x.gguf"
parameter_count_b = 1.0
size_bytes = 100
license = "Apache-2.0"
min_ram_mb = 100
tags = []
description = "x"
[model.source]
type = "url"
url = "http://example.invalid/x.gguf"

[[model]]
id = "dup"
display_name = "Y"
role = "qb"
file_name = "y.gguf"
parameter_count_b = 1.0
size_bytes = 100
license = "Apache-2.0"
min_ram_mb = 100
tags = []
description = "y"
[model.source]
type = "url"
url = "http://example.invalid/y.gguf"
'''
    )
    with pytest.raises(CatalogueError, match="duplicate"):
        ModelRegistry(catalogue_path=catalogue, search_dirs=[tmp_path])


# ─── 2: get by id ──────────────────────────────────────────────────────────


def test_get_by_id_returns_entry(tmp_path):
    reg = _make_registry(tmp_path)
    entry = reg.get("balanced-qb")
    assert isinstance(entry, ModelEntry)
    assert entry.id == "balanced-qb"
    assert entry.role == "qb"
    assert entry.parameter_count_b == 1.5


# ─── 3: unknown id raises ──────────────────────────────────────────────────


def test_get_unknown_id_raises(tmp_path):
    reg = _make_registry(tmp_path)
    with pytest.raises(UnknownModelError, match="not in catalogue"):
        reg.get("nonexistent-model")


# ─── 4: list_by_role ───────────────────────────────────────────────────────


def test_list_by_role_filters_correctly(tmp_path):
    reg = _make_registry(tmp_path)
    qbs = reg.list_by_role("qb")
    assert {e.id for e in qbs} == {"tiny-qb", "balanced-qb"}
    assert all(e.role == "qb" for e in qbs)

    pbs = reg.list_by_role("pb")
    assert {e.id for e in pbs} == {"test-pb"}

    drafts = reg.list_by_role("draft")
    assert {e.id for e in drafts} == {"test-draft"}


def test_list_by_role_rejects_unknown_role(tmp_path):
    reg = _make_registry(tmp_path)
    with pytest.raises(UnknownModelError, match="unknown role"):
        reg.list_by_role("xb")


# ─── 5: filter_by_hardware ─────────────────────────────────────────────────


def test_filter_by_hardware_respects_min_ram(tmp_path):
    reg = _make_registry(tmp_path)
    # 4 GB RAM: only tiny-qb and test-draft fit (others need 2000 MB but
    # 4096 is enough actually — let me pick smaller). 800 MB.
    fits_800 = reg.filter_by_hardware(
        available_ram_mb=800, available_disk_mb=10_000
    )
    ids = {e.id for e in fits_800}
    assert "tiny-qb" in ids
    assert "test-draft" in ids
    assert "balanced-qb" not in ids
    assert "test-pb" not in ids

    fits_4096 = reg.filter_by_hardware(
        available_ram_mb=4096, available_disk_mb=10_000
    )
    ids = {e.id for e in fits_4096}
    assert ids == {"tiny-qb", "balanced-qb", "test-pb", "test-draft"}


def test_filter_by_hardware_respects_disk(tmp_path):
    reg = _make_registry(tmp_path)
    # 200 MB disk: only tiny-qb + test-draft (min_disk_mb = 100); others need 500
    fits = reg.filter_by_hardware(
        available_ram_mb=8000, available_disk_mb=200
    )
    ids = {e.id for e in fits}
    assert ids == {"tiny-qb", "test-draft"}


# ─── 6+7: resolve_to_file search-path fallback ─────────────────────────────


def test_resolve_to_file_finds_in_first_search_dir(tmp_path):
    target = tmp_path / "models" / "balanced-qb.gguf"
    target.parent.mkdir(exist_ok=True)
    sha = _create_fake_gguf(target)
    reg = _make_registry(tmp_path, with_checksums={"balanced-qb.gguf": sha})
    resolved = reg.resolve_to_file("balanced-qb")
    assert resolved == target


def test_resolve_to_file_falls_through_to_second_dir(tmp_path):
    second_dir = tmp_path / "second-pool"
    second_dir.mkdir()
    sha = _create_fake_gguf(second_dir / "tiny-qb.gguf")
    reg = _make_registry(
        tmp_path,
        extra_search_dirs=[second_dir],
        with_checksums={"tiny-qb.gguf": sha},
    )
    resolved = reg.resolve_to_file("tiny-qb")
    assert resolved == second_dir / "tiny-qb.gguf"


# ─── 8: sha256 verification ────────────────────────────────────────────────


def test_resolve_to_file_verifies_sha256(tmp_path):
    correct_content = b"correct-data"
    wrong_content = b"tampered-data"
    correct_sha = hashlib.sha256(correct_content).hexdigest()
    # Receipt says the correct hash; file on disk has wrong content.
    models_dir = tmp_path / "models"
    models_dir.mkdir(exist_ok=True)
    (models_dir / "tiny-qb.gguf").write_bytes(wrong_content)
    reg = _make_registry(
        tmp_path, with_checksums={"tiny-qb.gguf": correct_sha}
    )
    # Need to re-create the file because _make_registry's models/ may have been overwritten
    (tmp_path / "models" / "tiny-qb.gguf").write_bytes(wrong_content)
    with pytest.raises(ChecksumError, match="sha256 mismatch"):
        reg.resolve_to_file("tiny-qb")


# ─── 9: model not installed ────────────────────────────────────────────────


def test_resolve_to_file_missing_everywhere_raises(tmp_path):
    reg = _make_registry(tmp_path)
    with pytest.raises(ModelNotInstalledError, match="not installed"):
        reg.resolve_to_file("tiny-qb")


def test_resolve_to_file_no_receipt_raises_checksum_error(tmp_path):
    """File on disk but no sha256 receipt → ChecksumError (fail-closed)."""
    reg = _make_registry(tmp_path)
    _create_fake_gguf(tmp_path / "models" / "tiny-qb.gguf")
    with pytest.raises(ChecksumError, match="no sha256 receipt"):
        reg.resolve_to_file("tiny-qb")


# ─── 10: installed_ids ─────────────────────────────────────────────────────


def test_installed_ids_returns_only_present(tmp_path):
    reg = _make_registry(tmp_path)
    _create_fake_gguf(tmp_path / "models" / "tiny-qb.gguf")
    _create_fake_gguf(tmp_path / "models" / "test-pb.gguf")
    # Not yet recreating reg — installed_ids checks the filesystem each call.
    ids = reg.installed_ids()
    assert ids == {"tiny-qb", "test-pb"}


# ─── 11+12: recommend ──────────────────────────────────────────────────────


def test_recommend_picks_balanced_on_16gb_with_gpu(tmp_path):
    reg = _make_registry(tmp_path)
    probe = HardwareProbe(
        total_ram_mb=16384,
        free_disk_mb=50000,
        has_gpu=True,
        gpu_name="Apple M4",
        cpu_model="arm",
        cpu_cores=10,
        platform="Darwin",
    )
    recs = recommend_for_hardware(reg, probe)
    # 16 GB + GPU → prefer "default" tagged entry. balanced-qb has tag "default".
    assert recs["qb"].id == "balanced-qb"
    assert recs["pb"].id == "test-pb"
    assert recs["draft"].id == "test-draft"


def test_recommend_picks_fast_on_constrained_hardware(tmp_path):
    reg = _make_registry(tmp_path)
    probe = HardwareProbe(
        total_ram_mb=1024,        # 1 GB only
        free_disk_mb=500,
        has_gpu=False,
        gpu_name=None,
        cpu_model="x86_64",
        cpu_cores=4,
        platform="Linux",
    )
    recs = recommend_for_hardware(reg, probe)
    # Only tiny-qb and test-draft fit (min_ram_mb=500). balanced-qb (2000)
    # and test-pb (2000) are filtered out.
    # Constrained-hardware preference order picks "fast" / "low-ram" first.
    assert recs["qb"] is not None
    assert recs["qb"].id == "tiny-qb"  # tagged "fast"
    # PB has no viable option since it needs 2 GB.
    assert recs["pb"] is None


# ─── 13+14: install + download ─────────────────────────────────────────────


def test_install_writes_receipt_and_returns_path(tmp_path):
    reg = _make_registry(tmp_path)
    expected_content = b"downloaded-bytes"

    def fake_fetch(url: str, dest: Path) -> None:
        dest.write_bytes(expected_content)

    dest = reg.install(
        "tiny-qb", target_dir=tmp_path / "models", url_fetch=fake_fetch
    )
    assert dest.exists()
    assert dest.read_bytes() == expected_content
    expected_sha = hashlib.sha256(expected_content).hexdigest()
    # Receipt was written; verify() should pass
    assert reg.verify("tiny-qb")
    # resolve_to_file should now succeed end-to-end
    resolved = reg.resolve_to_file("tiny-qb")
    assert resolved == dest


def test_install_skips_download_if_file_present(tmp_path):
    reg = _make_registry(tmp_path)
    target = tmp_path / "models" / "tiny-qb.gguf"
    target.write_bytes(b"already-there")

    def fail_fetch(url: str, dest: Path) -> None:
        raise AssertionError("install should not download when file exists")

    reg.install("tiny-qb", target_dir=tmp_path / "models", url_fetch=fail_fetch)
    assert reg.verify("tiny-qb")


def test_install_rejects_non_allowlist_license_without_acceptance(tmp_path):
    """Gemma-style license requires --accept-license."""
    catalogue = tmp_path / "catalogue.toml"
    catalogue.write_text(
        '''
schema_version = "1"
[[model]]
id = "restricted"
display_name = "R"
role = "qb"
file_name = "r.gguf"
parameter_count_b = 1.0
size_bytes = 100
license = "Gemma-Terms-of-Use"
min_ram_mb = 100
tags = []
description = "r"
[model.source]
type = "url"
url = "http://example.invalid/r.gguf"
'''
    )
    reg = ModelRegistry(
        catalogue_path=catalogue,
        search_dirs=[tmp_path / "models"],
        checksums_file=tmp_path / "checksums.sha256",
    )
    with pytest.raises(ModelRegistryError, match="accept_license"):
        reg.install("restricted", url_fetch=lambda u, d: None)


def test_install_accepts_non_allowlist_license_when_flagged(tmp_path):
    catalogue = tmp_path / "catalogue.toml"
    catalogue.write_text(
        '''
schema_version = "1"
[[model]]
id = "restricted"
display_name = "R"
role = "qb"
file_name = "r.gguf"
parameter_count_b = 1.0
size_bytes = 100
license = "Gemma-Terms-of-Use"
min_ram_mb = 100
tags = []
description = "r"
[model.source]
type = "url"
url = "http://example.invalid/r.gguf"
'''
    )
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    reg = ModelRegistry(
        catalogue_path=catalogue,
        search_dirs=[models_dir],
        checksums_file=tmp_path / "checksums.sha256",
    )

    def fake_fetch(url: str, dest: Path) -> None:
        dest.write_bytes(b"contents")

    dest = reg.install(
        "restricted",
        target_dir=models_dir,
        accept_license=True,
        url_fetch=fake_fetch,
    )
    assert dest.exists()


# ─── License allowlist sanity ──────────────────────────────────────────────


def test_allow_list_licenses_includes_common_permissive():
    assert "Apache-2.0" in ALLOW_LIST_LICENSES
    assert "MIT" in ALLOW_LIST_LICENSES
    assert "Gemma-Terms-of-Use" not in ALLOW_LIST_LICENSES
