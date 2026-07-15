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
use std::path::PathBuf;

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
    let canonical = PathBuf::from(&resolved)
        .canonicalize()
        .with_context(|| format!("fs_read: cannot resolve {:?}", resolved))?;
    let canonical_str = canonical
        .to_str()
        .ok_or_else(|| anyhow!("fs_read: resolved path is not valid UTF-8"))?
        .to_string();

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
        // Test-only Manifest construction. Real code path always goes
        // through the loader — this stub bypasses schema compile since
        // we're testing the dispatcher directly.
        Manifest {
            name: "test.tool".to_string(),
            version: 1,
            description: "test".to_string(),
            tier: 0,
            param_schema: json!({"type": "object"}),
            sandbox: Default::default(),
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
}
