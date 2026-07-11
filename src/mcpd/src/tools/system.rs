/// system.rs — Read-only system information tools.
///
/// All functions here are read-only. They read from /proc and /sys.
/// No writes, no side effects, Tier 0 (auto-execute, no HITL prompt needed).
///
/// Parsing is split from IO so the parsers can be unit-tested on any OS
/// (mcpd itself runs only on Linux because /proc is Linux-specific).
use anyhow::Result;
use serde_json::{json, Value};
use std::fs;
use std::time::Duration;

pub async fn status() -> Result<Value> {
    let uptime_secs = read_uptime_secs()?;
    let mem = read_meminfo()?;
    let load = read_loadavg()?;

    Ok(json!({
        "uptime_seconds": uptime_secs,
        "uptime_human": format_uptime(uptime_secs),
        "load_avg": load,
        "memory_mb": mem,
        "hostname": read_hostname().unwrap_or_else(|_| "unknown".into()),
    }))
}

pub async fn uptime() -> Result<Value> {
    let secs = read_uptime_secs()?;
    Ok(json!({
        "uptime_seconds": secs,
        "uptime_human": format_uptime(secs),
    }))
}

pub async fn cpu() -> Result<Value> {
    let stat1 = read_cpu_stat()?;
    // Sleep then re-read to compute utilization delta over the interval.
    tokio::time::sleep(Duration::from_millis(100)).await;
    let stat2 = read_cpu_stat()?;

    Ok(json!({ "cores": cpu_usage_deltas(&stat1, &stat2) }))
}

pub async fn memory() -> Result<Value> {
    let mem = read_meminfo()?;
    Ok(json!(mem))
}

pub async fn disk() -> Result<Value> {
    let mounts = read_mounts()?;
    Ok(json!({ "filesystems": mounts }))
}

/// system.unsupported — F-35 catalogue landing pad.
///
/// Emitted by QB when the user's request has no matching tool. mcpd echoes
/// the payload back so the Controller can render a helpful UNSUPPORTED card
/// without invoking PB. No side effects, no /proc reads, no I/O — this call
/// is *deliberately* a passthrough. The point is that QB has a legitimate
/// action to emit other than a plausible-but-wrong lookalike.
///
/// Params (per schemas/system.unsupported.json):
///   requested_intent   REQUIRED — the user's raw query
///   suggestion         OPTIONAL — QB's suggested adjacent action or hint
///   alternative_actions OPTIONAL — catalogue action names QB thinks are adjacent
pub async fn unsupported(
    requested_intent: &str,
    suggestion: Option<&str>,
    alternative_actions: Option<&Value>,
) -> Result<Value> {
    Ok(json!({
        "status": "unsupported",
        "requested_intent": requested_intent,
        "suggestion": suggestion.unwrap_or(""),
        "alternative_actions": alternative_actions.cloned().unwrap_or_else(|| json!([])),
    }))
}

// ── IO wrappers ───────────────────────────────────────────────────────────────

fn read_uptime_secs() -> Result<u64> {
    parse_uptime(&fs::read_to_string("/proc/uptime")?)
}

fn read_hostname() -> Result<String> {
    Ok(fs::read_to_string("/proc/sys/kernel/hostname")?.trim().to_string())
}

fn read_loadavg() -> Result<Value> {
    Ok(parse_loadavg(&fs::read_to_string("/proc/loadavg")?))
}

fn read_meminfo() -> Result<Value> {
    Ok(parse_meminfo(&fs::read_to_string("/proc/meminfo")?))
}

fn read_cpu_stat() -> Result<Vec<CpuStat>> {
    Ok(parse_cpu_stat(&fs::read_to_string("/proc/stat")?))
}

fn read_mounts() -> Result<Vec<Value>> {
    let content = fs::read_to_string("/proc/mounts")?;
    let mut result = Vec::new();
    for mount_point in parse_mount_points(&content) {
        if let Ok(stat) = nix_statvfs(&mount_point) {
            result.push(stat);
        }
    }
    Ok(result)
}

// ── Pure parsers (testable on any OS) ─────────────────────────────────────────

fn parse_uptime(content: &str) -> Result<u64> {
    let secs: f64 = content
        .split_whitespace()
        .next()
        .ok_or_else(|| anyhow::anyhow!("Malformed /proc/uptime"))?
        .parse()?;
    Ok(secs as u64)
}

fn format_uptime(secs: u64) -> String {
    let days = secs / 86400;
    let hours = (secs % 86400) / 3600;
    let mins = (secs % 3600) / 60;
    if days > 0 {
        format!("{}d {}h {}m", days, hours, mins)
    } else if hours > 0 {
        format!("{}h {}m", hours, mins)
    } else {
        format!("{}m", mins)
    }
}

fn parse_loadavg(content: &str) -> Value {
    let parts: Vec<&str> = content.split_whitespace().collect();
    json!({
        "1min":  parts.first().and_then(|s| s.parse::<f64>().ok()).unwrap_or(0.0),
        "5min":  parts.get(1).and_then(|s| s.parse::<f64>().ok()).unwrap_or(0.0),
        "15min": parts.get(2).and_then(|s| s.parse::<f64>().ok()).unwrap_or(0.0),
    })
}

fn parse_meminfo(content: &str) -> Value {
    let mut map = std::collections::HashMap::new();
    for line in content.lines() {
        let parts: Vec<&str> = line.splitn(2, ':').collect();
        if parts.len() == 2 {
            let key = parts[0].trim();
            let val_kb: u64 = parts[1]
                .split_whitespace()
                .next()
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
            map.insert(key.to_string(), val_kb / 1024); // kB → MB
        }
    }
    json!({
        "total_mb":     map.get("MemTotal").copied().unwrap_or(0),
        "free_mb":      map.get("MemFree").copied().unwrap_or(0),
        "available_mb": map.get("MemAvailable").copied().unwrap_or(0),
        "cached_mb":    map.get("Cached").copied().unwrap_or(0),
        "swap_total_mb":map.get("SwapTotal").copied().unwrap_or(0),
        "swap_free_mb": map.get("SwapFree").copied().unwrap_or(0),
    })
}

#[derive(Default, Debug, Clone, PartialEq)]
struct CpuStat {
    name:    String,
    user:    u64,
    nice:    u64,
    system:  u64,
    idle:    u64,
    iowait:  u64,
    irq:     u64,
    softirq: u64,
}

fn parse_cpu_stat(content: &str) -> Vec<CpuStat> {
    let mut stats = Vec::new();
    for line in content.lines() {
        if !line.starts_with("cpu") {
            break;
        }
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() < 8 {
            continue;
        }
        stats.push(CpuStat {
            name:    parts[0].to_string(),
            user:    parts[1].parse().unwrap_or(0),
            nice:    parts[2].parse().unwrap_or(0),
            system:  parts[3].parse().unwrap_or(0),
            idle:    parts[4].parse().unwrap_or(0),
            iowait:  parts[5].parse().unwrap_or(0),
            irq:     parts[6].parse().unwrap_or(0),
            softirq: parts[7].parse().unwrap_or(0),
        });
    }
    stats
}

fn cpu_usage_deltas(stat1: &[CpuStat], stat2: &[CpuStat]) -> Vec<Value> {
    let mut cores = Vec::new();
    for (s1, s2) in stat1.iter().zip(stat2.iter()) {
        let idle1 = s1.idle + s1.iowait;
        let idle2 = s2.idle + s2.iowait;
        let total1: u64 = s1.user + s1.nice + s1.system + s1.idle + s1.iowait + s1.irq + s1.softirq;
        let total2: u64 = s2.user + s2.nice + s2.system + s2.idle + s2.iowait + s2.irq + s2.softirq;
        let total_delta = total2.saturating_sub(total1);
        let idle_delta = idle2.saturating_sub(idle1);
        let usage = if total_delta == 0 {
            0.0
        } else {
            (1.0 - idle_delta as f64 / total_delta as f64) * 100.0
        };
        cores.push(json!({
            "name": s1.name,
            "usage_percent": (usage * 10.0).round() / 10.0,
        }));
    }
    cores
}

fn parse_mount_points(content: &str) -> Vec<String> {
    let mut out = Vec::new();
    for line in content.lines() {
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() < 3 {
            continue;
        }
        let mount_point = parts[1];
        let fs_type = parts[2];
        if is_pseudo_fs(fs_type) {
            continue;
        }
        out.push(mount_point.to_string());
    }
    out
}

fn is_pseudo_fs(fs_type: &str) -> bool {
    matches!(fs_type,
        "proc" | "sysfs" | "devtmpfs" | "cgroup" | "cgroup2"
        | "tmpfs" | "devpts" | "securityfs" | "debugfs" | "hugetlbfs"
        | "mqueue" | "fusectl" | "binfmt_misc"
    )
}

fn nix_statvfs(path: &str) -> Result<Value> {
    use std::ffi::CString;
    use std::mem;

    let c_path = CString::new(path)?;
    let mut stat: libc::statvfs = unsafe { mem::zeroed() };
    let ret = unsafe { libc::statvfs(c_path.as_ptr(), &mut stat) };
    if ret != 0 {
        anyhow::bail!("statvfs failed for {}", path);
    }
    let block_size = stat.f_frsize as u64;
    let total_mb = (stat.f_blocks as u64) * block_size / 1_048_576;
    let free_mb = (stat.f_bfree as u64) * block_size / 1_048_576;
    let used_mb = total_mb.saturating_sub(free_mb);
    let used_pct = if total_mb == 0 { 0.0 } else { used_mb as f64 / total_mb as f64 * 100.0 };
    Ok(json!({
        "mount": path,
        "total_mb": total_mb,
        "used_mb": used_mb,
        "free_mb": free_mb,
        "used_percent": (used_pct * 10.0).round() / 10.0,
    }))
}

// ── Unit tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_uptime_normal() {
        let secs = parse_uptime("12345.67 9876.54\n").unwrap();
        assert_eq!(secs, 12345);
    }

    #[test]
    fn parse_uptime_truncated_returns_err() {
        assert!(parse_uptime("").is_err());
    }

    #[test]
    fn parse_uptime_garbage_returns_err() {
        assert!(parse_uptime("not-a-number\n").is_err());
    }

    #[test]
    fn parse_uptime_single_field_ok() {
        let secs = parse_uptime("42.0").unwrap();
        assert_eq!(secs, 42);
    }

    #[test]
    fn format_uptime_seconds_only() {
        assert_eq!(format_uptime(30), "0m");
    }

    #[test]
    fn format_uptime_minutes() {
        assert_eq!(format_uptime(300), "5m");
    }

    #[test]
    fn format_uptime_hours_minutes() {
        assert_eq!(format_uptime(3661), "1h 1m");
    }

    #[test]
    fn format_uptime_days() {
        assert_eq!(format_uptime(86400 * 3 + 3600 * 2 + 60 * 5), "3d 2h 5m");
    }

    #[test]
    fn parse_loadavg_normal() {
        let v = parse_loadavg("0.50 1.00 1.50 1/234 5678\n");
        assert_eq!(v["1min"], 0.5);
        assert_eq!(v["5min"], 1.0);
        assert_eq!(v["15min"], 1.5);
    }

    #[test]
    fn parse_loadavg_empty_yields_zeroes() {
        let v = parse_loadavg("");
        assert_eq!(v["1min"], 0.0);
        assert_eq!(v["5min"], 0.0);
        assert_eq!(v["15min"], 0.0);
    }

    #[test]
    fn parse_meminfo_normal() {
        let s = "MemTotal:       16384000 kB\n\
                 MemFree:         4096000 kB\n\
                 MemAvailable:    8192000 kB\n\
                 Cached:          2048000 kB\n\
                 SwapTotal:       2097152 kB\n\
                 SwapFree:        2097152 kB\n";
        let v = parse_meminfo(s);
        assert_eq!(v["total_mb"], 16000);
        assert_eq!(v["free_mb"], 4000);
        assert_eq!(v["available_mb"], 8000);
        assert_eq!(v["cached_mb"], 2000);
        assert_eq!(v["swap_total_mb"], 2048);
        assert_eq!(v["swap_free_mb"], 2048);
    }

    #[test]
    fn parse_meminfo_older_kernel_no_memavailable() {
        // Older kernels (< 3.14) don't expose MemAvailable.
        let s = "MemTotal:       16384000 kB\n\
                 MemFree:         4096000 kB\n";
        let v = parse_meminfo(s);
        assert_eq!(v["total_mb"], 16000);
        assert_eq!(v["available_mb"], 0); // graceful default
    }

    #[test]
    fn parse_meminfo_empty_yields_zeroes() {
        let v = parse_meminfo("");
        assert_eq!(v["total_mb"], 0);
        assert_eq!(v["free_mb"], 0);
    }

    #[test]
    fn parse_cpu_stat_normal() {
        let s = "cpu  100 0 50 1000 10 0 5 0 0 0\n\
                 cpu0 50 0 25 500 5 0 2 0 0 0\n\
                 cpu1 50 0 25 500 5 0 3 0 0 0\n\
                 intr 12345 0 0 0\n";
        let stats = parse_cpu_stat(s);
        assert_eq!(stats.len(), 3);
        assert_eq!(stats[0].name, "cpu");
        assert_eq!(stats[0].user, 100);
        assert_eq!(stats[0].idle, 1000);
        assert_eq!(stats[1].name, "cpu0");
    }

    #[test]
    fn parse_cpu_stat_truncated_line_skipped() {
        let s = "cpu  100 0 50\n\
                 cpu0 50 0 25 500 5 0 2 0 0 0\n";
        let stats = parse_cpu_stat(s);
        assert_eq!(stats.len(), 1);
        assert_eq!(stats[0].name, "cpu0");
    }

    #[test]
    fn cpu_usage_deltas_idle() {
        let s1 = vec![CpuStat { name: "cpu".into(), idle: 100, ..Default::default() }];
        let s2 = vec![CpuStat { name: "cpu".into(), idle: 200, ..Default::default() }];
        let d = cpu_usage_deltas(&s1, &s2);
        assert_eq!(d[0]["usage_percent"], 0.0); // all idle
    }

    #[test]
    fn cpu_usage_deltas_full_load() {
        let s1 = vec![CpuStat { name: "cpu".into(), idle: 100, ..Default::default() }];
        let s2 = vec![CpuStat { name: "cpu".into(), user: 100, idle: 100, ..Default::default() }];
        let d = cpu_usage_deltas(&s1, &s2);
        assert_eq!(d[0]["usage_percent"], 100.0); // 100 user ticks, 0 idle delta
    }

    #[test]
    fn parse_mount_points_filters_pseudo() {
        let s = "/dev/sda1 /          ext4   rw,relatime 0 0\n\
                 proc      /proc      proc   rw,nosuid   0 0\n\
                 tmpfs     /run       tmpfs  rw,nosuid   0 0\n\
                 /dev/sda2 /home      ext4   rw,relatime 0 0\n";
        let mounts = parse_mount_points(s);
        assert_eq!(mounts, vec!["/", "/home"]);
    }

    #[test]
    fn parse_mount_points_handles_empty() {
        assert!(parse_mount_points("").is_empty());
    }

    #[test]
    fn parse_mount_points_skips_malformed_lines() {
        let s = "incomplete-line\n\
                 /dev/sda1 /           ext4 rw 0 0\n";
        let mounts = parse_mount_points(s);
        assert_eq!(mounts, vec!["/"]);
    }

    #[test]
    fn is_pseudo_fs_known_types() {
        assert!(is_pseudo_fs("proc"));
        assert!(is_pseudo_fs("sysfs"));
        assert!(is_pseudo_fs("cgroup2"));
        assert!(!is_pseudo_fs("ext4"));
        assert!(!is_pseudo_fs("btrfs"));
        assert!(!is_pseudo_fs("xfs"));
    }
}
