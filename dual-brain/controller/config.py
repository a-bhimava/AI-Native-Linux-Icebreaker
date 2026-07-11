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

_SYSTEM_CONFIG_PATH: Final[Path] = Path("/etc/icebreaker/controller.toml")


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
    # F-28: colon-joined extra fs read roots passed to mcpd via
    # MCPD_FS_READ_ROOTS. Populated from controller.toml [mcpd.fs] read_roots
    # (list of absolute paths). Landlock allows these at the kernel level and
    # userspace fs.list/fs.read validate() accepts them as additional roots.
    mcpd_fs_read_roots: str = ""
    # Phase 6 Scope B — user-controllable timeouts previously hardcoded.
    # F-25 rationale for the 600 s turn timeout ceiling still applies:
    # Rosetta 2 / cross-arch emulation needs ~10x native to complete a
    # PB inference turn. Bounds enforced by JSON Schema (30-7200).
    turn_timeout_seconds: float = 600.0
    # Hardware probe subprocess timeouts (sysctl / nvidia-smi / rocm-smi).
    # 5 s was a magic number; making it configurable lets slow VM disks
    # (encrypted rootfs, network-attached storage) work.
    model_probe_timeout_seconds: float = 5.0


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
    # Phase 6 Scope B — shell context caps previously hardcoded module
    # constants in session.py (_MAX_CONTEXT_LEN + _MAX_RECENT). Hot-reload
    # eligible: change takes effect on next turn without daemon restart.
    max_shell_context_chars: int = 512
    max_recent_commands: int = 5


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
    # F-49: retry strategy on rejection. See verifier.py::VerifierConfig for
    # the full description of each mode.
    retry_mode: str = "on_call_failed_only"


@dataclass(frozen=True)
class DaemonConfig:
    socket_path: str = "~/.local/state/icebreaker/controller.sock"
    pid_file: str = "~/.local/state/icebreaker/controller.pid"
    max_connections: int = 1
    socket_group: str = "icebreaker-users"
    # Phase 6 Scope B — client-transport tuning. Reader poll interval
    # trades responsiveness for CPU; reconnect cap trades failover
    # latency for busy-wait pressure. Restart to apply (the reader
    # thread reads these at construction).
    reader_recv_timeout_seconds: float = 1.0
    max_reconnect_delay_seconds: float = 30.0


@dataclass(frozen=True)
class GuiConfig:
    enabled: bool = True
    screenshot_dir: str = "/tmp/icebreaker-gui"
    screenshot_retention: int = 50
    prefer_app_api: bool = True
    a11y_timeout_ms: int = 5000


@dataclass(frozen=True)
class RpaConfig:
    enabled: bool = False
    timeout_seconds: int = 30
    max_keywords_per_workflow: int = 20
    screenshot_every_step: bool = True


@dataclass(frozen=True)
class DesktopConfig:
    start_tray: bool = False        # BP-2: tray icon off by default
    chatbot_on_tray: bool = True    # open chatbot when tray icon clicked
    default_window: str = "chatbot" # "chatbot" | "settings" | "audit"
    dark: bool = True               # True = dark palette; False = light


@dataclass(frozen=True)
class TerminalConfig:
    split_ratio: float = 0.6
    explanation_verbosity: str = "normal"
    shell_explanation: str = "none"
    cot_scrollback: int = 50
    nl_prefix: str = "#"
    theme: str = "oled-dark"
    input_position: str = "top"
    show_tier_badge: bool = True
    show_suggestions: bool = True


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
    terminal: TerminalConfig = field(default_factory=TerminalConfig)
    desktop: DesktopConfig = field(default_factory=DesktopConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)
    rpa: RpaConfig = field(default_factory=RpaConfig)
    # v6.65: ordered list of fully-built fallback backend configs, in the
    # order the runtime should try them after the primary fails on a
    # transport / API error. Empty tuple = no fallback (current behavior).
    qb_fallbacks: tuple[BackendConfig, ...] = field(default_factory=tuple)


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


def _load_raw_toml(path: Path) -> dict:
    """Read, validate forbidden keys, and schema-check a TOML file."""
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise BrainConfigError(
            f"TOML parse failure in {path}: {exc}"
        ) from None
    _check_forbidden_keys(raw)
    _validate_structure(raw)
    return raw


def _build_backend_config(raw: dict) -> BackendConfig:
    """Build the primary backend config from ``raw["qb"]["backend"]``."""
    return _build_backend_config_for(raw, raw["qb"]["backend"])


def _build_fallback_backends(raw: dict) -> tuple[BackendConfig, ...]:
    """Build BackendConfig for each name in ``qb.fallback_chain``.

    Entries lacking a corresponding ``[qb.<name>]`` section are silently
    skipped (per BP-2: invalid config shouldn't break startup — it just
    drops the invalid fallback entry). The primary backend name is also
    filtered out to prevent an infinite retry loop.
    """
    qb = raw.get("qb", {})
    chain = qb.get("fallback_chain", [])
    if not isinstance(chain, list):
        return ()
    primary_name = qb.get("backend")
    out: list[BackendConfig] = []
    seen: set[str] = set()
    for name in chain:
        if not isinstance(name, str):
            continue
        if name == primary_name or name in seen:
            continue
        if not isinstance(qb.get(name), dict):
            continue
        try:
            out.append(_build_backend_config_for(raw, name))
        except BrainConfigError:
            continue
        seen.add(name)
    return tuple(out)


def _build_backend_config_for(raw: dict, name: str) -> BackendConfig:
    qb = raw["qb"]
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
    mcpd_binary = section.get("mcpd_binary", "")
    if not mcpd_binary:
        distro_binary = Path("/usr/libexec/icebreaker/mcpd")
        if distro_binary.exists():
            mcpd_binary = str(distro_binary)
        else:
            mcpd_binary = "src/mcpd/target/release/mcpd"
    # F-28: [mcpd.fs] read_roots = ["/abs/path", ...] → colon-joined string
    fs_section = raw.get("mcpd", {}).get("fs", {}) if isinstance(raw.get("mcpd"), dict) else {}
    fs_read_roots_list = fs_section.get("read_roots", []) if isinstance(fs_section, dict) else []
    if not isinstance(fs_read_roots_list, list):
        fs_read_roots_list = []
    fs_read_roots = ":".join(
        str(p) for p in fs_read_roots_list
        if isinstance(p, str) and p.startswith("/")
    )
    return RunConfig(
        mcpd_binary=mcpd_binary,
        audit_log=section.get("audit_log", "~/.local/state/icebreaker/controller-audit.log"),
        pb_endpoint=section.get("pb_endpoint", "http://127.0.0.1:8080"),
        pb_model_id=section.get("pb_model_id", "run7_cot"),
        pb_max_tokens=section.get("pb_max_tokens", 256),
        pb_timeout_seconds=section.get("pb_timeout_seconds", 10),
        mcpd_timeout_seconds=section.get("mcpd_timeout_seconds", 10.0),
        qb_max_retries=section.get("qb_max_retries", 3),
        mcpd_schemas_dir=section.get("mcpd_schemas_dir", ""),
        pb_transport=section.get("pb_transport", "http"),
        mcpd_fs_read_roots=fs_read_roots,
        # Phase 6 Scope B new fields — defaults match the dataclass.
        turn_timeout_seconds=section.get("turn_timeout_seconds", 600.0),
        model_probe_timeout_seconds=section.get("model_probe_timeout_seconds", 5.0),
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
        # Phase 6 Scope B new fields.
        max_shell_context_chars=section.get("max_shell_context_chars", 512),
        max_recent_commands=section.get("max_recent_commands", 5),
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
        retry_mode=section.get("retry_mode", "on_call_failed_only"),
    )


def _build_daemon_config(raw: dict) -> DaemonConfig:
    section = raw.get("daemon", {})
    return DaemonConfig(
        socket_path=section.get("socket_path", "~/.local/state/icebreaker/controller.sock"),
        pid_file=section.get("pid_file", "~/.local/state/icebreaker/controller.pid"),
        max_connections=section.get("max_connections", 1),
        socket_group=section.get("socket_group", "icebreaker-users"),
        # Phase 6 Scope B new fields — client transport tuning.
        reader_recv_timeout_seconds=section.get("reader_recv_timeout_seconds", 1.0),
        max_reconnect_delay_seconds=section.get("max_reconnect_delay_seconds", 30.0),
    )


def _build_gui_config(raw: dict) -> GuiConfig:
    section = raw.get("gui", {})
    return GuiConfig(
        enabled=section.get("enabled", True),
        screenshot_dir=section.get("screenshot_dir", "/tmp/icebreaker-gui"),
        screenshot_retention=section.get("screenshot_retention", 50),
        prefer_app_api=section.get("prefer_app_api", True),
        a11y_timeout_ms=section.get("a11y_timeout_ms", 5000),
    )


def _build_rpa_config(raw: dict) -> RpaConfig:
    section = raw.get("rpa", {})
    return RpaConfig(
        enabled=section.get("enabled", False),
        timeout_seconds=section.get("timeout_seconds", 30),
        max_keywords_per_workflow=section.get("max_keywords_per_workflow", 20),
        screenshot_every_step=section.get("screenshot_every_step", True),
    )


def _build_desktop_config(raw: dict) -> DesktopConfig:
    section = raw.get("desktop", {})
    return DesktopConfig(
        start_tray=section.get("start_tray", False),
        chatbot_on_tray=section.get("chatbot_on_tray", True),
        default_window=section.get("default_window", "chatbot"),
        dark=section.get("dark", True),
    )


def _build_terminal_config(raw: dict) -> TerminalConfig:
    section = raw.get("terminal", {})
    appearance = section.get("appearance", {})
    interp = section.get("interpretation", {})
    return TerminalConfig(
        split_ratio=section.get("split_ratio", 0.6),
        explanation_verbosity=section.get("explanation_verbosity", "normal"),
        shell_explanation=section.get("shell_explanation", "none"),
        cot_scrollback=section.get("cot_scrollback", 50),
        nl_prefix=section.get("nl_prefix", "#"),
        theme=appearance.get("theme", "oled-dark"),
        input_position=appearance.get("input_position", "top"),
        show_tier_badge=interp.get("show_tier_badge", True),
        show_suggestions=interp.get("show_suggestions", True),
    )


def _build_keymap(raw: dict) -> Keymap:
    section = raw.get("keymap")
    try:
        return load_keymap(section)
    except KeymapValidationError as exc:
        raise BrainConfigError(str(exc)) from None


def _build_config(raw: dict, config_path: Path) -> ControllerConfig:
    """Build ControllerConfig from validated raw TOML dict."""
    return ControllerConfig(
        qb=_build_backend_config(raw),
        hitl=_build_hitl_config(raw),
        prompts=_build_prompts_config(raw),
        session=_build_session_config(raw),
        run=_build_run_config(raw),
        config_path=config_path.resolve(),
        keymap=_build_keymap(raw),
        risk=_build_risk_config(raw),
        tier2=_build_tier2_config(raw),
        cost=_build_cost_config(raw),
        limits=_build_limits_config(raw),
        undo=_build_undo_config(raw),
        verifier=_build_verifier_config(raw),
        daemon=_build_daemon_config(raw),
        terminal=_build_terminal_config(raw),
        desktop=_build_desktop_config(raw),
        gui=_build_gui_config(raw),
        rpa=_build_rpa_config(raw),
        qb_fallbacks=_build_fallback_backends(raw),
    )


def _merge_section_level(system: dict, user: dict) -> dict:
    """Merge user config over system config at section level.

    Each top-level key in user REPLACES the corresponding system key entirely.
    This is intentionally shallow — no deep-merging within sections (ADR-8).
    """
    merged = dict(system)
    merged.update(user)
    return merged


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
    raw = _load_raw_toml(resolved)
    return _build_config(raw, resolved)


def load_layered() -> ControllerConfig:
    """Load config with 2-tier layering: system then user.

    1. If system config exists (/etc/icebreaker/controller.toml), load it.
    2. If user config exists (~/.config/icebreaker/controller.toml), load it.
    3. If both exist, merge (user sections replace system sections).
    4. If neither exists, raise BrainConfigError.
    """
    install_root_redaction_filter()

    system_path = _SYSTEM_CONFIG_PATH
    user_path = _default_config_path()

    system_exists = system_path.exists()
    user_exists = user_path.exists()

    if not system_exists and not user_exists:
        raise BrainConfigError(
            f"no config found at {system_path} or {user_path}. "
            "Bootstrap with: cp dual-brain/controller/"
            "controller.toml.example "
            "~/.config/icebreaker/controller.toml"
        )

    if system_exists and user_exists:
        system_raw = _load_raw_toml(system_path)
        user_raw = _load_raw_toml(user_path)
        raw = _merge_section_level(system_raw, user_raw)
        _check_forbidden_keys(raw)
        _validate_structure(raw)
        config_path = user_path
    elif system_exists:
        raw = _load_raw_toml(system_path)
        config_path = system_path
    else:
        raw = _load_raw_toml(user_path)
        config_path = user_path

    return _build_config(raw, config_path)
