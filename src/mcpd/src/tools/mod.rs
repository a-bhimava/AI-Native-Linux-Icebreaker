use serde_json::{json, Value};

use crate::schema;

pub mod process;
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
    ToolDescriptor { name: "process.list", description: "Lists all running processes with PID, name, CPU%, and memory%.",
                     category: "process", tier: 0, read_only: true },
    ToolDescriptor { name: "process.inspect", description: "Returns detailed info about a specific process by PID.",
                     category: "process", tier: 0, read_only: true },
];

/// Returns the complete MCP tool catalogue for discovery (`tools/list`).
/// Each tool entry includes its real JSON Schema (from `schemas/<name>.json`),
/// so the Controller validates Intent Objects against the same schema mcpd
/// uses to gate dispatch (INV-4).
pub fn list_all() -> anyhow::Result<Value> {
    let tools: Vec<Value> = TOOLS.iter().map(|t| {
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

    Ok(json!({
        "schema_version": schema::SCHEMA_VERSION,
        "tools": tools,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn list_all_advertises_seven_tools() {
        let v = list_all().unwrap();
        let tools = v["tools"].as_array().unwrap();
        assert_eq!(tools.len(), 7);
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
