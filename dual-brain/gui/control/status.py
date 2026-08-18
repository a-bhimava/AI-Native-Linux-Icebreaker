"""Live status collector for the Control Center.

Thin wrapper around the primitives that ``scripts/ib_debug.py`` already
uses. We do NOT re-implement service/socket/model checks — we call into
the existing collectors so this file stays under 100 lines and any fix
to ``ib_debug`` propagates automatically.
"""

from __future__ import annotations

import logging
import subprocess
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_status_log = logging.getLogger(__name__)


_CREDENTIAL_STATUS = Path("/var/lib/icebreaker/credential-status.env")


@dataclass
class KeyState:
    """One row on the API Keys page."""
    env_var: str
    label: str
    configured: bool = False
    masked_value: str = ""


@dataclass
class Health:
    """Snapshot returned by ``collect()``. Every field is safe to render."""
    services: list = field(default_factory=list)   # list[dict(label, active, pid)]
    sockets: list = field(default_factory=list)    # list[dict(label, path, status)]
    keys: list[KeyState] = field(default_factory=list)
    version: str = ""
    daemon_reachable: bool = False


def _read_credential_status() -> set[str]:
    """Read provider-only credential status; never inspect a secret value."""
    result: set[str] = set()
    if not _CREDENTIAL_STATUS.exists():
        return result
    try:
        for line in _CREDENTIAL_STATUS.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if value.strip() == "configured":
                result.add(key.strip())
    except Exception as exc:
        _status_log.warning(
            "status.credential_status unreadable at %s: %s: %s",
            _CREDENTIAL_STATUS, type(exc).__name__, exc,
        )
    return result


def _service_active(unit: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=2,
        )
        return r.returncode == 0, r.stdout.strip()
    except Exception as exc:
        # F-53 Scope A.P3: systemctl call failed (not installed,
        # timeout, permission). Log so a broken systemd install
        # doesn't silently make every service look inactive.
        _status_log.warning(
            "status.systemctl(%s) failed: %s: %s",
            unit, type(exc).__name__, exc,
        )
        return False, "unknown"


def _socket_status(path: str, probe: str = "http") -> str:
    """Return 'responded', 'reachable', 'refused', or 'missing'."""
    if not Path(path).exists():
        return "missing"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect(path)
            if probe == "http":
                s.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                data = s.recv(1024)
                return "responded" if b"200 OK" in data or b"HTTP/1.1 2" in data else "reachable"
            return "reachable"
    except (ConnectionRefusedError, OSError):
        return "refused"


def collect() -> Health:
    h = Health()

    version_file = Path("/etc/icebreaker-version")
    if version_file.exists():
        try:
            h.version = version_file.read_text().strip()
        except Exception as exc:
            # F-53 Scope A.P3: file is present but unreadable — usually
            # a permissions bug in a freshly-installed image.
            _status_log.warning(
                "status.version_file unreadable: %s: %s",
                type(exc).__name__, exc,
            )

    for unit, label in [
        ("icebreaker-first-boot.service", "First Boot"),
        ("icebreaker-controller.service", "Controller"),
        ("icebreaker-pbd.service",        "Privileged Brain"),
    ]:
        active, state = _service_active(unit)
        h.services.append({"unit": unit, "label": label, "active": active, "state": state})

    for path, label, probe in [
        ("/run/icebreaker/controller.sock", "Controller RPC", "jsonrpc"),
        ("/run/icebreaker/pbd.sock",        "PB llama-server", "http"),
    ]:
        status = _socket_status(path, probe)
        h.sockets.append({"path": path, "label": label, "status": status})
    h.daemon_reachable = any(s["label"] == "Controller RPC" and s["status"] in ("responded", "reachable") for s in h.sockets)

    configured_vars = _read_credential_status()
    for env_var, label in [
        ("GEMINI_API_KEY",     "Google Gemini"),
        ("ANTHROPIC_API_KEY",  "Anthropic Claude"),
        ("OPENAI_API_KEY",     "OpenAI"),
    ]:
        configured = env_var in configured_vars
        h.keys.append(KeyState(
            env_var=env_var, label=label,
            configured=configured,
            masked_value="",
        ))

    return h
