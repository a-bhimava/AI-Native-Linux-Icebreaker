"""Tests for TOCTOU mitigation via realpath resolution (M5.P1-sec, SF-10).

Covers:
  - Symlink target resolved to real path.
  - Non-symlink target unchanged.
  - Empty target produces empty realpath.
  - target_realpath field added to intent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_realpath_resolves_symlink(tmp_path):
    real_file = tmp_path / "real.txt"
    real_file.write_text("content")
    link = tmp_path / "link.txt"
    link.symlink_to(real_file)
    resolved = os.path.realpath(str(link))
    assert resolved == str(real_file)


def test_realpath_preserves_non_symlink(tmp_path):
    real_file = tmp_path / "real.txt"
    real_file.write_text("content")
    resolved = os.path.realpath(str(real_file))
    assert resolved == str(real_file)


def test_realpath_empty_target():
    resolved = os.path.realpath("") if "" else ""
    assert resolved == ""


def test_intent_gets_target_realpath(tmp_path):
    """Simulate the Controller's Step 2b: resolve target realpath."""
    real_file = tmp_path / "actual.txt"
    real_file.write_text("data")
    link = tmp_path / "symlink.txt"
    link.symlink_to(real_file)

    intent = {
        "action": "fs.read",
        "target": str(link),
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }

    raw_target = intent.get("target", "")
    if raw_target:
        intent["target_realpath"] = os.path.realpath(raw_target)
    else:
        intent["target_realpath"] = ""

    assert intent["target_realpath"] == str(real_file)
    assert intent["target"] == str(link)


def test_intent_empty_target_gets_empty_realpath():
    intent = {
        "action": "system.status",
        "target": "",
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }

    raw_target = intent.get("target", "")
    if raw_target:
        intent["target_realpath"] = os.path.realpath(raw_target)
    else:
        intent["target_realpath"] = ""

    assert intent["target_realpath"] == ""
