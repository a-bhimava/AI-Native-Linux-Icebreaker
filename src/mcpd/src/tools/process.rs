/// process.rs — Read-only process inspection tools.
///
/// Reads from /proc only. No signals sent, no processes killed.
/// Tier 0 — auto-execute, no HITL prompt needed.
use anyhow::Result;
use serde_json::{json, Value};
use std::fs;

pub async fn list() -> Result<Value> {
    let mut procs = Vec::new();

    let proc_dir = fs::read_dir("/proc")?;
    for entry in proc_dir {
        let entry = entry?;
        let name = entry.file_name();
        let name_str = name.to_string_lossy();

        // Only numeric directories are PIDs
        let pid: u32 = match name_str.parse() {
            Ok(n) => n,
            Err(_) => continue,
        };

        if let Ok(info) = read_proc_info(pid) {
            procs.push(info);
        }
    }

    // Sort by CPU usage descending
    procs.sort_by(|a, b| {
        let a_cpu = a["cpu_percent"].as_f64().unwrap_or(0.0);
        let b_cpu = b["cpu_percent"].as_f64().unwrap_or(0.0);
        b_cpu.partial_cmp(&a_cpu).unwrap_or(std::cmp::Ordering::Equal)
    });

    Ok(json!({
        "count": procs.len(),
        "processes": procs,
    }))
}

pub async fn inspect(pid: u32) -> Result<Value> {
    let info = read_proc_info(pid)?;
    let cmdline = read_cmdline(pid)?;
    let status = read_status(pid)?;
    let fds = count_fds(pid);

    Ok(json!({
        "pid": pid,
        "info": info,
        "cmdline": cmdline,
        "status": status,
        "open_fds": fds,
    }))
}

// ── Internal helpers ──────────────────────────────────────────────────────────

fn read_proc_info(pid: u32) -> Result<Value> {
    let stat_path = format!("/proc/{}/stat", pid);
    let content = fs::read_to_string(&stat_path)?;

    // /proc/PID/stat format:
    // pid (comm) state ppid pgroup session ...
    // comm can contain spaces and parens, so parse carefully
    let comm_start = content.find('(').ok_or_else(|| anyhow::anyhow!("Malformed stat"))?;
    let comm_end = content.rfind(')').ok_or_else(|| anyhow::anyhow!("Malformed stat"))?;
    let comm = &content[comm_start + 1..comm_end];
    let rest: Vec<&str> = content[comm_end + 2..].split_whitespace().collect();

    let state = rest.first().copied().unwrap_or("?");
    let ppid: u32 = rest.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    // utime is field 11 (0-indexed from rest), stime is field 12
    let utime: u64 = rest.get(11).and_then(|s| s.parse().ok()).unwrap_or(0);
    let stime: u64 = rest.get(12).and_then(|s| s.parse().ok()).unwrap_or(0);
    let vsize_bytes: u64 = rest.get(20).and_then(|s| s.parse().ok()).unwrap_or(0);
    let rss_pages: i64 = rest.get(21).and_then(|s| s.parse().ok()).unwrap_or(0);

    let page_size = unsafe { libc::sysconf(libc::_SC_PAGESIZE) } as u64;
    let rss_mb = (rss_pages.max(0) as u64 * page_size) / 1_048_576;
    let vsize_mb = vsize_bytes / 1_048_576;

    // Rough CPU % (total ticks / uptime — not perfectly accurate but good enough)
    let ticks_per_sec = unsafe { libc::sysconf(libc::_SC_CLK_TCK) } as f64;
    let uptime_secs = fs::read_to_string("/proc/uptime")
        .ok()
        .and_then(|s| s.split_whitespace().next().and_then(|v| v.parse::<f64>().ok()))
        .unwrap_or(1.0);
    let total_ticks = (utime + stime) as f64;
    let cpu_pct = (total_ticks / ticks_per_sec / uptime_secs * 100.0 * 10.0).round() / 10.0;

    Ok(json!({
        "pid": pid,
        "name": comm,
        "state": state,
        "ppid": ppid,
        "cpu_percent": cpu_pct,
        "rss_mb": rss_mb,
        "vsize_mb": vsize_mb,
    }))
}

fn read_cmdline(pid: u32) -> Result<String> {
    let path = format!("/proc/{}/cmdline", pid);
    let bytes = fs::read(&path)?;
    // cmdline is null-separated
    Ok(bytes
        .split(|&b| b == 0)
        .filter(|s| !s.is_empty())
        .map(|s| String::from_utf8_lossy(s).into_owned())
        .collect::<Vec<_>>()
        .join(" "))
}

fn read_status(pid: u32) -> Result<Value> {
    let path = format!("/proc/{}/status", pid);
    let content = fs::read_to_string(&path)?;
    let mut map = serde_json::Map::new();
    for line in content.lines() {
        let parts: Vec<&str> = line.splitn(2, ':').collect();
        if parts.len() == 2 {
            map.insert(
                parts[0].trim().to_lowercase(),
                Value::String(parts[1].trim().to_string()),
            );
        }
    }
    Ok(Value::Object(map))
}

fn count_fds(pid: u32) -> usize {
    let path = format!("/proc/{}/fd", pid);
    fs::read_dir(&path)
        .map(|entries| entries.count())
        .unwrap_or(0)
}
