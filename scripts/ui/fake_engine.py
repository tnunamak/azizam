"""Deterministic OpenAI-compatible engine used only by the isolated UI harness."""
import time

from fastapi import FastAPI, Request

app = FastAPI()


@app.get("/health")
@app.get("/")
def health():
    return {"status": "ok"}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": "demo-chat", "object": "model", "owned_by": "demo"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    return {
        "id": f"chatcmpl-demo-{time.time_ns()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "demo-chat"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Your gateway is connected. This is a demonstration response from the isolated capture engine."},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 18, "completion_tokens": 22, "total_tokens": 40},
    }
