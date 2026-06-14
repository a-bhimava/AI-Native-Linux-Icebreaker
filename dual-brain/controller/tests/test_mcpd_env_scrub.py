"""Tests for mcpd subprocess environment scrubbing (M5.P1-sec, SF-7).

Covers:
  - API keys excluded from scrubbed env.
  - Safe vars (PATH, HOME, etc.) preserved.
  - MCPD_AUDIT_LOG and RUST_LOG additions work.
  - extra_env overrides work.
  - Whitelist is a frozenset (immutable).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from controller.mcpd_client import _SAFE_ENV_VARS, _scrubbed_env


def test_api_keys_excluded():
    fake_env = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "GEMINI_API_KEY": "AIzaSy-secret",
        "OPENAI_API_KEY": "sk-openai-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
    }
    with patch.dict(os.environ, fake_env, clear=True):
        env = _scrubbed_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_safe_vars_preserved():
    fake_env = {
        "PATH": "/usr/bin:/usr/local/bin",
        "HOME": "/home/testuser",
        "USER": "testuser",
        "LANG": "en_US.UTF-8",
        "TZ": "UTC",
        "SHELL": "/bin/bash",
        "TERM": "xterm-256color",
    }
    with patch.dict(os.environ, fake_env, clear=True):
        env = _scrubbed_env()
    assert env["PATH"] == "/usr/bin:/usr/local/bin"
    assert env["HOME"] == "/home/testuser"
    assert env["USER"] == "testuser"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["TZ"] == "UTC"
    assert env["SHELL"] == "/bin/bash"
    assert env["TERM"] == "xterm-256color"


def test_mcpd_audit_log_added():
    with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
        env = _scrubbed_env(audit_log="/tmp/audit.log")
    assert env["MCPD_AUDIT_LOG"] == "/tmp/audit.log"


def test_rust_log_added():
    with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
        env = _scrubbed_env(rust_log="debug")
    assert env["RUST_LOG"] == "debug"


def test_extra_env_added():
    with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
        env = _scrubbed_env(extra={"CUSTOM_VAR": "value"})
    assert env["CUSTOM_VAR"] == "value"


def test_extra_env_can_override_safe_vars():
    with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
        env = _scrubbed_env(extra={"PATH": "/custom/path"})
    assert env["PATH"] == "/custom/path"


def test_unknown_vars_excluded():
    fake_env = {
        "PATH": "/usr/bin",
        "RANDOM_VAR": "should_not_appear",
        "DATABASE_URL": "postgres://secret",
        "MY_CUSTOM_THING": "nope",
    }
    with patch.dict(os.environ, fake_env, clear=True):
        env = _scrubbed_env()
    assert "RANDOM_VAR" not in env
    assert "DATABASE_URL" not in env
    assert "MY_CUSTOM_THING" not in env
    assert env["PATH"] == "/usr/bin"


def test_empty_env_returns_minimal():
    with patch.dict(os.environ, {}, clear=True):
        env = _scrubbed_env()
    assert env == {}


def test_safe_env_vars_is_frozenset():
    assert isinstance(_SAFE_ENV_VARS, frozenset)


def test_all_params_together():
    fake_env = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "ANTHROPIC_API_KEY": "secret",
    }
    with patch.dict(os.environ, fake_env, clear=True):
        env = _scrubbed_env(
            audit_log="/var/log/mcpd.log",
            rust_log="info",
            extra={"EXTRA": "val"},
        )
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/user"
    assert env["MCPD_AUDIT_LOG"] == "/var/log/mcpd.log"
    assert env["RUST_LOG"] == "info"
    assert env["EXTRA"] == "val"
    assert "ANTHROPIC_API_KEY" not in env
