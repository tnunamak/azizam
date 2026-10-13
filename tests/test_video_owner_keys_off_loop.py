"""The /v1/videos handlers resolve the caller's owner keys (SQLite) off the event loop."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import proxy
from gateway import ownership, request_connections
from tests.support.access import access_context


class _ListRequest:
    query_params: dict[str, str] = {}


def test_video_list_resolves_owner_keys_off_the_loop():
    loop_thread = threading.get_ident()
    lookup_threads: list[int] = []

    def credential_owner_keys(*_args, **_kwargs):
        lookup_threads.append(threading.get_ident())
        return ("owner-a",)

    records = {"p1": {"owner_key_hash": "owner-a", "created_at": 1.0, "status": "completed"}}

    async def call():
        with request_connections.request_scope():
            return await proxy.proxy_comfy_video_list(_ListRequest(), access_context())

    with patch.object(proxy, "_video_job_records", records), \
            patch.object(proxy, "COMFY_ENABLED", True), \
            patch.object(proxy, "_asset_owner_key", lambda _ctx: "owner-a"), \
            patch.object(ownership, "credential_owner_keys", credential_owner_keys):
        response = asyncio.run(call())

    assert response.status_code == 200
    assert lookup_threads, "the owned job never reached the owner-key lookup"
    assert loop_thread not in lookup_threads, "owner keys were resolved on the event loop"
    assert len(lookup_threads) == 1, "owner keys were resolved more than once for one request"
