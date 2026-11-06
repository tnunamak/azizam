"""A performance score is valid only after the complete response passes."""
from __future__ import annotations

import base64
import io
import json
import wave

from PIL import Image

from .fixtures import GATEWAY_MODELS, MODELS, TEXT, TOKENS, TRANSCRIPT, WAV


class CorrectnessError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise CorrectnessError(message)


def grade(case, status, headers, content, events, direct=False):
    name = case["id"]
    require(status == 200, f"{name}: expected 200, got {status}: {content[:400].decode(errors='replace')}")
    kind = case["kind"]
    if case.get("body", {}).get("stream"):
        require("text/event-stream" in headers.get("content-type", ""), f"{name}: wrong SSE media type")
        if kind == "chat" or direct:
            payloads = [json.loads(data) for data in events if data != "[DONE]"]
            require(events[-1:] == ["[DONE]"], f"{name}: missing stream terminator")
            require(all(p.get("object") == "chat.completion.chunk" and p.get("model") == case["body"]["model"]
                        for p in payloads), f"{name}: wrong chat chunk shape/model")
            tokens = [p["choices"][0]["delta"].get("content", "") for p in payloads]
            require("".join(tokens) == "".join(TOKENS), f"{name}: dropped/changed stream text")
            require(payloads[-1]["choices"][0]["finish_reason"] == "stop", f"{name}: wrong finish reason")
            require(payloads[-1]["usage"]["total_tokens"] == 40, f"{name}: missing stream usage")
        else:
            payloads = [json.loads(data) for data in events if data != "[DONE]"]
            types = [p.get("type") for p in payloads]
            require(types[0] == "response.created" and types[-1] == "response.completed", f"{name}: incomplete Responses lifecycle")
            tokens = [p["delta"] for p in payloads if p.get("type") == "response.output_text.delta"]
            require("".join(tokens) == "".join(TOKENS), f"{name}: wrong Responses delta text")
            response = payloads[-1]["response"]
            require(response["status"] == "completed" and response["model"] == case["body"]["model"], f"{name}: wrong Responses completion")
            require(response["usage"]["total_tokens"] == 40, f"{name}: wrong Responses usage")
        return
    if kind == "tts":
        require(content == WAV, f"{name}: changed audio bytes")
        require("audio/" in headers.get("content-type", ""), f"{name}: wrong audio media type")
        with wave.open(io.BytesIO(content)) as handle:
            require(handle.getframerate() == 16000 and handle.getnframes() == 1600, f"{name}: invalid WAV")
        return
    if kind == "status":
        text = content.decode()
        require("text/html" in headers.get("content-type", "") and "<html" in text.lower(), f"{name}: missing HTML")
        require("status build failed" not in text and "status render failed" not in text, f"{name}: degraded status response")
        if not direct:
            require("Azizam" in text and len(content) > 10000, f"{name}: missing built status sections")
            for marker in ("Backends", "Disk pressure", "Models / LLM", "Recent activity timeline", "Backend readiness", "Configuration snapshot"):
                require(marker in text, f"{name}: missing rendered section {marker}")
        return
    if kind == "image" and direct:
        image_bytes = content
    else:
        require("application/json" in headers.get("content-type", ""), f"{name}: wrong JSON media type")
        payload = json.loads(content)
        require(payload.get("error") is None, f"{name}: error envelope {payload.get('error')}")
        if kind == "chat" or (kind == "responses" and direct):
            require(payload["object"] == "chat.completion" and payload["model"] == case["body"]["model"], f"{name}: wrong chat shape/model")
            require(payload["choices"][0]["message"]["content"] == TEXT, f"{name}: changed chat text")
            require(payload["usage"]["total_tokens"] == 40, f"{name}: wrong chat usage")
        elif kind == "responses":
            require(payload["object"] == "response" and payload["status"] == "completed" and
                    payload["model"] == case["body"]["model"], f"{name}: wrong Responses shape/model")
            require(payload["output"][0]["content"][0]["text"] == TEXT and payload["usage"]["total_tokens"] == 40,
                    f"{name}: wrong Responses output/usage")
        elif kind == "models":
            expected = set(MODELS) if direct else GATEWAY_MODELS
            actual = {m["id"] for m in payload["data"]}
            require(payload["object"] == "list" and actual == expected, f"{name}: model catalog mismatch: {sorted(actual)}")
            require(all(m.get("object") == "model" for m in payload["data"]), f"{name}: wrong model shape")
        elif kind == "stt":
            require(payload["text"] == TRANSCRIPT, f"{name}: wrong transcription")
        elif kind == "health":
            require(payload["status"] == ("ok" if direct else "healthy"), f"{name}: wrong health")
        elif kind == "image":
            require(len(payload["data"]) == 1, f"{name}: wrong image count")
            image_bytes = base64.b64decode(payload["data"][0]["b64_json"], validate=True)
        else:
            raise CorrectnessError(f"unknown request kind: {kind}")
    if kind == "image":
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            require(image.size == (32, 32) and image.getpixel((0, 0))[:3] == (31, 107, 83), f"{name}: wrong image pixels")
