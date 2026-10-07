"""Deterministic Chat Completions backend for the isolated Codex e2e harness."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


app = FastAPI()
PATCH = "*** Begin Patch\n*** Add File: hello.txt\n+hello from Azizam e2e\n*** End Patch\n"


def _record(body: dict) -> None:
    path = Path(os.environ["CODEX_E2E_BACKEND_LOG"])
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(body, sort_keys=True) + "\n")


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _has_tool_result(messages: list[object]) -> bool:
    return any(isinstance(message, dict) and message.get("role") == "tool" for message in messages)


def _namespace_function_name(tools: object) -> str | None:
    if not isinstance(tools, list):
        return None
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        description = function.get("description") if isinstance(function, dict) else None
        if isinstance(name, str) and isinstance(description, str) and "Namespace: mcp__azizam_e2e. Tool: namespace_probe." in description:
            return name
    return None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict:
    return {"object": "list", "data": [{"id": "azizam-e2e", "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    _record(body)
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    namespace_name = _namespace_function_name(body.get("tools"))
    tool_results = sum(isinstance(message, dict) and message.get("role") == "tool" for message in messages)
    if tool_results >= 2:
        delta = {"content": "Created hello.txt with apply_patch. Namespace tool path was available."}
        finish_reason = "stop"
    elif _has_tool_result(messages) and namespace_name:
        delta = {"tool_calls": [{"index": 0, "id": "call_namespace_probe", "type": "function", "function": {"name": namespace_name, "arguments": "{}"}}]}
        finish_reason = "tool_calls"
    else:
        delta = {"tool_calls": [{"index": 0, "id": "call_apply_patch", "type": "function", "function": {"name": "apply_patch", "arguments": json.dumps({"input": PATCH})}}]}
        finish_reason = "tool_calls"

    async def events():
        yield _sse({"id": "chatcmpl-azizam-e2e", "object": "chat.completion.chunk", "model": body.get("model", "azizam-e2e"), "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        yield _sse({"id": "chatcmpl-azizam-e2e", "object": "chat.completion.chunk", "model": body.get("model", "azizam-e2e"), "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
