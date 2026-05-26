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

        let response = match serde_json::from_str::<JsonRpcRequest>(trimmed) {
            Err(e) => {
                warn!("Parse error: {}", e);
                JsonRpcResponse::err(Value::Null, -32700, format!("Parse error: {}", e))
            }
            Ok(req) => {
                if req.jsonrpc != "2.0" {
                    JsonRpcResponse::err(
                        req.id.unwrap_or(Value::Null),
                        -32600,
                        "Invalid Request: jsonrpc must be '2.0'",
                    )
                } else {
                    let id = req.id.clone().unwrap_or(Value::Null);
                    dispatch(req).await.unwrap_or_else(|e| {
                        error!("Dispatch error: {}", e);
                        JsonRpcResponse::err(id, -32603, format!("Internal error: {}", e))
                    })
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

async fn dispatch(req: JsonRpcRequest) -> Result<JsonRpcResponse> {
    let id = req.id.unwrap_or(Value::Null);
    info!("method={} id={}", req.method, id);

    let result = match req.method.as_str() {
        // MCP discovery — returns the full tool catalogue
        "tools/list" => tools::list_all(),

        // System read-only tools
        "system.status"  => tools::system::status().await,
        "system.uptime"  => tools::system::uptime().await,
        "system.cpu"     => tools::system::cpu().await,
        "system.memory"  => tools::system::memory().await,
        "system.disk"    => tools::system::disk().await,

        // Process read-only tools
        "process.list"    => tools::process::list().await,
        "process.inspect" => {
            let pid = req.params.get("pid")
                .and_then(|v| v.as_u64())
                .ok_or_else(|| anyhow::anyhow!("Missing required param: pid (u64)"))?;
            tools::process::inspect(pid as u32).await
        }

        // Unknown method
        method => {
            return Ok(JsonRpcResponse::err(
                id,
                -32601,
                format!("Method not found: {}", method),
            ));
        }
    };

    match result {
        Ok(value) => Ok(JsonRpcResponse::ok(id, value)),
        Err(e) => Ok(JsonRpcResponse::err(id, -32603, e.to_string())),
    }
}
