"""Deterministic OpenAI-compatible engine used only by the isolated UI harness."""
import time
import os

from fastapi import FastAPI, Request

app = FastAPI()


@app.get("/health")
@app.get("/")
def health():
    return {"status": "ok"}


@app.get("/v1/models")
def models():
    if os.environ.get('UI_SEED_PROFILE') == 'household':
        from scripts.ui.household_seed import MODEL_GROUPS
        return {'object': 'list', 'data': [{'id': mid, 'object': 'model', 'owned_by': 'household', 'name': name} for group in MODEL_GROUPS.values() for mid, name, cap in group]}
    return {"object": "list", "data": [{"id": "demo-chat", "object": "model", "owned_by": "demo"}]}


@app.get("/provider/v1/key")
def provider_key_probe():
    return {"data": {"label": "isolated-provider-key"}}


@app.get("/provider/v1/models")
def provider_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "anthropic/claude-sonnet-4.5",
                "name": "Claude Sonnet 4.5",
                "object": "model",
                "owned_by": "isolated-openrouter",
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                "context_length": 200000,
            },
        ],
    }


@app.post("/v1/chat/completions")
@app.post("/provider/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages") or []
    content = " ".join(str((message or {}).get("content", "")) for message in messages if isinstance(message, dict))
    response = {
        "id": f"chatcmpl-demo-{time.time_ns()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "demo-chat"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Your gateway is connected. This is a demonstration response from the isolated capture engine."},
            "finish_reason": "stop",
        }],
    }
    if "force_unknown_usage" not in content:
        response["usage"] = {"prompt_tokens": 18, "completion_tokens": 22, "total_tokens": 40, "cost": "0.000100000"}
    return response


@app.get("/fixture/degraded")
def degraded():
    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "degraded", "detail": "Video worker recovering after queue timeout"}, status_code=503)


@app.get("/fixture/down")
def down():
    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "down", "detail": "Provider returned 503"}, status_code=503)
