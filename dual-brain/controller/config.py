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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Optional

import jsonschema

from .backends import (
    BrainConfigError,
    SecretRef,
    install_root_redaction_filter,
)
from .keymap import Keymap, KeymapValidationError, load_keymap

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
class HitlConfig:
    lockout_seconds: int = 3    # INV-6 approve-button lockout
    timeout_seconds: int = 30   # decision timeout → auto-deny
    presenter: str = "terminal" # Phase 5 hook: swap for "gtk" or "web"
    trust_ttl_seconds: int = 0  # 0 = [T]rust disabled; >0 = grant lifetime in seconds


@dataclass(frozen=True)
class PromptsConfig:
    prompts_dir: str = ""       # empty = in-package controller/prompts/
    qb_local: str = ""          # per-backend override path; empty = use prompts_dir
    qb_anthropic: str = ""
    qb_gemini: str = ""
    qb_openai: str = ""
    pb: str = ""
    qb_verifier: str = ""


class PromptLoader:
    """Loads system prompt text from disk. Thread-safe reads; call reload() to hot-swap."""

    def __init__(self, cfg: PromptsConfig) -> None:
        self._cfg = cfg
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str:
        """Return prompt text for name (qb_local|qb_anthropic|qb_gemini|pb|qb_verifier)."""
        if name not in self._cache:
            self._cache[name] = self._load(name)
        return self._cache[name]

    def reload(self) -> None:
        self._cache.clear()

    def _load(self, name: str) -> str:
        override = getattr(self._cfg, name, "")
        if override:
            return Path(override).expanduser().read_text(encoding="utf-8").strip()
        base = Path(self._cfg.prompts_dir).expanduser()
        path = base / f"{name}.txt"
        if not path.exists():
            raise FileNotFoundError(
                f"Prompt '{name}' not found at {path}. "
                "Set [prompts] prompts_dir or a per-backend override in controller.toml."
            )
        return path.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class RunConfig:
    mcpd_binary: str = "src/mcpd/target/release/mcpd"
    audit_log: str = "~/.local/state/icebreaker/controller-audit.log"
    pb_endpoint: str = "http://127.0.0.1:8080"
    pb_model_id: str = "run7_cot"
    pb_max_tokens: int = 256
    pb_timeout_seconds: int = 10
    mcpd_timeout_seconds: float = 10.0
    qb_max_retries: int = 3
    mcpd_schemas_dir: str = ""  # empty = auto-detect from mcpd_binary path
    pb_transport: str = "http"  # "http" or "unix"; used by _build_pb()


@dataclass(frozen=True)
class SessionConfig:
    session_ttl_seconds: int = 1800       # inactivity timeout; 0 = disabled
    max_turns: int = 50                   # P2-F20 memory bound
    history_path: str = "~/.local/state/icebreaker/repl_history"
    ephemeral_history: bool = True      # True = in-memory only (SF-8 safe default)
    show_spinner: bool = True
    show_progress: bool = True            # step-by-step pipeline progress
    stream_output: bool = True            # token streaming for QB summarisation
    color: str = "auto"                   # "auto" | "always" | "never"
    prompt_prefix: str = "icebreaker"     # shown as `(N) [backend] prefix > `
    max_tool_output_lines: int = 40       # QB summarisation truncation


@dataclass(frozen=True)
class RiskConfig:
    strategy: str = "rules"     # pluggable classifier: "rules" (default)


@dataclass(frozen=True)
class Tier2Config:
    enabled: bool = False       # off by default; set True to enable Tier-2 review
    strategy: str = "llm"       # "llm" | "rule" | "none"
    max_retries: int = 2


@dataclass(frozen=True)
class CostConfig:
    session_ceiling_usd: float = 0.0    # 0 = no ceiling
    warn_fraction: float = 0.8          # alert at this fraction of ceiling


@dataclass(frozen=True)
class LimitsConfig:
    max_input_chars: int = 4000
    max_turns_per_min: int = 30


@dataclass(frozen=True)
class UndoConfig:
    enabled: bool = False               # BP-2: off by default
    max_history: int = 10


@dataclass(frozen=True)
class VerifierConfig:
    votes: int = 1                      # 1 = current single-call behavior (BP-2)
    require: int = 0                    # 0 = majority; >0 = exact threshold
    parallel: bool = True               # parallel calls via ThreadPoolExecutor
    timeout_seconds: int = 30


@dataclass(frozen=True)
class DaemonConfig:
    socket_path: str = "~/.local/state/icebreaker/controller.sock"
    pid_file: str = "~/.local/state/icebreaker/controller.pid"
    max_connections: int = 1


@dataclass(frozen=True)
class ControllerConfig:
    qb: BackendConfig
    hitl: HitlConfig
    prompts: PromptsConfig
    session: SessionConfig
    run: RunConfig
    config_path: Path
    keymap: Keymap = field(default_factory=Keymap)
    risk: RiskConfig = field(default_factory=RiskConfig)
    tier2: Tier2Config = field(default_factory=Tier2Config)
    cost: CostConfig = field(default_factory=CostConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    undo: UndoConfig = field(default_factory=UndoConfig)
    verifier: VerifierConfig = field(default_factory=VerifierConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)


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

        # Resolve relative grammar_path relative to the controller package
        # so the value is cwd-independent.
        grammar_path_resolved: Path | None = None
        if grammar_path_str:
            gp = Path(grammar_path_str).expanduser()
            if not gp.is_absolute():
                gp = (Path(__file__).parent / gp).resolve()
            grammar_path_resolved = gp

        return BackendConfig(
            name=name,
            model=entry.display_name,
            max_tokens=section["max_tokens"],
            timeout_seconds=section["timeout_seconds"],
            endpoint=section["endpoint"],
            grammar_path=grammar_path_resolved,
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


def _build_hitl_config(raw: dict) -> HitlConfig:
    section = raw.get("hitl", {})
    return HitlConfig(
        lockout_seconds=section.get("lockout_seconds", 3),
        timeout_seconds=section.get("timeout_seconds", 30),
        presenter=section.get("presenter", "terminal"),
        trust_ttl_seconds=section.get("trust_ttl_seconds", 0),
    )


def _build_prompts_config(raw: dict) -> PromptsConfig:
    section = raw.get("prompts", {})
    prompts_dir = section.get("prompts_dir", "")
    if not prompts_dir:
        # Default: in-package prompts/ directory alongside this file.
        # Works for dev checkout and for Phase 6 system-wide installs.
        prompts_dir = str(Path(__file__).parent / "prompts")
    return PromptsConfig(
        prompts_dir=prompts_dir,
        qb_local=section.get("qb_local", ""),
        qb_anthropic=section.get("qb_anthropic", ""),
        qb_gemini=section.get("qb_gemini", ""),
        qb_openai=section.get("qb_openai", ""),
        pb=section.get("pb", ""),
        qb_verifier=section.get("qb_verifier", ""),
    )


def _build_run_config(raw: dict) -> RunConfig:
    section = raw.get("run", {})
    return RunConfig(
        mcpd_binary=section.get("mcpd_binary", "src/mcpd/target/release/mcpd"),
        audit_log=section.get("audit_log", "~/.local/state/icebreaker/controller-audit.log"),
        pb_endpoint=section.get("pb_endpoint", "http://127.0.0.1:8080"),
        pb_model_id=section.get("pb_model_id", "run7_cot"),
        pb_max_tokens=section.get("pb_max_tokens", 256),
        pb_timeout_seconds=section.get("pb_timeout_seconds", 10),
        mcpd_timeout_seconds=section.get("mcpd_timeout_seconds", 10.0),
        qb_max_retries=section.get("qb_max_retries", 3),
        mcpd_schemas_dir=section.get("mcpd_schemas_dir", ""),
        pb_transport=section.get("pb_transport", "http"),
    )


def _build_session_config(raw: dict) -> SessionConfig:
    section = raw.get("session", {})
    return SessionConfig(
        session_ttl_seconds=section.get("session_ttl_seconds", 1800),
        max_turns=section.get("max_turns", 50),
        history_path=section.get("history_path", "~/.local/state/icebreaker/repl_history"),
        ephemeral_history=section.get("ephemeral_history", True),
        show_spinner=section.get("show_spinner", True),
        show_progress=section.get("show_progress", True),
        stream_output=section.get("stream_output", True),
        color=section.get("color", "auto"),
        prompt_prefix=section.get("prompt_prefix", "icebreaker"),
        max_tool_output_lines=section.get("max_tool_output_lines", 40),
    )


def _build_risk_config(raw: dict) -> RiskConfig:
    section = raw.get("risk", {})
    return RiskConfig(
        strategy=section.get("strategy", "rules"),
    )


def _build_tier2_config(raw: dict) -> Tier2Config:
    section = raw.get("tier2", {})
    return Tier2Config(
        enabled=section.get("enabled", False),
        strategy=section.get("strategy", "llm"),
        max_retries=section.get("max_retries", 2),
    )


def _build_cost_config(raw: dict) -> CostConfig:
    section = raw.get("cost", {})
    return CostConfig(
        session_ceiling_usd=section.get("session_ceiling_usd", 0.0),
        warn_fraction=section.get("warn_fraction", 0.8),
    )


def _build_limits_config(raw: dict) -> LimitsConfig:
    section = raw.get("limits", {})
    return LimitsConfig(
        max_input_chars=section.get("max_input_chars", 4000),
        max_turns_per_min=section.get("max_turns_per_min", 30),
    )


def _build_undo_config(raw: dict) -> UndoConfig:
    section = raw.get("undo", {})
    return UndoConfig(
        enabled=section.get("enabled", False),
        max_history=section.get("max_history", 10),
    )


def _build_verifier_config(raw: dict) -> VerifierConfig:
    section = raw.get("verifier", {})
    return VerifierConfig(
        votes=section.get("votes", 1),
        require=section.get("require", 0),
        parallel=section.get("parallel", True),
        timeout_seconds=section.get("timeout_seconds", 30),
    )


def _build_daemon_config(raw: dict) -> DaemonConfig:
    section = raw.get("daemon", {})
    return DaemonConfig(
        socket_path=section.get("socket_path", "~/.local/state/icebreaker/controller.sock"),
        pid_file=section.get("pid_file", "~/.local/state/icebreaker/controller.pid"),
        max_connections=section.get("max_connections", 1),
    )


def _build_keymap(raw: dict) -> Keymap:
    section = raw.get("keymap")
    try:
        return load_keymap(section)
    except KeymapValidationError as exc:
        raise BrainConfigError(str(exc)) from None


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
    hitl = _build_hitl_config(raw)
    prompts = _build_prompts_config(raw)
    session = _build_session_config(raw)
    run = _build_run_config(raw)
    keymap = _build_keymap(raw)
    risk = _build_risk_config(raw)
    tier2 = _build_tier2_config(raw)
    cost = _build_cost_config(raw)
    limits = _build_limits_config(raw)
    undo = _build_undo_config(raw)
    verifier = _build_verifier_config(raw)
    daemon = _build_daemon_config(raw)
    return ControllerConfig(
        qb=qb, hitl=hitl, prompts=prompts, session=session, run=run,
        config_path=resolved.resolve(), keymap=keymap, risk=risk, tier2=tier2,
        cost=cost, limits=limits, undo=undo, verifier=verifier, daemon=daemon,
    )
