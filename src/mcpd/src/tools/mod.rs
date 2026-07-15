use serde_json::{json, Value};

use crate::schema;

pub mod fs;
pub mod network;
pub mod package;
pub mod process;
pub mod service;
pub mod system;

/// Static descriptor for one tool entry in the discovery catalogue.
/// The actual JSON Schema for params is pulled from `schema::schema_json()`
/// at catalogue-build time, so the schema in the catalogue stays in lock-step
/// with the schema used for validation.
struct ToolDescriptor {
    name: &'static str,
    description: &'static str,
    category: &'static str,
    tier: u8,
    read_only: bool,
}

const TOOLS: &[ToolDescriptor] = &[
    ToolDescriptor { name: "system.status", description: "Returns overall system health summary (uptime, load, memory, disk).",
                     category: "system", tier: 0, read_only: true },
    ToolDescriptor { name: "system.uptime", description: "Returns system uptime in seconds and human-readable form.",
                     category: "system", tier: 0, read_only: true },
    ToolDescriptor { name: "system.cpu", description: "Returns CPU usage per core and overall utilization percentage.",
                     category: "system", tier: 0, read_only: true },
    ToolDescriptor { name: "system.memory", description: "Returns total, used, free, and cached memory in MB.",
                     category: "system", tier: 0, read_only: true },
    ToolDescriptor { name: "system.disk", description: "Returns disk usage for all mounted filesystems.",
                     category: "system", tier: 0, read_only: true },
    // F-35: catalogue landing pad. QB uses this when a query has no matching tool,
    // instead of silently substituting a lookalike (e.g. fs.list for "cd").
    // mcpd echoes the payload back; the Controller renders a friendly UNSUPPORTED
    // card and never invokes PB. Read-only, Tier 0, always auto-approved.
    ToolDescriptor { name: "system.unsupported",
                     description: "Landing pad for user intents with no matching tool. Echoes back requested_intent + suggestion. Never a lookalike (F-35).",
                     category: "system", tier: 0, read_only: true },
    ToolDescriptor { name: "process.list", description: "Lists all running processes with PID, name, CPU%, and memory%.",
                     category: "process", tier: 0, read_only: true },
    ToolDescriptor { name: "process.inspect", description: "Returns detailed info about a specific process by PID.",
                     category: "process", tier: 0, read_only: true },
    ToolDescriptor { name: "fs.read", description: "Read a UTF-8 text file under the whitelist roots.",
                     category: "fs", tier: 0, read_only: true },
    ToolDescriptor { name: "fs.list", description: "List directory entries (name, type, size).",
                     category: "fs", tier: 0, read_only: true },
    ToolDescriptor { name: "fs.stat", description: "Return file metadata (size, mode, uid/gid, mtime).",
                     category: "fs", tier: 0, read_only: true },
    ToolDescriptor { name: "fs.write", description: "Write UTF-8 content to a file. Tier 1 inside safe $HOME, Tier 3 (COW gate) elsewhere.",
                     category: "fs", tier: 1, read_only: false },
    ToolDescriptor { name: "fs.delete", description: "Delete a file. Always Tier 3 (COW gate required).",
                     category: "fs", tier: 3, read_only: false },
    ToolDescriptor { name: "service.start", description: "Start a systemd unit.",
                     category: "service", tier: 2, read_only: false },
    ToolDescriptor { name: "service.stop", description: "Stop a systemd unit.",
                     category: "service", tier: 2, read_only: false },
    ToolDescriptor { name: "service.restart", description: "Restart a systemd unit.",
                     category: "service", tier: 2, read_only: false },
    ToolDescriptor { name: "service.logs", description: "Tail a systemd unit's journal.",
                     category: "service", tier: 0, read_only: true },
    ToolDescriptor { name: "network.status", description: "Interface list + IPv4 default route.",
                     category: "network", tier: 0, read_only: true },
    ToolDescriptor { name: "network.dns.read", description: "Parse /etc/resolv.conf (nameservers, search, domain).",
                     category: "network", tier: 0, read_only: true },
    ToolDescriptor { name: "package.query", description: "Query installed Debian packages by substring (dpkg-query).",
                     category: "package", tier: 0, read_only: true },
    ToolDescriptor { name: "package.install", description: "Install a Debian package (returns COW approval ticket).",
                     category: "package", tier: 2, read_only: false },
    ToolDescriptor { name: "package.remove", description: "Remove a Debian package (returns COW approval ticket).",
                     category: "package", tier: 2, read_only: false },
    ToolDescriptor { name: "package.upgrade", description: "Upgrade a Debian package (returns COW approval ticket).",
                     category: "package", tier: 2, read_only: false },
];

/// Returns the complete MCP tool catalogue for discovery (`tools/list`).
/// Each tool entry includes its real JSON Schema (from `schemas/<name>.json`),
/// so the Controller validates Intent Objects against the same schema mcpd
/// uses to gate dispatch (INV-4).
///
/// v6.9 Scope O Layer 2 Part B: also appends every manifest-registered
/// tool. Manifest tools carry `params_schema` from their own YAML +
/// `read_only` = (tier <= 1) heuristic (no dedicated field in the
/// manifest today; matches the ToolDescriptor convention above).
pub fn list_all() -> anyhow::Result<Value> {
    let mut tools: Vec<Value> = TOOLS.iter().map(|t| {
        let params_schema = schema::schema_json(t.name).cloned().unwrap_or_else(|| json!({}));
        json!({
            "name": t.name,
            "description": t.description,
            "category": t.category,
            "tier": t.tier,
            "read_only": t.read_only,
            "params_schema": params_schema,
        })
    }).collect();

    for entry in crate::manifest_loader::registry().all() {
        let m = &entry.manifest;
        tools.push(json!({
            "name": m.name,
            "description": m.description,
            "category": category_from_name(&m.name),
            "tier": m.tier,
            "read_only": m.tier <= 1,
            "params_schema": m.param_schema,
            "source": "manifest",
        }));
    }

    Ok(json!({
        "schema_version": schema::SCHEMA_VERSION,
        "tools": tools,
    }))
}


/// Derive the coarse category tag from a dotted tool name — matches the
/// legacy convention (e.g. "system.uptime" -> "system"). Falls back to
/// "misc" for names without a dot (shouldn't happen; loader enforces
/// dotted-lowercase).
fn category_from_name(name: &str) -> &'static str {
    match name.split('.').next().unwrap_or("misc") {
        "system" => "system",
        "process" => "process",
        "fs" => "fs",
        "service" => "service",
        "network" => "network",
        "package" => "package",
        _ => "misc",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn list_all_advertises_twenty_three_tools() {
        // F-35: 22 real tools + system.unsupported landing pad.
        let v = list_all().unwrap();
        let tools = v["tools"].as_array().unwrap();
        assert_eq!(tools.len(), 23);
    }

    #[test]
    fn list_all_includes_system_unsupported_as_tier0_readonly() {
        // F-35 invariant: the landing pad is auto-approved and side-effect-free.
        let v = list_all().unwrap();
        let unsup = v["tools"].as_array().unwrap().iter()
            .find(|t| t["name"] == "system.unsupported")
            .expect("system.unsupported must be in the catalogue");
        assert_eq!(unsup["tier"], 0, "system.unsupported must be Tier 0 (auto-execute)");
        assert_eq!(unsup["read_only"], true, "system.unsupported must be read_only");
        assert_eq!(unsup["category"], "system");
    }

    #[test]
    fn list_all_package_category_has_four_tools() {
        let v = list_all().unwrap();
        let pkg: Vec<_> = v["tools"].as_array().unwrap().iter()
            .filter(|t| t["category"] == "package").collect();
        assert_eq!(pkg.len(), 4);
        let query = pkg.iter().find(|t| t["name"] == "package.query").unwrap();
        assert_eq!(query["tier"], 0);
        assert_eq!(query["read_only"], true);
    }

    #[test]
    fn list_all_fs_category_has_five_tools() {
        let v = list_all().unwrap();
        let fs_tools: Vec<_> = v["tools"].as_array().unwrap().iter()
            .filter(|t| t["category"] == "fs").collect();
        assert_eq!(fs_tools.len(), 5);
        let ro: Vec<_> = fs_tools.iter().filter(|t| t["read_only"] == true).collect();
        assert_eq!(ro.len(), 3);
        assert!(ro.iter().all(|t| t["tier"] == 0));
        let del = fs_tools.iter().find(|t| t["name"] == "fs.delete").unwrap();
        assert_eq!(del["tier"], 3);
    }

    #[test]
    fn list_all_service_category_has_four_tools() {
        let v = list_all().unwrap();
        let svc: Vec<_> = v["tools"].as_array().unwrap().iter()
            .filter(|t| t["category"] == "service").collect();
        assert_eq!(svc.len(), 4);
        let logs = svc.iter().find(|t| t["name"] == "service.logs").unwrap();
        assert_eq!(logs["tier"], 0);
        assert_eq!(logs["read_only"], true);
    }

    #[test]
    fn list_all_includes_real_schemas() {
        let v = list_all().unwrap();
        let inspect = v["tools"].as_array().unwrap().iter()
            .find(|t| t["name"] == "process.inspect").unwrap();
        // The embedded schema must have the real `required: ["pid"]` field,
        // not a hand-written one.
        assert_eq!(inspect["params_schema"]["required"][0], "pid");
        assert_eq!(inspect["params_schema"]["properties"]["pid"]["minimum"], 1);
    }

    #[test]
    fn list_all_carries_schema_version() {
        let v = list_all().unwrap();
        assert_eq!(v["schema_version"], schema::SCHEMA_VERSION);
    }

    #[test]
    fn every_dispatched_tool_has_a_schema() {
        // Asserts the catalogue and the schema registry agree.
        let registered: std::collections::HashSet<&str> = schema::registered_methods().collect();
        for tool in TOOLS {
            assert!(registered.contains(tool.name),
                    "tool '{}' is in TOOLS but has no schema", tool.name);
        }
        assert_eq!(registered.len(), TOOLS.len(),
                   "schema count {} != tool count {}", registered.len(), TOOLS.len());
    }
}
