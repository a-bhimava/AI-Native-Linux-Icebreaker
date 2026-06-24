"""Tests for systemd unit files and distro start scripts.

Validates unit structure, security hardening directives, ordering
constraints, and script correctness without requiring a running systemd.
"""

from __future__ import annotations

import configparser
import os
import subprocess
from pathlib import Path

import pytest


SYSTEMD_DIR = Path(__file__).parent.parent / "systemd"
SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"


def _parse_unit(name: str) -> configparser.ConfigParser:
    """Parse a systemd unit file into a ConfigParser.

    Systemd units are INI-like but allow duplicate keys (e.g. multiple
    ReadOnlyPaths=). We use strict=False to handle that.
    """
    path = SYSTEMD_DIR / name
    assert path.exists(), f"unit file missing: {path}"
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read(str(path))
    return cp


# ── Unit file existence ──────────────────────────────────────────────────


_EXPECTED_UNITS = [
    "icebreaker-pbd.service",
    "icebreaker-qbd.service",
    "icebreaker-mcpd@.service",
    "icebreaker-controller.service",
    "icebreaker-controller.socket",
]


@pytest.mark.parametrize("unit", _EXPECTED_UNITS)
def test_unit_file_exists(unit):
    assert (SYSTEMD_DIR / unit).exists()


# ── Security hardening ───────────────────────────────────────────────────


_HARDENED_SERVICES = [
    "icebreaker-pbd.service",
    "icebreaker-qbd.service",
    "icebreaker-mcpd@.service",
    "icebreaker-controller.service",
]


@pytest.mark.parametrize("unit", _HARDENED_SERVICES)
def test_all_services_have_nonewprivileges(unit):
    cp = _parse_unit(unit)
    assert cp.get("Service", "NoNewPrivileges", fallback="") == "yes"


@pytest.mark.parametrize("unit", _HARDENED_SERVICES)
def test_all_services_have_protectsystem_strict(unit):
    cp = _parse_unit(unit)
    assert cp.get("Service", "ProtectSystem", fallback="") == "strict"


# ── mcpd is Type=notify with NotifyAccess=main ──────────────────────────


def test_mcpd_type_notify():
    cp = _parse_unit("icebreaker-mcpd@.service")
    assert cp.get("Service", "Type", fallback="") == "notify"


def test_mcpd_notifyaccess_main():
    cp = _parse_unit("icebreaker-mcpd@.service")
    assert cp.get("Service", "NotifyAccess", fallback="") == "main"


# ── Ordering: controller After= pbd + qbd ───────────────────────────────


def test_controller_after_pbd_and_qbd():
    cp = _parse_unit("icebreaker-controller.service")
    after = cp.get("Unit", "After", fallback="")
    assert "icebreaker-pbd.service" in after
    assert "icebreaker-qbd.service" in after


def test_controller_wants_pbd_and_qbd():
    cp = _parse_unit("icebreaker-controller.service")
    wants = cp.get("Unit", "Wants", fallback="")
    assert "icebreaker-pbd.service" in wants
    assert "icebreaker-qbd.service" in wants


# ── Parallel startup: pbd and qbd have NO ordering between them ─────────


def test_pbd_has_no_after_qbd():
    cp = _parse_unit("icebreaker-pbd.service")
    after = cp.get("Unit", "After", fallback="")
    assert "icebreaker-qbd" not in after


def test_qbd_has_no_after_pbd():
    cp = _parse_unit("icebreaker-qbd.service")
    after = cp.get("Unit", "After", fallback="")
    assert "icebreaker-pbd" not in after


# ── RuntimeDirectory present in pbd/qbd ─────────────────────────────────


@pytest.mark.parametrize("unit", [
    "icebreaker-pbd.service",
    "icebreaker-qbd.service",
])
def test_runtime_directory_icebreaker(unit):
    cp = _parse_unit(unit)
    assert cp.get("Service", "RuntimeDirectory", fallback="") == "icebreaker"


# ── pbd/qbd run as dedicated service users ──────────────────────────────


def test_pbd_runs_as_icebreaker_pb():
    cp = _parse_unit("icebreaker-pbd.service")
    assert cp.get("Service", "User", fallback="") == "_icebreaker_pb"


def test_qbd_runs_as_icebreaker_qb():
    cp = _parse_unit("icebreaker-qbd.service")
    assert cp.get("Service", "User", fallback="") == "_icebreaker_qb"


# ── MemoryMax on inference services ──────────────────────────────────────


@pytest.mark.parametrize("unit", [
    "icebreaker-pbd.service",
    "icebreaker-qbd.service",
])
def test_memory_max_set(unit):
    cp = _parse_unit(unit)
    mem = cp.get("Service", "MemoryMax", fallback="")
    assert mem, f"{unit} missing MemoryMax"


# ── UMask on inference services for socket permissions ───────────────────


@pytest.mark.parametrize("unit", [
    "icebreaker-pbd.service",
    "icebreaker-qbd.service",
])
def test_umask_set_for_socket_permissions(unit):
    """UMask=0117 ensures llama-server creates sockets as 0660."""
    cp = _parse_unit(unit)
    umask = cp.get("Service", "UMask", fallback="")
    assert umask == "0117", f"{unit} UMask={umask!r}, expected '0117'"


# ── Controller ExecStart uses venv Python ────────────────────────────────


def test_controller_execstart_uses_venv():
    cp = _parse_unit("icebreaker-controller.service")
    exec_start = cp.get("Service", "ExecStart", fallback="")
    assert "/opt/icebreaker/venv/bin/python3" in exec_start
    assert "--daemon" in exec_start
    assert "--config /etc/icebreaker/controller.toml" in exec_start


# ── Socket unit uses system path matching tmpfiles.d ─────────────────────


def test_socket_uses_system_path():
    cp = _parse_unit("icebreaker-controller.socket")
    listen = cp.get("Socket", "ListenStream", fallback="")
    assert "/run/icebreaker/controller.sock" in listen


def test_socket_path_matches_distro_config():
    """Socket unit path must agree with controller.toml [daemon].socket_path."""
    import sys
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib
    distro_config = Path(__file__).parent.parent.parent.parent / "cx-distro" / "distro" / "controller.toml"
    if not distro_config.exists():
        pytest.skip("cx-distro/distro/controller.toml not present")
    raw = tomllib.loads(distro_config.read_text())
    config_socket = raw.get("daemon", {}).get("socket_path", "")

    cp = _parse_unit("icebreaker-controller.socket")
    unit_socket = cp.get("Socket", "ListenStream", fallback="")
    assert config_socket == unit_socket, (
        f"Socket unit ListenStream={unit_socket!r} != "
        f"controller.toml daemon.socket_path={config_socket!r}"
    )


# ── Controller service has ExecStartPre and EnvironmentFile ────────────


def test_controller_has_environment_file():
    cp = _parse_unit("icebreaker-controller.service")
    env_file = cp.get("Service", "EnvironmentFile", fallback="")
    assert "/etc/icebreaker/locations.env" in env_file


def test_controller_has_exec_start_pre():
    cp = _parse_unit("icebreaker-controller.service")
    pre = cp.get("Service", "ExecStartPre", fallback="")
    assert "wait-for-sockets" in pre


def test_controller_has_logs_directory():
    cp = _parse_unit("icebreaker-controller.service")
    logs_dir = cp.get("Service", "LogsDirectory", fallback="")
    assert logs_dir == "icebreaker"


# ── start-qbd handles non-local backends ──────────────────────────────


def test_start_qbd_checks_backend_type():
    """start-qbd must check backend before assuming local llama-server."""
    content = (SCRIPTS_DIR / "start-qbd").read_text()
    assert "QB_BACKEND" in content, "start-qbd must check backend type"
    assert "sleep infinity" in content, "start-qbd must sleep if backend is not local"


# ── sysusers.d conf ──────────────────────────────────────────────────────


def test_sysusers_creates_group():
    path = SYSTEMD_DIR / "icebreaker.sysusers.d.conf"
    assert path.exists()
    content = path.read_text()
    assert "g icebreaker-users" in content


def test_sysusers_creates_pb_user():
    content = (SYSTEMD_DIR / "icebreaker.sysusers.d.conf").read_text()
    assert "u _icebreaker_pb" in content


def test_sysusers_creates_qb_user():
    content = (SYSTEMD_DIR / "icebreaker.sysusers.d.conf").read_text()
    assert "u _icebreaker_qb" in content


def test_sysusers_adds_users_to_group():
    content = (SYSTEMD_DIR / "icebreaker.sysusers.d.conf").read_text()
    assert "m _icebreaker_pb icebreaker-users" in content
    assert "m _icebreaker_qb icebreaker-users" in content


# ── tmpfiles.d conf ──────────────────────────────────────────────────────


def test_tmpfiles_creates_run_directory():
    path = SYSTEMD_DIR / "icebreaker.tmpfiles.d.conf"
    assert path.exists()
    content = path.read_text()
    assert "d /run/icebreaker" in content
    assert "2775" in content
    assert "icebreaker-users" in content


def test_tmpfiles_creates_log_directory():
    content = (SYSTEMD_DIR / "icebreaker.tmpfiles.d.conf").read_text()
    assert "d /var/log/icebreaker" in content


# ── Distro start scripts ────────────────────────────────────────────────


def test_start_pbd_exists_and_executable():
    path = SCRIPTS_DIR / "start-pbd"
    assert path.exists()
    assert os.access(path, os.X_OK)


def test_start_qbd_exists_and_executable():
    path = SCRIPTS_DIR / "start-qbd"
    assert path.exists()
    assert os.access(path, os.X_OK)


def test_start_pbd_has_set_euo_pipefail():
    content = (SCRIPTS_DIR / "start-pbd").read_text()
    assert "set -euo pipefail" in content


def test_start_qbd_has_set_euo_pipefail():
    content = (SCRIPTS_DIR / "start-qbd").read_text()
    assert "set -euo pipefail" in content


def test_start_pbd_verifies_checksum():
    content = (SCRIPTS_DIR / "start-pbd").read_text()
    assert "sha256sum" in content
    assert "checksums.sha256" in content


def test_start_qbd_verifies_checksum():
    content = (SCRIPTS_DIR / "start-qbd").read_text()
    assert "sha256sum" in content
    assert "checksums.sha256" in content


def test_start_pbd_uses_unix_socket():
    content = (SCRIPTS_DIR / "start-pbd").read_text()
    assert "pbd.sock" in content
    assert "--host" in content


def test_start_qbd_uses_unix_socket():
    content = (SCRIPTS_DIR / "start-qbd").read_text()
    assert "qbd.sock" in content
    assert "--host" in content


def test_wait_for_sockets_exists_and_executable():
    path = Path(__file__).parent.parent.parent.parent / "cx-distro" / "distro" / "wait-for-sockets"
    if not path.exists():
        pytest.skip("cx-distro/distro/wait-for-sockets not present")
    assert os.access(path, os.X_OK)


def test_wait_for_sockets_passes_bash_n():
    path = Path(__file__).parent.parent.parent.parent / "cx-distro" / "distro" / "wait-for-sockets"
    if not path.exists():
        pytest.skip("cx-distro/distro/wait-for-sockets not present")
    result = subprocess.run(
        ["bash", "-n", str(path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_start_pbd_passes_bash_n():
    """bash -n catches syntax errors without executing the script."""
    result = subprocess.run(
        ["bash", "-n", str(SCRIPTS_DIR / "start-pbd")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_start_qbd_passes_bash_n():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPTS_DIR / "start-qbd")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


@pytest.mark.skipif(
    subprocess.run(
        ["which", "shellcheck"], capture_output=True
    ).returncode != 0,
    reason="shellcheck not installed",
)
@pytest.mark.parametrize("script", ["start-pbd", "start-qbd"])
def test_scripts_pass_shellcheck(script):
    result = subprocess.run(
        ["shellcheck", "-S", "warning", str(SCRIPTS_DIR / script)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"shellcheck: {result.stdout}\n{result.stderr}"
