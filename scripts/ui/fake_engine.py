"""Deterministic OpenAI-compatible engine used only by the isolated UI harness."""
import asyncio
import os
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI()

# The first-run fixture (scripts/ui/serve.py, UI_E2E_FIRST_RUN) runs two copies:
# - FAKE_SUBSTRATE_CONFIG names the LiteLLM config the gateway writes. This copy
#   stands in for LiteLLM: a chat goes to the engine address that config gives its
#   model, so a reply proves the route the operator set up, not the stand-in itself.
# - FAKE_ENGINE_MODEL and FAKE_ENGINE_REPLY make a copy the operator can add as an
#   engine, with a model and a reply of its own.
_SUBSTRATE_CONFIG = os.environ.get("FAKE_SUBSTRATE_CONFIG")
_ENGINE_MODEL = os.environ.get("FAKE_ENGINE_MODEL")
_ENGINE_REPLY = os.environ.get("FAKE_ENGINE_REPLY")

# Request state per test namespace (docs/design/TEST_STRATEGY.md 3.4.7). One
# engine serves many parallel browser tests; a test tags its prompts with
# `[ns:<namespace>]` (web/e2e/fixtures/isolation.ts) and its teardown reads
# its own log, so two invocations never see each other's requests.
_NAMESPACE = re.compile(rb"\[ns:([a-z0-9-]+)\]")
_REQUESTS: dict[str, dict[str, int]] = {}


@app.middleware("http")
async def namespaced_request_log(request: Request, call_next):
    if request.url.path.startswith("/fixture/") or request.url.path in {"/health", "/"}:
        return await call_next(request)
    body = await request.body()
    match = _NAMESPACE.search(body)
    namespace = match.group(1).decode() if match else "untagged"
    entry = _REQUESTS.setdefault(namespace, {"in_flight": 0, "requests": 0})
    entry["in_flight"] += 1
    entry["requests"] += 1
    try:
        response = await call_next(request)
    except BaseException:
        entry["in_flight"] -= 1
        raise
    body_iterator = response.body_iterator

    # A request stays in flight until its last body byte is sent.
    async def tracked():
        try:
            async for chunk in body_iterator:
                yield chunk
        finally:
            entry["in_flight"] -= 1

    response.body_iterator = tracked()
    return response


@app.get("/fixture/requests")
def namespace_requests(namespace: str):
    # "*" sums every namespace, including untagged requests: the serial
    # global-state project reads it after each test, since no other test runs.
    if namespace == "*":
        return {key: sum(item[key] for item in _REQUESTS.values()) for key in ("in_flight", "requests")}
    return _REQUESTS.get(namespace, {"in_flight": 0, "requests": 0})


# A chat whose prompt holds `[hold]` waits until its namespace is released, so
# a test can leave an upstream request open (the fixture's teardown proof).
_HELD: dict[str, asyncio.Event] = {}


@app.post("/fixture/release")
def release(namespace: str):
    _HELD.setdefault(namespace, asyncio.Event()).set()
    return {"released": namespace}


@app.get("/health")
@app.get("/")
def health():
    return {"status": "ok"}


@app.get("/v1/models")
def models():
    if os.environ.get('UI_SEED_PROFILE') == 'household':
        from scripts.ui.household_seed import MODEL_GROUPS
        return {'object': 'list', 'data': [{'id': mid, 'object': 'model', 'owned_by': 'household', 'name': name} for group in MODEL_GROUPS.values() for mid, name, cap in group]}
    return {"object": "list", "data": [{"id": _ENGINE_MODEL or "demo-chat", "object": "model", "owned_by": "demo"}]}


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


async def send_to_configured_engine(body: dict):
    import httpx
    import yaml

    try:
        entries = yaml.safe_load(open(_SUBSTRATE_CONFIG).read())["model_list"]
    except (OSError, KeyError, TypeError):
        entries = []
    entry = next((item for item in entries if item.get("model_name") == body.get("model")), None)
    if entry is None:
        return JSONResponse({"error": {"code": "model_not_found",
                                       "message": f"{body.get('model')} is not in the LiteLLM config"}}, status_code=400)
    params = entry["litellm_params"]
    upstream = {**body, "model": params["model"].split("/", 1)[-1]}
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        answer = await client.post(params["api_base"].rstrip("/") + "/chat/completions", json=upstream)
    return Response(answer.content, status_code=answer.status_code, media_type="application/json")


@app.post("/v1/chat/completions")
@app.post("/provider/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    if _SUBSTRATE_CONFIG:
        return await send_to_configured_engine(body)
    messages = body.get("messages") or []
    content = " ".join(str((message or {}).get("content", "")) for message in messages if isinstance(message, dict))
    if "[hold]" in content and (held := _NAMESPACE.search(content.encode())):
        await _HELD.setdefault(held.group(1).decode(), asyncio.Event()).wait()
    response = {
        "id": f"chatcmpl-demo-{time.time_ns()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "demo-chat"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": _ENGINE_REPLY or "Your gateway is connected. This is a demonstration response from the isolated capture engine."},
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
# (proxy_graph_image_request), so this answers the ComfyUI calls it makes; speech
# answers the OpenAI speech route. Both return fixture media.
_FIXTURE_MEDIA = os.path.join(os.path.dirname(__file__), "fixtures", "household")
_COMFY_PROMPTS: dict[str, int] = {}


# Files the gateway sent on to the engine, so a browser test can prove the
# engine got the picture's bytes (not a link or a JSON note about it).
_UPLOADS: list[dict] = []


@app.post("/upload/image")
async def comfy_upload(request: Request):
    form = await request.form()
    for field, part in form.multi_items():
        if not hasattr(part, "read"):
            continue
        data = await part.read()
        _UPLOADS.append({
            "field": field,
            "filename": part.filename,
            "content_type": part.content_type,
            "bytes": len(data),
            "head_hex": data[:8].hex(),
        })
        return {"name": part.filename, "subfolder": "", "type": "input"}
    return JSONResponse({"error": "no file part"}, status_code=400)


@app.get("/fixture/uploads")
def fixture_uploads():
    return {"uploads": _UPLOADS}


@app.delete("/fixture/uploads")
def fixture_uploads_reset():
    _UPLOADS.clear()
    return {"uploads": []}


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


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """The OpenAI embeddings wire shape: one short vector per input."""
    body = await request.json()
    inputs = body.get("input")
    inputs = inputs if isinstance(inputs, list) else [inputs]
    return {
        "object": "list",
        "model": body.get("model", "demo-embedding"),
        "data": [{"object": "embedding", "index": index, "embedding": [0.25, -0.5, 0.125]} for index, _ in enumerate(inputs)],
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    }


@app.post("/v1/audio/transcriptions")
async def transcriptions(request: Request):
    """The OpenAI transcription wire shape for a fixed sentence."""
    await request.body()
    return {"text": "Your gateway is connected."}


@app.post("/v1/systemone")
async def systemone(request: Request):
    """The TypeSafe System One wire shape, built by the gateway's own readout code
    from fixed option scores, so the answer has the structure a real engine sends."""
    from fastapi.responses import JSONResponse

    from gateway import decision_engine

    class FixedScores:
        def score_options(self, context):
            return {index: -float(index) for index in range(len(context.options))}

    body = await request.json()
    answer = decision_engine.build_systemone_response(
        decision_engine.parse_request(body), FixedScores(), model=body.get("model", "demo-decider"),
        engine="cpu", placement="cpu", residency="on_demand",
    )
    return JSONResponse(answer, headers={"x-decision-engine": "cpu"})
