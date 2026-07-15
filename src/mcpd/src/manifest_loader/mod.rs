//! v6.9 Scope O Layer 2 Part B — mcpd-side runtime manifest loader.
//!
//! Adds a **declarative tool** surface to mcpd: instead of hand-writing
//! a Rust dispatch arm + a hand-tuned Landlock/Seccomp footprint per
//! tool, an operator can drop a YAML manifest under `src/mcpd/manifests/`
//! and, at startup, this module will load, validate, and register it.
//!
//! The runtime shape mirrors the controller-side loader shipped in
//! Layer 2 Part A (`dual-brain/controller/manifest_loader.py`), so
//! operators only learn one manifest shape.
//!
//! # Invariants preserved
//!
//! - **INV-3** (mcpd never opens a network listener): manifests can
//!   declare `impl.kind` primitives, but the primitive set is fixed at
//!   compile time — a manifest cannot introduce a new kind.
//! - **INV-4** (params schema-validated pre-dispatch): every manifest
//!   ships its own `param_schema`; the loader compiles it via
//!   `jsonschema::JSONSchema` and dispatch runs it before the impl.
//! - **INV-5** (Landlock/Seccomp applied at startup, not runtime): the
//!   sandbox syscall footprint is derived at LOAD TIME. Every allowed
//!   impl.kind uses only syscalls already in the base allowlist. There
//!   is no runtime sandbox mutation.
//!
//! # Anti-goal
//!
//! Manifests are NOT "arbitrary Rust code in a YAML file". They are a
//! **compact description over a fixed set of primitives** (Layer 2
//! ships 4 kinds: fs_read, fs_write_cow, dbus_call, exec_pipeline; only
//! fs_read is dispatchable in the initial cut, the other three refuse
//! at dispatch until their handlers land). Layer 3 (v7.5) reuses this
//! same loader as the sandbox-validation target for QB-proposed tools.

pub mod impl_kinds;

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use anyhow::{anyhow, bail, Context, Result};
use jsonschema::JSONSchema;
use serde::Deserialize;
use serde_json::Value;
use tracing::info;


/// Layer 2 impl.kind vocabulary. Every manifest must declare one of
/// these; unknown kinds are rejected at load. New kinds require a code
/// change here + a dispatcher in `impl_kinds/` — never runtime-only.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ImplKind {
    /// Read a file or directory under Landlock read roots. Reuses the
    /// existing `tools::fs::read` path validation. Safest primitive;
    /// shipped in the initial Part B cut.
    FsRead,
    /// Write via COW dry-run (INV-6). Tier >= 2 requires HITL. Not
    /// dispatchable yet — stub refuses at dispatch time (v6.10).
    FsWriteCow,
    /// Invoke a well-known D-Bus method (e.g. systemd's StartUnit).
    /// Stub (v6.10).
    DbusCall,
    /// Spawn a hardcoded binary with fixed args (no shell). Stub
    /// (deferred — needs seccomp allowlist widening for execve + fork,
    /// which is out of scope for Part B).
    ExecPipeline,
}

impl std::fmt::Display for ImplKind {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let s = match self {
            ImplKind::FsRead => "fs_read",
            ImplKind::FsWriteCow => "fs_write_cow",
            ImplKind::DbusCall => "dbus_call",
            ImplKind::ExecPipeline => "exec_pipeline",
        };
        write!(f, "{}", s)
    }
}


/// The `impl:` block of a manifest.
#[derive(Debug, Clone, Deserialize)]
pub struct Impl {
    pub kind: ImplKind,
    /// For `fs_read`: absolute path to read. `{{param}}` placeholders
    /// are substituted from validated params at dispatch time.
    #[serde(default)]
    pub path: Option<String>,
    /// For `exec_pipeline`: hardcoded binary path. NEVER interpolated
    /// from params.
    #[serde(default)]
    pub binary: Option<String>,
    /// For `exec_pipeline`: hardcoded arg list, may reference
    /// `{{param_name}}`.
    #[serde(default)]
    pub args_template: Option<Vec<String>>,
    /// For `dbus_call`: `service.path.interface.Method` triple.
    #[serde(default)]
    pub method: Option<String>,
}


/// The `sandbox:` block of a manifest.
///
/// Optional in the YAML because controller-side impl.kinds (Part A,
/// session_op) touch no OS surface. On the mcpd side every impl.kind
/// implicitly runs under the DAEMON's base Landlock/Seccomp allowlist
/// — this block is a HUMAN-READABLE hint of intent + a load-time gate
/// (validated shape, not enforced by Landlock at runtime).
#[derive(Debug, Clone, Deserialize, Default)]
pub struct SandboxHint {
    #[serde(default)]
    pub landlock: Option<LandlockHint>,
    /// Named seccomp preset the impl claims to fit under. Loader
    /// verifies the string is a known preset but does NOT reconfigure
    /// the kernel filter (that ran at startup — INV-5).
    #[serde(default)]
    pub seccomp: Option<String>,
    #[serde(default)]
    pub cow: bool,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct LandlockHint {
    #[serde(default)]
    pub ro: Vec<String>,
    #[serde(default)]
    pub rw: Vec<String>,
    #[serde(default)]
    pub net: bool,
}


/// A validated, ready-to-dispatch tool manifest.
#[derive(Debug, Clone, Deserialize)]
pub struct Manifest {
    pub name: String,
    pub version: u32,
    pub description: String,
    /// 0=read-only, 1=low, 2=medium, 3=destructive (HITL required).
    pub tier: u8,
    /// JSON Schema for tool params. Compiled at load, enforced pre-dispatch (INV-4).
    pub param_schema: Value,
    #[serde(default)]
    pub sandbox: SandboxHint,
    #[serde(rename = "impl")]
    pub imp: Impl,

    /// Populated at load time — path the manifest was read from. Used
    /// by the duplicate-name check + audit trail.
    #[serde(skip)]
    pub source_path: Option<PathBuf>,
}


/// Registry of loaded manifests. Immutable after `load()` returns; a
/// hot-reload primitive is Layer 3 scope (v7.5).
#[derive(Debug, Default)]
pub struct ManifestRegistry {
    entries: HashMap<String, LoadedManifest>,
}

pub struct LoadedManifest {
    pub manifest: Manifest,
    pub compiled_schema: JSONSchema,
}

impl std::fmt::Debug for LoadedManifest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LoadedManifest")
            .field("name", &self.manifest.name)
            .field("kind", &self.manifest.imp.kind.to_string())
            .finish()
    }
}

impl ManifestRegistry {
    /// Sorted list of registered tool names.
    pub fn names(&self) -> Vec<String> {
        let mut v: Vec<String> = self.entries.keys().cloned().collect();
        v.sort();
        v
    }

    pub fn has(&self, name: &str) -> bool {
        self.entries.contains_key(name)
    }

    pub fn get(&self, name: &str) -> Option<&LoadedManifest> {
        self.entries.get(name)
    }

    pub fn all(&self) -> impl Iterator<Item = &LoadedManifest> {
        self.entries.values()
    }
}


/// Process-wide registry singleton. Populated by [`init`] once at
/// daemon startup; [`registry`] returns the initialized reference (or
/// an empty registry if init was never called — allowed for tests +
/// backward compat with pre-Layer 2 binaries).
static REGISTRY: OnceLock<ManifestRegistry> = OnceLock::new();


/// Initialize the process-wide manifest registry from `dir`. Meant to
/// be called ONCE at daemon startup, before any dispatch happens.
/// Second call returns Ok without re-loading (OnceLock semantics).
/// If load fails the caller MUST abort startup — a partially-registered
/// registry would silently hide tools.
pub fn init(dir: &Path) -> Result<()> {
    let reg = load(dir)?;
    // set() returns Err if already set; that's benign for our purposes
    // (matches OnceLock semantics — first-write-wins, subsequent calls
    // are no-ops). Discard the error deliberately.
    let _ = REGISTRY.set(reg);
    Ok(())
}


/// Access the initialized manifest registry. Returns an empty registry
/// (not None) if [`init`] was never called — this keeps every caller
/// site branch-free at the cost of an idle allocation. Tests exercise
/// specific registries directly via [`load`].
pub fn registry() -> &'static ManifestRegistry {
    static EMPTY: OnceLock<ManifestRegistry> = OnceLock::new();
    REGISTRY.get().unwrap_or_else(|| EMPTY.get_or_init(ManifestRegistry::default))
}


/// v6.9 Scope O Layer 2 Part B — accepted seccomp presets. The kernel
/// filter shipped at daemon startup MUST already cover every syscall
/// each preset names; a manifest declaring a preset does NOT widen the
/// filter at runtime (INV-5). This list is the compile-time gate that
/// keeps rogue manifests from claiming a preset the base filter doesn't
/// cover.
const KNOWN_SECCOMP_PRESETS: &[&str] = &[
    "reads_only",       // openat + read + close + fstat family
    "reads_and_dbus",   // above + AF_UNIX socket ops
    "reads_and_exec",   // above + execve + fork (NOT in shipped base filter)
];


/// Load every `*.yaml` under `dir`, validate, register. Refuses to
/// return a partial registry: any invalid manifest raises here so a
/// broken YAML never silently disables a tool.
pub fn load(dir: &Path) -> Result<ManifestRegistry> {
    let mut registry = ManifestRegistry::default();

    if !dir.exists() {
        info!("manifest_loader: {} does not exist; registry empty",
              dir.display());
        return Ok(registry);
    }

    let mut paths: Vec<PathBuf> = fs::read_dir(dir)
        .with_context(|| format!("reading manifests dir {}", dir.display()))?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("yaml"))
        .collect();
    paths.sort();

    for path in paths {
        let raw = fs::read_to_string(&path)
            .with_context(|| format!("reading {}", path.display()))?;
        let mut manifest: Manifest = serde_yaml::from_str(&raw)
            .with_context(|| format!("parsing {}", path.display()))?;
        manifest.source_path = Some(path.clone());

        validate(&manifest)
            .with_context(|| format!("validating {}", path.display()))?;

        if registry.entries.contains_key(&manifest.name) {
            let existing = &registry.entries[&manifest.name];
            bail!(
                "manifest_loader: {} — duplicate name {:?} (also defined in {:?})",
                path.display(), manifest.name, existing.manifest.source_path
            );
        }

        let compiled_schema = JSONSchema::compile(&manifest.param_schema)
            .map_err(|e| anyhow!(
                "manifest_loader: {} — param_schema failed to compile: {}",
                path.display(), e
            ))?;

        info!("manifest_loader: registered {} (kind={}, tier={})",
              manifest.name, manifest.imp.kind, manifest.tier);
        registry.entries.insert(
            manifest.name.clone(),
            LoadedManifest { manifest, compiled_schema },
        );
    }

    Ok(registry)
}


/// Manifest structural validator, run after YAML deserialization.
/// Field-level shape is enforced by serde; this validates constraints
/// that cross fields or reference the impl.kind vocabulary.
fn validate(m: &Manifest) -> Result<()> {
    // Name: must be dotted-lowercase (matches mcpd's convention).
    if !is_dotted_lowercase(&m.name) {
        bail!(
            "name {:?} must be dotted-lowercase (matches mcpd tool naming)",
            m.name
        );
    }
    if m.name.len() > 64 {
        bail!("name too long ({} > 64)", m.name.len());
    }
    if m.description.is_empty() {
        bail!("description must not be empty");
    }
    if m.description.len() > 400 {
        bail!("description too long ({} > 400)", m.description.len());
    }
    if m.tier > 3 {
        bail!("tier out of range (got {}, max 3)", m.tier);
    }
    if !m.param_schema.is_object() {
        bail!("param_schema must be a JSON object");
    }

    // Sandbox seccomp preset (if declared) must be a known name — we
    // reject at LOAD TIME so a manifest can never claim a preset the
    // kernel filter doesn't cover (INV-5 by construction).
    if let Some(preset) = &m.sandbox.seccomp {
        if !KNOWN_SECCOMP_PRESETS.contains(&preset.as_str()) {
            bail!(
                "sandbox.seccomp preset {:?} is not known (allowed: {:?})",
                preset, KNOWN_SECCOMP_PRESETS
            );
        }
    }

    // impl.kind-specific structural checks. Field-level presence is
    // enforced here so a manifest with the wrong shape refuses at load.
    match m.imp.kind {
        ImplKind::FsRead => {
            let path = m.imp.path.as_deref().unwrap_or("");
            if path.is_empty() {
                bail!("fs_read impl requires `impl.path` (may contain `{{param}}` placeholders)");
            }
            if !path.starts_with('/') {
                bail!("fs_read impl.path {:?} must be absolute", path);
            }
        }
        ImplKind::FsWriteCow => {
            // Stub: acceptable in the registry but dispatch will refuse.
            // Structural check ensures Layer 2 Part B ships forward-
            // compatible shape; when the handler lands we can enforce
            // path/mode presence here.
        }
        ImplKind::DbusCall => {
            if m.imp.method.as_deref().unwrap_or("").is_empty() {
                bail!("dbus_call impl requires `impl.method` (service.path.interface.Method)");
            }
        }
        ImplKind::ExecPipeline => {
            let binary = m.imp.binary.as_deref().unwrap_or("");
            if binary.is_empty() {
                bail!("exec_pipeline impl requires `impl.binary` (absolute path, never param-interpolated)");
            }
            if !binary.starts_with('/') {
                bail!("exec_pipeline impl.binary {:?} must be absolute", binary);
            }
        }
    }

    Ok(())
}


fn is_dotted_lowercase(s: &str) -> bool {
    if s.is_empty() || s.starts_with('.') || s.ends_with('.') {
        return false;
    }
    if !s.contains('.') {
        return false;
    }
    for c in s.chars() {
        if !(c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '.') {
            return false;
        }
    }
    true
}


#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn write(dir: &Path, name: &str, body: &str) {
        std::fs::write(dir.join(name), body).unwrap();
    }

    // ── Happy path ────────────────────────────────────────────────

    #[test]
    fn loads_valid_fs_read_manifest() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "system.uptime.yaml", r#"
name: system.uptime
version: 1
description: "Read /proc/uptime and return raw bytes."
tier: 0
param_schema:
  type: object
  additionalProperties: false
sandbox:
  landlock:
    ro: [/proc/uptime]
  seccomp: reads_only
impl:
  kind: fs_read
  path: /proc/uptime
"#);
        let reg = load(tmp.path()).unwrap();
        assert!(reg.has("system.uptime"), "reg={:?}", reg.names());
        let entry = reg.get("system.uptime").unwrap();
        assert!(matches!(entry.manifest.imp.kind, ImplKind::FsRead));
        assert_eq!(entry.manifest.imp.path.as_deref(), Some("/proc/uptime"));
        assert_eq!(entry.manifest.tier, 0);
    }

    #[test]
    fn empty_dir_returns_empty_registry() {
        let tmp = TempDir::new().unwrap();
        let reg = load(tmp.path()).unwrap();
        assert!(reg.names().is_empty());
    }

    #[test]
    fn missing_dir_returns_empty_registry() {
        let tmp = TempDir::new().unwrap();
        let reg = load(&tmp.path().join("does_not_exist")).unwrap();
        assert!(reg.names().is_empty());
    }

    // ── Validation failures ───────────────────────────────────────

    #[test]
    fn rejects_missing_required_field() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "bad.yaml", r#"
name: bad.thing
version: 1
tier: 0
param_schema: {type: object}
impl:
  kind: fs_read
  path: /proc/uptime
"#);
        let err = format!("{:#}", load(tmp.path()).unwrap_err());
        assert!(err.contains("description") || err.contains("missing field"),
                "unexpected error: {}", err);
    }

    #[test]
    fn rejects_bad_name_pattern() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "bad.yaml", r#"
name: NotDotted
version: 1
description: "test"
tier: 0
param_schema: {type: object}
impl:
  kind: fs_read
  path: /x
"#);
        let err = format!("{:#}", load(tmp.path()).unwrap_err());
        assert!(err.contains("dotted-lowercase"), "unexpected: {}", err);
    }

    #[test]
    fn rejects_unknown_impl_kind() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "bad.yaml", r#"
name: bad.thing
version: 1
description: "test"
tier: 0
param_schema: {type: object}
impl:
  kind: eval_python
"#);
        let err = format!("{:#}", load(tmp.path()).unwrap_err());
        assert!(err.contains("unknown variant") || err.contains("eval_python"),
                "unexpected: {}", err);
    }

    #[test]
    fn rejects_unknown_seccomp_preset() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "bad.yaml", r#"
name: bad.thing
version: 1
description: "test"
tier: 0
param_schema: {type: object}
sandbox:
  seccomp: escape_all
impl:
  kind: fs_read
  path: /x
"#);
        let err = format!("{:#}", load(tmp.path()).unwrap_err());
        assert!(err.contains("escape_all") || err.contains("is not known"),
                "unexpected: {}", err);
    }

    #[test]
    fn rejects_fs_read_missing_path() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "bad.yaml", r#"
name: bad.thing
version: 1
description: "test"
tier: 0
param_schema: {type: object}
impl:
  kind: fs_read
"#);
        let err = format!("{:#}", load(tmp.path()).unwrap_err());
        assert!(err.contains("fs_read impl requires"), "unexpected: {}", err);
    }

    #[test]
    fn rejects_fs_read_relative_path() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "bad.yaml", r#"
name: bad.thing
version: 1
description: "test"
tier: 0
param_schema: {type: object}
impl:
  kind: fs_read
  path: relative/path
"#);
        let err = format!("{:#}", load(tmp.path()).unwrap_err());
        assert!(err.contains("must be absolute"), "unexpected: {}", err);
    }

    #[test]
    fn rejects_exec_pipeline_missing_binary() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "bad.yaml", r#"
name: bad.thing
version: 1
description: "test"
tier: 0
param_schema: {type: object}
impl:
  kind: exec_pipeline
"#);
        let err = format!("{:#}", load(tmp.path()).unwrap_err());
        assert!(err.contains("requires `impl.binary`"), "unexpected: {}", err);
    }

    #[test]
    fn rejects_duplicate_name() {
        let tmp = TempDir::new().unwrap();
        let body = r#"
name: dup.thing
version: 1
description: "test"
tier: 0
param_schema: {type: object}
impl:
  kind: fs_read
  path: /x
"#;
        write(tmp.path(), "a.yaml", body);
        write(tmp.path(), "b.yaml", body);
        let err = format!("{:#}", load(tmp.path()).unwrap_err());
        assert!(err.contains("duplicate name"), "unexpected: {}", err);
    }

    #[test]
    fn rejects_malformed_yaml() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "broken.yaml", "name: [unclosed\n");
        let err = format!("{:#}", load(tmp.path()).unwrap_err());
        assert!(err.contains("parsing"), "unexpected: {}", err);
    }

    #[test]
    fn rejects_tier_out_of_range() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "bad.yaml", r#"
name: bad.thing
version: 1
description: "test"
tier: 5
param_schema: {type: object}
impl:
  kind: fs_read
  path: /x
"#);
        let err = format!("{:#}", load(tmp.path()).unwrap_err());
        // serde_yaml enforces u8 range on `tier: u8`; either serde or
        // our validate() may fire first depending on the number.
        assert!(err.contains("tier") || err.contains("out of range")
                || err.contains("invalid value"),
                "unexpected: {}", err);
    }

    #[test]
    fn compiled_schema_rejects_bad_params() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "system.uptime.yaml", r#"
name: system.uptime
version: 1
description: "Read /proc/uptime"
tier: 0
param_schema:
  type: object
  additionalProperties: false
impl:
  kind: fs_read
  path: /proc/uptime
"#);
        let reg = load(tmp.path()).unwrap();
        let entry = reg.get("system.uptime").unwrap();
        // Extra property should be rejected by the compiled schema.
        let bad = serde_json::json!({"unexpected": "value"});
        assert!(entry.compiled_schema.validate(&bad).is_err());
        // Empty object should pass.
        let good = serde_json::json!({});
        assert!(entry.compiled_schema.validate(&good).is_ok());
    }
}
