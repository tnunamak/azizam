"""Install an AsyncClient replacement for tests that need a scripted response."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx


def install_async_client_stub(monkeypatch, post: Callable[..., Awaitable[httpx.Response]]) -> None:
    """Replace ``httpx.AsyncClient`` with a context-managed client using ``post``."""

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *args, **kwargs):
            return await post(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
