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


# The household fixture's image and voice engines (D1.5b): the two core member
# tasks, "Make a picture" and "Read text aloud", must finish in the isolated
# harness. The gateway always generates images through a ComfyUI API
# (proxy_comfy_request), so this answers the ComfyUI calls it makes; speech
# answers the OpenAI speech route. Both return fixture media.
_FIXTURE_MEDIA = os.path.join(os.path.dirname(__file__), "fixtures", "household")
_COMFY_PROMPTS: dict[str, int] = {}


@app.post("/prompt")
async def comfy_prompt(request: Request):
    await request.json()
    prompt_id = f"fixture-{time.time_ns()}"
    _COMFY_PROMPTS[prompt_id] = len(_COMFY_PROMPTS)
    return {"prompt_id": prompt_id, "number": _COMFY_PROMPTS[prompt_id], "node_errors": {}}


@app.get("/history/{prompt_id}")
def comfy_history(prompt_id: str):
    if prompt_id not in _COMFY_PROMPTS:
        return {}
    return {prompt_id: {
        "status": {"completed": True, "status_str": "success", "messages": []},
        "outputs": {"9": {"images": [{"filename": "apple-tart.png", "subfolder": "", "type": "output"}]}},
    }}


@app.get("/view")
def comfy_view(filename: str):
    """The fixture photo re-encoded as PNG, as ComfyUI saves its output. The
    gateway's cache is content-addressed, so serving the seeded file's own
    bytes would fold the new picture into an existing Library item."""
    import io
    from fastapi.responses import JSONResponse, Response
    from PIL import Image

    stem = os.path.splitext(os.path.basename(filename))[0]
    path = os.path.join(_FIXTURE_MEDIA, f"{stem}.webp")
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    buffer = io.BytesIO()
    with Image.open(path) as image:
        image.convert("RGB").save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")


@app.get("/queue")
def comfy_queue():
    return {"queue_running": [], "queue_pending": []}


@app.get("/system_stats")
def comfy_system_stats():
    return {"system": {"os": "fixture"}, "devices": []}


@app.post("/v1/audio/speech")
async def speech(request: Request):
    """Two seconds of a soft tone as WAV, as a real speech engine returns WAV
    that the gateway then encodes to the requested format."""
    import io
    import math
    import struct
    import wave
    from fastapi.responses import Response

    import hashlib

    body = await request.body()
    # Each text gets its own pitch, so different requests make different
    # files (assets are content-addressed) and the same text the same file.
    # One digest byte gave 256 pitches: two prompts made the same file 1 time
    # in 256 and became one Library item. Two bytes give 65,536 pitches
    # 1/256 Hz apart; over two seconds that is about 100 sample steps apart.
    pitch = 180 + int.from_bytes(hashlib.sha256(body).digest()[:2], "big") / 256
    rate = 16000
    frames = b"".join(
        struct.pack("<h", int(2000 * math.sin(2 * math.pi * pitch * n / rate) * min(1, n / 800, (2 * rate - n) / 800)))
        for n in range(2 * rate)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)
    return Response(buffer.getvalue(), media_type="audio/wav")
