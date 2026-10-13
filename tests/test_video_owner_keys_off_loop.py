"""The /v1/videos handlers resolve the caller's owner keys (SQLite) off the event loop."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import proxy
from gateway import ownership, request_connections
from tests.support.access import access_context


class _ListRequest:
    query_params: dict[str, str] = {}


class _EmptyComfy:
    """ComfyUI answering with an empty history and an idle queue."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **_kwargs):
        body = {"queue_running": [], "queue_pending": []} if url.endswith("/queue") else {}
        return SimpleNamespace(status_code=200, json=lambda: body, text="{}")


@pytest.mark.parametrize("call_route", ["list", "status"])
def test_video_routes_resolve_owner_keys_off_the_loop(call_route):
    loop_thread = threading.get_ident()
    lookup_threads: list[int] = []

    def credential_owner_keys(*_args, **_kwargs):
        lookup_threads.append(threading.get_ident())
        return ("owner-a",)

    records = {"p1": {"owner_key_hash": "owner-a", "created_at": 1.0, "status": "completed"}}

    async def call():
        with request_connections.request_scope():
            if call_route == "list":
                return await proxy.proxy_comfy_video_list(_ListRequest(), access_context())
            return await proxy.proxy_comfy_video_status(proxy._video_id_from_prompt_id("p1"), access_context())

    with patch.object(proxy, "_video_job_records", records), \
            patch.object(proxy, "COMFY_ENABLED", True), \
            patch.object(proxy.httpx, "AsyncClient", lambda **_k: _EmptyComfy()), \
            patch.object(proxy, "_asset_owner_key", lambda _ctx: "owner-a"), \
            patch.object(ownership, "credential_owner_keys", credential_owner_keys):
        response = asyncio.run(call())

    assert response.status_code in (200, 404)  # the oracle is the lookup thread, not the body
    assert lookup_threads, "the owned job never reached the owner-key lookup"
    assert loop_thread not in lookup_threads, "owner keys were resolved on the event loop"
    assert len(lookup_threads) == 1, "owner keys were resolved more than once for one request"
