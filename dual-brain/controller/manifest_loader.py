"""v6.9 Scope O Layer 2 Part A — controller-side manifest loader.

Loads YAML manifests from ``controller/manifests/*.yaml`` at daemon
startup, validates each against ``controller/schemas/tool_manifest.json``
(INV-4 gate for the manifest shape itself + Layer 3 R&D anchor), then
registers them by name in a ManifestRegistry the Controller consults
before dispatching to mcpd.

Load-time failure modes (all reject):
    - YAML that doesn't parse
    - manifest whose name doesn't match the pattern
    - manifest whose impl.kind is not in the enum
    - manifest whose impl.op (session_op) has no registered handler
    - duplicate name across the manifests/ directory

Runtime dispatch (ManifestRegistry.dispatch):
    1. Look up manifest by name.
    2. Validate params dict against manifest.param_schema (INV-4).
    3. Call the impl.kind's dispatch(manifest, params, ctx) function.
    4. Return the ImplResult.

The registry is IMMUTABLE after load. Hot-reload for Layer 3 (v7.5)
will introduce a rebuild-then-swap primitive; not needed for Part A.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator, ValidationError

from .impl_kinds import DispatchContext, ImplResult
from .impl_kinds import session_op as _session_op_mod


_log = logging.getLogger(__name__)

# Every impl.kind must appear here with a callable that takes
# (manifest, params, ctx) and returns ImplResult. New Part-A kinds
# (v6.10+) get one line each; Part B (mcpd-side kinds) will route
# via a McpdBridge dispatcher that doesn't live in this file.
_KIND_DISPATCH: dict[str, Any] = {
    "session_op": _session_op_mod.dispatch,
    # Part B (v6.9 Week 2-3, mcpd-side): fs_read, fs_write_cow,
    # dbus_call, exec_pipeline. Loader will accept the manifest
    # (meta-schema already permits the enum) but dispatch will error
    # until Part B ships. Meta-schema validation still runs — a
    # malformed exec_pipeline manifest is rejected at load, before
    # Part B lands.
}


_HERE = Path(__file__).resolve().parent
_MANIFESTS_DIR = _HERE / "manifests"
_META_SCHEMA_PATH = _HERE / "schemas" / "tool_manifest.json"


@dataclass(frozen=True)
class ManifestEntry:
    name: str
    manifest: dict
    kind: str


@dataclass
class ManifestRegistry:
    """Read-only registry of loaded manifests. Frozen after ``load()``
    returns; mutation after the fact is deliberately not supported."""
    _entries: dict[str, ManifestEntry] = field(default_factory=dict)

    def names(self) -> list[str]:
        return sorted(self._entries.keys())

    def has(self, name: str) -> bool:
        return name in self._entries

    def get(self, name: str) -> ManifestEntry:
        return self._entries[name]

    def dispatch(self, name: str, params: dict, ctx: DispatchContext) -> ImplResult:
        """Validate params + route to the impl.kind dispatcher.

        Raises KeyError if the name is not registered, ValidationError
        if params fail the manifest's param_schema, or the kind-specific
        exception (usually ValueError) if the impl refuses.
        """
        entry = self._entries[name]
        # INV-4: schema-validate params before the impl sees them.
        Draft7Validator(entry.manifest["param_schema"]).validate(params)
        dispatcher = _KIND_DISPATCH.get(entry.kind)
        if dispatcher is None:
            raise NotImplementedError(
                f"manifest {name!r} declares impl.kind={entry.kind!r} "
                "but no dispatcher is registered — expected for Part B "
                "kinds (fs_read/fs_write_cow/dbus_call/exec_pipeline) "
                "until the mcpd manifest loader lands."
            )
        return dispatcher(entry.manifest, params, ctx)


def _load_meta_schema() -> dict:
    import json
    with _META_SCHEMA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load(manifests_dir: Path | None = None) -> ManifestRegistry:
    """Scan ``manifests_dir`` (default: controller/manifests/), validate
    every ``.yaml`` file against the meta-schema, and return a registry.

    Refuses to return a partial registry: any invalid manifest raises
    at load time so a broken YAML can never silently disable a tool.
    """
    directory = Path(manifests_dir) if manifests_dir is not None else _MANIFESTS_DIR
    if not directory.exists():
        _log.info("manifest_loader: %s does not exist; registry empty", directory)
        return ManifestRegistry()

    meta_schema = _load_meta_schema()
    validator = Draft7Validator(meta_schema)

    entries: dict[str, ManifestEntry] = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"manifest_loader: {path.name} — YAML parse error: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(
                f"manifest_loader: {path.name} — top-level must be a mapping"
            )
        try:
            validator.validate(raw)
        except ValidationError as exc:
            raise ValueError(
                f"manifest_loader: {path.name} — schema error at "
                f"{list(exc.absolute_path)}: {exc.message}"
            ) from exc

        name = raw["name"]
        if name in entries:
            raise ValueError(
                f"manifest_loader: {path.name} — duplicate name {name!r} "
                f"(also defined in {entries[name].manifest.get('_source_file', '?')})"
            )

        kind = raw["impl"]["kind"]

        # Kind-specific structural checks (beyond the meta-schema, which
        # only enforces shape). session_op needs a registered op.
        if kind == "session_op":
            op = raw["impl"].get("op", "")
            registered_ops = getattr(_session_op_mod, "_HANDLERS", {}).keys()
            if op not in registered_ops:
                raise ValueError(
                    f"manifest_loader: {path.name} — impl.op={op!r} "
                    f"has no session_op handler (known: {sorted(registered_ops)})"
                )

        raw["_source_file"] = str(path)
        entries[name] = ManifestEntry(name=name, manifest=raw, kind=kind)
        _log.info("manifest_loader: registered %s (kind=%s, tier=%s)",
                  name, kind, raw["tier"])

    return ManifestRegistry(_entries=entries)
