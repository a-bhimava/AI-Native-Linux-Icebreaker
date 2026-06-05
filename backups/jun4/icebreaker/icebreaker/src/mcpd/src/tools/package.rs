/// package.rs — Debian package introspection (Phase 1 / M1.8).
///
/// Tools:
///   package.query(pattern)    Tier 0  dpkg-query -W -f=… "*pattern*"
///   package.install(package)  Tier 2  returns requires_cow_approval (Phase 3)
///   package.remove(package)   Tier 2  returns requires_cow_approval (Phase 3)
///   package.upgrade(package)  Tier 2  returns requires_cow_approval (Phase 3)
///
/// Why install/remove/upgrade are gated:
/// apt-get touches /var/lib/dpkg, /var/cache/apt, /etc/apt — all outside the
/// in-Phase-1 fs whitelist for direct writes, and many ops are irreversible.
/// INV-6 requires COW-driven dry-run review before commit. Phase 3 owns the
/// commit pipeline; Phase 1 emits the intent_id ticket.
///
/// Validation: package/pattern names must match Debian's allowed charset
/// (`[a-z0-9][a-z0-9+\-.]*`) plus the `*` glob char for query patterns. No
/// shell metacharacters reach the subprocess.
use anyhow::{bail, Result};
use serde_json::{json, Value};

const MAX_NAME_LEN: usize = 128;

pub async fn query(pattern: &str) -> Result<Value> {
    require_valid_query_pattern(pattern)?;
    run_dpkg_query(pattern).await
}

pub async fn install(package: &str) -> Result<Value> {
    require_valid_package_name(package)?;
    Ok(cow_gate("package.install", package))
}

pub async fn remove(package: &str) -> Result<Value> {
    require_valid_package_name(package)?;
    Ok(cow_gate("package.remove", package))
}

pub async fn upgrade(package: &str) -> Result<Value> {
    require_valid_package_name(package)?;
    Ok(cow_gate("package.upgrade", package))
}

async fn run_dpkg_query(pattern: &str) -> Result<Value> {
    let needle = format!("*{}*", pattern);
    let result = tokio::process::Command::new("dpkg-query")
        .arg("-W")
        .arg("-f=${Package} ${Version} ${Status}\n")
        .arg(&needle)
        .output()
        .await;

    let out = match result {
        Ok(o) => o,
        Err(e) => return Ok(json!({
            "status": "unavailable",
            "reason": format!("dpkg-query unavailable: {}", e),
        })),
    };

    if !out.status.success() {
        // dpkg-query returns 1 when no matches found — surface that as a
        // healthy empty result rather than an error.
        let stderr = String::from_utf8_lossy(&out.stderr);
        if stderr.contains("no packages found") || stderr.contains("no matching package") {
            return Ok(json!({
                "status": "ok",
                "pattern": pattern,
                "matches": [],
            }));
        }
        return Ok(json!({
            "status": "err",
            "pattern": pattern,
            "exit_code": out.status.code(),
            "stderr": stderr.into_owned(),
        }));
    }

    let stdout = String::from_utf8_lossy(&out.stdout);
    let matches: Vec<Value> = stdout.lines().filter_map(|line| {
        let mut parts = line.splitn(3, ' ');
        let name = parts.next()?.to_string();
        let version = parts.next()?.to_string();
        let status = parts.next().unwrap_or("").to_string();
        Some(json!({"name": name, "version": version, "status": status}))
    }).collect();

    Ok(json!({
        "status": "ok",
        "pattern": pattern,
        "matches": matches,
    }))
}

fn cow_gate(operation: &str, package: &str) -> Value {
    json!({
        "status": "requires_cow_approval",
        "intent_id": uuid::Uuid::new_v4().to_string(),
        "preview": {
            "operation": operation,
            "package": package,
            "note": "Phase 3 will commit this via apt-get inside the COW overlay.",
        },
    })
}

fn require_valid_query_pattern(p: &str) -> Result<()> {
    if !is_valid_query_pattern(p) {
        bail!("invalid query pattern (allowed: [a-z0-9+\\-.*_], len 1..{}): {:?}", MAX_NAME_LEN, p);
    }
    Ok(())
}

fn require_valid_package_name(p: &str) -> Result<()> {
    if !is_valid_package_name(p) {
        bail!("invalid Debian package name (allowed: [a-z0-9+\\-.], starts with [a-z0-9], len 2..{}): {:?}", MAX_NAME_LEN, p);
    }
    Ok(())
}

/// Debian package name spec (policy 5.6.7): `[a-z0-9][a-z0-9+\-.]*`, len ≥ 2.
fn is_valid_package_name(p: &str) -> bool {
    if p.len() < 2 || p.len() > MAX_NAME_LEN {
        return false;
    }
    let mut chars = p.chars();
    let first = chars.next().unwrap();
    if !(first.is_ascii_lowercase() || first.is_ascii_digit()) {
        return false;
    }
    chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '-' | '+' | '.'))
}

/// Query patterns allow underscore and asterisk in addition to package-name
/// chars. The leading char may be any allowed char (patterns are matched as
/// substrings).
fn is_valid_query_pattern(p: &str) -> bool {
    if p.is_empty() || p.len() > MAX_NAME_LEN {
        return false;
    }
    p.chars().all(|c|
        c.is_ascii_lowercase() || c.is_ascii_digit()
        || matches!(c, '-' | '+' | '.' | '_' | '*')
    )
}

// ── Unit tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_package_names() {
        assert!(is_valid_package_name("nginx"));
        assert!(is_valid_package_name("libssl3"));
        assert!(is_valid_package_name("g++"));
        assert!(is_valid_package_name("python3"));
        assert!(is_valid_package_name("apt-utils"));
        assert!(is_valid_package_name("a2ps"));
        assert!(is_valid_package_name("libc6-dev"));
        assert!(is_valid_package_name("0ad"));
    }

    #[test]
    fn invalid_package_names() {
        assert!(!is_valid_package_name(""));
        assert!(!is_valid_package_name("a"));               // too short
        assert!(!is_valid_package_name("Nginx"));            // uppercase
        assert!(!is_valid_package_name("-nginx"));           // starts with -
        assert!(!is_valid_package_name(".nginx"));           // starts with .
        assert!(!is_valid_package_name("nginx; rm -rf /"));  // metachar
        assert!(!is_valid_package_name("nginx`whoami`"));    // backtick
        assert!(!is_valid_package_name("nginx$(echo hi)"));  // $()
        assert!(!is_valid_package_name("nginx_pkg"));        // underscore not allowed in package names
        assert!(!is_valid_package_name("nginx pkg"));        // space
        assert!(!is_valid_package_name("../../etc/passwd")); // path traversal
    }

    #[test]
    fn valid_query_patterns() {
        // Patterns allow more shapes than strict package names.
        assert!(is_valid_query_pattern("nginx"));
        assert!(is_valid_query_pattern("ngin*"));
        assert!(is_valid_query_pattern("*lib*"));
        assert!(is_valid_query_pattern("python_3"));
        assert!(is_valid_query_pattern("g++"));
    }

    #[test]
    fn invalid_query_patterns() {
        assert!(!is_valid_query_pattern(""));
        assert!(!is_valid_query_pattern("nginx; rm"));
        assert!(!is_valid_query_pattern("nginx`whoami`"));
        assert!(!is_valid_query_pattern("nginx\n"));
        assert!(!is_valid_query_pattern("../../etc"));
    }

    #[test]
    fn name_length_capped() {
        let too_long = "a".repeat(MAX_NAME_LEN + 1);
        assert!(!is_valid_package_name(&too_long));
        assert!(!is_valid_query_pattern(&too_long));
        let just_right_pkg = "a".repeat(MAX_NAME_LEN);
        assert!(is_valid_package_name(&just_right_pkg));
    }

    #[tokio::test]
    async fn install_returns_cow_gate() {
        let r = install("nginx").await.unwrap();
        assert_eq!(r["status"], "requires_cow_approval");
        assert_eq!(r["preview"]["operation"], "package.install");
        assert_eq!(r["preview"]["package"], "nginx");
    }

    #[tokio::test]
    async fn install_rejects_invalid_name() {
        assert!(install("nginx; rm").await.is_err());
        assert!(remove("../../etc").await.is_err());
        assert!(upgrade("Nginx").await.is_err());
    }

    #[tokio::test]
    async fn query_rejects_invalid_pattern() {
        assert!(query("nginx`whoami`").await.is_err());
        assert!(query("nginx\n").await.is_err());
    }
}
