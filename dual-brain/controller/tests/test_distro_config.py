"""Tests for the cx-distro production controller.toml and support files.

Validates that the distro config passes schema validation, uses UNIX
transport, references correct catalogue model IDs, and that the
locations.env and CLI wrapper are well-formed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from controller.config import _load_raw_toml

_CX_DIR = Path(__file__).parent.parent.parent.parent / "cx-distro"
_DISTRO_CONFIG = _CX_DIR / "distro" / "controller.toml"
_DISTRO_LOCATIONS = _CX_DIR / "distro" / "locations.env"
_DISTRO_CLI = _CX_DIR / "distro" / "icebreaker-cli"
_CATALOGUE = Path(__file__).parent.parent / "catalogue.toml"


def _skip_if_no_cx_distro():
    if not _CX_DIR.exists():
        pytest.skip("cx-distro/ not present")


@pytest.fixture(autouse=True)
def _require_cx_distro():
    _skip_if_no_cx_distro()


def test_distro_config_loads_successfully():
    raw = _load_raw_toml(_DISTRO_CONFIG)
    assert raw["qb"]["backend"] in ("local", "gemini", "openai", "anthropic")


def test_distro_config_uses_unix_transport_for_pb():
    raw = _load_raw_toml(_DISTRO_CONFIG)
    assert raw["run"]["pb_transport"] == "unix"


def test_distro_config_local_qb_uses_unix_transport():
    raw = _load_raw_toml(_DISTRO_CONFIG)
    if raw["qb"]["backend"] != "local":
        pytest.skip("QB backend is not local")
    assert raw["qb"]["local"]["transport"] == "unix"


def test_distro_config_all_paths_absolute():
    raw = _load_raw_toml(_DISTRO_CONFIG)
    path_fields = [
        ("run", "mcpd_binary"),
        ("run", "mcpd_schemas_dir"),
        ("run", "audit_log"),
        ("run", "pb_endpoint"),
        ("paths", "catalogue_path"),
        ("prompts", "prompts_dir"),
        ("daemon", "socket_path"),
        ("daemon", "pid_file"),
    ]
    for section, key in path_fields:
        value = raw.get(section, {}).get(key, "")
        if not value:
            continue
        assert value.startswith("/") or value.startswith("unix://"), \
            f"[{section}].{key} = {value!r} is not an absolute path"


def test_distro_config_pb_model_id_in_catalogue():
    raw = _load_raw_toml(_DISTRO_CONFIG)
    cat = tomllib.loads(_CATALOGUE.read_text())
    catalogue_ids = {m["id"] for m in cat["model"]}
    pb_model_id = raw["run"]["pb_model_id"]
    assert pb_model_id in catalogue_ids, f"PB model_id {pb_model_id!r} not in catalogue"


def test_distro_config_local_qb_model_ids_in_catalogue():
    raw = _load_raw_toml(_DISTRO_CONFIG)
    if raw["qb"]["backend"] != "local":
        pytest.skip("QB backend is not local")
    cat = tomllib.loads(_CATALOGUE.read_text())
    catalogue_ids = {m["id"] for m in cat["model"]}

    qb_model_id = raw["qb"]["local"]["model_id"]
    assert qb_model_id in catalogue_ids, f"QB model_id {qb_model_id!r} not in catalogue"

    draft_id = raw["qb"]["local"].get("draft_model_id")
    if draft_id:
        assert draft_id in catalogue_ids, f"draft_model_id {draft_id!r} not in catalogue"


def test_distro_config_gemini_qb_has_required_fields():
    raw = _load_raw_toml(_DISTRO_CONFIG)
    if raw["qb"]["backend"] != "gemini":
        pytest.skip("QB backend is not gemini")
    gemini = raw["qb"]["gemini"]
    assert "model" in gemini, "gemini section missing 'model'"
    assert "api_key_env" in gemini, "gemini section missing 'api_key_env'"


def test_distro_config_pb_endpoint_is_unix_socket():
    raw = _load_raw_toml(_DISTRO_CONFIG)
    pb_endpoint = raw["run"]["pb_endpoint"]
    assert pb_endpoint.startswith("unix://"), f"PB endpoint not unix: {pb_endpoint}"


def test_distro_config_local_qb_endpoint_is_unix_socket():
    raw = _load_raw_toml(_DISTRO_CONFIG)
    if raw["qb"]["backend"] != "local":
        pytest.skip("QB backend is not local")
    qb_endpoint = raw["qb"]["local"]["endpoint"]
    assert qb_endpoint.startswith("unix://"), f"QB endpoint not unix: {qb_endpoint}"


def test_distro_locations_env_pure_keyvalue():
    text = _DISTRO_LOCATIONS.read_text()
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert "$(" not in stripped, f"line {lineno}: shell expansion $( in locations.env"
        assert "${" not in stripped, f"line {lineno}: shell expansion ${{ in locations.env"
        assert "`" not in stripped, f"line {lineno}: backtick in locations.env"
        assert re.match(r'^[A-Z_][A-Z0-9_]*=', stripped), \
            f"line {lineno}: not KEY=VALUE format: {stripped!r}"


def test_distro_cli_wrapper_is_posix_sh():
    text = _DISTRO_CLI.read_text()
    lines = text.splitlines()
    assert lines[0] == "#!/bin/sh", f"shebang is {lines[0]!r}, expected #!/bin/sh"
    assert any("exec " in line for line in lines), "CLI wrapper must use exec"
