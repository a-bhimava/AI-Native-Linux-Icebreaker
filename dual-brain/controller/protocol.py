"""JSON-RPC 2.0 protocol for daemon<->client communication.

Message types:
  Requests (client->daemon): turn.run, session.new, session.reset,
      hitl.respond, daemon.status, daemon.shutdown
  Notifications (daemon->client): turn.progress, turn.token,
      turn.result, turn.error, hitl.prompt
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


JSONRPC_VERSION = "2.0"


@dataclass(frozen=True)
class JsonRpcRequest:
    method: str
    params: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_bytes(self) -> bytes:
        msg = {
            "jsonrpc": JSONRPC_VERSION,
            "method": self.method,
            "params": self.params,
            "id": self.id,
        }
        return json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"


@dataclass(frozen=True)
class JsonRpcNotification:
    method: str
    params: dict = field(default_factory=dict)

    def to_bytes(self) -> bytes:
        msg = {
            "jsonrpc": JSONRPC_VERSION,
            "method": self.method,
            "params": self.params,
        }
        return json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"


@dataclass(frozen=True)
class JsonRpcResponse:
    id: str
    result: Optional[dict] = None
    error: Optional[dict] = None

    def to_bytes(self) -> bytes:
        msg: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": self.id,
        }
        if self.error is not None:
            msg["error"] = self.error
        else:
            msg["result"] = self.result or {}
        return json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"


def parse_message(data: bytes) -> dict:
    """Parse a single JSON-RPC message from wire bytes."""
    return json.loads(data.decode("utf-8"))


def is_request(msg: dict) -> bool:
    return "method" in msg and "id" in msg


def is_notification(msg: dict) -> bool:
    return "method" in msg and "id" not in msg


def is_response(msg: dict) -> bool:
    return "id" in msg and "method" not in msg


def make_error(id: str, code: int, message: str) -> JsonRpcResponse:
    return JsonRpcResponse(id=id, error={"code": code, "message": message})


# Standard JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
AUTH_REJECTED = -32000
