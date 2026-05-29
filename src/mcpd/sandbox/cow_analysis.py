"""
cow_analysis.py — Structured analysis of COW (Copy-on-Write) dry-run diffs.

After the COW overlay executes a command in a sandbox, this module compares the
resulting filesystem diff against the expected scope implied by the Intent Object.
If the diff touches paths outside the expected scope, it returns a structured
correction that the Controller uses to create a revised Intent Object (via
intent_store.revise()).

This is Architecture 5 from the accuracy plan — the "observe-correct loop" that
runs BEFORE the HITL prompt, giving PB one chance to self-correct when its
command had unexpected filesystem side-effects.

Security note: this module reads overlayfs diff paths only — it does not execute
any commands. The correction it returns flows through the Controller as a revised
Intent Object (same schema-validated path as the original), never as raw text
to the Privileged Brain.

Usage:
    from cow_analysis import analyse_diff
    result = analyse_diff(intent, changed_paths)
    if not result.within_scope:
        new_ref = intent_store.revise(ref_id, result.correction)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Paths that are always considered unexpected if touched by the model
# (in addition to the intent's declared target)
_ALWAYS_SENSITIVE = re.compile(
    r"^(/boot|/etc/sudoers|/etc/shadow|/etc/passwd|/root|"
    r"/proc/sys/kernel|/dev/|/sys/firmware)",
    re.IGNORECASE,
)


@dataclass
class DiffAnalysis:
    within_scope: bool
    expected_paths: list[str]
    unexpected_paths: list[str]
    sensitive_paths: list[str]
    correction: dict = field(default_factory=dict)
    summary: str = ""


def _infer_expected_scope(intent: dict) -> list[str]:
    """Derive the set of filesystem paths the intent is expected to affect.

    The Intent Object's 'target' field is the primary indicator. For service
    operations, the expected scope is a small set of known service directories.
    For fs.* operations, the expected scope is the target path (and its children).
    """
    action = intent.get("action", "")
    target = intent.get("target", "").strip()
    params  = intent.get("params", {}) or {}

    if not target and not params:
        return []

    expected: list[str] = []

    if action.startswith("fs."):
        if target:
            expected.append(target)
        # Also accept writes to temp dirs within the target's parent
        if target:
            expected.append(str(Path(target).parent))

    elif action.startswith("service."):
        # Systemd service operations may touch a predictable set of paths
        service = target or params.get("service", "")
        if service:
            expected += [
                f"/etc/systemd/system/{service}.service",
                f"/usr/lib/systemd/system/{service}.service",
                f"/run/systemd/units/{service}.service",
            ]

    elif action.startswith("package."):
        # Package operations affect /usr, /var/lib/dpkg, /var/cache/apt
        expected += ["/usr", "/var/lib/dpkg", "/var/cache/apt", "/lib"]

    elif action.startswith("network."):
        # Firewall/network config changes may touch /etc/iptables or /etc/ufw
        expected += ["/etc/iptables", "/etc/ufw", "/proc/net"]

    # Always allow writes within user home dir (Tier 1 scope)
    user_home = os.path.expanduser("~")
    expected.append(user_home)

    # Also allow writes to /tmp — many commands use temp files
    expected.append("/tmp")

    return expected


def _path_is_within(path: str, scope: list[str]) -> bool:
    """Return True if path starts with any scope entry."""
    abs_path = os.path.abspath(path)
    for scope_entry in scope:
        abs_scope = os.path.abspath(scope_entry)
        if abs_path == abs_scope or abs_path.startswith(abs_scope + os.sep):
            return True
    return False


def analyse_diff(intent: dict, changed_paths: list[str]) -> DiffAnalysis:
    """Analyse a COW dry-run diff against the expected scope of the intent.

    Args:
        intent:        The validated Intent Object from intent_store.
        changed_paths: List of absolute filesystem paths touched by the dry-run
                       (files created, modified, or deleted).

    Returns:
        DiffAnalysis with within_scope flag, categorised path lists, and
        a correction dict suitable for intent_store.revise() if out-of-scope.
    """
    expected_scope = _infer_expected_scope(intent)

    expected_paths: list[str]   = []
    unexpected_paths: list[str] = []
    sensitive_paths: list[str]  = []

    user_home = os.path.expanduser("~")

    for path in changed_paths:
        # User home takes priority over the sensitive-path regex (e.g. /root)
        if _path_is_within(path, [user_home]):
            expected_paths.append(path)
        elif _ALWAYS_SENSITIVE.match(path):
            sensitive_paths.append(path)
        elif _path_is_within(path, expected_scope):
            expected_paths.append(path)
        else:
            unexpected_paths.append(path)

    within_scope = not unexpected_paths and not sensitive_paths

    correction: dict = {}
    summary = ""

    if sensitive_paths:
        summary = (
            f"Dry-run touched {len(sensitive_paths)} sensitive system path(s): "
            f"{', '.join(sensitive_paths[:3])}. Operation escalated to Tier 3 HITL."
        )
        # Sensitive path hits cannot be corrected by revising the intent; they
        # require HITL escalation. Return an empty correction so the Controller
        # knows NOT to retry.

    elif unexpected_paths:
        # Build a restrict_to correction: add the unexpected paths to params
        # so the next PB inference knows to limit scope.
        restrict_to = sorted(set(expected_paths + unexpected_paths))
        correction = {
            "params": {
                **(intent.get("params") or {}),
                "restrict_to": ", ".join(restrict_to[:8]),  # cap at 8 for field length
            }
        }
        summary = (
            f"Dry-run touched {len(unexpected_paths)} unexpected path(s) outside "
            f"declared target scope: {', '.join(unexpected_paths[:3])}. "
            "Retrying with restrict_to constraint."
        )

    else:
        summary = f"Dry-run within expected scope ({len(expected_paths)} path(s) affected)."

    return DiffAnalysis(
        within_scope=within_scope,
        expected_paths=expected_paths,
        unexpected_paths=unexpected_paths,
        sensitive_paths=sensitive_paths,
        correction=correction,
        summary=summary,
    )


def paths_from_overlayfs_upper(upper_dir: str) -> list[str]:
    """Walk an overlayfs upper directory and return all changed absolute paths.

    overlayfs stores all modified/created files under 'upper'. Whiteout files
    (prefixed 'char device 0/0') represent deletions. We include both.

    Args:
        upper_dir: The 'upper' layer of the overlayfs mount (where changes land).

    Returns:
        List of absolute paths that were touched (relative to the overlay root).
    """
    paths: list[str] = []
    upper = Path(upper_dir)
    if not upper.is_dir():
        return paths

    for entry in upper.rglob("*"):
        # Compute the effective path relative to the overlay root
        rel = str(entry.relative_to(upper))
        abs_path = "/" + rel
        paths.append(abs_path)

    return paths
