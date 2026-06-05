/// fs.rs — Read-only filesystem tools (Tier 0, Phase 1 / M1.2).
///
/// Implements `fs.read`, `fs.list`, `fs.stat` with TWO layers of defense:
///
/// 1. Pure `validate()` rejects obvious abuse (NUL bytes, percent-encoding,
///    backslashes) and enforces that the path is absolute and falls under a
///    whitelist root. Cross-platform, no IO, easy to fuzz.
/// 2. `openat2()` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS` atomically
///    opens within the whitelisted root. The kernel rejects any resolution
///    that traverses a symlink or escapes the root via `..`, so we are TOCTOU-
///    safe (the check and the open are one syscall).
///
/// Whitelist roots:
///   $HOME           — user's home directory (read access)
///   /proc, /sys     — kernel-exposed read-only data
///   /tmp            — temp scratch
///   /var/log        — system logs (read-only here)
///   /etc            — system config (read-only here)
///
/// Phase 1 reads are UTF-8 only with a 10 MB ceiling. Binary reads land in
/// Phase 5 alongside the base64 encoding layer. Writes (`fs.write` /
/// `fs.delete`) land in M1.5 with the COW gate (INV-6).
///
/// macOS dev fallback: `openat2` is Linux-only, so on macOS we fall back to
/// `std::fs::canonicalize` + prefix check. That fallback is for the dev loop
/// only — production runs on Linux with the openat2 path active.
use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

const STATIC_ROOTS: &[&str] = &["/proc", "/sys", "/tmp", "/var/log", "/etc"];
const MAX_READ_BYTES: u64 = 10 * 1024 * 1024;
const MAX_WRITE_BYTES: u64 = 10 * 1024 * 1024;
const MAX_PATH_LEN: usize = 4096; // matches Linux PATH_MAX
const DEFAULT_FILE_MODE: u32 = 0o644;

/// Subdirs under $HOME considered too sensitive to write directly even though
/// they are technically under $HOME. Writes here ALWAYS go through the COW
/// gate (INV-6). Match is by leading path component after $HOME.
const SENSITIVE_HOME_SUBDIRS: &[&str] = &[
    ".ssh", ".aws", ".gnupg", ".gpg", ".kube", ".docker", ".config/secrets",
];

// ── Pure path validation ──────────────────────────────────────────────────────

/// Result of validating a user-supplied path.
/// `root` is the matched whitelist root (absolute); `rel` is the path under
/// the root (relative, never starting with `/`). `rel` is `"."` when the user
/// passed the root itself (e.g. `/etc`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidatedPath {
    pub root: PathBuf,
    pub rel: String,
}

fn home() -> &'static PathBuf {
    static HOME: OnceLock<PathBuf> = OnceLock::new();
    HOME.get_or_init(|| {
        PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/".into()))
    })
}

fn default_roots() -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = STATIC_ROOTS.iter().map(PathBuf::from).collect();
    v.push(home().clone());
    // Test-only widening for the M1.3 Landlock kernel-enforcement test.
    // Behind a cargo feature so production release binaries cannot honour
    // MCPD_FS_TEST_ROOTS even if it is set. Landlock's allow list is NOT
    // affected by this; the whole point of the test is to prove the kernel
    // (not validate()) is the one rejecting the read.
    #[cfg(feature = "fs-test-roots")]
    if let Ok(extra) = std::env::var("MCPD_FS_TEST_ROOTS") {
        for p in extra.split(':').filter(|s| !s.is_empty()) {
            v.push(PathBuf::from(p));
        }
    }
    v
}

/// Validate a user-supplied path against the default whitelist.
///
/// Public so the cargo-fuzz target in `fuzz/fuzz_targets/validate.rs` can
/// hammer this entry point directly. Internal mcpd code should keep calling
/// it via the existing `tools::fs::*` API; do not bypass it.
pub fn validate(user_path: &str) -> Result<ValidatedPath> {
    validate_against(user_path, &default_roots())
}

/// Validate against an arbitrary root list. Public for fuzzing and tests
/// that want explicit control over the whitelist; not used by production code.
pub fn validate_against(user_path: &str, roots: &[PathBuf]) -> Result<ValidatedPath> {
    if user_path.is_empty() {
        bail!("path is empty");
    }
    if user_path.len() > MAX_PATH_LEN {
        bail!("path exceeds PATH_MAX ({} bytes)", MAX_PATH_LEN);
    }
    if user_path.contains('\0') {
        bail!("path contains NUL byte");
    }
    if user_path.contains('%') {
        bail!("percent-encoded characters not allowed");
    }
    if user_path.contains('\\') {
        bail!("backslash characters not allowed");
    }

    let p = Path::new(user_path);
    if !p.is_absolute() {
        bail!("path must be absolute (starts with /)");
    }

    // Pick the longest matching whitelist root so e.g. /var/log wins over /
    // (if / were ever whitelisted) for paths like /var/log/syslog.
    let mut best: Option<&PathBuf> = None;
    for root in roots {
        if p.starts_with(root) {
            match best {
                None => best = Some(root),
                Some(b) if root.as_os_str().len() > b.as_os_str().len() => best = Some(root),
                _ => {}
            }
        }
    }
    let root = best.ok_or_else(|| {
        anyhow!("'{}' is not under any whitelisted root", user_path)
    })?;

    let rel = p.strip_prefix(root)
        .map_err(|_| anyhow!("internal: strip_prefix failed"))?;
    let rel_str = if rel.as_os_str().is_empty() {
        ".".to_string()
    } else {
        rel.to_string_lossy().into_owned()
    };

    Ok(ValidatedPath { root: root.clone(), rel: rel_str })
}

// ── Tool functions ────────────────────────────────────────────────────────────

pub async fn read(path: &str) -> Result<Value> {
    let v = validate(path)?;
    let mut file = safe_open_readonly(&v.root, &v.rel)?;
    let metadata = file.metadata()?;
    if metadata.is_dir() {
        bail!("path is a directory; use fs.list instead");
    }
    if metadata.len() > MAX_READ_BYTES {
        bail!("file too large ({} bytes > {} max)", metadata.len(), MAX_READ_BYTES);
    }
    use std::io::Read;
    let mut content = String::new();
    file.read_to_string(&mut content)
        .map_err(|e| anyhow!("not valid UTF-8 (binary reads land in Phase 5): {}", e))?;
    Ok(json!({
        "path": path,
        "size_bytes": metadata.len(),
        "content": content,
    }))
}

pub async fn list(path: &str) -> Result<Value> {
    let v = validate(path)?;
    // openat2 with O_DIRECTORY confirms no symlink escape; we then enumerate
    // via std::fs::read_dir on the resolved full path. The TOCTOU window is
    // tiny and Landlock (M1.3) closes it at the kernel level.
    let _dir_fd = safe_open_directory(&v.root, &v.rel)?;

    let full = if v.rel == "." { v.root.clone() } else { v.root.join(&v.rel) };
    let mut entries = Vec::new();
    for entry in std::fs::read_dir(&full)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        let metadata = entry.metadata().ok();
        entries.push(json!({
            "name": name,
            "is_dir": metadata.as_ref().map(|m| m.is_dir()).unwrap_or(false),
            "is_symlink": entry.file_type().ok().map(|t| t.is_symlink()).unwrap_or(false),
            "size_bytes": metadata.as_ref().map(|m| m.len()).unwrap_or(0),
        }));
    }
    entries.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));

    Ok(json!({
        "path": path,
        "count": entries.len(),
        "entries": entries,
    }))
}

/// fs.write — Tier 1 inside safe $HOME, Tier 3 (COW gate) everywhere else.
/// INV-6: writes outside $HOME never execute synchronously in Phase 1.
pub async fn write(path: &str, content: &str, mode: Option<u32>) -> Result<Value> {
    let v = validate(path)?;
    if content.len() as u64 > MAX_WRITE_BYTES {
        bail!("content too large ({} bytes > {} max)", content.len(), MAX_WRITE_BYTES);
    }

    if !is_tier_1_safe(&v) {
        return Ok(cow_gate_response("fs.write", path, content.len() as u64));
    }

    let mode = mode.unwrap_or(DEFAULT_FILE_MODE);
    safe_write(&v.root, &v.rel, content, mode)?;
    Ok(json!({
        "status": "ok",
        "path": path,
        "bytes_written": content.len(),
        "mode": format!("{:o}", mode),
    }))
}

/// fs.delete — ALWAYS Tier 3 per the catalogue. Never executes synchronously
/// in Phase 1; the response is always a COW gate ticket.
pub async fn delete(path: &str) -> Result<Value> {
    let v = validate(path)?;
    // Capture metadata so the preview is useful even before Phase 3 commits.
    let preview_size = safe_open_readonly(&v.root, &v.rel)
        .ok()
        .and_then(|f| f.metadata().ok())
        .map(|m| m.len())
        .unwrap_or(0);
    Ok(cow_gate_response_with_size("fs.delete", path, preview_size, 0))
}

/// `Tier 1` predicate: under $HOME and NOT in a sensitive subdir.
fn is_tier_1_safe(v: &ValidatedPath) -> bool {
    if v.root != *home() {
        return false;
    }
    // v.rel is relative to $HOME. Reject if it starts with any sensitive subdir.
    let rel = v.rel.as_str();
    for sub in SENSITIVE_HOME_SUBDIRS {
        if rel == *sub || rel.starts_with(&format!("{}/", sub)) {
            return false;
        }
    }
    true
}

/// Build a Tier 3 COW gate response (INV-6). Phase 3 will accept this
/// `intent_id` and run the actual COW commit pipeline.
fn cow_gate_response(operation: &str, path: &str, proposed_size: u64) -> Value {
    cow_gate_response_with_size(operation, path, 0, proposed_size)
}

fn cow_gate_response_with_size(
    operation: &str,
    path: &str,
    current_size: u64,
    proposed_size: u64,
) -> Value {
    json!({
        "status": "requires_cow_approval",
        "intent_id": uuid::Uuid::new_v4().to_string(),
        "preview": {
            "operation": operation,
            "path": path,
            "current_size_bytes": current_size,
            "proposed_size_bytes": proposed_size,
        },
    })
}

pub async fn stat(path: &str) -> Result<Value> {
    let v = validate(path)?;
    let file = safe_open_readonly(&v.root, &v.rel)?;
    let metadata = file.metadata()?;
    #[cfg(unix)]
    use std::os::unix::fs::MetadataExt;

    #[cfg(unix)]
    let extra = json!({
        "mode": format!("{:o}", metadata.mode() & 0o7777),
        "uid":  metadata.uid(),
        "gid":  metadata.gid(),
        "mtime": metadata.mtime(),
    });
    #[cfg(not(unix))]
    let extra = json!({});

    Ok(json!({
        "path": path,
        "size_bytes": metadata.len(),
        "is_dir":     metadata.is_dir(),
        "is_file":    metadata.is_file(),
        "is_symlink": metadata.file_type().is_symlink(),
        "extra": extra,
    }))
}

// ── openat2 — direct libc syscall, since nix 0.28 doesn't expose it ─────────

#[cfg(target_os = "linux")]
mod openat2 {
    use anyhow::{anyhow, Result};
    use std::ffi::CString;
    use std::io;
    use std::os::fd::RawFd;

    // From include/uapi/linux/openat2.h.
    #[repr(C)]
    pub(super) struct OpenHow {
        pub flags: u64,
        pub mode: u64,
        pub resolve: u64,
    }

    // Flag values from include/uapi/asm-generic/fcntl.h
    const O_RDONLY:    u64 = 0;
    const O_WRONLY:    u64 = 1;
    const O_CREAT:     u64 = 0o100;
    const O_TRUNC:     u64 = 0o1000;
    const O_DIRECTORY: u64 = 0o200_000;
    const O_CLOEXEC:   u64 = 0o2_000_000;

    // Resolve flags from include/uapi/linux/openat2.h
    const RESOLVE_NO_SYMLINKS: u64 = 0x04;
    const RESOLVE_BENEATH:     u64 = 0x08;

    const RESOLVE_STRICT: u64 = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS;

    impl OpenHow {
        pub fn read_only() -> Self {
            OpenHow {
                flags: O_RDONLY | O_CLOEXEC,
                mode: 0,
                resolve: RESOLVE_STRICT,
            }
        }
        pub fn read_only_directory() -> Self {
            OpenHow {
                flags: O_RDONLY | O_DIRECTORY | O_CLOEXEC,
                mode: 0,
                resolve: RESOLVE_STRICT,
            }
        }
        pub fn write_create_trunc(mode: u32) -> Self {
            OpenHow {
                flags: O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC,
                mode: mode as u64,
                resolve: RESOLVE_STRICT,
            }
        }
    }

    pub(super) fn call(dirfd: RawFd, path: &str, how: &OpenHow) -> Result<RawFd> {
        let c_path = CString::new(path)
            .map_err(|e| anyhow!("path contained NUL: {}", e))?;
        let ret = unsafe {
            libc::syscall(
                libc::SYS_openat2,
                dirfd,
                c_path.as_ptr(),
                how as *const OpenHow,
                std::mem::size_of::<OpenHow>(),
            )
        };
        if ret < 0 {
            return Err(anyhow!("openat2: {}", io::Error::last_os_error()));
        }
        Ok(ret as RawFd)
    }
}

// ── safe_open: openat2 on Linux, canonicalize-check on macOS ─────────────────

#[cfg(target_os = "linux")]
fn safe_open_readonly(root: &Path, rel: &str) -> Result<std::fs::File> {
    use std::os::fd::{AsRawFd, FromRawFd};
    let dir = std::fs::File::open(root)
        .map_err(|e| anyhow!("cannot open whitelist root '{}': {}", root.display(), e))?;
    let how = openat2::OpenHow::read_only();
    let fd = openat2::call(dir.as_raw_fd(), rel, &how)
        .map_err(|e| anyhow!("openat2 refused '{}': {}", rel, e))?;
    Ok(unsafe { std::fs::File::from_raw_fd(fd) })
}

#[cfg(not(target_os = "linux"))]
fn safe_open_readonly(root: &Path, rel: &str) -> Result<std::fs::File> {
    let full = if rel == "." { root.to_path_buf() } else { root.join(rel) };
    let canonical = std::fs::canonicalize(&full)
        .map_err(|e| anyhow!("canonicalize failed: {}", e))?;
    let root_canon = std::fs::canonicalize(root)
        .map_err(|e| anyhow!("canonicalize root failed: {}", e))?;
    if !canonical.starts_with(&root_canon) {
        bail!("path escapes root after canonicalization (symlink?): {:?}", canonical);
    }
    std::fs::File::open(&canonical).map_err(|e| anyhow!("open failed: {}", e))
}

#[cfg(target_os = "linux")]
fn safe_write(root: &Path, rel: &str, content: &str, mode: u32) -> Result<()> {
    use std::io::Write;
    use std::os::fd::{AsRawFd, FromRawFd};
    let dir = std::fs::File::open(root)
        .map_err(|e| anyhow!("cannot open whitelist root '{}': {}", root.display(), e))?;
    let how = openat2::OpenHow::write_create_trunc(mode);
    let fd = openat2::call(dir.as_raw_fd(), rel, &how)
        .map_err(|e| anyhow!("openat2(write) refused '{}': {}", rel, e))?;
    let mut file = unsafe { std::fs::File::from_raw_fd(fd) };
    file.write_all(content.as_bytes())
        .map_err(|e| anyhow!("write failed: {}", e))?;
    file.sync_all().ok();
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn safe_write(root: &Path, rel: &str, content: &str, mode: u32) -> Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let full = root.join(rel);
    // canonicalize the parent so we catch symlink-escape attempts; the file
    // itself may not exist yet.
    if let Some(parent) = full.parent() {
        let root_canon = std::fs::canonicalize(root)
            .map_err(|e| anyhow!("canonicalize root failed: {}", e))?;
        let parent_canon = std::fs::canonicalize(parent)
            .map_err(|e| anyhow!("canonicalize parent failed: {}", e))?;
        if !parent_canon.starts_with(&root_canon) {
            bail!("write target escapes root after canonicalization: {:?}", parent_canon);
        }
    }
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(mode)
        .open(&full)
        .map_err(|e| anyhow!("open(write) failed: {}", e))?;
    file.write_all(content.as_bytes())
        .map_err(|e| anyhow!("write failed: {}", e))?;
    Ok(())
}

#[cfg(target_os = "linux")]
fn safe_open_directory(root: &Path, rel: &str) -> Result<std::fs::File> {
    use std::os::fd::{AsRawFd, FromRawFd};
    let dir = std::fs::File::open(root)?;
    let how = openat2::OpenHow::read_only_directory();
    let fd = openat2::call(dir.as_raw_fd(), rel, &how)
        .map_err(|e| anyhow!("openat2(dir) refused '{}': {}", rel, e))?;
    Ok(unsafe { std::fs::File::from_raw_fd(fd) })
}

#[cfg(not(target_os = "linux"))]
fn safe_open_directory(root: &Path, rel: &str) -> Result<std::fs::File> {
    safe_open_readonly(root, rel)
}

// ── Unit tests for validate() — adversarial path corpus ──────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn roots() -> Vec<PathBuf> {
        vec![
            PathBuf::from("/proc"),
            PathBuf::from("/sys"),
            PathBuf::from("/tmp"),
            PathBuf::from("/var/log"),
            PathBuf::from("/etc"),
            PathBuf::from("/home/aditya"),
        ]
    }

    // ── Happy paths ───────────────────────────────────────────────────────────

    #[test]
    fn validate_etc_hosts() {
        let v = validate_against("/etc/hosts", &roots()).unwrap();
        assert_eq!(v.root, PathBuf::from("/etc"));
        assert_eq!(v.rel, "hosts");
    }

    #[test]
    fn validate_proc_pid_status() {
        let v = validate_against("/proc/1/status", &roots()).unwrap();
        assert_eq!(v.root, PathBuf::from("/proc"));
        assert_eq!(v.rel, "1/status");
    }

    #[test]
    fn validate_home_file() {
        let v = validate_against("/home/aditya/.bashrc", &roots()).unwrap();
        assert_eq!(v.root, PathBuf::from("/home/aditya"));
        assert_eq!(v.rel, ".bashrc");
    }

    #[test]
    fn validate_root_itself_yields_dot() {
        let v = validate_against("/etc", &roots()).unwrap();
        assert_eq!(v.root, PathBuf::from("/etc"));
        assert_eq!(v.rel, ".");
    }

    #[test]
    fn validate_root_with_trailing_slash() {
        let v = validate_against("/etc/", &roots()).unwrap();
        assert_eq!(v.root, PathBuf::from("/etc"));
    }

    #[test]
    fn validate_longest_root_wins() {
        // /var/log should win over an imagined / root for this path.
        let v = validate_against("/var/log/syslog", &roots()).unwrap();
        assert_eq!(v.root, PathBuf::from("/var/log"));
        assert_eq!(v.rel, "syslog");
    }

    #[test]
    fn validate_deep_nested_path() {
        let v = validate_against("/proc/1/fd/0", &roots()).unwrap();
        assert_eq!(v.rel, "1/fd/0");
    }

    // ── Whitelist enforcement ─────────────────────────────────────────────────

    #[test]
    fn rejects_path_outside_whitelist() {
        assert!(validate_against("/root/.ssh/id_rsa", &roots()).is_err());
        assert!(validate_against("/boot/grub/grub.cfg", &roots()).is_err());
        assert!(validate_against("/var/lib/mysql/data", &roots()).is_err());
        assert!(validate_against("/opt/secret", &roots()).is_err());
        assert!(validate_against("/usr/bin/sudo", &roots()).is_err());
    }

    #[test]
    fn rejects_other_user_home() {
        assert!(validate_against("/home/otheruser/.ssh/id_rsa", &roots()).is_err());
    }

    #[test]
    fn rejects_path_that_only_partially_matches_root() {
        // /etcpasswd is NOT under /etc, even though string-prefix matches.
        assert!(validate_against("/etcpasswd", &roots()).is_err());
        assert!(validate_against("/etc-public/foo", &roots()).is_err());
        assert!(validate_against("/procbar/x", &roots()).is_err());
    }

    // ── Format rejection ──────────────────────────────────────────────────────

    #[test]
    fn rejects_empty_path() {
        assert!(validate_against("", &roots()).is_err());
    }

    #[test]
    fn rejects_relative_path() {
        assert!(validate_against("etc/hosts", &roots()).is_err());
        assert!(validate_against("./etc/hosts", &roots()).is_err());
        assert!(validate_against("../etc/hosts", &roots()).is_err());
        assert!(validate_against("hosts", &roots()).is_err());
    }

    #[test]
    fn rejects_nul_byte() {
        assert!(validate_against("/etc/hosts\0/passwd", &roots()).is_err());
        assert!(validate_against("\0/etc/hosts", &roots()).is_err());
        assert!(validate_against("/etc/\0hosts", &roots()).is_err());
    }

    #[test]
    fn rejects_percent_encoding() {
        assert!(validate_against("/etc/%2e%2e/passwd", &roots()).is_err());
        assert!(validate_against("/etc/%2Fpasswd", &roots()).is_err());
        assert!(validate_against("/etc/hosts%00.png", &roots()).is_err());
    }

    #[test]
    fn rejects_backslash() {
        assert!(validate_against("/etc/\\..\\passwd", &roots()).is_err());
        assert!(validate_against("/etc\\hosts", &roots()).is_err());
    }

    #[test]
    fn rejects_oversize_path() {
        let long = "/etc/".to_string() + &"a/".repeat(2048);
        assert!(validate_against(&long, &roots()).is_err());
    }

    // ── Path-traversal corpus (the headline gate from §M1.2) ──────────────────
    //
    // validate() catches obvious surface attacks. openat2(RESOLVE_BENEATH) is
    // the kernel-side gate that catches the rest at the actual open syscall.
    // Together they form INV-4's two-layer defense.

    #[test]
    fn traversal_double_dot_relative() {
        assert!(validate_against("../etc/passwd", &roots()).is_err());
        assert!(validate_against("..\\..\\etc\\passwd", &roots()).is_err());
    }

    #[test]
    fn traversal_double_dot_inside_whitelist() {
        // These ARE under /etc (string-wise) but should be caught by the
        // kernel's RESOLVE_BENEATH if attempted; validate() lets them through
        // because the surface form is valid. This is intentional: defense in
        // depth — validate() handles obvious abuse, openat2 handles resolution.
        let v = validate_against("/etc/../etc/passwd", &roots());
        assert!(v.is_ok(), "validate accepts this; openat2 will reject if .. escapes");
    }

    #[test]
    fn traversal_absolute_escape_attempts() {
        // Absolute paths that try to escape via .. — strip_prefix sees the
        // raw form. /etc/../root/secret strips to ../root/secret (rel),
        // openat2(RESOLVE_BENEATH) rejects this at the kernel.
        // validate() accepts the surface form.
        // What we DO reject at validate(): paths that aren't under any root.
        assert!(validate_against("/root/../etc/passwd", &roots()).is_err());
    }

    #[test]
    fn traversal_unicode_dot_dot_lookalikes() {
        // Unicode chars that visually resemble '.' should not be coerced into
        // path traversal by our validator. The kernel handles bytes literally
        // so as long as we don't decode them, they're just regular filename
        // characters.
        // U+2024 (ONE DOT LEADER), U+FF0E (FULLWIDTH FULL STOP)
        let v = validate_against("/etc/\u{2024}\u{2024}/passwd", &roots());
        // Path is technically under /etc and chars are not '.', so this is
        // allowed at the validate layer. openat2 will look for a file literally
        // named "‥" which won't exist, so it fails naturally.
        assert!(v.is_ok(), "unicode lookalikes are treated as literal filename bytes");
    }

    #[test]
    fn traversal_null_in_middle() {
        assert!(validate_against("/etc/hosts\0../passwd", &roots()).is_err());
    }

    #[test]
    fn traversal_url_encoded_double_dot() {
        for enc in &["%2e%2e", "%2E%2E", "%2e.", ".%2e"] {
            let p = format!("/etc/{}/passwd", enc);
            assert!(validate_against(&p, &roots()).is_err(),
                    "should reject {}", p);
        }
    }

    #[test]
    fn traversal_long_relative_climb() {
        let p = "/etc/".to_string() + &"../".repeat(20) + "etc/passwd";
        // Accepted at validate (under /etc, no NUL/%), kernel catches via BENEATH.
        assert!(validate_against(&p, &roots()).is_ok());
    }

    #[test]
    fn traversal_absolute_path_with_dot_segments() {
        // /etc/./hosts is a legitimate resolution.
        assert!(validate_against("/etc/./hosts", &roots()).is_ok());
        assert!(validate_against("/etc/./../etc/hosts", &roots()).is_ok());
    }

    #[test]
    fn traversal_proc_self_root() {
        // /proc/self/root is a magic-link to / — RESOLVE_NO_MAGICLINKS catches
        // this at openat2 (we use RESOLVE_NO_SYMLINKS which subsumes magic
        // links per kernel docs). validate() accepts surface form.
        assert!(validate_against("/proc/self/root/etc/passwd", &roots()).is_ok());
    }

    // ── Symlink-escape simulations (rely on openat2 to reject at IO) ─────────

    #[test]
    fn validate_dotfile_in_home_allowed() {
        let v = validate_against("/home/aditya/.config/foo", &roots()).unwrap();
        assert_eq!(v.root, PathBuf::from("/home/aditya"));
        assert_eq!(v.rel, ".config/foo");
    }

    #[test]
    fn validate_etc_subdir_allowed() {
        let v = validate_against("/etc/systemd/system/myunit.service", &roots()).unwrap();
        assert_eq!(v.root, PathBuf::from("/etc"));
        assert_eq!(v.rel, "systemd/system/myunit.service");
    }

    // ── More adversarial payloads (bulk corpus, gate G4 seed) ────────────────

    #[test]
    fn corpus_traversal_payloads() {
        let bad = [
            "/etc/passwd\0",
            "/etc/passwd\0\0",
            "//etc//passwd",          // accepted (canonicalized later); not a security issue
            "/etc/%00",
            "/etc\\\\windows-style",
            "/no-such-root/file",
            "/root/.ssh/authorized_keys",
            "/boot/vmlinuz",
            "/dev/sda",
            "/dev/mem",
            "/sys/firmware/efi/efivars/foo",  // under /sys, accepted by validate
            "/etc",                           // root itself, accepted (= ".")
            "/proc/kcore",                    // under /proc, accepted; openat2 will refuse fileops
        ];
        let mut errs = 0usize;
        let mut oks = 0usize;
        for p in &bad {
            match validate_against(p, &roots()) {
                Ok(_) => oks += 1,
                Err(_) => errs += 1,
            }
        }
        // Spot-check: known-out-of-whitelist must be rejected.
        assert!(validate_against("/root/.ssh/authorized_keys", &roots()).is_err());
        assert!(validate_against("/boot/vmlinuz", &roots()).is_err());
        assert!(validate_against("/dev/sda", &roots()).is_err());
        // We should reject more than we accept in this corpus.
        assert!(errs >= 4, "expected at least 4 rejections, got {} errs / {} oks", errs, oks);
    }

    #[test]
    fn corpus_all_whitelist_roots_accepted_at_root_level() {
        for r in roots().iter() {
            let v = validate_against(r.to_str().unwrap(), &roots()).unwrap();
            assert_eq!(&v.root, r);
            assert_eq!(v.rel, ".");
        }
    }

    #[test]
    fn corpus_each_root_subpath_accepted() {
        for r in roots().iter() {
            let p = format!("{}/some/sub/path", r.display());
            let v = validate_against(&p, &roots()).unwrap();
            assert_eq!(&v.root, r);
            assert_eq!(v.rel, "some/sub/path");
        }
    }

    // ── Tier-1 vs Tier-3 routing (INV-6) ─────────────────────────────────────

    fn home_path() -> PathBuf {
        PathBuf::from("/home/aditya")
    }

    fn v_in_home(rel: &str) -> ValidatedPath {
        ValidatedPath { root: home_path(), rel: rel.to_string() }
    }

    fn v_in_etc(rel: &str) -> ValidatedPath {
        ValidatedPath { root: PathBuf::from("/etc"), rel: rel.to_string() }
    }

    // is_tier_1_safe consults home() which reads $HOME at startup. To keep
    // these tests deterministic, we use the home_path() constant and
    // construct ValidatedPath manually — bypassing the env var.
    //
    // We can't directly assert is_tier_1_safe() without overriding home(),
    // so we test the *exact rule* with a parallel helper.
    fn tier1_test(v: &ValidatedPath, fake_home: &Path) -> bool {
        if v.root != *fake_home {
            return false;
        }
        for sub in SENSITIVE_HOME_SUBDIRS {
            if v.rel == *sub || v.rel.starts_with(&format!("{}/", sub)) {
                return false;
            }
        }
        true
    }

    #[test]
    fn tier1_safe_home_paths() {
        let h = home_path();
        assert!(tier1_test(&v_in_home("notes.txt"), &h));
        assert!(tier1_test(&v_in_home("projects/foo.md"), &h));
        assert!(tier1_test(&v_in_home(".bashrc"), &h));
        assert!(tier1_test(&v_in_home(".config/code/settings.json"), &h));
    }

    #[test]
    fn tier1_rejects_sensitive_subdirs_in_home() {
        let h = home_path();
        assert!(!tier1_test(&v_in_home(".ssh"), &h));
        assert!(!tier1_test(&v_in_home(".ssh/id_rsa"), &h));
        assert!(!tier1_test(&v_in_home(".ssh/authorized_keys"), &h));
        assert!(!tier1_test(&v_in_home(".aws/credentials"), &h));
        assert!(!tier1_test(&v_in_home(".gnupg/secring.gpg"), &h));
        assert!(!tier1_test(&v_in_home(".kube/config"), &h));
        assert!(!tier1_test(&v_in_home(".docker/config.json"), &h));
        assert!(!tier1_test(&v_in_home(".config/secrets/token"), &h));
    }

    #[test]
    fn tier1_rejects_outside_home() {
        let h = home_path();
        assert!(!tier1_test(&v_in_etc("hosts"), &h));
        assert!(!tier1_test(&v_in_etc("systemd/system/x.service"), &h));
    }

    #[test]
    fn tier1_substring_not_prefix_collision() {
        let h = home_path();
        // .sshconfig (no slash) should NOT be treated as inside .ssh/
        assert!(tier1_test(&v_in_home(".sshconfig"), &h));
        assert!(tier1_test(&v_in_home(".awsxxx"), &h));
        assert!(tier1_test(&v_in_home(".kubectl-cache"), &h));
    }

    // Coverage-guided fuzzing of validate() lives in `fuzz/fuzz_targets/validate.rs`
    // (libFuzzer via cargo-fuzz). ci.sh G4 runs it for 60 s and asserts zero
    // crashes. The seed corpus under `fuzz/corpus/validate/` carries the
    // known-bad inputs the old hand-rolled LCG used to generate.
}
