"""A transport that answers each request from an async handler and reaches no network.

For a test that needs one scripted answer, or one transport failure, from an engine. A scenario that needs an engine
with state belongs in ``scenarios`` instead.
"""

from __future__ import annotations

import typing

import httpx


class HandlerTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler: typing.Callable[[httpx.Request], typing.Awaitable[httpx.Response]]) -> None:
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._handler(request)
