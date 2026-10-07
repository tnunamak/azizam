"""Bound inbound HTTP bodies without buffering them in middleware."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping

from starlette.responses import JSONResponse


_MIB = 1024 * 1024
_MEDIA_PREFIXES = (
    "/v1/images", "/v1/audio", "/v1/videos",
    "/gateway/v1/media", "/gateway/v1/workflows", "/gateway/v1/images", "/gateway/v1/faces",
    "/sdapi/v1",
)


@dataclass(frozen=True)
class RequestBodyLimits:
    json_bytes: int = 32 * _MIB
    # STT accepts a 512 MiB input file; leave room for multipart metadata.
    media_bytes: int = 513 * _MIB

    @classmethod
    def from_config(cls, config: Mapping) -> RequestBodyLimits:
        settings = config.get("request_body_limits") or {}
        defaults = cls()
        values = {}
        for name in ("json_bytes", "media_bytes"):
            raw = os.environ.get(f"AZIZAM_REQUEST_BODY_{name.upper()}", settings.get(name, getattr(defaults, name)))
            value = int(raw)
            if value <= 0:
                raise ValueError(f"request_body_limits.{name} must be a positive byte count")
            values[name] = value
        return cls(**values)

    def for_path(self, path: str) -> int:
        if path.startswith("/openai/"):
            path = path.removeprefix("/openai")
        if any(path == prefix or path.startswith(prefix + "/") for prefix in _MEDIA_PREFIXES):
            return self.media_bytes
        return self.json_bytes


class RequestBodyLimitExceeded(Exception):
    """Raised before an over-limit chunk is handed to a request parser."""


class RequestBodyLimitMiddleware:
    def __init__(self, app, *, limits: RequestBodyLimits):
        self.app = app
        self.limits = limits

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.limits.for_path(scope.get("path", ""))
        error = JSONResponse(
            {"error": {
                "message": f"Request body exceeds the {limit}-byte limit for this route.",
                "type": "invalid_request_error", "param": None, "code": "request_too_large",
            }},
            status_code=413,
        )
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    declared_length = int(value)
                except ValueError:
                    continue  # The HTTP server handles invalid framing.
                if declared_length > limit:
                    return await error(scope, receive, send)

        received = 0
        exceeded = False
        error_sent = False
        response_started = False

        async def limited_receive():
            nonlocal received, exceeded
            if exceeded:
                raise RequestBodyLimitExceeded()
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise RequestBodyLimitExceeded()
            return message

        async def send_error():
            nonlocal error_sent
            if not error_sent and not response_started:
                error_sent = True
                await error(scope, receive, send)

        async def limited_send(message):
            nonlocal response_started
            if exceeded:
                # Some routes catch parser failures broadly. Replace their 500
                # or 502 response with the size error at the ASGI boundary.
                await send_error()
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not exceeded:
                raise
            await send_error()
        if exceeded:
            await send_error()
