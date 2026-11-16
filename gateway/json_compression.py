"""gzip for the JSON the web app reads over a slow link.

Home is 45 KB of JSON, the service registry 43 KB, the status 108 KB. They crossed the wire as
written, so on a phone at 1.6 Mbit/s Home alone took a quarter of a second to arrive; gzip makes
it 6 KB. The static files are already served pre-compressed. This covers the dynamic GET
answers of the app's own API (``/me/api``, ``/admin/api``) when they are JSON and the client
accepts gzip. Buffered JSON varies on Accept-Encoding even when sent without compression.
Streams (``text/event-stream``), anything already encoded, and every other path are untouched.
"""
from __future__ import annotations

import re
import zlib

PREFIXES = ("/me/api/", "/admin/api/")
MIN_BYTES = 1024
LEVEL = 5


def _accepts_gzip(headers):
    accepts = b",".join(value for name, value in headers if name.lower() == b"accept-encoding")
    explicit = False
    gzip_ok = wildcard_ok = False
    for token in accepts.split(b","):
        name, *parameters = token.split(b";")
        name = name.strip().lower()
        if name not in (b"gzip", b"x-gzip", b"*"):
            continue
        quality = []
        for parameter in parameters:
            key, separator, value = parameter.strip().partition(b"=")
            if key.strip().lower() == b"q":
                quality.append(value.strip() if separator else b"")
        # HTTP qvalues range from zero to one, with up to three fractional digits.
        # Repeated or malformed weights are refusals, not implicit defaults.
        acceptable = not quality or (
            len(quality) == 1
            and re.fullmatch(rb"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)", quality[0]) is not None
            and float(quality[0]) > 0
        )
        if name == b"*":
            wildcard_ok |= acceptable
        else:
            explicit = True
            gzip_ok |= acceptable
    return gzip_ok if explicit else wildcard_ok


class JsonCompressionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "GET" or not scope.get("path", "").startswith(PREFIXES):
            return await self.app(scope, receive, send)
        accepts_gzip = _accepts_gzip(scope.get("headers") or ())

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
                if accepts_gzip and len(body) >= MIN_BYTES:
                    compressor = zlib.compressobj(LEVEL, zlib.DEFLATED, 31)  # 31: gzip framing
                    body = compressor.compress(body) + compressor.flush()
                    headers += [(b"content-encoding", b"gzip")]
                headers += [(b"content-length", str(len(body)).encode()), (b"vary", b"Accept-Encoding")]
                await send({**start, "headers": headers})
                return await send({"type": "http.response.body", "body": body})
            return await send(message)

        await self.app(scope, receive, intercept)
