"""Shared fixtures for the speech tier tests."""

from __future__ import annotations

import copy
import shutil
import struct
import tempfile
import unittest
import wave
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import proxy

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _multipart(fields: list[tuple[str, str]], file_bytes: bytes, *, file_first: bool = False, filename: str = "a.wav") -> tuple[bytes, str]:
    boundary = "XBOUNDARYX"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields
    ]
    file_part = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + file_bytes + b"\r\n"
    ordered = [file_part, *parts] if file_first else [*parts, file_part]
    return b"".join(ordered) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def _wav_bytes(rate: int, channels: int, seconds: float = 0.5) -> bytes:
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 1000) * int(rate * seconds) * channels)
    return buffer.getvalue()


def _client_factory(handler):
    real = httpx.AsyncClient

    def factory(*_args, **_kwargs):
        return real(transport=httpx.MockTransport(handler))

    return factory


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def _base_config(spool: str) -> dict:
    cfg = copy.deepcopy(proxy.config)
    cfg["backends"]["tts-supertonic"] = {
        "name": "supertonic", "host": "127.0.0.1", "port": 8010, "capabilities": ["audio", "speech"], "priority": 200,
        "lifecycle": {"driver": "systemd", "driver_config": {"unit": "supertonic-tts.service", "scope": "user"}},
    }
    cfg["backends"]["stt"]["chunking"] = {**(cfg["backends"]["stt"].get("chunking") or {}), "spool_dir": spool}
    cfg["backends"]["stt-nemotron"] = {
        "name": "nemotron", "host": "127.0.0.1", "port": 8012, "capabilities": ["audio", "transcriptions"], "priority": 200,
        "default_model": "nemotron-3.5-asr-streaming-0.6b", "timeout": 30,
        "chunking": {"enabled": False, "spool_dir": spool},
        "lifecycle": {"driver": "systemd", "driver_config": {"unit": "nemotron-stt.service", "scope": "user"}},
    }
    cfg["backends"]["stt-canary"] = {
        "name": "canary", "host": "127.0.0.1", "port": 8013, "capabilities": ["audio", "transcriptions"], "priority": 200,
        "default_model": "canary-180m-flash", "timeout": 30,
        "chunking": {"enabled": False, "spool_dir": spool},
    }
    return cfg


class RouteCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _base_config(self.tmp.name)
        self.client = TestClient(proxy.app)
        self.ensure = AsyncMock(return_value=True)
        self.health = AsyncMock(return_value=True)
        self.patches = [
            patch.object(proxy, "AUTH_ENABLED", False),
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy, "config", self.cfg),
            patch.object(proxy, "_service_availability_response", return_value=None),
            patch.object(proxy, "ensure_target", self.ensure),
            patch("gateway.lifecycle_drivers._poll_health", self.health),
            patch.object(proxy.receipt_store, "begin"),
            patch.object(proxy.receipt_store, "finalize", return_value=True),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def use_upstream(self, handler) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        async def wrapped(request: httpx.Request) -> httpx.Response:
            await request.aread()
            seen.append(request)
            return handler(request)

        patcher = patch.object(proxy.httpx, "AsyncClient", side_effect=_client_factory(wrapped))
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen



class SttCase(RouteCase):
    def _post(self, fields, *, file_first=False, audio=None, filename="a.wav"):
        body, ctype = _multipart(fields, audio if audio is not None else _wav_bytes(16000, 1), file_first=file_first, filename=filename)
        return self.client.post("/v1/audio/transcriptions", content=body, headers={"content-type": ctype})

    def _json_upstream(self, text="hello world"):
        return self.use_upstream(lambda r: httpx.Response(200, json={"text": text}))
