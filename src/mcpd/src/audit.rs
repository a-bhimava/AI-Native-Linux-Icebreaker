/// audit.rs — Append-only JSONL audit log (INV-8).
///
/// Every dispatched intent — including parse errors, schema rejects,
/// method-not-found, sandbox refuses, and COW-gated tickets — produces
/// exactly one line in the audit log. The log is opened with O_APPEND so
/// POSIX guarantees that writes ≤ PIPE_BUF (4096 bytes) are atomic across
/// concurrent writers. We keep each line under that ceiling by redacting
/// params and bounding payload size.
///
/// Default path:
///   Linux:  /var/log/mcpd/audit.log  (override: MCPD_AUDIT_LOG)
///   macOS:  /tmp/mcpd-audit.log      (dev only)
///
/// On open failure (permission denied, parent dir missing) the audit
/// writer falls back to stderr — the daemon continues but emits a one-time
/// warning. CLAUDE.md INV-8 says the audit log must not be writable by the
/// AI models; the systemd unit (Phase 6) chowns the parent dir to root:adm
/// with mode 0750.
use serde::Serialize;
use serde_json::Value;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::OnceLock;

/// Result class — string union the Controller's audit consumer will dispatch on.
#[derive(Debug, Clone, Copy)]
pub enum ResultClass {
    Ok,
    Err,
    Refused,
    Unavailable,
    CowRequired,
    MethodNotFound,
    InvalidParams,
    ParseError,
}

impl ResultClass {
    fn as_str(self) -> &'static str {
        match self {
            ResultClass::Ok               => "ok",
            ResultClass::Err              => "err",
            ResultClass::Refused          => "refused",
            ResultClass::Unavailable      => "unavailable",
            ResultClass::CowRequired      => "cow_required",
            ResultClass::MethodNotFound   => "method_not_found",
            ResultClass::InvalidParams    => "invalid_params",
            ResultClass::ParseError       => "parse_error",
        }
    }
}

#[derive(Serialize)]
struct AuditLine<'a> {
    timestamp: String,
    method: &'a str,
    request_id: Option<&'a Value>,
    params_redacted: Value,
    result_class: &'static str,
    latency_us: u128,
}

/// Persistent writer state. We use std::sync::Mutex (blocking) — writes are
/// rare and short, contention is negligible compared to async overhead.
struct Writer {
    file: Option<Mutex<std::fs::File>>,
}

fn writer() -> &'static Writer {
    static W: OnceLock<Writer> = OnceLock::new();
    W.get_or_init(|| {
        let path = audit_path();
        // Best-effort: create parent dir if it doesn't exist.
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let opened = OpenOptions::new()
            .create(true)
            .append(true)
            .mode_if_unix(0o640)
            .open(&path);
        match opened {
            Ok(f) => Writer { file: Some(Mutex::new(f)) },
            Err(e) => {
                tracing::warn!("audit: cannot open '{}': {}; falling back to stderr", path.display(), e);
                Writer { file: None }
            }
        }
    })
}

fn audit_path() -> PathBuf {
    if let Ok(s) = std::env::var("MCPD_AUDIT_LOG") {
        return PathBuf::from(s);
    }
    #[cfg(target_os = "linux")]
    { PathBuf::from("/var/log/mcpd/audit.log") }
    #[cfg(not(target_os = "linux"))]
    { PathBuf::from("/tmp/mcpd-audit.log") }
}

/// Convenience extension so we can call `.mode_if_unix(...)` without #[cfg]
/// noise at every call site.
trait OpenOptionsExt2 {
    fn mode_if_unix(&mut self, mode: u32) -> &mut Self;
}
impl OpenOptionsExt2 for OpenOptions {
    #[cfg(unix)]
    fn mode_if_unix(&mut self, mode: u32) -> &mut Self {
        use std::os::unix::fs::OpenOptionsExt;
        self.mode(mode)
    }
    #[cfg(not(unix))]
    fn mode_if_unix(&mut self, _mode: u32) -> &mut Self { self }
}

/// Append one audit line. Never panics — even if the log file is unavailable,
/// the message goes to stderr.
pub fn log_intent(
    method: &str,
    request_id: Option<&Value>,
    params: &Value,
    result_class: ResultClass,
    latency_us: u128,
) {
    let line = AuditLine {
        timestamp: chrono::Utc::now().to_rfc3339(),
        method,
        request_id,
        params_redacted: redact(params),
        result_class: result_class.as_str(),
        latency_us,
    };
    let payload = match serde_json::to_string(&line) {
        Ok(s) => s,
        Err(e) => {
            tracing::error!("audit: serialize failed: {}", e);
            return;
        }
    };

    // Truncate at 4 KB so the write fits within PIPE_BUF and stays atomic.
    let mut bytes = payload.into_bytes();
    if bytes.len() > 4000 {
        bytes.truncate(4000);
        bytes.extend_from_slice(b"...\"}");
    }
    bytes.push(b'\n');

    match &writer().file {
        Some(mu) => {
            let mut guard = mu.lock().expect("audit mutex poisoned");
            let _ = guard.write_all(&bytes);
        }
        None => {
            eprintln!("[audit-fallback] {}", String::from_utf8_lossy(&bytes).trim_end());
        }
    }
}

/// Redact path-like fields whose value is OUTSIDE the user's $HOME.
/// Limits info leak: a request like {"path": "/etc/shadow"} appears in the
/// audit log as {"path": "<redacted:/etc>"} so reviewers see the family of
/// the action without seeing the exact target.
fn redact(params: &Value) -> Value {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
    redact_inner(params, &home)
}

fn redact_inner(v: &Value, home: &str) -> Value {
    match v {
        Value::Object(map) => {
            let mut out = serde_json::Map::new();
            for (k, val) in map {
                if k == "content" {
                    // Never log raw write content — could include secrets.
                    out.insert(k.clone(), Value::String(format!("<redacted:{} bytes>",
                        val.as_str().map(|s| s.len()).unwrap_or(0))));
                } else if k == "path" {
                    if let Some(s) = val.as_str() {
                        if s.starts_with(home) {
                            out.insert(k.clone(), Value::String(s.to_string()));
                        } else {
                            let family = first_two_components(s);
                            out.insert(k.clone(), Value::String(format!("<redacted:{}>", family)));
                        }
                    } else {
                        out.insert(k.clone(), redact_inner(val, home));
                    }
                } else {
                    out.insert(k.clone(), redact_inner(val, home));
                }
            }
            Value::Object(out)
        }
        Value::Array(arr) => Value::Array(arr.iter().map(|x| redact_inner(x, home)).collect()),
        other => other.clone(),
    }
}

/// Return the parent directory of an absolute path, used for redaction:
///   /etc/shadow          → "/etc"
///   /var/log/syslog      → "/var/log"
///   /etc                 → "/etc"   (already a top-level family)
///   relative or empty    → input
fn first_two_components(path: &str) -> String {
    let p = std::path::Path::new(path);
    if !p.is_absolute() {
        return path.to_string();
    }
    match p.parent() {
        // No parent (e.g. "/") or just "/" — return path as-is.
        None => path.to_string(),
        Some(parent) if parent.as_os_str() == "/" || parent.as_os_str().is_empty() => path.to_string(),
        Some(parent) => parent.to_string_lossy().into_owned(),
    }
}

// ── Unit tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn redact_preserves_home_paths() {
        std::env::set_var("HOME", "/home/aditya");
        let v = redact(&json!({"path": "/home/aditya/notes.txt"}));
        assert_eq!(v["path"], "/home/aditya/notes.txt");
    }

    #[test]
    fn redact_collapses_non_home_paths() {
        std::env::set_var("HOME", "/home/aditya");
        let v = redact(&json!({"path": "/etc/shadow"}));
        let s = v["path"].as_str().unwrap();
        assert!(s.starts_with("<redacted:"));
        assert!(s.contains("/etc"));
        assert!(!s.contains("shadow"));
    }

    #[test]
    fn redact_truncates_content() {
        let v = redact(&json!({"path": "/tmp/x", "content": "secret-token-12345"}));
        let s = v["content"].as_str().unwrap();
        assert!(s.starts_with("<redacted:"));
        assert!(s.contains("18 bytes"));
    }

    #[test]
    fn redact_handles_nested_objects() {
        std::env::set_var("HOME", "/home/aditya");
        let v = redact(&json!({"outer": {"path": "/etc/passwd"}}));
        assert!(v["outer"]["path"].as_str().unwrap().starts_with("<redacted:"));
    }

    #[test]
    fn redact_passes_through_other_fields() {
        let v = redact(&json!({"unit": "nginx.service", "lines": 200}));
        assert_eq!(v["unit"], "nginx.service");
        assert_eq!(v["lines"], 200);
    }

    #[test]
    fn first_two_components_etc_passwd() {
        assert_eq!(first_two_components("/etc/passwd"), "/etc");
        assert_eq!(first_two_components("/var/log/syslog"), "/var/log");
        assert_eq!(first_two_components("/boot/grub/x.cfg"), "/boot/grub");
    }

    #[test]
    fn log_intent_does_not_panic_when_file_unavailable() {
        std::env::set_var("MCPD_AUDIT_LOG", "/dev/null/cannot-create");
        // Even with an unwritable path the call must not panic.
        log_intent("system.status", Some(&json!(1)), &json!({}), ResultClass::Ok, 100);
        std::env::remove_var("MCPD_AUDIT_LOG");
    }
}
