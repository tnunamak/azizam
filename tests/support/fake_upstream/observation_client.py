"""HTTP observation peer used by contract tests."""

from __future__ import annotations

from collections.abc import Callable

import httpx


def observation_client(handler: Callable[[httpx.Request], httpx.Response]):
    def make_client(**_kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

    return make_client
