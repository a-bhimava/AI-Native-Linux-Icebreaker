/// network.rs — Read-only network introspection (Phase 1 / M1.7).
///
/// Tools:
///   network.status     Tier 0  Interface list + default route
///   network.dns.read   Tier 0  /etc/resolv.conf nameservers
///
/// Write-side methods (`network.firewall.*`, `network.dns.set`) are deferred
/// to Phase 5 with the HITL gate; in Phase 1 they're stubbed as
/// `{"status": "not_implemented"}` so the catalogue stays stable.
///
/// `network.status` uses getifaddrs(3) (cross-platform via nix) for the
/// interface list and parses /proc/net/route on Linux for the default
/// gateway. On macOS dev builds, the default route fetch returns
/// {"status": "unavailable", ...} since /proc/net is Linux-only.
use anyhow::Result;
use serde_json::{json, Value};
use std::fs;

pub async fn status() -> Result<Value> {
    let interfaces = read_interfaces()?;
    let default_route = read_default_route_v4();
    Ok(json!({
        "interfaces": interfaces,
        "default_route_v4": default_route,
    }))
}

pub async fn dns_read() -> Result<Value> {
    Ok(parse_resolv_conf(&fs::read_to_string("/etc/resolv.conf")?))
}

pub async fn firewall_status() -> Result<Value> {
    Ok(json!({
        "status": "not_implemented",
        "phase": 5,
        "reason": "network.firewall.* lands in Phase 5 with the HITL gate (Tier 3)."
    }))
}

pub async fn dns_set() -> Result<Value> {
    Ok(json!({
        "status": "not_implemented",
        "phase": 5,
        "reason": "network.dns.set is Tier 2 and lands in Phase 5 with COW."
    }))
}

// ── Implementation ───────────────────────────────────────────────────────────

fn read_interfaces() -> Result<Vec<Value>> {
    let mut by_name: std::collections::BTreeMap<String, InterfaceAcc> = Default::default();

    for ifaddr in nix::ifaddrs::getifaddrs()? {
        let entry = by_name.entry(ifaddr.interface_name.clone()).or_default();
        entry.name = ifaddr.interface_name.clone();
        entry.is_up = ifaddr.flags.contains(nix::net::if_::InterfaceFlags::IFF_UP);
        entry.is_loopback = ifaddr.flags.contains(nix::net::if_::InterfaceFlags::IFF_LOOPBACK);

        if let Some(addr) = ifaddr.address {
            if let Some(ipv4) = addr.as_sockaddr_in() {
                entry.ipv4.push(std::net::Ipv4Addr::from(ipv4.ip()).to_string());
            } else if let Some(ipv6) = addr.as_sockaddr_in6() {
                entry.ipv6.push(ipv6.ip().to_string());
            }
        }
    }

    Ok(by_name.into_values().map(|i| json!({
        "name": i.name,
        "is_up": i.is_up,
        "is_loopback": i.is_loopback,
        "ipv4": i.ipv4,
        "ipv6": i.ipv6,
    })).collect())
}

#[derive(Default)]
struct InterfaceAcc {
    name: String,
    is_up: bool,
    is_loopback: bool,
    ipv4: Vec<String>,
    ipv6: Vec<String>,
}

#[cfg(target_os = "linux")]
fn read_default_route_v4() -> Value {
    match fs::read_to_string("/proc/net/route") {
        Ok(s) => parse_default_route_v4(&s),
        Err(e) => json!({"status": "unavailable", "reason": format!("read /proc/net/route: {}", e)}),
    }
}

#[cfg(not(target_os = "linux"))]
fn read_default_route_v4() -> Value {
    json!({"status": "unavailable", "reason": "default route lookup is Linux-only in v1"})
}

/// Parse /proc/net/route format. Header line is field names; data lines are
/// tab-separated with hex IP (little-endian byte order on x86).
/// Default route is the entry with Destination=00000000.
fn parse_default_route_v4(content: &str) -> Value {
    for (i, line) in content.lines().enumerate() {
        if i == 0 {
            continue; // header
        }
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() < 3 {
            continue;
        }
        if parts[1] != "00000000" {
            continue;
        }
        let iface = parts[0];
        let gateway_hex = parts[2];
        let gateway = hex_le_to_ipv4(gateway_hex).unwrap_or_else(|| "?".into());
        return json!({"interface": iface, "gateway": gateway});
    }
    json!({"status": "no_default_route"})
}

fn hex_le_to_ipv4(hex: &str) -> Option<String> {
    if hex.len() != 8 {
        return None;
    }
    let n = u32::from_str_radix(hex, 16).ok()?;
    let bytes = n.to_le_bytes(); // little-endian wire order
    Some(format!("{}.{}.{}.{}", bytes[0], bytes[1], bytes[2], bytes[3]))
}

fn parse_resolv_conf(content: &str) -> Value {
    let mut nameservers = Vec::new();
    let mut search = Vec::new();
    let mut domain = None;
    for line in content.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        let mut parts = line.split_whitespace();
        match parts.next() {
            Some("nameserver") => {
                if let Some(ip) = parts.next() {
                    nameservers.push(ip.to_string());
                }
            }
            Some("search") => {
                for s in parts {
                    search.push(s.to_string());
                }
            }
            Some("domain") => {
                domain = parts.next().map(String::from);
            }
            _ => {}
        }
    }
    json!({
        "nameservers": nameservers,
        "search": search,
        "domain": domain,
    })
}

// ── Unit tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_resolv_normal() {
        let s = "# Generated by NetworkManager\n\
                 nameserver 8.8.8.8\n\
                 nameserver 1.1.1.1\n\
                 search corp.example.com home\n\
                 domain example.com\n";
        let v = parse_resolv_conf(s);
        assert_eq!(v["nameservers"][0], "8.8.8.8");
        assert_eq!(v["nameservers"][1], "1.1.1.1");
        assert_eq!(v["search"][0], "corp.example.com");
        assert_eq!(v["search"][1], "home");
        assert_eq!(v["domain"], "example.com");
    }

    #[test]
    fn parse_resolv_handles_comments_and_blanks() {
        let s = "\n# header comment\n; sometimes\nnameserver 192.0.2.1\n\n";
        let v = parse_resolv_conf(s);
        assert_eq!(v["nameservers"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn parse_resolv_empty() {
        let v = parse_resolv_conf("");
        assert!(v["nameservers"].as_array().unwrap().is_empty());
        assert!(v["search"].as_array().unwrap().is_empty());
        assert!(v["domain"].is_null());
    }

    #[test]
    fn hex_le_to_ipv4_localhost() {
        // /proc/net/route stores IPs little-endian: 127.0.0.1 → 0100007F
        assert_eq!(hex_le_to_ipv4("0100007F"), Some("127.0.0.1".into()));
    }

    #[test]
    fn hex_le_to_ipv4_zero_default_dest() {
        assert_eq!(hex_le_to_ipv4("00000000"), Some("0.0.0.0".into()));
    }

    #[test]
    fn hex_le_to_ipv4_typical_gateway() {
        // 192.168.1.1 → 0101A8C0 (LE bytes: 0xC0, 0xA8, 0x01, 0x01)
        assert_eq!(hex_le_to_ipv4("0101A8C0"), Some("192.168.1.1".into()));
    }

    #[test]
    fn hex_le_to_ipv4_invalid_length() {
        assert_eq!(hex_le_to_ipv4("123"), None);
        assert_eq!(hex_le_to_ipv4("1234567890"), None);
    }

    #[test]
    fn parse_default_route_picks_zero_dest() {
        let s = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n\
                 eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n\
                 eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n";
        let v = parse_default_route_v4(s);
        assert_eq!(v["interface"], "eth0");
        assert_eq!(v["gateway"], "192.168.1.1");
    }

    #[test]
    fn parse_default_route_none_present() {
        let s = "Iface\tDestination\tGateway\tFlags\n\
                 eth0\t0001A8C0\t00000000\t0001\n";
        let v = parse_default_route_v4(s);
        assert_eq!(v["status"], "no_default_route");
    }

    #[test]
    fn parse_default_route_empty() {
        let v = parse_default_route_v4("");
        assert_eq!(v["status"], "no_default_route");
    }
}
