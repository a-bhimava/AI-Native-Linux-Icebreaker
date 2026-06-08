"""Controller configuration loader.

Reads ``$XDG_CONFIG_HOME/icebreaker/controller.toml`` (or
``~/.config/icebreaker/controller.toml``) and produces a frozen
``ControllerConfig``. Defends against three failure modes:

* **Disk-based secret leakage (D11 hardening)** — rejects any TOML key
  matching ``/api_key|secret|token|password|bearer/i`` before parsing
  the value. The ``api_key_env`` key (env var NAME only) is allowlisted.
* **Schema drift** — validates the parsed TOML against
  ``schemas/controller_config.json`` (Draft-07). Selected-backend-only
  enforcement: ``[qb.<backend>]`` is required only for the value of
  ``qb.backend``; the other sections may be present or absent.
* **Long-lived raw API keys (D21)** — credentials are wrapped in
  ``SecretRef`` immediately on construction and never enter
  ``BackendConfig`` as a plain string.

Side effect of the first ``load()`` call: installs
``KeyRedactionFilter`` on the root logger (D22). Idempotent.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import jsonschema

from .backends import (
    BrainConfigError,
    SecretRef,
    install_root_redaction_filter,
)

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


_SCHEMA_PATH: Final[Path] = (
    Path(__file__).parent / "schemas" / "controller_config.json"
)

# Word-boundary match: each forbidden word must be a snake_case
# component (preceded by start-of-string or `_`, followed by end or `_`).
# Catches `api_key`, `auth_token`, `client_secret`, `bearer_value`
# without snagging `max_tokens`, `tokens_in`, `endpoint`.
_FORBIDDEN_TOML_KEY_REGEX: Final = re.compile(
    r"(?:^|_)(api_key|secret|token|password|bearer)(?:_|$)",
    re.IGNORECASE,
)

_TOML_KEY_ALLOWLIST: Final = frozenset({"api_key_env"})


@dataclass(frozen=True)
class BackendConfig:
    """Per-backend resolved config.

    ``api_key`` is a ``SecretRef`` for API backends, ``None`` for local.
    The raw key value is never stored on this object.
    """

    name: str
    model: str
    max_tokens: int
    timeout_seconds: int
    endpoint: str | None = None
    grammar_path: Path | None = None
    api_key: SecretRef | None = None


@dataclass(frozen=True)
class ControllerConfig:
    qb: BackendConfig
    config_path: Path


def _default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "icebreaker" / "controller.toml"


def _check_forbidden_keys(node: Any, path: str = "") -> None:
    """Recursively reject any forbidden TOML key. Raises ``BrainConfigError``.

    Runs on the parsed TOML dict BEFORE jsonschema validation so the
    error message never echoes a credential-shaped value.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            if key in _TOML_KEY_ALLOWLIST:
                _check_forbidden_keys(value, f"{path}/{key}")
                continue
            match = _FORBIDDEN_TOML_KEY_REGEX.search(key)
            if match:
                raise BrainConfigError(
                    f"forbidden TOML key {key!r} at {path or '/'}: "
                    f"contains snake_case component {match.group(1)!r}. "
                    "Secrets must come from environment variables; use a "
                    "*_env key whose value is the env var NAME."
                )
            _check_forbidden_keys(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _check_forbidden_keys(value, f"{path}[{index}]")


def _load_schema() -> dict:
    with _SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _validate_structure(raw: dict) -> None:
    schema = _load_schema()
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(raw), key=lambda exc: exc.path)
    if errors:
        first = errors[0]
        raise BrainConfigError(
            f"controller config schema violation at "
            f"{list(first.absolute_path)}: {first.message}"
        )


def _build_backend_config(raw: dict) -> BackendConfig:
    qb = raw["qb"]
    name = qb["backend"]
    section = qb.get(name)
    if section is None:
        raise BrainConfigError(
            f"missing required section [qb.{name}] for backend "
            f"{name!r}. Either add the section or change `qb.backend`."
        )

    if name == "local":
        grammar_path_str = section.get("grammar_path")
        return BackendConfig(
            name=name,
            model=section["model"],
            max_tokens=section["max_tokens"],
            timeout_seconds=section["timeout_seconds"],
            endpoint=section["endpoint"],
            grammar_path=(
                Path(grammar_path_str) if grammar_path_str else None
            ),
            api_key=None,
        )

    return BackendConfig(
        name=name,
        model=section["model"],
        max_tokens=section["max_tokens"],
        timeout_seconds=section["timeout_seconds"],
        api_key=SecretRef(section["api_key_env"]),
    )


def load(path: Path | None = None) -> ControllerConfig:
    """Load and validate the controller config.

    Side effect on first call: installs ``KeyRedactionFilter`` on the
    root logger (D22). Idempotent.

    Raises ``BrainConfigError`` on:

    * missing file
    * TOML parse failure (with line number where the parser reported one)
    * forbidden TOML key (D11 hardening)
    * JSON-Schema violation
    * missing ``[qb.<backend>]`` section for the selected backend
    """
    install_root_redaction_filter()

    resolved = path or _default_config_path()
    if not resolved.exists():
        raise BrainConfigError(
            f"controller config not found at {resolved}. "
            "Bootstrap with: cp dual-brain/controller/"
            "controller.toml.example "
            "~/.config/icebreaker/controller.toml"
        )

    try:
        with resolved.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise BrainConfigError(
            f"TOML parse failure in {resolved}: {exc}"
        ) from None

    _check_forbidden_keys(raw)
    _validate_structure(raw)
    qb = _build_backend_config(raw)
    return ControllerConfig(qb=qb, config_path=resolved.resolve())
