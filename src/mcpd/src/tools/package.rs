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
    let diff = simulate_and_serialize("package.install", package).await;
    Ok(cow_gate("package.install", package, diff))
}

pub async fn remove(package: &str) -> Result<Value> {
    require_valid_package_name(package)?;
    let diff = simulate_and_serialize("package.remove", package).await;
    Ok(cow_gate("package.remove", package, diff))
}

pub async fn upgrade(package: &str) -> Result<Value> {
    require_valid_package_name(package)?;
    let diff = simulate_and_serialize("package.upgrade", package).await;
    Ok(cow_gate("package.upgrade", package, diff))
}

/// M7.0.1b: run the async COW simulator and serialize its result to a
/// serde_json::Value for embedding in the ticket's preview.diff field.
/// Wrapped in tokio::time::timeout so a hung apt-get can't stall the RPC
/// past 15s — on timeout we return a visible-warn diff rather than
/// silent-pass. Simulator failures return a Value too (they build a diff
/// with an "unavailable" human_summary), so this only returns None on a
/// panic-like unwind path, which shouldn't happen.
async fn simulate_and_serialize(op: &str, package: &str) -> Option<Value> {
    match tokio::time::timeout(
        std::time::Duration::from_secs(15),
        crate::tools::cow::simulate_package_op(op, package),
    )
    .await
    {
        Ok(Ok(diff)) => Some(diff.to_json()),
        Ok(Err(_)) => None,
        Err(_) => Some(json!({
            "operation": op,
            "bytes_delta": 0,
            "file_count_delta": 0,
            "affected_paths_sample": [],
            "human_summary": "Preview unavailable: apt-get -s timed out after 15 s. Approve only if you trust the intent.",
            "risk": "MED",
            "reversible": false,
        })),
    }
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

/// Build a Tier 2/3 COW gate response for package.*. M7.0.1c will accept
/// this `intent_id` back via `cow.commit` and run apt-get for real.
///
/// The `diff` param (M7.0.1b) carries the DryRunDiff.to_json() from
/// `simulate_package_op`. `None` collapses to no `preview.diff` field — the
/// classic ticket shape (operation + package + note) is preserved for
/// backward compat with pre-v6.16 controllers.
fn cow_gate(operation: &str, package: &str, diff: Option<Value>) -> Value {
    let mut preview = json!({
        "operation": operation,
        "package": package,
        "note": "M7.0.1c will commit this via cow.commit (real apt-get).",
    });
    if let Some(d) = diff {
        preview["diff"] = d;
    }
    json!({
        "status": "requires_cow_approval",
        "intent_id": uuid::Uuid::new_v4().to_string(),
        "preview": preview,
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
        // M7.0.1b: preview.diff sub-object appears if the simulator returned
        // Some. On macOS dev where apt-get is missing, the simulator returns
        // Ok(DryRunDiff{human_summary: "Preview unavailable: ..."}) → Some,
        // so we always see the field on macOS AND on any Linux that has
        // apt-get (even if apt-get -s itself fails).
        assert!(!r["preview"]["diff"].is_null(), "preview.diff must exist (M7.0.1b)");
        assert_eq!(r["preview"]["diff"]["operation"], "package.install");
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
