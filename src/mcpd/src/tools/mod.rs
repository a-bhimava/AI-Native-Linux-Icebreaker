use serde_json::{json, Value};

pub mod process;
pub mod system;

/// Returns the complete MCP tool catalogue for discovery.
/// The Controller uses this to validate Intent Objects against real tool schemas.
pub fn list_all() -> anyhow::Result<Value> {
    Ok(json!({
        "schema_version": "0.1.0",
        "tools": [
            {
                "name": "system.status",
                "description": "Returns overall system health summary (uptime, load, memory, disk).",
                "category": "system",
                "tier": 0,
                "read_only": true,
                "params": {}
            },
            {
                "name": "system.uptime",
                "description": "Returns system uptime in seconds and human-readable form.",
                "category": "system",
                "tier": 0,
                "read_only": true,
                "params": {}
            },
            {
                "name": "system.cpu",
                "description": "Returns CPU usage per core and overall utilization percentage.",
                "category": "system",
                "tier": 0,
                "read_only": true,
                "params": {}
            },
            {
                "name": "system.memory",
                "description": "Returns total, used, free, and cached memory in MB.",
                "category": "system",
                "tier": 0,
                "read_only": true,
                "params": {}
            },
            {
                "name": "system.disk",
                "description": "Returns disk usage for all mounted filesystems.",
                "category": "system",
                "tier": 0,
                "read_only": true,
                "params": {}
            },
            {
                "name": "process.list",
                "description": "Lists all running processes with PID, name, CPU%, and memory%.",
                "category": "process",
                "tier": 0,
                "read_only": true,
                "params": {}
            },
            {
                "name": "process.inspect",
                "description": "Returns detailed info about a specific process by PID.",
                "category": "process",
                "tier": 0,
                "read_only": true,
                "params": {
                    "pid": {
                        "type": "integer",
                        "description": "Process ID to inspect",
                        "required": true
                    }
                }
            }
        ]
    }))
}
