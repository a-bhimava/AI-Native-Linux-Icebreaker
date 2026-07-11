/// schema.rs — JSON Schema validation layer (INV-4).
///
/// Every dispatched call validates its `params` against the tool's schema
/// before the handler runs. Schemas live in `schemas/*.json` and are embedded
/// at compile time via `include_str!`, so the validator has no runtime IO and
/// can't be tampered with at runtime.
///
/// On validation failure the caller should return JSON-RPC error -32602
/// (Invalid params) per the JSON-RPC 2.0 spec.
use anyhow::{anyhow, Result};
use jsonschema::JSONSchema;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::OnceLock;

/// SemVer of the schema catalogue. Bumped on any schema change.
/// Major bump = breaking change (Controller must re-pin).
/// Minor bump = additive (new tool, new optional field).
pub const SCHEMA_VERSION: &str = "1.0.0";

/// Source of each schema JSON, embedded at compile time.
/// Add a new tool by adding both a schema JSON file under `schemas/`
/// AND a line here, then exposing the method in `tools::mod` and `server::dispatch`.
const SCHEMA_SOURCES: &[(&str, &str)] = &[
    ("system.status",  include_str!("../schemas/system.status.json")),
    ("system.uptime",  include_str!("../schemas/system.uptime.json")),
    ("system.cpu",     include_str!("../schemas/system.cpu.json")),
    ("system.memory",  include_str!("../schemas/system.memory.json")),
    ("system.disk",    include_str!("../schemas/system.disk.json")),
    // F-35: catalogue landing pad for queries QB can't map. Never a lookalike.
    ("system.unsupported", include_str!("../schemas/system.unsupported.json")),
    ("process.list",   include_str!("../schemas/process.list.json")),
    ("process.inspect", include_str!("../schemas/process.inspect.json")),
    ("fs.read",        include_str!("../schemas/fs.read.json")),
    ("fs.list",        include_str!("../schemas/fs.list.json")),
    ("fs.stat",        include_str!("../schemas/fs.stat.json")),
    ("fs.write",       include_str!("../schemas/fs.write.json")),
    ("fs.delete",      include_str!("../schemas/fs.delete.json")),
    ("service.start",  include_str!("../schemas/service.start.json")),
    ("service.stop",   include_str!("../schemas/service.stop.json")),
    ("service.restart", include_str!("../schemas/service.restart.json")),
    ("service.logs",   include_str!("../schemas/service.logs.json")),
    ("network.status", include_str!("../schemas/network.status.json")),
    ("network.dns.read", include_str!("../schemas/network.dns.read.json")),
    ("package.query",  include_str!("../schemas/package.query.json")),
    ("package.install", include_str!("../schemas/package.install.json")),
    ("package.remove", include_str!("../schemas/package.remove.json")),
    ("package.upgrade", include_str!("../schemas/package.upgrade.json")),
];

/// `tools/list` has no schema — it's the discovery endpoint, accepts any params.
const UNCHECKED_METHODS: &[&str] = &["tools/list"];

struct Registry {
    schemas: HashMap<String, JSONSchema>,
    /// Kept around so JSON Schema's borrowed references remain valid for the
    /// lifetime of the registry.
    sources: HashMap<String, Value>,
}

fn registry() -> &'static Registry {
    static REGISTRY: OnceLock<Registry> = OnceLock::new();
    REGISTRY.get_or_init(|| {
        let mut sources = HashMap::new();
        for (name, src) in SCHEMA_SOURCES {
            let parsed: Value = serde_json::from_str(src)
                .unwrap_or_else(|e| panic!("schemas/{}.json is not valid JSON: {}", name, e));
            sources.insert(name.to_string(), parsed);
        }
        let mut schemas = HashMap::new();
        for (name, parsed) in &sources {
            let compiled = JSONSchema::compile(parsed)
                .unwrap_or_else(|e| panic!("schemas/{}.json failed to compile: {}", name, e));
            schemas.insert(name.clone(), compiled);
        }
        Registry { schemas, sources }
    })
}

/// Validate `params` against the schema for `method`.
///
/// Returns `Err` with a human-readable message if the schema doesn't exist or
/// validation fails. Methods in `UNCHECKED_METHODS` return `Ok` regardless.
///
/// IMPORTANT: methods that are NOT in the schema catalogue also return `Err`.
/// The dispatcher should check for "unknown method" BEFORE calling validate,
/// so that a real "method not found" returns -32601 instead of -32602.
pub fn validate(method: &str, params: &Value) -> Result<()> {
    if UNCHECKED_METHODS.contains(&method) {
        return Ok(());
    }
    let reg = registry();
    let schema = reg.schemas.get(method)
        .ok_or_else(|| anyhow!("No schema registered for method '{}'", method))?;
    if let Err(errors) = schema.validate(params) {
        let msgs: Vec<String> = errors.map(|e| format!("{} at {}", e, e.instance_path)).collect();
        return Err(anyhow!("{}", msgs.join("; ")));
    }
    Ok(())
}

/// Returns the raw schema JSON for `method`, or `None` if not registered.
/// Used by `tools::list_all()` to embed each tool's schema in the catalogue
/// response so the Controller can validate Intent Objects against it.
pub fn schema_json(method: &str) -> Option<&'static Value> {
    // Safety: registry() returns a 'static reference; the inner Value lives
    // as long as the registry, which is 'static.
    registry().sources.get(method)
}

/// Iterate over all registered method names. Used to assert that every
/// dispatchable method has a schema.
pub fn registered_methods() -> impl Iterator<Item = &'static str> {
    SCHEMA_SOURCES.iter().map(|(name, _)| *name)
}

// ── Unit tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn no_params_methods_accept_empty_object() {
        assert!(validate("system.status", &json!({})).is_ok());
        assert!(validate("system.uptime", &json!({})).is_ok());
        assert!(validate("process.list", &json!({})).is_ok());
    }

    #[test]
    fn no_params_methods_reject_extra_fields() {
        let result = validate("system.status", &json!({"unexpected": "field"}));
        assert!(result.is_err(), "additionalProperties=false should reject extras");
    }

    #[test]
    fn process_inspect_accepts_valid_pid() {
        assert!(validate("process.inspect", &json!({"pid": 1})).is_ok());
        assert!(validate("process.inspect", &json!({"pid": 12345})).is_ok());
    }

    #[test]
    fn process_inspect_rejects_missing_pid() {
        let result = validate("process.inspect", &json!({}));
        assert!(result.is_err());
    }

    #[test]
    fn process_inspect_rejects_non_integer_pid() {
        assert!(validate("process.inspect", &json!({"pid": "abc"})).is_err());
        assert!(validate("process.inspect", &json!({"pid": 1.5})).is_err());
        assert!(validate("process.inspect", &json!({"pid": null})).is_err());
    }

    #[test]
    fn process_inspect_rejects_pid_out_of_range() {
        // 0 is below minimum (Linux PIDs start at 1)
        assert!(validate("process.inspect", &json!({"pid": 0})).is_err());
        // Below zero
        assert!(validate("process.inspect", &json!({"pid": -1})).is_err());
        // Above PID_MAX_LIMIT
        assert!(validate("process.inspect", &json!({"pid": 5_000_000})).is_err());
    }

    #[test]
    fn unknown_method_errors() {
        let result = validate("does.not.exist", &json!({}));
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("No schema registered"));
    }

    #[test]
    fn tools_list_is_unchecked() {
        assert!(validate("tools/list", &json!({})).is_ok());
        assert!(validate("tools/list", &json!({"anything": "goes"})).is_ok());
    }

    #[test]
    fn schema_json_returns_source() {
        let s = schema_json("process.inspect").expect("schema exists");
        assert_eq!(s["properties"]["pid"]["type"], "integer");
        assert_eq!(s["required"][0], "pid");
    }

    #[test]
    fn schema_json_unknown_returns_none() {
        assert!(schema_json("does.not.exist").is_none());
    }

    #[test]
    fn registered_methods_includes_all_twenty_three() {
        // F-35: 22 real tools + system.unsupported (catalogue landing pad).
        let methods: Vec<&str> = registered_methods().collect();
        assert_eq!(methods.len(), 23);
        assert!(methods.contains(&"system.status"));
        assert!(methods.contains(&"system.unsupported"));
        assert!(methods.contains(&"process.inspect"));
        assert!(methods.contains(&"fs.read"));
        assert!(methods.contains(&"fs.list"));
        assert!(methods.contains(&"fs.stat"));
        assert!(methods.contains(&"fs.write"));
        assert!(methods.contains(&"fs.delete"));
        assert!(methods.contains(&"service.start"));
        assert!(methods.contains(&"service.stop"));
        assert!(methods.contains(&"service.restart"));
        assert!(methods.contains(&"service.logs"));
    }

    #[test]
    fn service_start_validates_unit_pattern() {
        assert!(validate("service.start", &json!({"unit": "nginx.service"})).is_ok());
        assert!(validate("service.start", &json!({"unit": "getty@tty1.service"})).is_ok());
        assert!(validate("service.start", &json!({})).is_err());
        // pattern rejects shell metachars
        assert!(validate("service.start", &json!({"unit": "nginx; rm -rf /"})).is_err());
        assert!(validate("service.start", &json!({"unit": "nginx`whoami`"})).is_err());
        assert!(validate("service.start", &json!({"unit": ""})).is_err());
    }

    #[test]
    fn service_logs_validates_lines_bounds() {
        assert!(validate("service.logs", &json!({"unit": "nginx"})).is_ok());
        assert!(validate("service.logs", &json!({"unit": "nginx", "lines": 100})).is_ok());
        assert!(validate("service.logs", &json!({"unit": "nginx", "lines": 0})).is_err());
        assert!(validate("service.logs", &json!({"unit": "nginx", "lines": 99999})).is_err());
    }

    #[test]
    fn fs_write_requires_path_and_content() {
        assert!(validate("fs.write", &json!({})).is_err());
        assert!(validate("fs.write", &json!({"path": "/tmp/x"})).is_err());
        assert!(validate("fs.write", &json!({"content": "x"})).is_err());
        assert!(validate("fs.write", &json!({"path": "/tmp/x", "content": "x"})).is_ok());
        assert!(validate("fs.write", &json!({"path": "/tmp/x", "content": "x", "mode": 420})).is_ok());
    }

    #[test]
    fn fs_write_rejects_invalid_mode() {
        assert!(validate("fs.write", &json!({"path": "/tmp/x", "content": "x", "mode": -1})).is_err());
        assert!(validate("fs.write", &json!({"path": "/tmp/x", "content": "x", "mode": 99999})).is_err());
    }

    #[test]
    fn fs_delete_requires_path() {
        assert!(validate("fs.delete", &json!({})).is_err());
        assert!(validate("fs.delete", &json!({"path": "/tmp/x"})).is_ok());
    }

    #[test]
    fn fs_read_requires_path_string() {
        assert!(validate("fs.read", &json!({})).is_err());
        assert!(validate("fs.read", &json!({"path": 42})).is_err());
        assert!(validate("fs.read", &json!({"path": ""})).is_err()); // minLength 1
        assert!(validate("fs.read", &json!({"path": "/etc/hosts"})).is_ok());
    }

    #[test]
    fn fs_read_rejects_extra_fields() {
        assert!(validate("fs.read", &json!({"path": "/etc/hosts", "encoding": "utf8"})).is_err());
    }

    #[test]
    fn fs_list_and_stat_share_shape() {
        for m in ["fs.list", "fs.stat"] {
            assert!(validate(m, &json!({"path": "/etc"})).is_ok());
            assert!(validate(m, &json!({})).is_err());
        }
    }

    #[test]
    fn schema_version_is_semver() {
        let parts: Vec<&str> = SCHEMA_VERSION.split('.').collect();
        assert_eq!(parts.len(), 3);
        for p in parts {
            assert!(p.parse::<u32>().is_ok(), "version part '{}' not numeric", p);
        }
    }
}
