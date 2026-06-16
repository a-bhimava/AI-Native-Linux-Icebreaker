"""Tests for ``pyproject.toml`` validity and consistency."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_PYPROJECT = Path(__file__).parent.parent.parent / "pyproject.toml"
_REQUIREMENTS = Path(__file__).parent.parent.parent / "requirements.txt"

_TEST_ONLY_PREFIXES = ("hypothesis", "pytest")


def test_pyproject_toml_is_valid():
    raw = tomllib.loads(_PYPROJECT.read_text())
    assert "build-system" in raw
    assert "project" in raw
    assert raw["project"]["name"] == "icebreaker-controller"


def test_pyproject_dependencies_match_requirements():
    raw = tomllib.loads(_PYPROJECT.read_text())
    pyproject_deps = {
        d.split(">")[0].split("<")[0].split("=")[0].split(";")[0].strip()
        for d in raw["project"]["dependencies"]
    }
    req_lines = _REQUIREMENTS.read_text().splitlines()
    for line in req_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pkg = line.split(">")[0].split("<")[0].split("=")[0].split(";")[0].strip()
        if any(pkg.startswith(p) for p in _TEST_ONLY_PREFIXES):
            continue
        assert pkg in pyproject_deps, f"{pkg} in requirements.txt but not in pyproject.toml"


def test_console_script_entry_point():
    raw = tomllib.loads(_PYPROJECT.read_text())
    entry = raw["project"]["scripts"]["icebreaker"]
    assert entry == "controller.__main__:main"
    from controller.__main__ import main
    assert callable(main)
