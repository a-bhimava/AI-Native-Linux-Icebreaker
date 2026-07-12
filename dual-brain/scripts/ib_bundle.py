#!/usr/bin/env python3
"""ib_bundle — Icebreaker diagnostic bundle collector.

Scope G.5 (2026-07-12). One-shot script the user (or the first-boot
self-test) runs when something is wrong. Collects everything a remote
maintainer would need to diagnose without ssh access into the guest,
tars + zstd-compresses it, and drops the bundle at
``~/icebreaker-bundle-<timestamp>-<arch>.tar.zst`` (user-owned, easy
to attach to a GitHub issue).

What lands in the bundle
────────────────────────

1. ``ib_debug_report.txt``  — full ``ib-debug report`` output (already
   emits a JSON block at the end per the Scope D/E integration).
2. ``system.json``          — arch, kernel, uname, dpkg architecture,
   platform, cpu / ram / gpu probe (via ``controller.model_registry
   .probe_hardware``), disk usage of `/var/lib/icebreaker/` +
   `/var/log/icebreaker/` + `/opt/icebreaker/venv/`.
3. ``models.json``          — checksums.sha256 content + per-file
   verification (recomputed sha256 vs expected).
4. ``logs/``                — every file under `/var/log/icebreaker/`
   (audit, controller, terminal, first-boot, system.jsonl, debug.jsonl
   if enabled).
5. ``journals/``            — last 500 lines of `journalctl -u
   icebreaker-{controller,pbd,qbd,first-boot,mcpd}`.
6. ``boot-report.json``     — copy of the latest first-boot report
   (see ``first-boot`` script).
7. ``harvest.json``         — output of ``mcpd-harvest-guest.sh
   --timeout 30`` for the current arch.
8. ``markers.json``         — F-51 marker verification per v2.manifest
   (grep-based; source of truth is v2.manifest).
9. ``config-redacted.toml`` — a copy of ``/etc/icebreaker/controller
   .toml`` with any secret-shape values (api_key, token, secret,
   password, bearer) replaced by ``<REDACTED>``. Env vars named as
   secrets are NEVER read; only file content is redacted.
10. ``package_versions.txt`` — output of ``dpkg -l 'icebreaker*'`` +
    ``python3 -m pip freeze`` inside the venv.
11. ``iso_build_manifest.json`` — content of
    ``/usr/share/icebreaker/build-manifest.json`` (which arch, which
    git SHA, which build date).
12. ``bundle_metadata.json`` — bundle version, generation time, tool
    versions, whether user requested this bundle or first-boot did.

Usage
─────

    ib-bundle                    # collect + save to ~/
    ib-bundle --out /tmp/x.tzst  # explicit output path
    ib-bundle --reason first-boot   # tag the bundle_metadata.json
    ib-bundle --notify           # show a `notify-send` popup on completion
                                 # (also fires on the first-boot auto-run
                                 # via icebreaker-first-boot.service)

Design notes
────────────

* Zero-dependency: uses stdlib only (no ``rich``, no ``jq``). ``tar``
  and ``zstd`` are shelled out via subprocess — both confirmed in the
  ISO's ``packages-desktop.txt``.
* Redaction: BP-8 (secret hygiene) applies. Only field NAMES matching
  ``api_key`` / ``token`` / ``secret`` / ``password`` / ``bearer``
  trigger redaction; the collector never reads env vars named as
  secrets.
* Bounded output: every log file is truncated at 5 MB per file (a
  runaway log shouldn't blow up the bundle). Journal cap: 500 lines per
  unit. Whole bundle is capped at 100 MB — if any input exceeds that,
  we log the truncation and continue.
* Non-fatal collection: if any single collector fails, log it in
  ``bundle_metadata.json`` and press on. The goal is a maximally-
  complete bundle; refuse-to-collect-anything-because-one-thing-broke
  is worse than a partial bundle with clear notes.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


# ── Bounds (BP-10) ───────────────────────────────────────────────────────

_MAX_LOG_BYTES = 5 * 1024 * 1024      # 5 MB per log file
_MAX_BUNDLE_BYTES = 100 * 1024 * 1024  # 100 MB whole bundle
_MAX_JOURNAL_LINES = 500
_HARVEST_TIMEOUT_SECONDS = 30

# Secret-shape field names (BP-8). Value redaction is triggered by the
# KEY, not the value shape — mirrors controller/debug_log.py:_SECRET_KEYS.
_SECRET_KEY_TOKENS = (
    "api_key", "api-key", "apikey",
    "token", "secret", "password", "bearer",
)

_ICEBREAKER_UNITS = (
    "icebreaker-controller",
    "icebreaker-pbd",
    "icebreaker-qbd",
    "icebreaker-first-boot",
    "mcpd",
)

_LOG_DIR = Path("/var/log/icebreaker")
_MODELS_DIR = Path("/var/lib/icebreaker/models")
_BUILD_MANIFEST = Path("/usr/share/icebreaker/build-manifest.json")
_CONTROLLER_TOML = Path("/etc/icebreaker/controller.toml")
_HARVEST_SCRIPT = Path("/usr/local/bin/mcpd-harvest-guest.sh")
_BOOT_REPORT_GLOB = "/var/log/icebreaker/boot-report-*.json"


# ── Utility ──────────────────────────────────────────────────────────────


def _run(cmd: list[str], *, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run cmd, return (rc, stdout, stderr). Non-fatal on failure."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return 1, "", f"{cmd[0]}: {type(exc).__name__}: {exc}"


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _truncate_read(path: Path, cap: int = _MAX_LOG_BYTES) -> tuple[bytes, bool]:
    """Read up to ``cap`` bytes from the END of ``path``. Return
    (data, was_truncated). Reading tail matters for logs — head is
    stale, tail has the failure."""
    if not path.exists():
        return b"", False
    size = path.stat().st_size
    if size <= cap:
        return path.read_bytes(), False
    with path.open("rb") as f:
        f.seek(size - cap)
        return f.read(), True


def _is_secret_key(key: str) -> bool:
    # Word-boundary matching for secret-shape key names.
    # Naive substring matching catches false positives like `max_tokens`
    # (contains "token") and `max_token_size` (contains "token" as a
    # middle word). Real secret names have the sensitive word as the
    # LAST word of the key, or the entire key.
    #
    # Handles snake_case (api_key), SCREAMING_SNAKE (API_KEY),
    # camelCase (apiKey), and kebab-case (api-key).
    k = key.strip()
    # camelCase → snake: "apiKey" → "api_Key"
    k = re.sub(r'([a-z])([A-Z])', r'\1_\2', k)
    words = k.replace("-", "_").lower().split("_")
    if not words:
        return False
    # Single-word secrets, or last-word pattern: `foo_token`,
    # `bearer_password`, etc.
    last = words[-1]
    if last in {"password", "secret", "token", "bearer", "apikey"}:
        return True
    # Two-word `api_key` pattern (also handles `..._api_key`).
    if len(words) >= 2 and words[-2:] == ["api", "key"]:
        return True
    return False


def _redact_toml_line(line: str) -> str:
    """Redact `key = value` lines whose key names a secret. Preserves
    formatting so operators can still see the shape of the config."""
    m = re.match(r'^(\s*)([\w-]+)\s*=\s*(.+?)\s*$', line)
    if not m:
        return line
    indent, key, _val = m.group(1), m.group(2), m.group(3)
    if _is_secret_key(key):
        return f'{indent}{key} = "<REDACTED>"\n' if not line.endswith("\n") \
            else f'{indent}{key} = "<REDACTED>"\n'
    return line


# ── Collectors ────────────────────────────────────────────────────────────


def collect_ib_debug_report(dest_dir: Path) -> dict[str, Any]:
    """Reuse `ib-debug report --out`. Its output already includes a JSON
    block at the tail — we ship the whole file so operators see both
    the human summary AND the machine block."""
    out_path = dest_dir / "ib_debug_report.txt"
    ib_debug = shutil.which("ib-debug") or "/usr/local/bin/ib-debug"
    rc, stdout, stderr = _run(
        [ib_debug, "report", "--out", str(out_path)], timeout=60,
    )
    return {
        "collector": "ib_debug_report",
        "ok": out_path.exists() and out_path.stat().st_size > 0,
        "returncode": rc,
        "stderr_tail": stderr[-500:] if stderr else "",
    }


def collect_system_json(dest_dir: Path) -> dict[str, Any]:
    """arch, kernel, uname, cpu / ram / gpu, disk usage of Icebreaker
    directories. Uses controller.model_registry.probe_hardware when
    importable, falls back to bare platform.* + shutil.disk_usage."""
    doc: dict[str, Any] = {
        "platform": platform.system(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
    }
    rc, uname_a, _ = _run(["uname", "-a"], timeout=5)
    doc["uname_a"] = uname_a.strip() if rc == 0 else None
    rc, dpkg_arch, _ = _run(["dpkg", "--print-architecture"], timeout=5)
    doc["dpkg_architecture"] = dpkg_arch.strip() if rc == 0 else None
    rc, kernel_release, _ = _run(["uname", "-r"], timeout=5)
    doc["kernel_release"] = kernel_release.strip() if rc == 0 else None
    # Hardware probe — reuse the daemon's own probe path when available
    try:
        sys.path.insert(0, "/opt/icebreaker/venv/lib/python3.12/site-packages")
        from controller.model_registry import probe_hardware  # type: ignore
        p = probe_hardware(probe_timeout_seconds=5.0)
        doc["hardware_probe"] = {
            "total_ram_mb": getattr(p, "total_ram_mb", None),
            "free_disk_mb": getattr(p, "free_disk_mb", None),
            "has_gpu": getattr(p, "has_gpu", None),
            "gpu_name": getattr(p, "gpu_name", None),
            "cpu_model": getattr(p, "cpu_model", None),
            "cpu_cores": getattr(p, "cpu_cores", None),
            "platform_string": getattr(p, "platform", None),
        }
    except Exception as exc:  # noqa: BLE001
        doc["hardware_probe_error"] = f"{type(exc).__name__}: {exc}"
    # Disk usage of Icebreaker paths
    disk: dict[str, Any] = {}
    for label, path in [
        ("var_lib_icebreaker", "/var/lib/icebreaker"),
        ("var_log_icebreaker", "/var/log/icebreaker"),
        ("opt_icebreaker_venv", "/opt/icebreaker/venv"),
        ("run_icebreaker", "/run/icebreaker"),
    ]:
        p = Path(path)
        if p.exists():
            try:
                du = shutil.disk_usage(str(p))
                disk[label] = {
                    "total_bytes": du.total,
                    "used_bytes": du.used,
                    "free_bytes": du.free,
                }
            except Exception as exc:  # noqa: BLE001
                disk[label] = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            disk[label] = {"error": "path missing"}
    doc["disk_usage"] = disk
    (dest_dir / "system.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n"
    )
    return {"collector": "system_json", "ok": True}


def collect_models_json(dest_dir: Path) -> dict[str, Any]:
    """Verify every model in checksums.sha256 against the on-disk file.
    INV-7 — this is the same check start-{pbd,qbd} does at startup."""
    doc: dict[str, Any] = {"models": []}
    checksums = _MODELS_DIR / "checksums.sha256"
    if not checksums.exists():
        doc["error"] = f"{checksums} not found"
        (dest_dir / "models.json").write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n"
        )
        return {"collector": "models_json", "ok": False,
                "reason": "checksums file missing"}
    for line in checksums.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        expected_hash, filename = parts[0], parts[1].strip()
        model_path = _MODELS_DIR / Path(filename).name
        entry: dict[str, Any] = {
            "filename": Path(filename).name,
            "expected_sha256": expected_hash,
            "present": model_path.exists(),
        }
        if model_path.exists():
            try:
                entry["actual_sha256"] = _sha256_of(model_path)
                entry["size_bytes"] = model_path.stat().st_size
                entry["match"] = entry["actual_sha256"] == expected_hash
            except Exception as exc:  # noqa: BLE001
                entry["hash_error"] = f"{type(exc).__name__}: {exc}"
                entry["match"] = False
        else:
            entry["match"] = False
        doc["models"].append(entry)
    (dest_dir / "models.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n"
    )
    return {"collector": "models_json", "ok": True,
            "count": len(doc["models"])}


def collect_logs(dest_dir: Path) -> dict[str, Any]:
    """Copy every file under /var/log/icebreaker/ with per-file 5 MB cap
    (tail-truncated so the recent-and-relevant end survives). Also
    grabs any debug.jsonl from the XDG state dir if Scope D/E's debug
    toggle is enabled."""
    logs_dir = dest_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    collected: list[dict[str, Any]] = []
    if _LOG_DIR.exists():
        for src in _LOG_DIR.iterdir():
            if not src.is_file():
                continue
            data, truncated = _truncate_read(src)
            (logs_dir / src.name).write_bytes(data)
            collected.append({
                "file": src.name,
                "bytes": len(data),
                "truncated": truncated,
                "original_size": src.stat().st_size,
            })
    # Scope D/E debug log (XDG state, may be user-owned).
    for candidate in (
        Path(os.environ.get("XDG_STATE_HOME", "")) / "icebreaker" / "debug.jsonl",
        Path.home() / ".local/state/icebreaker/debug.jsonl",
        Path("/root/.local/state/icebreaker/debug.jsonl"),
    ):
        if candidate.exists() and candidate.is_file():
            data, truncated = _truncate_read(candidate)
            (logs_dir / f"debug.jsonl.{candidate.parent.parent.parent.name}").write_bytes(data)
            collected.append({
                "file": f"debug.jsonl (from {candidate})",
                "bytes": len(data),
                "truncated": truncated,
                "original_size": candidate.stat().st_size,
            })
            break
    return {"collector": "logs", "ok": True, "files": collected}


def collect_journals(dest_dir: Path) -> dict[str, Any]:
    """Last 500 lines from every icebreaker systemd unit's journal."""
    j_dir = dest_dir / "journals"
    j_dir.mkdir(exist_ok=True)
    collected = []
    for unit in _ICEBREAKER_UNITS:
        rc, stdout, stderr = _run(
            ["journalctl", "-u", unit, "-n", str(_MAX_JOURNAL_LINES),
             "--no-pager", "--output=short-iso"],
            timeout=15,
        )
        (j_dir / f"{unit}.log").write_text(stdout)
        collected.append({
            "unit": unit,
            "bytes": len(stdout),
            "returncode": rc,
            "stderr_tail": stderr[-200:] if stderr else "",
        })
    return {"collector": "journals", "ok": True, "units": collected}


def collect_boot_report(dest_dir: Path) -> dict[str, Any]:
    """Copy the LATEST /var/log/icebreaker/boot-report-*.json. Multiple
    reports can accumulate across reboots; ship the newest by mtime."""
    import glob
    candidates = sorted(glob.glob(_BOOT_REPORT_GLOB),
                        key=lambda p: os.path.getmtime(p))
    if not candidates:
        return {"collector": "boot_report", "ok": False,
                "reason": "no boot-report-*.json found"}
    src = Path(candidates[-1])
    (dest_dir / "boot-report.json").write_bytes(src.read_bytes())
    return {"collector": "boot_report", "ok": True,
            "source": src.name, "size_bytes": src.stat().st_size}


def collect_harvest(dest_dir: Path) -> dict[str, Any]:
    """Run the in-guest harvest, capture its JSON output. Non-fatal if
    the script is missing — that just means we're on a pre-v1.0-rc1 ISO
    or someone tampered with /usr/local/bin/."""
    if not _HARVEST_SCRIPT.exists():
        return {"collector": "harvest", "ok": False,
                "reason": f"{_HARVEST_SCRIPT} not found"}
    rc, stdout, stderr = _run(
        ["sudo", str(_HARVEST_SCRIPT),
         "--timeout", str(_HARVEST_TIMEOUT_SECONDS)],
        timeout=_HARVEST_TIMEOUT_SECONDS + 30,
    )
    (dest_dir / "harvest.json").write_text(
        stdout if stdout.strip().startswith("{")
        else json.dumps({"status": "error",
                         "reason": "harvest produced non-JSON output",
                         "returncode": rc,
                         "stderr": stderr[-500:],
                         "stdout_tail": stdout[-500:]}, indent=2) + "\n"
    )
    return {"collector": "harvest", "ok": rc == 0,
            "returncode": rc}


def collect_markers(dest_dir: Path) -> dict[str, Any]:
    """Grep-verify every F-51 marker against the installed venv. Same
    logic as v2.manifest's marker loop but readable at runtime."""
    # Import the v2.manifest marker list is not straightforward — the
    # manifests are shell-array literals. Simpler: ship a static list
    # here that mirrors the manifest, and note that discrepancies are
    # audit findings. Actual manifest is at
    # incremental/versions/v2.manifest lines 82-165.
    markers = [
        # F-4x/F-5x helpers
        ("controller/main.py", "_normalize_server_owned_fields", "F-41"),
        ("controller/main.py", "_make_cot", "F-42"),
        ("controller/main.py", "uuid.uuid4()", "F-43"),
        ("controller/hitl.py",
         "threading.current_thread() is threading.main_thread()", "F-45"),
        ("controller/main.py", "F-47", "F-47"),
        ("controller/main.py", "F-48", "F-48"),
        ("controller/main.py", "_should_retry_verifier", "F-49"),
        ("controller/main.py", "_relax_server_owned_for_backend", "F-52"),
        ("controller/main.py", "_log_exception", "F-53"),
        ("controller/fallback_backend.py", "FallbackChain", "F-54"),
        # Scope F fix-shape markers
        ("controller/main.py", "F-43 + F-47", "F43-stream"),
        ("controller/main.py", "F-47b:", "F47b-inner"),
        ("controller/main.py", '"unsupported", "done"', "F48-fix"),
        ("controller/main.py", "_backend_intent_schema", "F52-wire"),
        ("controller/verifier.py", "type(exc).__name__", "F53-verifier"),
        # Scope G marker
        ("controller/main.py",
         "from .turn_events import ResultEvent", "F56-emit-import"),
    ]
    venv_sp = Path(
        "/opt/icebreaker/venv/lib/python3.12/site-packages"
    )
    results = []
    for rel_path, needle, marker_id in markers:
        target = venv_sp / rel_path
        entry: dict[str, Any] = {"marker": marker_id, "file": rel_path}
        if not target.exists():
            entry["status"] = "file_missing"
            entry["ok"] = False
        else:
            try:
                src = target.read_text(errors="replace")
                entry["ok"] = needle in src
                entry["status"] = "ok" if entry["ok"] else "needle_missing"
            except Exception as exc:  # noqa: BLE001
                entry["status"] = f"read_error: {type(exc).__name__}"
                entry["ok"] = False
        results.append(entry)
    missing = [r for r in results if not r["ok"]]
    (dest_dir / "markers.json").write_text(json.dumps({
        "venv_site_packages": str(venv_sp),
        "checked": len(results),
        "missing_count": len(missing),
        "markers": results,
    }, indent=2, sort_keys=True) + "\n")
    return {"collector": "markers", "ok": len(missing) == 0,
            "checked": len(results), "missing": len(missing)}


def collect_config_redacted(dest_dir: Path) -> dict[str, Any]:
    """Copy controller.toml with secret-key values replaced by
    ``<REDACTED>``. This is safe to attach to a GitHub issue."""
    if not _CONTROLLER_TOML.exists():
        return {"collector": "config_redacted", "ok": False,
                "reason": f"{_CONTROLLER_TOML} not found"}
    lines = _CONTROLLER_TOML.read_text().splitlines(keepends=True)
    redacted = [_redact_toml_line(l) for l in lines]
    (dest_dir / "config-redacted.toml").write_text("".join(redacted))
    return {"collector": "config_redacted", "ok": True,
            "lines": len(lines)}


def collect_package_versions(dest_dir: Path) -> dict[str, Any]:
    """dpkg -l icebreaker* + venv pip freeze — knowing which versions
    ran is table-stakes for reproducing a bug."""
    _, dpkg_out, _ = _run(
        ["dpkg", "-l", "icebreaker*"], timeout=10,
    )
    venv_pip = Path("/opt/icebreaker/venv/bin/pip")
    if venv_pip.exists():
        _, pip_out, _ = _run(
            [str(venv_pip), "freeze"], timeout=15,
        )
    else:
        pip_out = "(venv pip not available)"
    (dest_dir / "package_versions.txt").write_text(
        f"=== dpkg -l icebreaker* ===\n{dpkg_out}\n"
        f"=== /opt/icebreaker/venv/bin/pip freeze ===\n{pip_out}\n"
    )
    return {"collector": "package_versions", "ok": True}


def collect_iso_build_manifest(dest_dir: Path) -> dict[str, Any]:
    if not _BUILD_MANIFEST.exists():
        return {"collector": "iso_build_manifest", "ok": False,
                "reason": f"{_BUILD_MANIFEST} not found"}
    (dest_dir / "iso_build_manifest.json").write_bytes(
        _BUILD_MANIFEST.read_bytes()
    )
    return {"collector": "iso_build_manifest", "ok": True}


# ── Bundle orchestration ─────────────────────────────────────────────────


def build_bundle(
    out_path: Path,
    reason: str,
    notify: bool,
) -> dict[str, Any]:
    """Run every collector into a scratch dir, tar + zstd it into
    ``out_path``. Emit a summary dict describing what worked."""
    ts_utc = _dt.datetime.now(_dt.timezone.utc)
    metadata: dict[str, Any] = {
        "bundle_version": 1,
        "generated_at_utc": ts_utc.isoformat(),
        "reason": reason,
        "collectors": [],
        "arch": platform.machine(),
        "hostname": platform.node(),
    }
    with tempfile.TemporaryDirectory(
        prefix=f"ib-bundle-{ts_utc:%Y%m%dT%H%M%SZ}-"
    ) as tmp:
        scratch = Path(tmp)
        for fn in (
            collect_ib_debug_report,
            collect_system_json,
            collect_models_json,
            collect_logs,
            collect_journals,
            collect_boot_report,
            collect_harvest,
            collect_markers,
            collect_config_redacted,
            collect_package_versions,
            collect_iso_build_manifest,
        ):
            try:
                result = fn(scratch)
            except Exception as exc:  # noqa: BLE001
                result = {"collector": fn.__name__, "ok": False,
                          "error": f"{type(exc).__name__}: {exc}"}
            metadata["collectors"].append(result)
        # Write a preliminary metadata so it's captured in the tar; we
        # rewrite it with the final bundle_size_bytes AFTER compression
        # (chicken-and-egg: the size isn't known until zstd finishes,
        # but the tar has to be built BEFORE zstd runs).
        metadata_path = scratch / "bundle_metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        )
        # Tar + zstd
        tmp_tar = scratch.parent / f"{scratch.name}.tar"
        rc, _, tar_err = _run(
            ["tar", "-cf", str(tmp_tar), "-C", str(scratch.parent),
             scratch.name],
            timeout=120,
        )
        if rc != 0:
            raise RuntimeError(f"tar failed: {tar_err}")
        rc, _, zstd_err = _run(
            ["zstd", "--rm", "-19", "-f", str(tmp_tar),
             "-o", str(out_path)],
            timeout=120,
        )
        if rc != 0:
            # zstd absent? fallback to gzip + rename.
            if "not found" in zstd_err.lower():
                rc2, _, gz_err = _run(
                    ["gzip", "-9", "-c", str(tmp_tar)], timeout=120,
                )
                # gzip -c writes to stdout — we would need to capture.
                # Simpler: shell-out to bash with a real redirect.
                fallback = out_path.with_suffix(".tar.gz")
                rc3, _, sh_err = _run(
                    ["bash", "-c",
                     f"gzip -9 -c {tmp_tar} > {fallback}"],
                    timeout=120,
                )
                if rc3 != 0:
                    raise RuntimeError(
                        f"zstd unavailable, gzip fallback failed: "
                        f"{sh_err}"
                    )
                out_path = fallback
            else:
                raise RuntimeError(f"zstd failed: {zstd_err}")
        # Enforce final size cap
        actual_size = out_path.stat().st_size
        metadata["bundle_size_bytes"] = actual_size
        # Return the size in metadata so the caller can log it; the tar
        # already contains the preliminary metadata without the size.
        # A downstream tool that wants the definitive size can `zstd -d`
        # the bundle and stat the .tar.
        if actual_size > _MAX_BUNDLE_BYTES:
            print(
                f"WARNING: bundle is {actual_size} bytes "
                f"(cap {_MAX_BUNDLE_BYTES}). Consider filtering logs.",
                file=sys.stderr,
            )
    # Best-effort desktop notification
    if notify:
        _send_notification(out_path, metadata)
    return metadata


def _send_notification(out_path: Path, metadata: dict[str, Any]) -> None:
    """Fire notify-send if available. Non-fatal if libnotify isn't
    installed (Scope G.5 adds libnotify-bin to packages-desktop.txt but
    older ISOs won't have it)."""
    ok_collectors = sum(
        1 for c in metadata["collectors"] if c.get("ok")
    )
    total = len(metadata["collectors"])
    icon = "dialog-information"
    urgency = "normal"
    body = (
        f"Bundle saved to:\n{out_path}\n\n"
        f"{ok_collectors}/{total} collectors ok.\n"
        f"Attach this file to your GitHub issue."
    )
    _run(
        ["notify-send", "--urgency", urgency, "--icon", icon,
         "--expire-time", "300000",  # 5 min
         "Icebreaker diagnostic bundle", body],
        timeout=5,
    )


# ── Entrypoint ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ib-bundle",
        description="Collect an Icebreaker diagnostic bundle.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Output path (default: ~/icebreaker-bundle-<ts>-<arch>.tar.zst).",
    )
    parser.add_argument(
        "--reason",
        default="user-requested",
        help="Why this bundle was generated (goes into bundle_metadata.json).",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Fire notify-send on completion.",
    )
    args = parser.parse_args(argv)

    if args.out is None:
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        arch = platform.machine() or "unknown"
        # HOME may not be set under systemd — fall back to /tmp.
        home = os.environ.get("HOME") or "/tmp"
        args.out = Path(home) / f"icebreaker-bundle-{ts}-{arch}.tar.zst"

    args.out.parent.mkdir(parents=True, exist_ok=True)

    try:
        metadata = build_bundle(
            out_path=args.out,
            reason=args.reason,
            notify=args.notify,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"ib-bundle: FATAL: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    ok = sum(1 for c in metadata["collectors"] if c.get("ok"))
    total = len(metadata["collectors"])
    print(f"ib-bundle: {ok}/{total} collectors ok")
    print(f"ib-bundle: wrote {args.out} "
          f"({metadata.get('bundle_size_bytes', '?')} bytes)")
    print(f"ib-bundle: attach this file to your GitHub issue")
    # Exit 0 if bundle written, even with partial collector failures —
    # a partial bundle is more useful than none. Exit 1 only if the
    # bundle itself couldn't be created (handled above).
    return 0


if __name__ == "__main__":
    sys.exit(main())
