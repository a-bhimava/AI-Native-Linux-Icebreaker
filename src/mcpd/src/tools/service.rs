/// service.rs — systemd unit control via D-Bus (Phase 1 / M1.6).
///
/// Methods exposed:
///   service.start(unit)    Tier 2  org.freedesktop.systemd1.Manager.StartUnit
///   service.stop(unit)     Tier 2  org.freedesktop.systemd1.Manager.StopUnit
///   service.restart(unit)  Tier 2  org.freedesktop.systemd1.Manager.RestartUnit
///   service.logs(unit, n)  Tier 0  journalctl -u <unit> -n <n> --no-pager
///
/// Graceful degradation (P1-F3): if the system bus can't be reached
/// (container without /run/dbus, broken socket, Linux without systemd) the
/// tools return `{"status": "unavailable", "reason": "..."}` and mcpd stays
/// up. The bus connection is lazily initialized on first use and cached.
///
/// On macOS (dev loop): every service.* call returns `unavailable`.
///
/// Unit-name validation is strict: only `[A-Za-z0-9._@:-]` and `\`. Anything
/// else is refused before we hit D-Bus or spawn a subprocess. This blocks
/// shell-metachar injection at the Controller boundary.
use anyhow::{bail, Result};
use serde_json::{json, Value};

#[cfg(target_os = "linux")]
use tokio::sync::OnceCell;

#[cfg(target_os = "linux")]
static BUS: OnceCell<std::result::Result<zbus::Connection, String>> = OnceCell::const_new();

const MAX_UNIT_NAME_LEN: usize = 256;
const MAX_LOG_LINES: u64 = 10_000;

// ── Public tool entry points ─────────────────────────────────────────────────

pub async fn start(unit: &str) -> Result<Value> {
    require_valid_unit(unit)?;
    invoke_manager(unit, "StartUnit").await
}

pub async fn stop(unit: &str) -> Result<Value> {
    require_valid_unit(unit)?;
    invoke_manager(unit, "StopUnit").await
}

pub async fn restart(unit: &str) -> Result<Value> {
    require_valid_unit(unit)?;
    invoke_manager(unit, "RestartUnit").await
}

pub async fn logs(unit: &str, lines: u64) -> Result<Value> {
    require_valid_unit(unit)?;
    let lines = lines.min(MAX_LOG_LINES);
    fetch_logs(unit, lines).await
}

// ── Implementation ───────────────────────────────────────────────────────────

#[cfg(target_os = "linux")]
async fn try_bus() -> std::result::Result<&'static zbus::Connection, String> {
    let stored = BUS
        .get_or_init(|| async {
            match zbus::Connection::system().await {
                Ok(c) => Ok(c),
                Err(e) => Err(format!("system bus not reachable: {}", e)),
            }
        })
        .await;
    match stored {
        Ok(c) => Ok(c),
        Err(msg) => Err(msg.clone()),
    }
}

#[cfg(target_os = "linux")]
async fn invoke_manager(unit: &str, method: &str) -> Result<Value> {
    let bus = match try_bus().await {
        Ok(c) => c,
        Err(reason) => return Ok(unavailable(reason)),
    };
    let proxy = match zbus::Proxy::new(
        bus,
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
    )
    .await
    {
        Ok(p) => p,
        Err(e) => return Ok(unavailable(format!("Proxy::new failed: {}", e))),
    };

    // Per systemd's bus API, the second arg is the start mode:
    //   "replace" cancels any queued conflicting job and starts a new one.
    match proxy.call::<_, _, zbus::zvariant::OwnedObjectPath>(method, &(unit, "replace")).await {
        Ok(job) => Ok(json!({
            "status": "ok",
            "unit": unit,
            "operation": method,
            "job_path": job.as_str(),
        })),
        Err(e) => Ok(json!({
            "status": "err",
            "unit": unit,
            "operation": method,
            "reason": e.to_string(),
        })),
    }
}

#[cfg(not(target_os = "linux"))]
async fn invoke_manager(_unit: &str, _method: &str) -> Result<Value> {
    Ok(unavailable("systemd not available on this platform"))
}

async fn fetch_logs(unit: &str, lines: u64) -> Result<Value> {
    // Use an explicit arg vec — never shell=true. The require_valid_unit gate
    // above already excludes shell metachars, but defense in depth.
    let result = tokio::process::Command::new("journalctl")
        .arg("-u").arg(unit)
        .arg("-n").arg(lines.to_string())
        .arg("--no-pager")
        .arg("--output=cat") // bare lines; the structured JSON of journald is overkill for v1
        .output()
        .await;

    match result {
        Ok(out) if out.status.success() => Ok(json!({
            "status": "ok",
            "unit": unit,
            "lines_requested": lines,
            "content": String::from_utf8_lossy(&out.stdout).into_owned(),
        })),
        Ok(out) => Ok(json!({
            "status": "err",
            "unit": unit,
            "exit_code": out.status.code(),
            "stderr": String::from_utf8_lossy(&out.stderr).into_owned(),
        })),
        Err(e) => Ok(unavailable(format!("journalctl unavailable: {}", e))),
    }
}

fn unavailable(reason: impl Into<String>) -> Value {
    json!({
        "status": "unavailable",
        "reason": reason.into(),
    })
}

fn require_valid_unit(name: &str) -> Result<()> {
    if !is_valid_unit_name(name) {
        bail!("invalid unit name (allowed: [A-Za-z0-9._@:\\-], length 1..{}): {:?}",
              MAX_UNIT_NAME_LEN, name);
    }
    Ok(())
}

fn is_valid_unit_name(name: &str) -> bool {
    if name.is_empty() || name.len() > MAX_UNIT_NAME_LEN {
        return false;
    }
    // systemd-allowed chars (sufficient subset for v1: nginx.service, getty@tty1, dbus, etc.)
    name.chars().all(|c|
        c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '@' | ':' | '\\')
    )
}

// ── Unit tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_unit_names() {
        assert!(is_valid_unit_name("cron"));
        assert!(is_valid_unit_name("nginx.service"));
        assert!(is_valid_unit_name("getty@tty1.service"));
        assert!(is_valid_unit_name("docker.socket"));
        assert!(is_valid_unit_name("user-1000.slice"));
        assert!(is_valid_unit_name("system-getty.slice"));
        assert!(is_valid_unit_name("foo:bar.service")); // colon allowed (template instance)
    }

    #[test]
    fn invalid_unit_names_rejected() {
        assert!(!is_valid_unit_name(""));
        assert!(!is_valid_unit_name(" "));
        assert!(!is_valid_unit_name("nginx; rm -rf /"));
        assert!(!is_valid_unit_name("nginx`whoami`"));
        assert!(!is_valid_unit_name("nginx$(echo hi)"));
        assert!(!is_valid_unit_name("nginx | cat /etc/passwd"));
        assert!(!is_valid_unit_name("nginx && cat /etc/passwd"));
        assert!(!is_valid_unit_name("nginx\nfoo"));
        assert!(!is_valid_unit_name("nginx\tfoo"));
        assert!(!is_valid_unit_name("nginx/../etc/shadow"));
        assert!(!is_valid_unit_name("nginx\0null"));
    }

    #[test]
    fn unit_name_length_capped() {
        let too_long = "a".repeat(MAX_UNIT_NAME_LEN + 1);
        assert!(!is_valid_unit_name(&too_long));
        let just_right = "a".repeat(MAX_UNIT_NAME_LEN);
        assert!(is_valid_unit_name(&just_right));
    }

    #[test]
    fn unavailable_shape() {
        let v = unavailable("foo");
        assert_eq!(v["status"], "unavailable");
        assert_eq!(v["reason"], "foo");
    }

    #[tokio::test]
    async fn service_start_invalid_unit_is_err() {
        // No bus required — validation rejects before we attempt D-Bus.
        let r = start("nginx; rm -rf /").await;
        assert!(r.is_err());
        let msg = format!("{}", r.unwrap_err());
        assert!(msg.contains("invalid unit name"));
    }

    #[cfg(not(target_os = "linux"))]
    #[tokio::test]
    async fn macos_service_start_returns_unavailable() {
        let r = start("cron.service").await.unwrap();
        assert_eq!(r["status"], "unavailable");
    }

    #[tokio::test]
    async fn logs_invalid_unit_is_err() {
        let r = logs("foo; cat", 10).await;
        assert!(r.is_err());
    }

    #[tokio::test]
    async fn logs_caps_line_count() {
        // Pass a huge value; we expect the implementation to cap it without
        // erroring. Result depends on journalctl availability, but no panic.
        let _ = logs("definitely-not-a-real-unit-aaaaaaaa", u64::MAX).await;
    }
}
