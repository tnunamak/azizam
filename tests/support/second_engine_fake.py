"""A fake upstream for the made-up second engine of tests/fixtures/engine_protocol/acme.

An ASGI app. It records every request and serves a small API that is not OpenAI-shaped: custom paths, a nested
answer, an NDJSON stream cut across chunks, a header key, a multipart upload and a submit-poll-fetch render job.
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "engine_protocol" / "acme"
KEY = "acme-secret-key"
OUTPUTS = {"o1": b"\x89PNG-one", "o 2": b"\x89PNG-two"}


class AcmeFake:
    def __init__(self, *, phases: tuple[str, ...] = ("WAITING", "WORKING", "FINISHED")) -> None:
        self.requests: list[dict] = []
        self.phases = list(phases)
        self.cancelled = False

    async def __call__(self, scope, receive, send) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = {k.decode(): v.decode("latin-1") for k, v in scope["headers"]}
        target = scope["raw_path"].decode() + (("?" + scope["query_string"].decode()) if scope["query_string"] else "")
        self.requests.append({"method": scope["method"], "target": target, "headers": headers, "body": body})
        if headers.get("x-acme-key") != KEY:
            return await self._json(send, 401, {"error": "bad key"})
        method, path = scope["method"], scope["path"]
        if (method, path) == ("GET", "/api/v3/ping"):
            return await self._reply(send, 204, b"", b"")
        if (method, path) == ("GET", "/api/v3/catalog"):
            return await self._json(send, 200, {"items": [{"slug": "acme-1"}, {"slug": "acme-2"}]})
        if (method, path) == ("POST", "/api/v3/converse"):
            return await self._converse(send, json.loads(body))
        if (method, path) == ("POST", "/api/v3/listen"):
            return await self._json(send, 200, {"transcript": {"text": "hello from audio"}})
        if (method, path) == ("POST", "/api/v3/renders"):
            return await self._json(send, 202, {"ticket": {"id": "r-7"}})
        if path == "/api/v3/renders/r-7" and method == "GET":
            phase = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
            doc = {"phase": phase}
            if phase == "FINISHED":
                doc["outputs"] = [{"key": key} for key in OUTPUTS]
            return await self._json(send, 200, doc)
        if path.startswith("/api/v3/renders/r-7/outputs/") and method == "GET":
            key = path.rsplit("/", 1)[1]
            if key in OUTPUTS:
                return await self._reply(send, 200, OUTPUTS[key], b"image/png")
        if path == "/api/v3/renders/r-7" and method == "DELETE":
            self.cancelled = True
            return await self._reply(send, 204, b"", b"")
        return await self._json(send, 404, {"error": "no such route"})

    async def _converse(self, send, request: dict) -> None:
        if request.get("delivery") == "live":
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/x-ndjson")]})
            wire = (FIXTURES / "stream.upstream.ndjson").read_bytes()
            cut = wire.index(b'"lo"') + 2  # cut inside the second line
            for piece in (wire[:cut], wire[cut:]):
                await send({"type": "http.response.body", "body": piece, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
            return
        await self._reply(send, 200, (FIXTURES / "chat.upstream.json").read_bytes().strip(), b"application/json")

    async def _json(self, send, status: int, doc: dict) -> None:
        await self._reply(send, status, json.dumps(doc).encode(), b"application/json")

    @staticmethod
    async def _reply(send, status: int, body: bytes, content_type: bytes) -> None:
        headers = [(b"content-type", content_type)] if content_type else []
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
