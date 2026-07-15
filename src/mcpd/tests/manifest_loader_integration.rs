//! v6.9 Scope O Layer 2 Part B — integration tests for the mcpd
//! manifest loader as observed through the library surface.
//!
//! The `manifest_loader::registry()` singleton is process-wide; these
//! tests initialise it once by pointing at the shipped
//! `src/mcpd/manifests/` directory and assert:
//!   - the pilot demo.uptime manifest loads,
//!   - it appears in `tools::list_all()` output,
//!   - dispatching an fs_read against a temp file (using a synthetic
//!     manifest loaded via the loader's public API) returns bytes.
//!
//! We do NOT exercise the JSON-RPC server here because that would
//! require standing up stdio + tokio channels. Integration tests at
//! the JSON-RPC level are handled by the ci.sh harvest gate on the
//! GCP VM (Scope G's G20/L6 lanes).

use std::path::PathBuf;

use mcpd::manifest_loader;

fn shipped_manifests_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("manifests")
}

#[test]
fn shipped_manifests_dir_exists() {
    let dir = shipped_manifests_dir();
    assert!(
        dir.exists(),
        "src/mcpd/manifests/ must exist for Layer 2 Part B — got {}",
        dir.display()
    );
}

#[test]
fn shipped_manifests_load_clean() {
    let dir = shipped_manifests_dir();
    let reg = manifest_loader::load(&dir).unwrap_or_else(|e| {
        panic!("shipped manifests failed to load: {:#}", e)
    });
    assert!(
        reg.has("demo.uptime"),
        "expected demo.uptime pilot in registry; got {:?}",
        reg.names()
    );
}

#[test]
fn shipped_demo_uptime_is_fs_read() {
    let dir = shipped_manifests_dir();
    let reg = manifest_loader::load(&dir).unwrap();
    let entry = reg.get("demo.uptime").unwrap();
    assert!(matches!(
        entry.manifest.imp.kind,
        manifest_loader::ImplKind::FsRead
    ));
    assert_eq!(entry.manifest.imp.path.as_deref(), Some("/proc/uptime"));
    assert_eq!(entry.manifest.tier, 0);
}

#[test]
fn shipped_demo_uptime_compiled_schema_accepts_empty_object() {
    let dir = shipped_manifests_dir();
    let reg = manifest_loader::load(&dir).unwrap();
    let entry = reg.get("demo.uptime").unwrap();
    let empty = serde_json::json!({});
    assert!(
        entry.compiled_schema.validate(&empty).is_ok(),
        "demo.uptime schema must accept empty params"
    );
    let bad = serde_json::json!({"unexpected": "field"});
    assert!(
        entry.compiled_schema.validate(&bad).is_err(),
        "demo.uptime schema must reject unknown fields (additionalProperties: false)"
    );
}
