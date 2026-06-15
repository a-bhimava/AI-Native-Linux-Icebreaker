"""Tests for ``controller.config`` — TOML loader contract.

Covers tests 40-49b from the plan:

* 40-46  — well-formed load, missing file, parse errors, unknown
           backend, selected-section enforcement, D11 forbidden-key
           blacklist + allowlist.
* 47     — SecretRef.reveal() error format (D21).
* 48     — ControllerConfig + BackendConfig immutability (D10/D20).
* 49     — XDG path resolution + fallback.
* 49a    — D22 KeyRedactionFilter installed by load().
* 49b    — D21 BackendConfig.api_key is a SecretRef, not a str.
"""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from controller.backends import (
    BrainConfigError,
    KeyRedactionFilter,
    SecretRef,
)
from controller.config import (
    BackendConfig,
    ControllerConfig,
    _default_config_path,
    load,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "controller.toml"
    path.write_text(body)
    return path


_LOCAL_TOML = (
    '[qb]\nbackend = "local"\n\n'
    '[qb.local]\n'
    'model_id = "qwen-2.5-1.5b-instruct-q4_k_m"\n'
    'endpoint = "http://127.0.0.1:8081"\n'
    'max_tokens = 512\n'
    'timeout_seconds = 30\n'
)

_ANTHROPIC_TOML = (
    '[qb]\nbackend = "anthropic"\n\n'
    '[qb.anthropic]\n'
    'model = "claude-haiku-4-5"\n'
    'api_key_env = "ANTHROPIC_API_KEY"\n'
    'max_tokens = 1024\n'
    'timeout_seconds = 30\n'
)


@pytest.fixture(autouse=True)
def _clean_root_filters():
    """Strip any KeyRedactionFilter on the root logger before each test
    so 49a's "exactly one" assertion is order-independent."""
    root = logging.getLogger()
    pre = [f for f in root.filters if isinstance(f, KeyRedactionFilter)]
    for f in pre:
        root.removeFilter(f)
    yield
    post = [f for f in root.filters if isinstance(f, KeyRedactionFilter)]
    for f in post:
        root.removeFilter(f)


# ── 40: well-formed example loads ────────────────────────────────────────────


def test_load_well_formed_toml():
    example = (
        Path(__file__).parent.parent / "controller.toml.example"
    )
    cfg = load(example)
    assert isinstance(cfg, ControllerConfig)
    assert cfg.qb.name == "local"
    # `model` is the resolved display_name from the registry
    assert cfg.qb.model == "Qwen 2.5 1.5B Instruct"
    assert cfg.qb.model_id == "qwen-2.5-1.5b-instruct-q4_k_m"
    assert cfg.qb.draft_model_id == "qwen-2.5-coder-0.5b-instruct-q4_k_m"
    assert cfg.qb.transport == "http"
    assert cfg.qb.endpoint == "http://127.0.0.1:8081"
    assert cfg.qb.api_key is None  # local backend has no API key
    assert cfg.config_path == example.resolve()


def test_load_anthropic_toml_populates_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-x")  # pragma: allowlist secret
    cfg = load(_write_toml(tmp_path, _ANTHROPIC_TOML))
    assert cfg.qb.name == "anthropic"
    assert cfg.qb.endpoint is None
    assert cfg.qb.grammar_path is None
    assert isinstance(cfg.qb.api_key, SecretRef)
    assert cfg.qb.api_key.env_var_name == "ANTHROPIC_API_KEY"


# ── 41: missing file ─────────────────────────────────────────────────────────


def test_load_missing_file_raises_brainconfigerror(tmp_path):
    missing = tmp_path / "does-not-exist.toml"
    with pytest.raises(BrainConfigError) as exc_info:
        load(missing)
    msg = str(exc_info.value)
    assert str(missing) in msg
    assert "controller.toml.example" in msg  # bootstrap hint


# ── 42: bad TOML syntax surfaces line number ─────────────────────────────────


def test_load_bad_toml_syntax_raises_with_line_number(tmp_path):
    bad = _write_toml(
        tmp_path,
        '[qb]\nbackend = "local"\n\n'
        '[qb.local]\n'
        'model = "x"\n'
        'endpoint = "http://x"\n'
        'max_tokens = \n'
        'timeout_seconds = 30\n',
    )
    with pytest.raises(BrainConfigError) as exc_info:
        load(bad)
    msg = str(exc_info.value).lower()
    assert "toml parse failure" in msg
    assert "line" in msg


# ── 43: unknown backend value ────────────────────────────────────────────────


def test_load_rejects_unknown_backend(tmp_path):
    bad = _write_toml(
        tmp_path,
        '[qb]\nbackend = "cohere"\n',
    )
    with pytest.raises(BrainConfigError) as exc_info:
        load(bad)
    assert "cohere" in str(exc_info.value) or "enum" in str(exc_info.value).lower()


# ── 44: selected-backend-only section enforcement (D12) ──────────────────────


def test_load_requires_selected_backend_section(tmp_path):
    bad = _write_toml(
        tmp_path,
        '[qb]\nbackend = "anthropic"\n\n'
        '[qb.gemini]\nmodel = "gemini-2.0-flash"\n'
        'api_key_env = "GEMINI_API_KEY"\n'
        'max_tokens = 1\ntimeout_seconds = 1\n',
    )
    with pytest.raises(BrainConfigError) as exc_info:
        load(bad)
    msg = str(exc_info.value)
    assert "qb.anthropic" in msg


def test_load_allows_missing_unused_sections(monkeypatch, tmp_path):
    """`backend = "anthropic"` + only `[qb.anthropic]` populated → OK."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    cfg = load(_write_toml(tmp_path, _ANTHROPIC_TOML))
    assert cfg.qb.name == "anthropic"


# ── 45: forbidden TOML key substrings (D11 hardening) ────────────────────────


def test_load_rejects_forbidden_api_key_key_in_toml(tmp_path):
    """A literal `api_key = "sk-..."` is the failure mode D11 closes.
    The error must NOT echo the value back."""
    secret = "sk-ant-MUST-NOT-APPEAR-IN-ERROR-MESSAGE-Z9"  # pragma: allowlist secret
    bad = _write_toml(
        tmp_path,
        '[qb]\nbackend = "anthropic"\n\n'
        '[qb.anthropic]\n'
        'model = "claude-haiku-4-5"\n'
        f'api_key = "{secret}"\n'
        'max_tokens = 1024\ntimeout_seconds = 30\n',
    )
    with pytest.raises(BrainConfigError) as exc_info:
        load(bad)
    msg = str(exc_info.value)
    assert "api_key" in msg
    assert secret not in msg


@pytest.mark.parametrize(
    "key",
    ["password", "client_secret", "auth_token", "bearer_value"],
)
def test_load_rejects_other_forbidden_substrings(tmp_path, key):
    bad = _write_toml(
        tmp_path,
        '[qb]\nbackend = "local"\n\n'
        '[qb.local]\n'
        'model = "x"\nendpoint = "http://x"\n'
        f'{key} = "leak-value-DO-NOT-ECHO"\n'
        'max_tokens = 1\ntimeout_seconds = 1\n',
    )
    with pytest.raises(BrainConfigError) as exc_info:
        load(bad)
    msg = str(exc_info.value)
    assert key in msg
    assert "leak-value-DO-NOT-ECHO" not in msg


def test_load_does_not_flag_non_secret_token_suffix(monkeypatch, tmp_path):
    """`max_tokens` and `tokens_in` are legitimate; the word-boundary
    regex must not snag them. Otherwise the example TOML wouldn't load."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    cfg = load(_write_toml(tmp_path, _ANTHROPIC_TOML))
    assert cfg.qb.max_tokens == 1024


# ── 46: api_key_env IS allowed (allowlist exception) ─────────────────────────


def test_load_allows_api_key_env_in_toml(monkeypatch, tmp_path):
    """The whole point of `api_key_env` is to ship an env-var NAME via
    TOML. It MUST pass the forbidden-key filter."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    cfg = load(_write_toml(tmp_path, _ANTHROPIC_TOML))
    assert isinstance(cfg.qb.api_key, SecretRef)
    assert cfg.qb.api_key.env_var_name == "ANTHROPIC_API_KEY"


# ── 47: SecretRef.reveal() error format when env var missing ────────────────


def test_secret_ref_reveal_raises_with_env_var_name(monkeypatch):
    monkeypatch.delenv("ICEBREAKER_TEST_KEY_47", raising=False)
    s = SecretRef("ICEBREAKER_TEST_KEY_47")
    with pytest.raises(BrainConfigError) as exc_info:
        s.reveal()
    msg = str(exc_info.value)
    assert "ICEBREAKER_TEST_KEY_47" in msg


def test_secret_ref_reveal_does_not_log_value(monkeypatch, caplog):
    """Even if the env var IS set, simply revealing it must not write
    the value to any log handler."""
    secret = "sk-ant-must-not-log-Z9X"  # pragma: allowlist secret
    monkeypatch.setenv("ICEBREAKER_TEST_KEY_47B", secret)
    with caplog.at_level(logging.DEBUG):
        s = SecretRef("ICEBREAKER_TEST_KEY_47B")
        _ = s.reveal()  # discard
    assert secret not in caplog.text


# ── 48: config objects are frozen ────────────────────────────────────────────


def test_controller_config_is_frozen(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    cfg = load(_write_toml(tmp_path, _ANTHROPIC_TOML))
    with pytest.raises(FrozenInstanceError):
        cfg.qb = None  # type: ignore[misc]


def test_backend_config_is_frozen(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    cfg = load(_write_toml(tmp_path, _ANTHROPIC_TOML))
    with pytest.raises(FrozenInstanceError):
        cfg.qb.model = "different-model"  # type: ignore[misc]


def test_backend_config_does_not_allow_attribute_injection(monkeypatch, tmp_path):
    """Frozen dataclasses without `slots=True` still permit
    `__setattr__('new_attr', ...)` via direct __dict__ poke on some
    Python versions. Test that the dataclass freeze covers the
    documented surface."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    cfg = load(_write_toml(tmp_path, _ANTHROPIC_TOML))
    with pytest.raises(FrozenInstanceError):
        cfg.qb.injected_field = "bad"  # type: ignore[attr-defined]


# ── 49: XDG path resolution ──────────────────────────────────────────────────


def test_xdg_config_home_is_honored(monkeypatch, tmp_path):
    """When XDG_CONFIG_HOME is set, the default path lives under it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    expected = tmp_path / "icebreaker" / "controller.toml"
    assert _default_config_path() == expected


def test_xdg_fallback_to_home_dot_config(monkeypatch, tmp_path):
    """With XDG_CONFIG_HOME unset, the default path is
    ~/.config/icebreaker/controller.toml. We monkeypatch Path.home()
    so the test does not depend on the actual $HOME."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    expected = tmp_path / ".config" / "icebreaker" / "controller.toml"
    assert _default_config_path() == expected


def test_load_with_no_argument_reads_default_path(monkeypatch, tmp_path):
    """End-to-end: monkeypatch XDG_CONFIG_HOME so default points at a
    file we control, then call load() with no argument."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    target_dir = tmp_path / "icebreaker"
    target_dir.mkdir()
    target = target_dir / "controller.toml"
    target.write_text(_LOCAL_TOML)
    cfg = load()
    assert cfg.qb.name == "local"
    assert cfg.config_path == target.resolve()


# ── 49a: D22 root logger filter installed by load() ─────────────────────────


def test_load_installs_root_redaction_filter(tmp_path):
    """After load() returns, the root logger has exactly one
    KeyRedactionFilter attached (filters cleaned in fixture)."""
    load(_write_toml(tmp_path, _LOCAL_TOML))
    root = logging.getLogger()
    filters = [f for f in root.filters if isinstance(f, KeyRedactionFilter)]
    assert len(filters) == 1


def test_load_root_filter_install_is_idempotent(tmp_path):
    """Calling load() twice does not duplicate the filter."""
    p = _write_toml(tmp_path, _LOCAL_TOML)
    load(p)
    load(p)
    root = logging.getLogger()
    filters = [f for f in root.filters if isinstance(f, KeyRedactionFilter)]
    assert len(filters) == 1


# ── 49b: api_key is a SecretRef, not a str (D21) ────────────────────────────


def test_loaded_config_api_key_is_secretref_not_str(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")  # pragma: allowlist secret
    cfg = load(_write_toml(tmp_path, _ANTHROPIC_TOML))
    assert isinstance(cfg.qb.api_key, SecretRef)
    assert not isinstance(cfg.qb.api_key, str)


def test_local_config_api_key_is_none(tmp_path):
    """Local backend has no API key; the slot is None (not an empty
    string and not a SecretRef pointing at an unset var)."""
    cfg = load(_write_toml(tmp_path, _LOCAL_TOML))
    assert cfg.qb.api_key is None
