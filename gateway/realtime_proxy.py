"""Relay for the OpenAI Realtime WebSocket (``/v1/realtime``).

The gateway does not implement the Realtime protocol. A sibling service does
(Hugging Face ``speech-to-speech``: VAD, turns, barge-in, tools), and its three
model slots call the gateway's own routes. This module is the front door only:
it takes the client's WebSocket, opens one to the service, and copies frames
both ways until either side closes. Auth and policy stay in ``proxy.py`` with
the other routes; this file holds no gateway state and imports nothing from it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from typing import Iterable, Mapping
from urllib.parse import urlencode

from gateway.outbound_request import backend_query_string

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState
from websockets.exceptions import ConnectionClosed

# Browsers cannot set headers on a WebSocket, so OpenAI lets them carry the key as a subprotocol.
KEY_SUBPROTOCOL_PREFIX = "openai-insecure-api-key."
# The subprotocol OpenAI's server selects. A client that offers any subprotocol needs one echoed back.
SELECTED_SUBPROTOCOL = "realtime"
# One Realtime audio event is base64 PCM in a JSON text frame; a full-turn append can reach a few MB.
MAX_FRAME_BYTES = 32 * 1024 * 1024
CLOSE_GOING_AWAY = 1001
CLOSE_INTERNAL_ERROR = 1011
CLOSE_TRY_AGAIN_LATER = 1013


def key_from_subprotocols(offered: Iterable[str]) -> str:
    for name in offered:
        if name.startswith(KEY_SUBPROTOCOL_PREFIX):
            return name[len(KEY_SUBPROTOCOL_PREFIX):]
    return ""


def accepted_subprotocol(offered: Iterable[str]) -> str | None:
    return SELECTED_SUBPROTOCOL if SELECTED_SUBPROTOCOL in set(offered) else None


def upstream_url(host: str, port: int, query: Mapping[str, str]) -> str:
    """Service URL for one client connection. The client's own key never goes upstream."""
    kept = backend_query_string(urlencode(query))
    suffix = "?" + kept if kept else ""
    return f"ws://{host}:{port}/v1/realtime{suffix}"


def error_event(code: str, message: str) -> dict:
    """Build a server error event, with a fresh Realtime event ID."""
    return {
        "type": "error",
        "event_id": "event_" + secrets.token_hex(12),
        "error": {
            "type": "server_error",
            "code": code,
            "message": message,
            "param": None,
            "event_id": None,
        },
    }


async def relay(client: WebSocket, upstream) -> None:
    """Copy frames until either side closes; report abnormal service failures."""
    client_disconnected = False

    async def client_to_upstream() -> None:
        nonlocal client_disconnected
        while True:
            try:
                message = await client.receive()
            except WebSocketDisconnect:
                client_disconnected = True
                return
            if message["type"] == "websocket.disconnect":
                client_disconnected = True
                return
            data = message.get("text")
            if data is None:
                data = message.get("bytes")
            if data is not None:
                await upstream.send(data)

    async def upstream_to_client() -> None:
        nonlocal client_disconnected
        async for frame in upstream:
            try:
                if isinstance(frame, bytes):
                    await client.send_bytes(frame)
                else:
                    await client.send_text(frame)
            except WebSocketDisconnect:
                client_disconnected = True
                return

    tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
    completed = False
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        completed = True
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        upstream_code = upstream.close_code
        valid_code = upstream_code not in (None, 1005, 1006)
        code = upstream_code if valid_code else CLOSE_GOING_AWAY
        client_open = (
            not client_disconnected
            and client.client_state == WebSocketState.CONNECTED
            and client.application_state == WebSocketState.CONNECTED
        )
        # A real upstream close (including policy code 1008) can raise
        # ConnectionClosedError. Preserve its code, but not unrelated pump errors.
        pump_failed = any(
            isinstance(result, Exception)
            and not (isinstance(result, ConnectionClosed) and valid_code)
            for result in results
        )
        abnormal = completed and client_open and (
            pump_failed or (not tasks[1].cancelled() and not valid_code)
        )
        if abnormal:
            code = CLOSE_INTERNAL_ERROR
        with contextlib.suppress(Exception):
            await upstream.close()
        if abnormal:
            with contextlib.suppress(Exception, WebSocketDisconnect):
                await client.send_text(json.dumps(error_event(
                    "upstream_closed", "The speech service connection ended unexpectedly."
                )))
        if client.application_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception, WebSocketDisconnect):
                await client.close(code=code)
