"""gzip for the JSON the web app reads over a slow link.

Home is 45 KB of JSON, the service registry 43 KB, the status 108 KB. They crossed the wire as
written, so on a phone at 1.6 Mbit/s Home alone took a quarter of a second to arrive; gzip makes
it 6 KB. The static files are already served pre-compressed. This covers the dynamic GET
answers of the app's own API (``/me/api``, ``/admin/api``) when they are JSON and the client
accepts gzip. Streams (``text/event-stream``), anything already encoded, anything small, and
every other path are passed through untouched.
"""
from __future__ import annotations

import zlib

PREFIXES = ("/me/api/", "/admin/api/")
MIN_BYTES = 1024
LEVEL = 5


class JsonCompressionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "GET" or not scope.get("path", "").startswith(PREFIXES):
            return await self.app(scope, receive, send)
        accepts = b""
        for name, value in scope.get("headers") or ():
            if name == b"accept-encoding":
                accepts = value
        if b"gzip" not in accepts:
            return await self.app(scope, receive, send)

        start: dict | None = None
        chunks: list[bytes] = []
        passthrough = False

        async def intercept(message):
            nonlocal start, passthrough
            if passthrough:
                return await send(message)
            if message["type"] == "http.response.start":
                headers = {k.lower(): v for k, v in message.get("headers") or ()}
                compressible = (
                    message["status"] == 200
                    and headers.get(b"content-type", b"").startswith(b"application/json")
                    and b"content-encoding" not in headers
                )
                if not compressible:
                    passthrough = True
                    return await send(message)
                start = message
                return None
            if message["type"] == "http.response.body" and start is not None:
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return None
                body = b"".join(chunks)
                headers = [(k, v) for k, v in start.get("headers") or () if k.lower() != b"content-length"]
                if len(body) >= MIN_BYTES:
                    compressor = zlib.compressobj(LEVEL, zlib.DEFLATED, 31)  # 31: gzip framing
                    body = compressor.compress(body) + compressor.flush()
                    headers += [(b"content-encoding", b"gzip")]
                headers += [(b"content-length", str(len(body)).encode()), (b"vary", b"Accept-Encoding")]
                await send({**start, "headers": headers})
                return await send({"type": "http.response.body", "body": body})
            return await send(message)

        await self.app(scope, receive, intercept)
