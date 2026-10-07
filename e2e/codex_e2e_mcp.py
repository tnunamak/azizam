"""Small stdio MCP server used only to make Codex emit a namespace tool."""

from __future__ import annotations

import json
import sys


def reply(request: dict, result: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = request.get("method")
        if method == "initialize":
            reply(request, {"protocolVersion": request.get("params", {}).get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}}, "serverInfo": {"name": "azizam-e2e", "version": "1"}})
        elif method == "tools/list":
            reply(request, {"tools": [{"name": "namespace_probe", "description": "Return a deterministic Azizam namespace marker.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]})
        elif method == "tools/call":
            reply(request, {"content": [{"type": "text", "text": "azizam namespace probe"}]})
        elif "id" in request:
            reply(request, {})


if __name__ == "__main__":
    main()
