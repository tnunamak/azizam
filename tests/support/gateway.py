"""Point the in-process gateway at fake upstreams through its real registry."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

import proxy
from gateway import admin_store
from tests.registry_test_helpers import write_service_registry_for_test
from tests.support.fake_upstream import StreamingASGITransport

LITELLM_KEY_ENV = "FK_LITELLM_MASTER_KEY"
LITELLM_MASTER_KEY = "fk-litellm-master-key"
# The gateway's own read budget in a slow-first-byte case is real time. The
# request must reach the fake before it expires (0.2 s and 1 s both lost that
# race under gate load), so the budget is far above a loaded host's delivery
# time of one local request. Cost: each such case waits this long.
READ_BUDGET_SECONDS = 5.0


def use_admin_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Give the gateway a fresh, real admin store for this test."""
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", db)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    return db


def register_litellm(monkeypatch: pytest.MonkeyPatch, db: Path, base_url: str, **config: Any) -> None:
    """Register the LLM execution substrate (an openai-compatible upstream) at ``base_url``."""
    monkeypatch.setenv(LITELLM_KEY_ENV, LITELLM_MASTER_KEY)
    write_service_registry_for_test(
        {
            "services": {
                "litellm": {
                    "id": "litellm",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "capabilities": ["llm.execution"],
                    "config": {"base_url": base_url, "api_key_env": LITELLM_KEY_ENV, **config},
                }
            }
        },
        path=db,
    )


def gateway_client(*, client: tuple[str, int] = ("127.0.0.1", 40000)) -> httpx.AsyncClient:
    """An httpx client for the in-process gateway that streams chunk by chunk."""
    return httpx.AsyncClient(
        transport=StreamingASGITransport(proxy.app, client=client),
        base_url="http://gateway.test",
    )


class ResponseTap:
    """Wrap an ASGI app and let a test wait on the response bytes it has sent.

    Protocol frontends reach the chat route over the gateway's own loopback
    hop. Tapping that hop lets a test release the next upstream chunk only
    after the hop has relayed the previous one, whatever the frontend emits.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.sent = bytearray()
        self._changed: asyncio.Event | None = None

    async def __call__(self, scope, receive, send) -> None:
        async def tapped(message) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                self.sent += message["body"]
                if self._changed is not None:
                    self._changed.set()
            await send(message)

        await self.app(scope, receive, tapped)

    async def until_sent(self, marker: bytes, count: int) -> None:
        """Return once ``marker`` occurs ``count`` times in the bytes sent so far."""
        while self.sent.count(marker) < count:
            self._changed = asyncio.Event()
            await self._changed.wait()
