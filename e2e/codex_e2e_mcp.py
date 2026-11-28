"""Small stdio MCP server used only to make Codex emit a namespace tool."""

from __future__ import annotations

import json
import sys


def reply(request: dict, result: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}) + "\n")
    sys.stdout.flush()


def error_reply(request: dict, code: int, message: str) -> None:
    sys.stdout.write(json.dumps({
        "jsonrpc": "2.0", "id": request.get("id"),
        "error": {"code": code, "message": message},
    }) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print("azizam-e2e: malformed JSON frame", file=sys.stderr, flush=True)
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }) + "\n")
            sys.stdout.flush()
            continue
        if not isinstance(request, dict):
            print("azizam-e2e: expected an object frame", file=sys.stderr, flush=True)
            continue
        method = request.get("method")
        if method == "initialize":
            reply(request, {"protocolVersion": request.get("params", {}).get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}}, "serverInfo": {"name": "azizam-e2e", "version": "1"}})
        elif method == "tools/list":
            reply(request, {"tools": [{"name": "namespace_probe", "description": "Return a deterministic Azizam namespace marker.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]})
        elif method == "tools/call":
            params = request.get("params")
            if (not isinstance(params, dict)
                    or params.get("name") != "namespace_probe"
                    or params.get("arguments", {}) != {}
                    or not isinstance(params.get("arguments", {}), dict)):
                reply(request, {"isError": True, "content": [{"type": "text", "text": "Invalid namespace_probe call"}]})
            else:
                reply(request, {"content": [{"type": "text", "text": "azizam namespace probe"}]})
        elif "id" in request:
            error_reply(request, -32601, "Method not found")


if __name__ == "__main__":
    main()
