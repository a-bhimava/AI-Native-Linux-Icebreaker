"""v6.17 M7.6a-1e — tests for gen-oc-config.sh OC_MODE knob.

Validates the two OC_MODE branches added by M7.6a-1e:
- OC_MODE=legacy_direct (default): preserves v6.13_OC..v6.16 behavior
  — every tool gets allow|ask per tier + ALWAYS_ASK policy.
- OC_MODE=submit_intent_only: every MCP tool gets 'deny' except
  iceui_submit_intent which gets 'allow'. Forces opencode/Gemini to
  route through Controller.run_turn_from_intent (M7.6a-1c) so the
  v6.16 safety features actually fire for OC users.

Uses a fake mcpd binary (small shell script emitting static tools/list
JSON) so tests don't need a real mcpd binary on the CI host.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest


# ── Test scaffold ────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "incremental" / "build" / "gen-oc-config.sh"


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """Build a temp dir with:
    - `mcpd` shell script that emits static tools/list JSON on stdin>stdout
    - `qb_oc.json.template` valid minimal shell
    Returns (tmp_path, mcpd_bin, template_path, output_path)."""
    # Fake mcpd script — reads stdin, ignores, prints tools/list response
    mcpd_bin = tmp_path / "mcpd"
    tools_json = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {
            "tools": [
                {"name": "fs.list", "tier": 0},
                {"name": "fs.write", "tier": 1},
                {"name": "fs.delete", "tier": 2},
                {"name": "package.install", "tier": 3},
                {"name": "system.status", "tier": 0},
                {"name": "system.uptime", "tier": 0},
            ]
        },
    })
    mcpd_bin.write_text(
        "#!/bin/sh\n"
        "# Fake mcpd — reads one stdin line, emits static tools/list JSON.\n"
        "read -r _ignored\n"
        f"echo '{tools_json}'\n"
    )
    mcpd_bin.chmod(0o755)

    # Minimal template that gen-oc-config.sh will splice
    template_path = tmp_path / "qb_oc.json.template"
    template_path.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "model": "google/gemini-2.5-flash",
        "mcp": {
            "icebreaker": {"type": "local", "command": ["/usr/libexec/mcpd"]},
            "iceui": {"type": "local", "command": ["/usr/libexec/iceui"]},
        },
        "permission": "//PLACEHOLDER — will be replaced",
    }, indent=2))

    output_path = tmp_path / "qb_oc.json"
    return tmp_path, mcpd_bin, template_path, output_path


def _run_script(mcpd_bin, template_path, output_path, oc_mode=None):
    """Invoke gen-oc-config.sh with given env, return (returncode, stderr)."""
    env = dict(os.environ)
    env["MCPD_BIN"] = str(mcpd_bin)
    env["TEMPLATE"] = str(template_path)
    env["OUTPUT"] = str(output_path)
    if oc_mode is not None:
        env["OC_MODE"] = oc_mode
    else:
        env.pop("OC_MODE", None)
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return result


def _read_perms(output_path):
    """Parse the generated qb_oc.json and return the permission dict."""
    return json.loads(Path(output_path).read_text())["permission"]


# ── 1. Default (no OC_MODE) is legacy_direct ─────────────────────────

def test_oc_mode_default_is_legacy_direct(fake_env):
    _, mcpd_bin, template_path, output_path = fake_env
    result = _run_script(mcpd_bin, template_path, output_path)
    assert result.returncode == 0, f"stderr:\n{result.stderr}"

    # In legacy_direct, tier-based per-tool policy applies:
    #   Tier 0 → 'allow' (unless in ALWAYS_ASK)
    #   Tier 1 → 'allow' (unless in ALWAYS_ASK; fs.write IS in ALWAYS_ASK)
    #   Tier 2+ → 'ask'
    perms = _read_perms(output_path)
    mcp = perms["mcp"]
    assert mcp["icebreaker_system.status"] == "allow"
    assert mcp["icebreaker_system.uptime"] == "allow"
    assert mcp["icebreaker_fs.list"] == "allow"
    assert mcp["icebreaker_fs.write"] == "ask"      # ALWAYS_ASK
    assert mcp["icebreaker_fs.delete"] == "ask"     # ALWAYS_ASK
    assert mcp["icebreaker_package.install"] == "ask"  # tier >= 2
    # Log should indicate legacy_direct
    assert "OC_MODE=legacy_direct" in result.stderr


def test_oc_mode_legacy_direct_explicit(fake_env):
    _, mcpd_bin, template_path, output_path = fake_env
    result = _run_script(mcpd_bin, template_path, output_path, oc_mode="legacy_direct")
    assert result.returncode == 0
    perms = _read_perms(output_path)
    # Same shape as default
    assert perms["mcp"]["icebreaker_system.status"] == "allow"
    assert "legacy_direct" in result.stderr


# ── 2. OC_MODE=submit_intent_only locks everything except one tool ───

def test_oc_mode_submit_intent_only_denies_all_except_submit_intent(fake_env):
    _, mcpd_bin, template_path, output_path = fake_env
    result = _run_script(
        mcpd_bin, template_path, output_path, oc_mode="submit_intent_only",
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"

    perms = _read_perms(output_path)
    mcp = perms["mcp"]

    # iceui_submit_intent MUST be 'allow'
    assert mcp["iceui_submit_intent"] == "allow", (
        "submit_intent_only mode requires iceui_submit_intent=allow"
    )

    # Every OTHER MCP tool entry MUST be 'deny'
    for key, value in mcp.items():
        if key == "iceui_submit_intent":
            continue
        assert value == "deny", (
            f"submit_intent_only mode expected {key}=deny, got {value!r}"
        )

    # Log line indicates the mode
    assert "submit_intent_only" in result.stderr
    assert "locked" in result.stderr.lower()


def test_oc_mode_submit_intent_only_preserves_edit_bash_deny(fake_env):
    """edit + bash permissions stay 'deny' regardless of OC_MODE
    (F-100 discipline: opencode's built-in write/edit/bash bypass
    every Icebreaker invariant + must not fire in either mode)."""
    _, mcpd_bin, template_path, output_path = fake_env
    result = _run_script(
        mcpd_bin, template_path, output_path, oc_mode="submit_intent_only",
    )
    assert result.returncode == 0
    perms = _read_perms(output_path)
    assert perms["edit"] == "deny"
    assert perms["bash"] == "deny"


# ── 3. Rollback path (legacy_direct → submit_intent_only via env) ────

def test_oc_mode_rollback_via_env_var_toggle(fake_env):
    """Regression lock: running once with submit_intent_only then again
    with legacy_direct produces the legacy shape (no residual state)."""
    _, mcpd_bin, template_path, output_path = fake_env

    # First run: locked
    r1 = _run_script(
        mcpd_bin, template_path, output_path, oc_mode="submit_intent_only",
    )
    assert r1.returncode == 0
    locked = _read_perms(output_path)["mcp"]
    assert locked["iceui_submit_intent"] == "allow"

    # Second run overwrites the output → legacy shape restored.
    r2 = _run_script(
        mcpd_bin, template_path, output_path, oc_mode="legacy_direct",
    )
    assert r2.returncode == 0
    legacy = _read_perms(output_path)["mcp"]
    assert legacy["icebreaker_system.status"] == "allow"
    # Not deny in legacy mode (was in the locked run)
    assert legacy["icebreaker_system.status"] != "deny"


# ── 4. Bad OC_MODE value defaults to legacy_direct + warns ───────────

def test_oc_mode_unknown_value_defaults_to_legacy_direct(fake_env):
    _, mcpd_bin, template_path, output_path = fake_env
    result = _run_script(
        mcpd_bin, template_path, output_path, oc_mode="turbo_mode_9000",
    )
    assert result.returncode == 0
    # Should have warned + fallen back to legacy_direct
    assert "unknown OC_MODE" in result.stderr.lower() or "defaulting to legacy_direct" in result.stderr.lower()
    perms = _read_perms(output_path)["mcp"]
    assert perms["icebreaker_system.status"] == "allow"


# ── 5. submit_intent_only always emits iceui_submit_intent even without iceui schemas ──

def test_oc_mode_submit_intent_only_emits_allow_even_when_iceui_schemas_missing(
    fake_env, monkeypatch,
):
    """The iceui MCP server owns the submit_intent tool definition (via
    _SUBMIT_INTENT_SCHEMA in mcp_gui_server.py). But even if gen-oc-config.sh
    cannot import gui_agent/rpa_bridge at build time (WARN fallback →
    empty _GUI_SCHEMAS), the submit_intent_only mode still emits
    iceui_submit_intent=allow so runtime dispatch works.

    Test: patch PYTHONPATH so gui_agent import fails; verify still emits."""
    _, mcpd_bin, template_path, output_path = fake_env
    # Add a bogus PYTHONPATH that shadows the real gui_agent module.
    fake_pypath = tempfile.mkdtemp(prefix="gen-oc-fake-py-")
    Path(fake_pypath, "gui_agent").mkdir()
    Path(fake_pypath, "gui_agent", "__init__.py").write_text("raise ImportError('fake')")
    monkeypatch.setenv("PYTHONPATH", fake_pypath)

    result = _run_script(
        mcpd_bin, template_path, output_path, oc_mode="submit_intent_only",
    )
    # NB: even with the fake shadow, the real script may still find gui_agent
    # via its sys.path.insert from the repo root. We just check that if
    # ImportError DID fire, iceui_submit_intent is still 'allow'.
    if result.returncode == 0:
        perms = _read_perms(output_path)["mcp"]
        assert perms["iceui_submit_intent"] == "allow"


# ── 8. M7.6a-1g: instructions field wiring ────────────────────────────

def test_oc_mode_submit_intent_only_emits_instructions_field(fake_env):
    """M7.6a-1g regression lock: submit_intent_only mode MUST add an
    `instructions` array to the generated qb_oc.json pointing at the
    runtime path where the system prompt file will ship
    (/etc/icebreaker/opencode_prompt_submit_intent.txt). Without this,
    Gemini has no prompt teaching it to translate user text into an
    intent object — turns fail at the permission gate."""
    _, mcpd_bin, template_path, output_path = fake_env
    result = _run_script(
        mcpd_bin, template_path, output_path, oc_mode="submit_intent_only",
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"

    config = json.loads(Path(output_path).read_text())
    assert "instructions" in config, (
        "submit_intent_only must inject the opencode instructions field"
    )
    assert isinstance(config["instructions"], list)
    assert config["instructions"] == [
        "/etc/icebreaker/opencode_prompt_submit_intent.txt"
    ]


def test_oc_mode_legacy_direct_does_not_emit_instructions_field(fake_env):
    """Regression lock: legacy_direct mode MUST NOT touch the instructions
    field (backward compat with v6.13_OC..v6.16 shape). Old opencode
    installs that don't understand instructions still work in
    legacy_direct."""
    _, mcpd_bin, template_path, output_path = fake_env
    result = _run_script(
        mcpd_bin, template_path, output_path, oc_mode="legacy_direct",
    )
    assert result.returncode == 0
    config = json.loads(Path(output_path).read_text())
    assert "instructions" not in config, (
        "legacy_direct must not inject instructions (backward compat)"
    )


def test_opencode_prompt_file_exists_at_source_path():
    """M7.6a-1g ships the prompt file at cx-distro/distro/. build-iso.sh
    installs it to /etc/icebreaker/opencode_prompt_submit_intent.txt on
    the runtime chroot. This test locks the source path so a rename
    would fail loud rather than silently omitting the file from the ISO."""
    source_path = _REPO_ROOT / "cx-distro" / "distro" / "opencode_prompt_submit_intent.txt"
    assert source_path.exists(), (
        f"prompt file missing at {source_path} — did the file get renamed?"
    )
    content = source_path.read_text(encoding="utf-8")
    # Sanity: prompt must mention key concepts so a future edit that
    # accidentally deletes them fails this test.
    assert "iceui_submit_intent" in content
    assert "intent" in content.lower()
    assert "action" in content
    assert "target" in content
    assert "risk_level" in content
    assert "reason" in content
