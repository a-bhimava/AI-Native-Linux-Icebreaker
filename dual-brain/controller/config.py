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

    The ``model`` field holds the *display name* (human-readable, used in
    audit rows). For API backends (anthropic / gemini), it doubles as
    the SDK model identifier. For the local backend it's resolved from
    ``model_id`` via the ``ModelRegistry`` at load time.

    ``model_id`` and ``draft_model_id`` are local-only — they reference
    entries in ``catalogue.toml`` and the registry resolves them to the
    on-disk GGUF path inside the ops scripts. Never used by API backends.

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
    # Local-only fields (None for API backends):
    model_id: str | None = None
    draft_model_id: str | None = None
    transport: str = "http"  # "http" today; "unix" reserved for Phase 6


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
        # Local backend uses the model registry — resolve model_id to
        # a display_name for audit rows. The on-disk file path is
        # resolved by the ops scripts, not by the Controller.
        # Inline import to avoid a circular dependency at module load.
        from .model_registry import (
            ModelRegistry,
            ModelRegistryError,
            UnknownModelError,
        )

        model_id = section["model_id"]
        draft_model_id = section.get("draft_model_id")
        grammar_path_str = section.get("grammar_path")
        transport = section.get("transport", "http")

        # Resolve display_name via registry (catalogue lookup only;
        # does not require the file to be on disk yet).
        paths = raw.get("paths", {})
        catalogue_path_str = paths.get(
            "catalogue_path",
            str(Path(__file__).parent / "catalogue.toml"),
        )
        search_dirs = [
            Path(d) for d in paths.get(
                "model_search_dirs",
                [str(Path(__file__).parent.parent.parent / "models")],
            )
        ]
        try:
            registry = ModelRegistry(
                catalogue_path=Path(catalogue_path_str).expanduser(),
                search_dirs=search_dirs,
            )
            entry = registry.get(model_id)
        except (ModelRegistryError, FileNotFoundError) as exc:
            raise BrainConfigError(
                f"local backend model_id {model_id!r}: {exc}"
            ) from None
        if entry.role != "qb":
            raise BrainConfigError(
                f"local backend model_id {model_id!r} has role "
                f"{entry.role!r}; expected 'qb'"
            )
        if draft_model_id is not None:
            try:
                draft_entry = registry.get(draft_model_id)
                if draft_entry.role != "draft":
                    raise BrainConfigError(
                        f"draft_model_id {draft_model_id!r} has role "
                        f"{draft_entry.role!r}; expected 'draft'"
                    )
            except UnknownModelError as exc:
                raise BrainConfigError(str(exc)) from None

        return BackendConfig(
            name=name,
            model=entry.display_name,
            max_tokens=section["max_tokens"],
            timeout_seconds=section["timeout_seconds"],
            endpoint=section["endpoint"],
            grammar_path=(
                Path(grammar_path_str) if grammar_path_str else None
            ),
            api_key=None,
            model_id=model_id,
            draft_model_id=draft_model_id,
            transport=transport,
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
