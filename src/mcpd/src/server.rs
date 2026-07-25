/// JSON-RPC 2.0 server over stdin/stdout.
///
/// SECURITY INVARIANT (INV-3): This server communicates EXCLUSIVELY over stdio.
/// No TCP socket. No UNIX socket listener. No UDP. Nothing.
/// If you need to add a debug interface, use a named pipe with strict file permissions
/// accessible only to the current user — never a network listener.
use anyhow::Result;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tracing::{error, info, warn};

use crate::tools;

#[derive(Debug, Deserialize)]
struct JsonRpcRequest {
    jsonrpc: String,
    id: Option<Value>,
    method: String,
    #[serde(default)]
    params: Value,
}

#[derive(Debug, Serialize)]
struct JsonRpcResponse {
    jsonrpc: String,
    id: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<JsonRpcError>,
}

#[derive(Debug, Serialize)]
struct JsonRpcError {
    code: i32,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    data: Option<Value>,
}

impl JsonRpcResponse {
    fn ok(id: Value, result: Value) -> Self {
        Self { jsonrpc: "2.0".into(), id, result: Some(result), error: None }
    }

    fn err(id: Value, code: i32, message: impl Into<String>) -> Self {
        Self {
            jsonrpc: "2.0".into(),
            id,
            result: None,
            error: Some(JsonRpcError { code, message: message.into(), data: None }),
        }
    }
}

pub async fn run_stdio_server() -> Result<()> {
    let stdin = tokio::io::stdin();
    let stdout = tokio::io::stdout();
    let mut reader = BufReader::new(stdin);
    let mut writer = stdout;
    let mut line = String::new();

    info!("Listening on stdin for JSON-RPC requests");

    loop {
        line.clear();
        let bytes_read = reader.read_line(&mut line).await?;

        if bytes_read == 0 {
            info!("stdin closed — shutting down");
            break;
        }

        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }

        let started = std::time::Instant::now();
        let response = match serde_json::from_str::<JsonRpcRequest>(trimmed) {
            Err(e) => {
                warn!("Parse error: {}", e);
                let resp = JsonRpcResponse::err(Value::Null, -32700, format!("Parse error: {}", e));
                crate::audit::log_intent(
                    "<parse_error>",
                    None,
                    &Value::Null,
                    crate::audit::ResultClass::ParseError,
                    started.elapsed().as_micros(),
                );
                resp
            }
            Ok(req) => {
                if req.jsonrpc != "2.0" {
                    let resp = JsonRpcResponse::err(
                        req.id.clone().unwrap_or(Value::Null),
                        -32600,
                        "Invalid Request: jsonrpc must be '2.0'",
                    );
                    crate::audit::log_intent(
                        &req.method,
                        req.id.as_ref(),
                        &req.params,
                        crate::audit::ResultClass::InvalidParams,
                        started.elapsed().as_micros(),
                    );
                    resp
                } else {
                    let id = req.id.clone().unwrap_or(Value::Null);
                    let method = req.method.clone();
                    let req_id_clone = req.id.clone();
                    let params_clone = req.params.clone();
                    let resp = dispatch(req).await.unwrap_or_else(|e| {
                        error!("Dispatch error: {}", e);
                        JsonRpcResponse::err(id, -32603, format!("Internal error: {}", e))
                    });
                    crate::audit::log_intent(
                        &method,
                        req_id_clone.as_ref(),
                        &params_clone,
                        classify(&resp),
                        started.elapsed().as_micros(),
                    );
                    resp
                }
            }
        };

        let mut response_str = serde_json::to_string(&response)?;
        response_str.push('\n');
        writer.write_all(response_str.as_bytes()).await?;
        writer.flush().await?;
    }

    Ok(())
}

/// Map a JSON-RPC response to an audit result class.
fn classify(resp: &JsonRpcResponse) -> crate::audit::ResultClass {
    if let Some(err) = &resp.error {
        return match err.code {
            -32601 => crate::audit::ResultClass::MethodNotFound,
            -32602 => crate::audit::ResultClass::InvalidParams,
            -32700 => crate::audit::ResultClass::ParseError,
            _ => crate::audit::ResultClass::Err,
        };
    }
    // Look inside the result envelope for a sentinel status field.
    if let Some(result) = &resp.result {
        if let Some(status) = result.get("status").and_then(|v| v.as_str()) {
            return match status {
                "requires_cow_approval" => crate::audit::ResultClass::CowRequired,
                "unavailable" => crate::audit::ResultClass::Unavailable,
                "refused" => crate::audit::ResultClass::Refused,
                "err" => crate::audit::ResultClass::Err,
                _ => crate::audit::ResultClass::Ok,
            };
        }
    }
    crate::audit::ResultClass::Ok
}

async fn dispatch(mut req: JsonRpcRequest) -> Result<JsonRpcResponse> {
    let id = req.id.clone().unwrap_or(Value::Null);
    info!("method={} id={}", req.method, id);

    // v6.13_OC Fix K' — MCP protocol: `initialize` returns capabilities.
    // Short-circuits before every downstream check; no schema, no side effects.
    if req.method == "initialize" {
        return Ok(JsonRpcResponse::ok(id, serde_json::json!({
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": false}},
            "serverInfo": {
                "name": "icebreaker-mcpd",
                "version": env!("CARGO_PKG_VERSION"),
            },
        })));
    }

    // v6.13_OC Fix K' — MCP protocol: `tools/call` envelope unwrap.
    // Set mcp_wrap so the exit paths wrap the tool result in MCP shape.
    // Method + params are mutated in place; the rest of dispatch runs
    // against the inner tool name (goes through is_known_method,
    // schema validate, and the existing match). Manifest tools are
    // reachable through this path for free.
    let mcp_wrap = if req.method == "tools/call" {
        let name = match req.params.get("name").and_then(|v| v.as_str()) {
            Some(n) if n != "tools/call" && n != "initialize" => n.to_string(),
            Some(_) => {
                return Ok(JsonRpcResponse::err(
                    id, -32602,
                    "tools/call cannot wrap protocol methods (initialize, tools/call)",
                ));
            }
            None => {
                return Ok(JsonRpcResponse::err(
                    id, -32602,
                    "tools/call: 'name' missing or not a string",
                ));
            }
        };
        let args = req.params.get("arguments").cloned()
            .unwrap_or_else(|| Value::Object(serde_json::Map::new()));
        req.method = name;
        req.params = args;
        true
    } else {
        false
    };

    // Sequence matters: known-method check FIRST (so unknown methods return
    // -32601 Method not found), then schema validation (-32602 Invalid params),
    // then execution. This keeps error codes faithful to JSON-RPC 2.0 §5.1.
    //
    // v6.9 Scope O Layer 2 Part B: manifest-registered methods count as
    // known — this is how declarative tools join tools/list without a
    // hand-written match arm.
    let is_manifest_method = crate::manifest_loader::registry().has(&req.method);
    if !is_known_method(&req.method) && !is_manifest_method {
        return Ok(JsonRpcResponse::err(
            id,
            -32601,
            format!("Method not found: {}", req.method),
        ));
    }

    // JSON-RPC 2.0 § 4.1: `params` MAY be omitted. We coerce `null` (the
    // serde_json default for a missing field) to `{}` so schemas of shape
    // `{type: object, additionalProperties: false}` accept no-arg calls.
    if req.params.is_null() {
        req.params = Value::Object(serde_json::Map::new());
    }

    // INV-4: every dispatched call's params are validated against its schema
    // before the tool function runs. Legacy tools use compile-time embedded
    // schemas; manifest tools use their own compiled param_schema (Layer 2
    // Part B). Both paths refuse invalid params with -32602.
    if is_manifest_method {
        let entry = crate::manifest_loader::registry().get(&req.method).expect("checked above");
        if let Err(errors) = entry.compiled_schema.validate(&req.params) {
            let joined: Vec<String> = errors.map(|e| e.to_string()).collect();
            return Ok(JsonRpcResponse::err(
                id, -32602,
                format!("Invalid params: {}", joined.join("; ")),
            ));
        }
        let result = crate::manifest_loader::impl_kinds::dispatch(
            &entry.manifest, &req.params,
        ).await;
        return Ok(match (result, mcp_wrap) {
            (Ok(v), true)  => JsonRpcResponse::ok(id, mcp_content(&v)),
            (Ok(v), false) => JsonRpcResponse::ok(id, v),
            (Err(e), true)  => JsonRpcResponse::ok(id, mcp_error(&format!("Internal error: {}", e))),
            (Err(e), false) => JsonRpcResponse::err(id, -32603, format!("Internal error: {}", e)),
        });
    }
    if let Err(e) = crate::schema::validate(&req.method, &req.params) {
        return Ok(JsonRpcResponse::err(id, -32602, format!("Invalid params: {}", e)));
    }

    let result = match req.method.as_str() {
        // MCP discovery — returns the full tool catalogue
        "tools/list" => tools::list_all(),

        // System read-only tools
        "system.status"  => tools::system::status().await,
        "system.uptime"  => tools::system::uptime().await,
        "system.cpu"     => tools::system::cpu().await,
        "system.memory"  => tools::system::memory().await,
        "system.disk"    => tools::system::disk().await,
        // F-35: catalogue landing pad. Params validated above; safe to access.
        "system.unsupported" => {
            let requested = req.params["requested_intent"].as_str().expect("schema-validated required field");
            let suggestion = req.params.get("suggestion").and_then(|v| v.as_str());
            let alt = req.params.get("alternative_actions");
            tools::system::unsupported(requested, suggestion, alt).await
        },

        // Process read-only tools
        "process.list"    => tools::process::list().await,
        "process.inspect" => {
            // Safe to unwrap: schema validation above guarantees `pid` is an
            // integer in the valid range.
            let pid = req.params["pid"].as_u64().expect("schema-validated") as u32;
            tools::process::inspect(pid).await
        }

        // Filesystem read-only tools (Tier 0). Path validation is in fs::validate;
        // openat2(RESOLVE_BENEATH) is the kernel-side gate inside the tool fn.
        "fs.read" => {
            let path = req.params["path"].as_str().expect("schema-validated");
            tools::fs::read(path).await
        }
        "fs.list" => {
            let path = req.params["path"].as_str().expect("schema-validated");
            tools::fs::list(path).await
        }
        "fs.stat" => {
            let path = req.params["path"].as_str().expect("schema-validated");
            tools::fs::stat(path).await
        }
        "fs.write" => {
            let path = req.params["path"].as_str().expect("schema-validated");
            let content = req.params["content"].as_str().expect("schema-validated");
            let mode = req.params.get("mode").and_then(|v| v.as_u64()).map(|m| m as u32);
            tools::fs::write(path, content, mode).await
        }
        "fs.delete" => {
            let path = req.params["path"].as_str().expect("schema-validated");
            tools::fs::delete(path).await
        }

        // systemd unit control via D-Bus (M1.6). Graceful degradation on bus
        // failure happens inside the tool fn — never bubbles up as -32603.
        "service.start" => {
            let unit = req.params["unit"].as_str().expect("schema-validated");
            tools::service::start(unit).await
        }
        "service.stop" => {
            let unit = req.params["unit"].as_str().expect("schema-validated");
            tools::service::stop(unit).await
        }
        "service.restart" => {
            let unit = req.params["unit"].as_str().expect("schema-validated");
            tools::service::restart(unit).await
        }
        "service.logs" => {
            let unit = req.params["unit"].as_str().expect("schema-validated");
            let lines = req.params.get("lines").and_then(|v| v.as_u64()).unwrap_or(200);
            tools::service::logs(unit, lines).await
        }

        // Network read-only (M1.7). Write-side tools (firewall, dns.set) are
        // not yet exposed in the catalogue; they land in Phase 5.
        "network.status"   => tools::network::status().await,
        "network.dns.read" => tools::network::dns_read().await,

        // Package introspection (M1.8). Query is direct; install/remove/upgrade
        // always return a COW ticket — Phase 3 commits via apt-get inside an overlay.
        "package.query" => {
            let pattern = req.params["pattern"].as_str().expect("schema-validated");
            tools::package::query(pattern).await
        }
        "package.install" => {
            let package = req.params["package"].as_str().expect("schema-validated");
            tools::package::install(package).await
        }
        "package.remove" => {
            let package = req.params["package"].as_str().expect("schema-validated");
            tools::package::remove(package).await
        }
        "package.upgrade" => {
            let package = req.params["package"].as_str().expect("schema-validated");
            tools::package::upgrade(package).await
        }

        // Unreachable: is_known_method() gates this match above.
        other => unreachable!("dispatch reached unknown method '{}' after is_known_method check", other),
    };

    match (result, mcp_wrap) {
        (Ok(value), true)  => Ok(JsonRpcResponse::ok(id, mcp_content(&value))),
        (Ok(value), false) => Ok(JsonRpcResponse::ok(id, value)),
        (Err(e), true)  => Ok(JsonRpcResponse::ok(id, mcp_error(&e.to_string()))),
        (Err(e), false) => Ok(JsonRpcResponse::err(id, -32603, e.to_string())),
    }
}

/// v6.13_OC Fix K' — wrap a tool result in MCP `tools/call` response shape.
/// Serialization failure is very rare (would need a Value with a bad float
/// or non-string-key map); we substitute a diagnostic string rather than
/// letting `?` propagate, since the caller has no way to recover.
fn mcp_content(value: &Value) -> Value {
    let text = serde_json::to_string(value).unwrap_or_else(|e| {
        format!("{{\"__serialize_error\":\"{}\"}}", e)
    });
    serde_json::json!({
        "content": [{"type": "text", "text": text}],
        "isError": false,
    })
}

/// v6.13_OC Fix K' — wrap a tool error in MCP `tools/call` response shape.
/// Note: MCP spec allows both JSON-RPC error and isError:true; we use
/// isError only for tool-execution failures (-32603 class). Schema
/// validation (-32602) and method-not-found (-32601) still return
/// proper JSON-RPC errors so opencode can distinguish protocol errors
/// from tool errors.
fn mcp_error(msg: &str) -> Value {
    serde_json::json!({
        "content": [{"type": "text", "text": msg}],
        "isError": true,
    })
}

/// Returns true if `method` is one of the methods this dispatcher routes.
/// Single source of truth for both the -32601 check and the match arms.
fn is_known_method(method: &str) -> bool {
    matches!(method,
        "tools/list"
        | "system.status" | "system.uptime" | "system.cpu" | "system.memory" | "system.disk"
        | "system.unsupported"  // F-35 catalogue landing pad
        | "process.list" | "process.inspect"
        | "fs.read" | "fs.list" | "fs.stat" | "fs.write" | "fs.delete"
        | "service.start" | "service.stop" | "service.restart" | "service.logs"
        | "network.status" | "network.dns.read"
        | "package.query" | "package.install" | "package.remove" | "package.upgrade"
    )
}
