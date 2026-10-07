"""Controlled backend payloads; extend the existing UI fake, never route stubs."""
from __future__ import annotations

import asyncio
import io
import json
import time
import wave

from fastapi import Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from PIL import Image

from scripts.ui import fake_engine

DELAY_MS = 2.0
CHUNK_MS = 2.0
TEXT = "Your gateway is connected. This is a demonstration response from the isolated capture engine."
TOKENS = ["Your ", "gateway ", "is ", "connected."]
TRANSCRIPT = "controlled transcription"
MODELS = ["demo-chat", "demo-chat-heldout"]
GATEWAY_MODELS = set(MODELS) | {
    "ace-step-1.5-2b-base", "ace-step-1.5-xl-turbo", "anima-turbo", "current", "current-fast",
    "face-likeness", "flux2-klein-4b", "flux2-klein-4b-edit", "flux2-klein-4b-inpaint",
    "flux2-klein-4b-seamless-tile", "image-identity", "krea2-turbo", "krea2-turbo-snofs",
    "qwen-image-2.1-edit", "qwen-image-2.1-edit-multiref", "qwen-image-2.1-inpaint",
    "sa3-small-sfx", "sdxl-seamless-tile", "seedvr2-upscale", "stt", "tangoflux", "tts",
    "video-ltx-2.5-i2v", "video-ltx-2.5-long", "video-ltx-2.5-t2v", "video-minimax-h3-i2v",
    "video-minimax-h3-long", "video-minimax-h3-t2v", "video-wan-2.2-ti2v-5b-q8", "yue2-song",
}


def wav_bytes(frames: int = 1600) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 32), (31, 107, 83)).save(output, format="PNG")
    return output.getvalue()


PNG = png_bytes()
WAV = wav_bytes()


def create_engine():
    # Reuse the UI fake's app and chat implementation; replace only its variable
    # IDs/timestamps and add its missing streaming/media contracts.
    app = fake_engine.app
    emissions = {}
    calls = {}

    @app.middleware("http")
    async def count_calls(request, call_next):
        path = request.url.path
        if not path.startswith("/__bench"):
            calls[path] = calls.get(path, 0) + 1
        return await call_next(request)

    @app.get("/__bench/counts")
    async def counts():
        return calls
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") not in
                         {"/v1/models", "/v1/chat/completions"}]

    @app.get("/v1/models")
    async def models():
        await asyncio.sleep(DELAY_MS / 1000)
        return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "bench"} for m in MODELS]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        if not body.get("stream"):
            await asyncio.sleep(DELAY_MS / 1000)
            payload = await fake_engine.chat(request)
            payload.update(id="chatcmpl-bench", created=1700000000)
            return payload

        async def chunks():
            stamps = []
            emissions[body["user"]] = stamps
            await asyncio.sleep(DELAY_MS / 1000)
            for index, token in enumerate(TOKENS):
                if index:
                    await asyncio.sleep(CHUNK_MS / 1000)
                chunk = {"id": "chatcmpl-bench", "object": "chat.completion.chunk", "created": 1700000000,
                         "model": body.get("model"), "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}]}
                encoded = "data: " + json.dumps(chunk) + "\n\n"
                stamps.append(time.monotonic_ns())
                yield encoded
            final = {"id": "chatcmpl-bench", "object": "chat.completion.chunk", "created": 1700000000,
                     "model": body.get("model"), "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 18, "completion_tokens": 22, "total_tokens": 40}}
            yield "data: " + json.dumps(final) + "\n\ndata: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    @app.post("/__bench/emissions")
    async def get_emissions(request: Request):
        return {key: emissions.pop(key) for key in await request.json()}

    @app.post("/v1/audio/speech")
    async def speech(request: Request):
        assert (await request.json())["input"]
        await asyncio.sleep(DELAY_MS / 1000)
        return Response(WAV, media_type="audio/wav")

    @app.post("/v1/audio/transcriptions")
    async def transcription(request: Request):
        form = await request.form()
        assert await form["file"].read()
        await asyncio.sleep(DELAY_MS / 1000)
        return {"text": TRANSCRIPT}

    @app.post("/prompt")
    async def prompt(request: Request):
        assert (await request.json())["prompt"]
        await asyncio.sleep(DELAY_MS / 1000)
        return {"prompt_id": "bench-prompt", "node_errors": {}}

    @app.get("/history/{prompt_id}")
    async def history(prompt_id: str):
        return {prompt_id: {"status": {"completed": True, "status_str": "success"},
                            "outputs": {"3": {"images": [{"filename": "bench.png", "type": "output", "subfolder": ""}]}}}}

    @app.get("/view")
    async def view():
        return Response(PNG, media_type="image/png")

    @app.get("/queue")
    async def queue():
        return {"queue_running": [], "queue_pending": []}

    @app.get("/system_stats")
    async def system_stats():
        return {"system": {"ram_total": 17179869184, "ram_free": 8589934592}, "devices": []}

    @app.get("/props")
    async def props():
        return {"default_generation_settings": {}, "total_slots": 32}

    @app.get("/object_info")
    async def object_info():
        return {}

    @app.post("/__bench/image")
    async def image():
        await asyncio.sleep(DELAY_MS / 1000)
        return Response(PNG, media_type="image/png")

    @app.get("/__bench/status")
    async def status():
        return HTMLResponse("<!doctype html><html><body>fixture status</body></html>")

    return app
