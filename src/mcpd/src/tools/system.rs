/// system.rs — Read-only system information tools.
///
/// All functions here are read-only. They read from /proc and /sys.
/// No writes, no side effects, Tier 0 (auto-execute, no HITL prompt needed).
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
    // Read /proc/stat for CPU utilization
    let stat1 = read_cpu_stat()?;
    // Brief sleep then read again to compute utilization delta
    tokio::time::sleep(Duration::from_millis(100)).await;
    let stat2 = read_cpu_stat()?;

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

    Ok(json!({ "cores": cores }))
}

pub async fn memory() -> Result<Value> {
    let mem = read_meminfo()?;
    Ok(json!(mem))
}

pub async fn disk() -> Result<Value> {
    let mounts = read_mounts()?;
    Ok(json!({ "filesystems": mounts }))
}

// ── Internal helpers ──────────────────────────────────────────────────────────

fn read_uptime_secs() -> Result<u64> {
    let content = fs::read_to_string("/proc/uptime")?;
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

fn read_hostname() -> Result<String> {
    Ok(fs::read_to_string("/proc/sys/kernel/hostname")?.trim().to_string())
}

fn read_loadavg() -> Result<Value> {
    let content = fs::read_to_string("/proc/loadavg")?;
    let parts: Vec<&str> = content.split_whitespace().collect();
    Ok(json!({
        "1min":  parts.first().and_then(|s| s.parse::<f64>().ok()).unwrap_or(0.0),
        "5min":  parts.get(1).and_then(|s| s.parse::<f64>().ok()).unwrap_or(0.0),
        "15min": parts.get(2).and_then(|s| s.parse::<f64>().ok()).unwrap_or(0.0),
    }))
}

fn read_meminfo() -> Result<Value> {
    let content = fs::read_to_string("/proc/meminfo")?;
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
            map.insert(key.to_string(), val_kb / 1024); // convert kB to MB
        }
    }
    Ok(json!({
        "total_mb":     map.get("MemTotal").copied().unwrap_or(0),
        "free_mb":      map.get("MemFree").copied().unwrap_or(0),
        "available_mb": map.get("MemAvailable").copied().unwrap_or(0),
        "cached_mb":    map.get("Cached").copied().unwrap_or(0),
        "swap_total_mb":map.get("SwapTotal").copied().unwrap_or(0),
        "swap_free_mb": map.get("SwapFree").copied().unwrap_or(0),
    }))
}

#[derive(Default)]
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

fn read_cpu_stat() -> Result<Vec<CpuStat>> {
    let content = fs::read_to_string("/proc/stat")?;
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
    Ok(stats)
}

fn read_mounts() -> Result<Vec<Value>> {
    let content = fs::read_to_string("/proc/mounts")?;
    let mut result = Vec::new();
    for line in content.lines() {
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() < 3 {
            continue;
        }
        let mount_point = parts[1];
        // Skip pseudo-filesystems
        let fs_type = parts[2];
        if matches!(fs_type, "proc" | "sysfs" | "devtmpfs" | "cgroup" | "cgroup2"
            | "tmpfs" | "devpts" | "securityfs" | "debugfs" | "hugetlbfs"
            | "mqueue" | "fusectl" | "binfmt_misc") {
            continue;
        }
        // Try to get statvfs info
        if let Ok(stat) = nix_statvfs(mount_point) {
            result.push(stat);
        }
    }
    Ok(result)
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
