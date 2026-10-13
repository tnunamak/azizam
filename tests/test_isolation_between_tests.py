"""State a test sets on a shared module must not reach the next test.

The pairs below only prove isolation when pytest runs them in file order, which
is the default without a shuffle plugin; under a shuffle the second test passes
either way, so a failure here is always a real leak.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient, WebSocketDenialResponse

from gateway import media_delivery


def test_a_begin_delivery_without_a_request_scope() -> None:
    media_delivery.begin(profile_default=None)
    assert media_delivery.current() is not None


def test_b_next_test_sees_no_delivery_context() -> None:
    assert media_delivery.current() is None


def test_websocket_handshake_works_while_a_test_patches_asyncio_sleep() -> None:
    # conftest imports anyio's asyncio backend before any test; if a test
    # imported it first while asyncio.sleep was mocked, anyio would keep the
    # mock and every later websocket handshake in the worker would fail.
    async def deny(websocket):
        await websocket.send_denial_response(PlainTextResponse("denied", status_code=403))

    client = TestClient(Starlette(routes=[WebSocketRoute("/ws", deny)]))
    with patch.object(asyncio, "sleep", AsyncMock()):
        with pytest.raises(WebSocketDenialResponse) as denied:
            with client.websocket_connect("/ws"):
                pass
    assert denied.value.status_code == 403
    assert sys.modules["anyio._backends._asyncio"].sleep is asyncio.sleep
