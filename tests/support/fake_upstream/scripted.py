"""A scripted HTTP upstream for tests that run a client in-process: routes by (method, path), calls recorded.

It serves the request through ``httpx.MockTransport``, so no socket is opened. Tests reach it through a client from
``client()``; a handler may read ``request.extensions["timeout"]`` to act as a slow upstream.
"""

from __future__ import annotations

from typing import Callable

import httpx


class ScriptedUpstream:
    """One scripted upstream. ``routes`` maps (method, url-path) to a handler returning an httpx.Response."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(404, json={"error": "unscripted"})
        return handler(request)

    def client(self, timeout: httpx.Timeout | float | None = 5.0) -> httpx.AsyncClient:
        """A client over this upstream. The request carries ``timeout`` as a real client's would, so a handler can read
        ``request.extensions["timeout"]`` to act as an upstream that is slower than that limit."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self), timeout=timeout)

    def count(self, path: str) -> int:
        return sum(1 for call in self.calls if call.url.path == path)
