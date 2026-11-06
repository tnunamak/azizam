"""Server-Timing for the person API, so real timings show in browser dev tools.

``ServerTimingMiddleware`` adds ``total`` to every ``/me/api/`` response.
A handler adds a named phase with ``span(name)``; phases of one name add up.
The header carries durations only, no data.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_PHASES: ContextVar[dict[str, float] | None] = ContextVar("azizam_server_timing", default=None)


@contextmanager
def span(name: str) -> Iterator[None]:
    phases = _PHASES.get()
    started = time.perf_counter()
    try:
        yield
    finally:
        if phases is not None:
            phases[name] = phases.get(name, 0.0) + (time.perf_counter() - started) * 1000


def header_value(phases: dict[str, float], total_ms: float) -> str:
    parts = [f"{name};dur={ms:.1f}" for name, ms in phases.items()]
    parts.append(f"total;dur={total_ms:.1f}")
    return ", ".join(parts)


class ServerTimingMiddleware:
    PREFIX = "/me/api/"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.PREFIX):
            return await self.app(scope, receive, send)
        phases: dict[str, float] = {}
        token = _PHASES.set(phases)
        started = time.perf_counter()

        async def send_with_timing(message):
            if message["type"] == "http.response.start":
                total = (time.perf_counter() - started) * 1000
                headers = [*message.get("headers", []), (b"server-timing", header_value(phases, total).encode("ascii"))]
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        finally:
            _PHASES.reset(token)
