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


def _result_text(value: object) -> str | None:
    """Accept known text envelopes, never success text carried by an error."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value.strip()
        if isinstance(decoded, (dict, list)):
            return _result_text(decoded)
        return value.strip()
    if isinstance(value, dict):
        metadata = value.get("metadata", {})
        if (value.get("isError") or value.get("error")
                or value.get("exit_code", 0) != 0
                or (isinstance(metadata, dict) and metadata.get("exit_code", 0) != 0)):
            return None
        if "output" in value:
            return _result_text(value["output"])
        if "content" in value:
            return _result_text(value["content"])
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return value["text"].strip()
        return None
    if isinstance(value, list):
        parts = [_result_text(part) for part in value]
        return "\n".join(parts) if all(part is not None for part in parts) else None
    return None


def _decision(messages: list[object], namespace_name: str | None) -> tuple[dict, str]:
    def failure(reason: str) -> tuple[dict, str]:
        return {"content": "AZIZAM_E2E_FAILURE: " + reason}, "stop"

    expected = {"call_apply_patch": "apply_patch", "call_namespace_probe": namespace_name}
    issued: set[str] = set()
    results: dict[str, object] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list):
                return failure("malformed tool calls")
            for call in calls:
                if not isinstance(call, dict):
                    return failure("malformed tool call")
                call_id = call.get("id")
                function = call.get("function", {})
                if (not isinstance(call_id, str) or call_id not in expected or call_id in issued
                        or not isinstance(function, dict)
                        or not expected[call_id] or function.get("name") != expected[call_id]):
                    return failure("unexpected or duplicate tool call")
                issued.add(call_id)
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in issued or call_id in results:
                return failure("unmatched or duplicate tool result")
            results[call_id] = message.get("content")
    if not issued:
        return {"tool_calls": [{"index": 0, "id": "call_apply_patch", "type": "function",
                               "function": {"name": "apply_patch", "arguments": json.dumps({"input": PATCH})}}]}, "tool_calls"
    patch = _result_text(results.get("call_apply_patch"))
    if patch != "Success. Updated the following files:\nA hello.txt":
        return failure("apply_patch did not report the expected successful file update")
    if not namespace_name:
        return failure("namespace tool was not advertised")
    if "call_namespace_probe" not in issued:
        return {"tool_calls": [{"index": 0, "id": "call_namespace_probe", "type": "function",
                               "function": {"name": namespace_name, "arguments": "{}"}}]}, "tool_calls"
    if _result_text(results.get("call_namespace_probe")) != "azizam namespace probe":
        return failure("namespace tool did not return its successful marker")
    return {"content": "Created hello.txt with apply_patch. Namespace tool path was available."}, "stop"


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
    delta, finish_reason = _decision(messages, namespace_name)

    async def events():
        yield _sse({"id": "chatcmpl-azizam-e2e", "object": "chat.completion.chunk", "model": body.get("model", "azizam-e2e"), "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        yield _sse({"id": "chatcmpl-azizam-e2e", "object": "chat.completion.chunk", "model": body.get("model", "azizam-e2e"), "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
