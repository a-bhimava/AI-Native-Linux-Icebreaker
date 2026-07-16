//! v6.9 Scope O Layer 2 Part B — `fs_read` impl.kind dispatcher.
//!
//! Reads a file (or reports it doesn't exist) from an absolute path
//! declared in the manifest's `impl.path`. Params may fill
//! `{{param_name}}` placeholders in the path template — the substituted
//! path is re-validated to remain absolute AND under the base Landlock
//! read roots so a `{{path}}` = `../foo` can't escape.
//!
//! Reuses the existing seccomp footprint (openat + read + close + fstat
//! + newfstatat + faccessat) — no widening.

use anyhow::{anyhow, bail, Context, Result};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

use crate::manifest_loader::Manifest;

/// Max bytes we'll read + return in one call. Matches `fs::read`
/// semantics; oversized files are truncated with a `truncated: true`
/// flag so the caller can decide what to do.
const MAX_READ_BYTES: usize = 65_536;


pub async fn dispatch(manifest: &Manifest, params: &Value) -> Result<Value> {
    let template = manifest
        .imp
        .path
        .as_deref()
        .ok_or_else(|| anyhow!("fs_read: manifest.impl.path missing (validator bug)"))?;

    let resolved = substitute_placeholders(template, params)?;

    // Post-substitution sanity: still absolute. `..` collapsing +
    // symlink resolution happen via canonicalize() so a `{{path}}`
    // pattern like `..` can't escape the intended root.
    if !resolved.starts_with('/') {
        bail!(
            "fs_read: substituted path {:?} is not absolute (template {:?})",
            resolved, template
        );
    }
    // canonicalize() collapses .. + follows symlinks BEFORE the
    // allow-list check so a symlink-under-declared-root pointing
    // outside the roots is caught by userspace, not by kernel
    // Landlock only.
    let canonical = PathBuf::from(&resolved)
        .canonicalize()
        .with_context(|| format!("fs_read: cannot resolve {:?}", resolved))?;
    let canonical_str = canonical
        .to_str()
        .ok_or_else(|| anyhow!("fs_read: resolved path is not valid UTF-8"))?
        .to_string();

    // v6.9 Scope O P0-2 (2026-07-15 CT scan) — userspace defense in
    // depth per BP-9. The manifest's own `sandbox.landlock.ro` array
    // is the allow-list for this manifest's fs_read impl. Landlock at
    // the kernel is the second layer; refusing at userspace produces
    // a sanitized error string ("not under any declared") instead of
    // a raw ENOACCES from the kernel + gives us test coverage that
    // symlink-escape is caught pre-kernel.
    check_against_declared_roots(&canonical, manifest)
        .context("fs_read: sandbox.landlock.ro userspace enforcement")?;

    // Read (up to MAX_READ_BYTES). Errors bubble as anyhow — the
    // dispatch caller converts to JSON-RPC -32603 (Internal error)
    // with the exception message, matching legacy tool behavior.
    let bytes = tokio::fs::read(&canonical)
        .await
        .with_context(|| format!("fs_read: reading {:?}", canonical))?;

    let truncated = bytes.len() > MAX_READ_BYTES;
    let content = if truncated {
        String::from_utf8_lossy(&bytes[..MAX_READ_BYTES]).into_owned()
    } else {
        String::from_utf8_lossy(&bytes).into_owned()
    };

    Ok(json!({
        "path": canonical_str,
        "content": content,
        "size_bytes": bytes.len(),
        "truncated": truncated,
        "tool_name": manifest.name,
    }))
}


/// v6.9 Scope O P0-2 (2026-07-15 CT scan) — userspace enforcement of
/// the manifest's `sandbox.landlock.ro` array as the read allow-list
/// for this manifest's `fs_read` impl.
///
/// This is the userspace half of a defense-in-depth pair; kernel
/// Landlock is the other half (applied at daemon startup per INV-5).
/// Symlinks pointing outside the declared roots are caught here
/// because `canonical` has already had `canonicalize()` applied.
///
/// A manifest with no `sandbox.landlock.ro` (or an empty ro array)
/// refuses ALL reads at userspace — the operator must declare intent.
/// This is deliberate: an undeclared manifest is a rogue manifest.
fn check_against_declared_roots(canonical: &Path, manifest: &Manifest) -> Result<()> {
    let ro_roots: &[String] = manifest
        .sandbox
        .landlock
        .as_ref()
        .map(|ll| ll.ro.as_slice())
        .unwrap_or(&[]);
    if ro_roots.is_empty() {
        bail!(
            "manifest {:?} declares no sandbox.landlock.ro roots; fs_read \
             refuses to read any path at userspace. Add ro: [<paths>] to \
             the manifest's sandbox.landlock block.",
            manifest.name
        );
    }
    for root in ro_roots {
        // Canonicalize the root too so a declared root that itself
        // contains a symlink (Linux /tmp on some distros; macOS /var)
        // matches the canonicalized target. Fall back to the raw
        // string on canonicalize failure (e.g. root doesn't exist
        // yet on the system running mcpd) — that path won't match a
        // real canonical target anyway, which is the safe answer.
        let root_canonical = PathBuf::from(root)
            .canonicalize()
            .unwrap_or_else(|_| PathBuf::from(root));
        if canonical.starts_with(&root_canonical) {
            return Ok(());
        }
    }
    bail!(
        "fs_read: canonical path {:?} not under any declared \
         sandbox.landlock.ro root {:?}",
        canonical, ro_roots
    );
}


/// Substitute every `{{param}}` occurrence in `template` with
/// `params.param` (params must already be schema-validated). Any
/// referenced placeholder that isn't in `params` raises — we don't
/// silently emit empty strings, because a missing param usually means
/// a shape mismatch the caller should surface.
fn substitute_placeholders(template: &str, params: &Value) -> Result<String> {
    let mut out = String::with_capacity(template.len());
    let mut chars = template.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '{' && chars.peek() == Some(&'{') {
            chars.next(); // consume second '{'
            let mut name = String::new();
            let mut closed = false;
            while let Some(nc) = chars.next() {
                if nc == '}' && chars.peek() == Some(&'}') {
                    chars.next(); // consume second '}'
                    closed = true;
                    break;
                }
                name.push(nc);
            }
            if !closed {
                bail!("fs_read: unterminated `{{{{...}}}}` in path template");
            }
            let name = name.trim().to_string();
            let value = params
                .get(&name)
                .and_then(|v| v.as_str())
                .ok_or_else(|| anyhow!(
                    "fs_read: template references {{{{{}}}}} but params has no string field with that name",
                    name
                ))?;
            if value.contains('/') && !value.starts_with('/') {
                // Allow absolute substitutions (the schema pattern
                // gates this), reject relative segments — they'd be
                // path-traversal fuel.
                bail!(
                    "fs_read: param {:?} value {:?} contains `/` but is not absolute",
                    name, value
                );
            }
            out.push_str(value);
        } else {
            out.push(c);
        }
    }
    Ok(out)
}


#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::TempDir;

    fn mk_manifest(path_template: &str) -> Manifest {
        // Legacy helper — permissive ro: ["/"] so pre-P0-2 tests
        // (path shape, substitution, truncation, etc.) continue to
        // exercise only what they intended to test. New P0-2 tests
        // that assert enforcement build their manifests with
        // mk_manifest_with_roots.
        mk_manifest_with_roots(path_template, &["/"])
    }

    fn mk_manifest_with_roots(path_template: &str, ro_roots: &[&str]) -> Manifest {
        // Test-only Manifest construction. Real code path always goes
        // through the loader — this stub bypasses schema compile since
        // we're testing the dispatcher directly.
        Manifest {
            name: "test.tool".to_string(),
            version: 1,
            description: "test".to_string(),
            tier: 0,
            param_schema: json!({"type": "object"}),
            sandbox: crate::manifest_loader::SandboxHint {
                landlock: Some(crate::manifest_loader::LandlockHint {
                    ro: ro_roots.iter().map(|s| s.to_string()).collect(),
                    rw: vec![],
                    net: false,
                }),
                seccomp: None,
                cow: false,
            },
            imp: crate::manifest_loader::Impl {
                kind: crate::manifest_loader::ImplKind::FsRead,
                path: Some(path_template.to_string()),
                binary: None,
                args_template: None,
                method: None,
            },
            source_path: None,
        }
    }

    #[tokio::test]
    async fn reads_absolute_path() {
        let tmp = TempDir::new().unwrap();
        let f = tmp.path().join("hello.txt");
        std::fs::write(&f, "hello world").unwrap();

        let m = mk_manifest(f.to_str().unwrap());
        let result = dispatch(&m, &json!({})).await.unwrap();
        assert_eq!(result["content"], "hello world");
        assert_eq!(result["size_bytes"], 11);
        assert_eq!(result["truncated"], false);
        assert_eq!(result["tool_name"], "test.tool");
    }

    #[tokio::test]
    async fn substitutes_params() {
        let tmp = TempDir::new().unwrap();
        let f = tmp.path().join("sub.txt");
        std::fs::write(&f, "ok").unwrap();

        let m = mk_manifest("{{full_path}}");
        let result = dispatch(&m, &json!({"full_path": f.to_str().unwrap()}))
            .await.unwrap();
        assert_eq!(result["content"], "ok");
    }

    #[tokio::test]
    async fn refuses_relative_substitution() {
        let m = mk_manifest("/etc/{{name}}");
        let err = dispatch(&m, &json!({"name": "../passwd"}))
            .await.unwrap_err().to_string();
        assert!(err.contains("not absolute"), "unexpected: {}", err);
    }

    #[tokio::test]
    async fn refuses_missing_placeholder_param() {
        let m = mk_manifest("/tmp/{{missing}}");
        let err = dispatch(&m, &json!({})).await.unwrap_err().to_string();
        assert!(err.contains("has no string field"), "unexpected: {}", err);
    }

    #[tokio::test]
    async fn refuses_nonexistent_file() {
        let m = mk_manifest("/nonexistent/definitely/not/here");
        let err = dispatch(&m, &json!({})).await.unwrap_err().to_string();
        assert!(err.contains("cannot resolve"), "unexpected: {}", err);
    }

    #[tokio::test]
    async fn truncates_oversized_file() {
        let tmp = TempDir::new().unwrap();
        let f = tmp.path().join("big.txt");
        let big = vec![b'a'; MAX_READ_BYTES + 100];
        std::fs::write(&f, &big).unwrap();

        let m = mk_manifest(f.to_str().unwrap());
        let result = dispatch(&m, &json!({})).await.unwrap();
        assert_eq!(result["size_bytes"], MAX_READ_BYTES + 100);
        assert_eq!(result["truncated"], true);
        assert_eq!(result["content"].as_str().unwrap().len(), MAX_READ_BYTES);
    }

    // ── v6.9 P0-2 (2026-07-15 CT scan) — userspace ro-root enforcement ──

    #[tokio::test]
    async fn refuses_read_when_sandbox_landlock_ro_missing() {
        let tmp = TempDir::new().unwrap();
        let f = tmp.path().join("target.txt");
        std::fs::write(&f, "content").unwrap();

        // Manifest with NO sandbox.landlock declared:
        let m = Manifest {
            name: "test.no_ro".to_string(),
            version: 1,
            description: "test".to_string(),
            tier: 0,
            param_schema: json!({"type": "object"}),
            sandbox: Default::default(),
            imp: crate::manifest_loader::Impl {
                kind: crate::manifest_loader::ImplKind::FsRead,
                path: Some(f.to_str().unwrap().to_string()),
                binary: None,
                args_template: None,
                method: None,
            },
            source_path: None,
        };
        let err = format!("{:#}", dispatch(&m, &json!({})).await.unwrap_err());
        assert!(
            err.contains("declares no sandbox.landlock.ro"),
            "expected refusal for undeclared ro; got: {}", err
        );
    }

    #[tokio::test]
    async fn refuses_read_outside_declared_ro_roots() {
        let tmp = TempDir::new().unwrap();
        let allowed = tmp.path().join("allowed");
        std::fs::create_dir(&allowed).unwrap();
        let target = tmp.path().join("target.txt");
        std::fs::write(&target, "content").unwrap();

        // Manifest allows only $tmp/allowed, tries to read $tmp/target.txt:
        let allowed_str = allowed.to_str().unwrap().to_string();
        let m = mk_manifest_with_roots(target.to_str().unwrap(), &[&allowed_str]);
        let err = format!("{:#}", dispatch(&m, &json!({})).await.unwrap_err());
        assert!(
            err.contains("not under any declared"),
            "expected refusal for path outside declared ro; got: {}", err
        );
    }

    #[tokio::test]
    async fn allows_read_under_declared_ro_root() {
        let tmp = TempDir::new().unwrap();
        let allowed = tmp.path().join("allowed");
        std::fs::create_dir(&allowed).unwrap();
        let target = allowed.join("data.txt");
        std::fs::write(&target, "hello").unwrap();

        let allowed_str = allowed.to_str().unwrap().to_string();
        let m = mk_manifest_with_roots(target.to_str().unwrap(), &[&allowed_str]);
        let result = dispatch(&m, &json!({})).await.unwrap();
        assert_eq!(result["content"], "hello");
    }

    #[tokio::test]
    async fn refuses_symlink_escape_from_declared_root() {
        // Create a symlink UNDER the allowed root that points OUTSIDE
        // it. canonicalize() resolves the symlink, our check verifies
        // the canonical path is still under the declared root. Catches
        // the escape at userspace before Landlock would.
        #[cfg(unix)]
        {
            use std::os::unix::fs::symlink;
            let tmp = TempDir::new().unwrap();
            let allowed = tmp.path().join("allowed");
            std::fs::create_dir(&allowed).unwrap();
            let outside = tmp.path().join("outside");
            std::fs::create_dir(&outside).unwrap();
            let secret = outside.join("secret.txt");
            std::fs::write(&secret, "should-not-be-readable").unwrap();
            let symlink_in_allowed = allowed.join("escape");
            symlink(&secret, &symlink_in_allowed).unwrap();

            let allowed_str = allowed.to_str().unwrap().to_string();
            let m = mk_manifest_with_roots(
                symlink_in_allowed.to_str().unwrap(),
                &[&allowed_str],
            );
            let err = format!("{:#}", dispatch(&m, &json!({})).await.unwrap_err());
            assert!(
                err.contains("not under any declared"),
                "expected userspace refusal for symlink escape; got: {}", err
            );
        }
    }
}
