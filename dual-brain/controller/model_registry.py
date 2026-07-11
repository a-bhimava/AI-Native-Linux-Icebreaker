"""Model registry — catalogue resolver, checksum verifier, downloader.

The Controller never sees a GGUF filename in code. It only sees a
``model_id``. This module resolves IDs to on-disk files via the
catalogue (``catalogue.toml``) plus a list of search directories,
verifying SHA-256 against ``models/checksums.sha256`` before returning.

Three failure modes:

* ``UnknownModelError`` — model_id not in the catalogue
* ``ModelNotInstalledError`` — catalogue knows it, but no file on disk
* ``ChecksumError`` — file present, sha256 mismatches the receipt

Public surface:

* ``ModelEntry`` — frozen dataclass per catalogue entry
* ``ModelRegistry`` — the resolver
* CLI subcommands (``python3 -m controller.model_registry ...``):
  ``list`` / ``resolve`` / ``verify`` / ``install`` / ``recommend``

The catalogue file is the MENU (what's supported).
``models/checksums.sha256`` is the RECEIPT (what's been downloaded and
verified). They are separate by design — the catalogue ships with the
project; receipts are written at install time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)


# F-53 Scope A.P3: hardware probes are legitimately best-effort — a
# nvidia-smi call on a laptop without an Nvidia card *should* fail —
# but silently swallowing the exception meant the Errors page and Model
# recommender had no way to explain *why* they thought "no GPU". Every
# probe now records its exception here under a "<subsystem>/<platform>/
# <probe>" key, and callers can inspect via ``get_last_probe_errors()``.
_LAST_PROBE_ERRORS: dict[str, str] = {}


def _record_probe_error(key: str, exc: BaseException) -> None:
    """Store the last exception seen by a specific probe. Truncated to
    400 chars so a rogue subprocess stderr can't blow up the audit."""
    _LAST_PROBE_ERRORS[key] = f"{type(exc).__name__}: {exc}"[:400]
    log.debug("hw_probe.%s failed: %s: %s", key, type(exc).__name__, exc)


def get_last_probe_errors() -> dict[str, str]:
    """Return a snapshot of the last-seen probe exceptions. Errors page
    reads this to explain "why no GPU detected" style questions."""
    return dict(_LAST_PROBE_ERRORS)

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


# ─── Errors ──────────────────────────────────────────────────────────────────


class ModelRegistryError(Exception):
    """Base for all registry errors."""


class UnknownModelError(ModelRegistryError):
    """Catalogue has no entry for the given model_id."""


class ModelNotInstalledError(ModelRegistryError):
    """File listed in catalogue isn't present in any search directory."""


class ChecksumError(ModelRegistryError):
    """File present, but sha256 does not match the receipt."""


class CatalogueError(ModelRegistryError):
    """Catalogue file is malformed, missing required fields, or version-mismatched."""


# ─── Constants ──────────────────────────────────────────────────────────────


SUPPORTED_CATALOGUE_VERSIONS: tuple[str, ...] = ("1",)
ALLOW_LIST_LICENSES: frozenset[str] = frozenset(
    {"Apache-2.0", "MIT", "BSD-3-Clause", "BSD-2-Clause"}
)
VALID_ROLES: frozenset[str] = frozenset({"qb", "pb", "draft"})


# ─── Data model ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelSource:
    """How to fetch a model from upstream."""

    type: str  # "huggingface" | "url"
    repo: str | None = None
    file: str | None = None
    url: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> ModelSource:
        return cls(
            type=d["type"],
            repo=d.get("repo"),
            file=d.get("file"),
            url=d.get("url"),
        )


@dataclass(frozen=True)
class ModelEntry:
    """One row of the model catalogue."""

    id: str
    display_name: str
    role: str  # "qb" | "pb" | "draft"
    family: str
    file_name: str
    parameter_count_b: float
    quantization: str
    size_bytes: int
    license: str
    min_ram_mb: int
    min_disk_mb: int
    tags: tuple[str, ...]
    description: str
    source: ModelSource
    throughput_estimates: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> ModelEntry:
        required = {
            "id",
            "display_name",
            "role",
            "file_name",
            "parameter_count_b",
            "size_bytes",
            "license",
            "min_ram_mb",
            "tags",
            "description",
            "source",
        }
        missing = required - d.keys()
        if missing:
            raise CatalogueError(
                f"catalogue entry {d.get('id')!r} missing fields: {sorted(missing)}"
            )
        if d["role"] not in VALID_ROLES:
            raise CatalogueError(
                f"catalogue entry {d['id']!r} has invalid role "
                f"{d['role']!r}; expected one of {sorted(VALID_ROLES)}"
            )
        return cls(
            id=d["id"],
            display_name=d["display_name"],
            role=d["role"],
            family=d.get("family", ""),
            file_name=d["file_name"],
            parameter_count_b=float(d["parameter_count_b"]),
            quantization=d.get("quantization", "Q4_K_M"),
            size_bytes=int(d["size_bytes"]),
            license=d["license"],
            min_ram_mb=int(d["min_ram_mb"]),
            min_disk_mb=int(d.get("min_disk_mb", 0)),
            tags=tuple(d["tags"]),
            description=d["description"],
            source=ModelSource.from_dict(d["source"]),
            throughput_estimates=dict(d.get("throughput_estimates", {})),
        )


# ─── Registry ───────────────────────────────────────────────────────────────


class ModelRegistry:
    """Resolves model_id -> on-disk file path, with checksum verification."""

    def __init__(
        self,
        catalogue_path: Path,
        search_dirs: list[Path],
        checksums_file: Path | None = None,
    ) -> None:
        self._catalogue_path = catalogue_path
        self._search_dirs = [Path(d).expanduser() for d in search_dirs]
        self._checksums_file = (
            Path(checksums_file).expanduser() if checksums_file else None
        )
        self._entries: dict[str, ModelEntry] = {}
        self._load_catalogue()

    def _load_catalogue(self) -> None:
        if not self._catalogue_path.exists():
            raise CatalogueError(
                f"catalogue not found: {self._catalogue_path}"
            )
        with self._catalogue_path.open("rb") as handle:
            raw = tomllib.load(handle)
        version = str(raw.get("schema_version", ""))
        if version not in SUPPORTED_CATALOGUE_VERSIONS:
            raise CatalogueError(
                f"catalogue schema_version {version!r} not in supported "
                f"{SUPPORTED_CATALOGUE_VERSIONS}"
            )
        if "model" not in raw or not isinstance(raw["model"], list):
            raise CatalogueError(
                "catalogue must contain at least one [[model]] entry"
            )
        for raw_entry in raw["model"]:
            entry = ModelEntry.from_dict(raw_entry)
            if entry.id in self._entries:
                raise CatalogueError(
                    f"duplicate model_id {entry.id!r} in catalogue"
                )
            self._entries[entry.id] = entry

    # ── Lookup ──────────────────────────────────────────────────────────

    def get(self, model_id: str) -> ModelEntry:
        try:
            return self._entries[model_id]
        except KeyError:
            raise UnknownModelError(
                f"model_id {model_id!r} not in catalogue; known: "
                f"{sorted(self._entries.keys())}"
            ) from None

    def list_by_role(self, role: str) -> list[ModelEntry]:
        if role not in VALID_ROLES:
            raise UnknownModelError(
                f"unknown role {role!r}; expected one of {sorted(VALID_ROLES)}"
            )
        return [e for e in self._entries.values() if e.role == role]

    def all_entries(self) -> list[ModelEntry]:
        return list(self._entries.values())

    def filter_by_hardware(
        self, available_ram_mb: int, available_disk_mb: int
    ) -> list[ModelEntry]:
        return [
            e
            for e in self._entries.values()
            if e.min_ram_mb <= available_ram_mb
            and e.min_disk_mb <= available_disk_mb
        ]

    # ── Resolution + verification ──────────────────────────────────────

    def find_on_disk(self, model_id: str) -> Path | None:
        """Search the configured dirs for the entry's file_name.
        Returns the first match or None. Does NOT verify checksum."""
        entry = self.get(model_id)
        for directory in self._search_dirs:
            candidate = directory / entry.file_name
            if candidate.is_file():
                return candidate
        return None

    def installed_ids(self) -> set[str]:
        return {
            entry.id
            for entry in self._entries.values()
            if self.find_on_disk(entry.id) is not None
        }

    def _read_checksums(self) -> dict[str, str]:
        """Parse sha256sum-format file. Returns {file_name: sha256_hex}.
        Missing file returns empty mapping (no receipts yet)."""
        if self._checksums_file is None or not self._checksums_file.exists():
            return {}
        result: dict[str, str] = {}
        with self._checksums_file.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(maxsplit=1)
                if len(parts) != 2:
                    continue
                sha_hex, path_part = parts
                file_name = Path(path_part.lstrip("*")).name
                result[file_name] = sha_hex.lower()
        return result

    def verify(self, model_id: str) -> bool:
        """Compute sha256 of the on-disk file and compare to receipt.
        Returns True iff the receipt exists and matches."""
        entry = self.get(model_id)
        path = self.find_on_disk(model_id)
        if path is None:
            raise ModelNotInstalledError(
                f"model {model_id!r} not installed; expected file "
                f"{entry.file_name!r} in any of {self._search_dirs}"
            )
        receipts = self._read_checksums()
        expected = receipts.get(entry.file_name)
        if expected is None:
            return False  # no receipt — install hasn't recorded a sha
        actual = _sha256_of(path)
        return actual == expected

    def resolve_to_file(self, model_id: str) -> Path:
        """The Controller's primary entry point.
        Returns a verified on-disk path, or raises.
        Fail-closed: never returns an unverified file."""
        entry = self.get(model_id)
        path = self.find_on_disk(model_id)
        if path is None:
            raise ModelNotInstalledError(
                f"model {model_id!r} not installed. Run: "
                f"python3 -m controller.model_registry install --id {model_id}"
            )
        receipts = self._read_checksums()
        expected = receipts.get(entry.file_name)
        if expected is None:
            raise ChecksumError(
                f"no sha256 receipt for {entry.file_name!r}. Run: "
                f"python3 -m controller.model_registry install --id {model_id} "
                f"(this will compute + record the checksum)"
            )
        actual = _sha256_of(path)
        if actual != expected:
            raise ChecksumError(
                f"sha256 mismatch for {entry.file_name!r}: "
                f"expected {expected[:16]}..., got {actual[:16]}..."
            )
        return path

    # ── Install / receipt management ───────────────────────────────────

    def install(
        self,
        model_id: str,
        target_dir: Path | None = None,
        *,
        accept_license: bool = False,
        url_fetch: callable | None = None,
    ) -> Path:
        """Download (if absent) and verify a model. Writes a sha256 receipt.

        ``url_fetch`` is injectable for tests. In production it defaults to
        a urllib-based streaming download; tests pass a fake.
        """
        entry = self.get(model_id)
        if entry.license not in ALLOW_LIST_LICENSES and not accept_license:
            raise ModelRegistryError(
                f"{model_id!r} is licensed under {entry.license!r}; "
                f"pass accept_license=True (CLI: --accept-license) to install"
            )

        if target_dir is None:
            target_dir = self._search_dirs[0]
        target_dir = Path(target_dir).expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)

        dest = target_dir / entry.file_name
        if not dest.exists():
            url = _resolve_source_url(entry.source)
            fetch = url_fetch if url_fetch is not None else _download_streaming
            fetch(url, dest)

        # Compute sha256 and record receipt.
        sha_hex = _sha256_of(dest)
        self._write_receipt(entry.file_name, sha_hex)
        return dest

    def _write_receipt(self, file_name: str, sha_hex: str) -> None:
        if self._checksums_file is None:
            return
        self._checksums_file.parent.mkdir(parents=True, exist_ok=True)
        existing = self._read_checksums()
        existing[file_name] = sha_hex
        with self._checksums_file.open("w", encoding="utf-8") as handle:
            for name in sorted(existing):
                handle.write(f"{existing[name]}  {name}\n")


# ─── Helpers ────────────────────────────────────────────────────────────────


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_source_url(source: ModelSource) -> str:
    if source.type == "huggingface":
        if not source.repo or not source.file:
            raise ModelRegistryError("huggingface source needs repo + file")
        return f"https://huggingface.co/{source.repo}/resolve/main/{source.file}"
    if source.type == "url":
        if not source.url:
            raise ModelRegistryError("url source needs url field")
        return source.url
    raise ModelRegistryError(f"unknown source type {source.type!r}")


def _download_streaming(url: str, dest: Path) -> None:
    """urllib-based streaming download with a tmp file + atomic rename."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as response, tmp.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 1024)
    tmp.replace(dest)


# ─── Hardware probe ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HardwareProbe:
    total_ram_mb: int
    free_disk_mb: int
    has_gpu: bool
    gpu_name: str | None
    cpu_model: str
    cpu_cores: int
    platform: str


# Phase 6 Scope B: default probe timeout for callers that don't pass one
# through (unit tests, legacy call sites). Real users get the config
# value via probe_hardware(probe_timeout_seconds=cfg.run.model_probe_timeout_seconds).
_DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0


def probe_hardware(
    *,
    probe_timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> HardwareProbe:
    """Cross-platform, best-effort hardware introspection.

    ``probe_timeout_seconds`` bounds each sysctl / nvidia-smi / rocm-smi
    subprocess. Raised via ``cfg.run.model_probe_timeout_seconds`` (Phase
    6 Scope B) when a caller has cfg in scope.
    """
    # RAM
    total_ram_mb = _probe_ram_mb(probe_timeout_seconds)
    # Disk in $HOME
    home = Path.home()
    free_disk_mb = shutil.disk_usage(home).free // (1024 * 1024)
    # GPU
    has_gpu, gpu_name = _probe_gpu(probe_timeout_seconds)
    # CPU
    cpu_model = platform.processor() or platform.machine()
    cpu_cores = os.cpu_count() or 1
    return HardwareProbe(
        total_ram_mb=total_ram_mb,
        free_disk_mb=free_disk_mb,
        has_gpu=has_gpu,
        gpu_name=gpu_name,
        cpu_model=cpu_model,
        cpu_cores=cpu_cores,
        platform=platform.system(),
    )


def _probe_ram_mb(
    timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> int:
    sysname = platform.system()
    if sysname == "Darwin":
        try:
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True,
                timeout=timeout_seconds,
            )
            return int(out.strip()) // (1024 * 1024)
        except Exception as exc:
            _record_probe_error("ram/darwin/sysctl", exc)
    if sysname == "Linux":
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb // 1024
        except Exception as exc:
            _record_probe_error("ram/linux/meminfo", exc)
    # Fallback: assume 8 GB so we don't crash the recommender
    return 8192


def _probe_gpu(
    timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> tuple[bool, str | None]:
    sysname = platform.system()
    if sysname == "Darwin":
        # Apple Silicon has unified memory; treat as GPU-equivalent for inference
        try:
            out = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                text=True,
                timeout=timeout_seconds,
            )
            if "Apple" in out:
                return True, out.strip()
        except Exception as exc:
            _record_probe_error("gpu/darwin/sysctl", exc)
        return False, None
    if sysname == "Linux":
        # Try nvidia-smi first
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                text=True,
                timeout=timeout_seconds,
                stderr=subprocess.DEVNULL,
            )
            name = out.strip().splitlines()[0] if out.strip() else None
            if name:
                return True, name
        except Exception as exc:
            _record_probe_error("gpu/linux/nvidia-smi", exc)
        # rocm-smi for AMD
        try:
            out = subprocess.check_output(
                ["rocm-smi", "--showproductname"],
                text=True,
                timeout=timeout_seconds,
                stderr=subprocess.DEVNULL,
            )
            if "GPU" in out:
                return True, "AMD GPU (rocm)"
        except Exception as exc:
            _record_probe_error("gpu/linux/rocm-smi", exc)
    return False, None


def recommend_for_hardware(
    registry: ModelRegistry, probe: HardwareProbe
) -> dict[str, ModelEntry | None]:
    """Pick a default model per role given hardware constraints."""
    viable = registry.filter_by_hardware(probe.total_ram_mb, probe.free_disk_mb)

    def pick(role: str, prefer_tags: tuple[str, ...]) -> ModelEntry | None:
        candidates = [e for e in viable if e.role == role]
        if not candidates:
            return None
        for tag in prefer_tags:
            for entry in candidates:
                if tag in entry.tags:
                    return entry
        # Fall through: pick smallest viable
        return min(candidates, key=lambda e: e.parameter_count_b)

    # Prefer "default" tag, then "balanced" if GPU/strong CPU, then "fast"
    if probe.has_gpu or probe.total_ram_mb >= 8192:
        preferred = ("default", "balanced", "fast")
    else:
        preferred = ("fast", "low-ram", "default", "balanced")

    return {
        "qb": pick("qb", preferred),
        "pb": pick("pb", ("default",)),
        "draft": pick("draft", ("default", "draft")),
    }


# ─── CLI ────────────────────────────────────────────────────────────────────


_DEFAULT_CATALOGUE = Path(__file__).parent / "catalogue.toml"
_DEFAULT_SEARCH_DIRS = [
    Path(__file__).parent.parent.parent / "models",
    Path.home() / ".local" / "share" / "icebreaker" / "models",
    Path("/var/lib/icebreaker/models"),
]
_DEFAULT_CHECKSUMS = Path(
    os.environ.get("ICEBREAKER_CHECKSUMS")
    or (
        "/var/lib/icebreaker/models/checksums.sha256"
        if Path("/var/lib/icebreaker/models/checksums.sha256").exists()
        else Path(__file__).parent.parent.parent / "models" / "checksums.sha256"
    )
)


def _make_default_registry() -> ModelRegistry:
    return ModelRegistry(
        catalogue_path=_DEFAULT_CATALOGUE,
        search_dirs=_DEFAULT_SEARCH_DIRS,
        checksums_file=_DEFAULT_CHECKSUMS,
    )


def _cmd_list(args: argparse.Namespace) -> int:
    reg = _make_default_registry()
    entries = (
        reg.list_by_role(args.role) if args.role else reg.all_entries()
    )
    if args.installed_only:
        installed = reg.installed_ids()
        entries = [e for e in entries if e.id in installed]
    print(f"{'ID':<45} {'ROLE':<6} {'SIZE':>10} {'INSTALLED':<10} LICENSE")
    installed = reg.installed_ids()
    for entry in entries:
        size_mb = entry.size_bytes // (1024 * 1024)
        mark = "yes" if entry.id in installed else "no"
        print(
            f"{entry.id:<45} {entry.role:<6} {size_mb:>8} MB {mark:<10} {entry.license}"
        )
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    reg = _make_default_registry()
    try:
        path = reg.resolve_to_file(args.id)
    except ModelRegistryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    reg = _make_default_registry()
    try:
        ok = reg.verify(args.id)
    except ModelRegistryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if ok:
        print(f"{args.id}: sha256 ok")
        return 0
    print(f"{args.id}: NO RECEIPT (run install to record sha256)", file=sys.stderr)
    return 2


def _cmd_install(args: argparse.Namespace) -> int:
    reg = _make_default_registry()
    target = Path(args.dir) if args.dir else None
    try:
        dest = reg.install(
            args.id, target_dir=target, accept_license=args.accept_license
        )
    except ModelRegistryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"installed: {dest}")
    print(f"sha256 receipt: {reg.verify(args.id)}")
    return 0


def _cmd_recommend(args: argparse.Namespace) -> int:
    reg = _make_default_registry()
    probe = probe_hardware()
    print("Hardware probe:")
    print(f"  Platform: {probe.platform}")
    print(f"  CPU:      {probe.cpu_model} ({probe.cpu_cores} cores)")
    print(f"  RAM:      {probe.total_ram_mb} MB")
    print(f"  Disk:     {probe.free_disk_mb} MB free in $HOME")
    if probe.has_gpu:
        print(f"  GPU:      {probe.gpu_name}")
    else:
        print(f"  GPU:      none detected")
    print()
    recommendations = recommend_for_hardware(reg, probe)
    print("Recommended for this hardware:")
    for role in ("qb", "pb", "draft"):
        entry = recommendations.get(role)
        if entry:
            size_mb = entry.size_bytes // (1024 * 1024)
            print(f"  {role:<6} -> {entry.id} ({size_mb} MB, {entry.license})")
        else:
            print(f"  {role:<6} -> (no viable model on this hardware)")
    print()
    print("To install, run:")
    for role in ("qb", "pb", "draft"):
        entry = recommendations.get(role)
        if entry and entry.id not in reg.installed_ids():
            print(f"  python3 -m controller.model_registry install --id {entry.id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="model_registry",
        description="Icebreaker model registry — catalogue lookup, install, verify, recommend.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list catalogue entries")
    p_list.add_argument("--role", choices=sorted(VALID_ROLES))
    p_list.add_argument("--installed-only", action="store_true")
    p_list.set_defaults(func=_cmd_list)

    p_resolve = sub.add_parser("resolve", help="print on-disk path for a model_id")
    p_resolve.add_argument("--id", required=True)
    p_resolve.set_defaults(func=_cmd_resolve)

    p_verify = sub.add_parser("verify", help="verify sha256 of an installed model")
    p_verify.add_argument("--id", required=True)
    p_verify.set_defaults(func=_cmd_verify)

    p_install = sub.add_parser("install", help="download (if absent) + verify a model")
    p_install.add_argument("--id", required=True)
    p_install.add_argument("--dir", help="install into this dir (default: first search dir)")
    p_install.add_argument("--accept-license", action="store_true")
    p_install.set_defaults(func=_cmd_install)

    p_rec = sub.add_parser("recommend", help="hardware probe + suggested models")
    p_rec.set_defaults(func=_cmd_recommend)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
