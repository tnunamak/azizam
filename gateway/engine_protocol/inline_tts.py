"""The in-tree speech client, behind the adapter interface.

This is the first reviewed adapter (``executor: adapter:inline-tts``). It
moves *where the request leaves the gateway*, not what it sends: the URL, the
header set, the body bytes (``json.dumps`` defaults, as the gateway always
wrote them), the timeouts and the way each failure surfaces are those the
speech dispatch used before. The caller (``proxy.py``) still decides the
engine, the voice, the headers and the audio conversion; policy stays there.

Operations
- ``speech.synthesize``: one buffered POST. A non-2xx answer is a ``Reply``
  (the caller maps it); transport failures are ``TransportFailure`` with the
  original exception text.
- ``speech.stream``: the same POST, streamed. A non-2xx answer is a buffered
  ``Reply``. Otherwise a ``StreamReply`` of raw upstream chunks; a transport
  error while reading becomes ``TransportFailure("aborted")``.
- ``speech.voice_check``: POST ``/v1/audio/voice-check`` with ``request.body``
  as httpx ``json=`` (compact), as the custom-voice quality probe always sent
  it (Chatterbox answers it with a speaker similarity).
- ``speech.design``: POST ``/v1/audio/speech`` with ``request.body`` as httpx
  ``json=``, as voice design always sent it.

The two voice operations answer a ``WireReply`` (``json()`` raises a parse
error where it is called). Only httpx errors become ``TransportFailure`` (the
original exception is its ``__cause__``); any other error passes through, as
it did from the call they replace.

``health`` is unknown: readiness of these engines belongs to the lifecycle
driver today. Inventory, jobs and recovery are not implemented.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from gateway.engine_protocol.adapter import ADAPTER_INTERFACE_VERSION
from gateway.engine_protocol.errors import TransportFailure, UnknownOperation, UnsupportedByAdapter
from gateway.engine_protocol.httpx_wire import failure, reply_from
from gateway.engine_protocol.types import (
    AssetRef, CanonicalRequest, Connection, Health, Inventory, JobEvent, JobRef, JobStatus, Reply, StreamReply,
)

SPEECH_PATH = "/v1/audio/speech"
VOICE_CHECK_PATH = "/v1/audio/voice-check"
# The voice operations post ``request.body`` as httpx ``json=``; each names its route.
_JSON_ROUTES = {"speech.voice_check": VOICE_CHECK_PATH, "speech.design": SPEECH_PATH}
OPERATIONS = ("speech.synthesize", "speech.stream", *_JSON_ROUTES)


def _lower(headers: httpx.Headers) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()}


def _failure(error: Exception) -> TransportFailure:
    if isinstance(error, httpx.TimeoutException):
        return TransportFailure("timeout", str(error))
    if isinstance(error, httpx.ConnectError):
        return TransportFailure("unreachable", str(error))
    return TransportFailure("aborted", str(error))


class InlineTtsAdapter:
    interface_version = ADAPTER_INTERFACE_VERSION

    async def health(self, conn: Connection) -> Health:
        return Health("unknown", "Readiness of this engine is observed by its lifecycle driver, not by this adapter.")

    async def inventory(self, conn: Connection) -> Inventory:
        raise UnsupportedByAdapter("The speech adapter lists no inventory.")

    async def invoke(self, conn: Connection, op: str, request: CanonicalRequest) -> Reply | StreamReply:
        if op not in OPERATIONS:
            raise UnknownOperation(f"The speech adapter has no operation '{op}'.")
        if op in _JSON_ROUTES:
            return await self._posted_json(f"{conn.address}{_JSON_ROUTES[op]}", request.body, conn)
        url = f"{conn.address}{SPEECH_PATH}"
        body = request.raw_body if request.raw_body is not None else json.dumps(request.body).encode()
        headers = request.headers
        timeout = httpx.Timeout(conn.timeouts.first_byte_s, connect=conn.timeouts.connect_s)
        if op == "speech.synthesize":
            return await self._buffered(url, headers, body, timeout)
        return await self._streamed(url, headers, body, timeout)

    @staticmethod
    async def _posted_json(url: str, payload: Any, conn: Connection) -> Reply:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(conn.timeouts.first_byte_s, connect=conn.timeouts.connect_s)) as client:
                resp = await client.post(url, json=payload)
        except httpx.HTTPError as error:
            raise failure(error) from error
        return reply_from(resp)

    @staticmethod
    async def _buffered(url: str, headers: Any, body: bytes, timeout: httpx.Timeout) -> Reply:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, content=body)
        except Exception as error:  # noqa: BLE001 - every failure surfaces typed, with the original text
            raise _failure(error) from error
        return Reply(resp.status_code, _lower(resp.headers), raw=resp.content)

    @staticmethod
    async def _streamed(url: str, headers: Any, body: bytes, timeout: httpx.Timeout) -> Reply | StreamReply:
        client = httpx.AsyncClient(timeout=timeout)
        upstream: httpx.Response | None = None
        handed_over = False
        try:
            upstream = await client.send(client.build_request("POST", url, headers=headers, content=body), stream=True)
            if not 200 <= upstream.status_code < 300:
                content = await upstream.aread()
                return Reply(upstream.status_code, _lower(upstream.headers), raw=content)
            raw_chunks = upstream.aiter_raw()
            response, owner = upstream, client

            async def chunks() -> AsyncIterator[bytes]:
                try:
                    async for chunk in raw_chunks:
                        yield chunk
                except (httpx.TransportError, httpx.StreamError) as error:
                    raise TransportFailure("aborted", repr(error)) from error

            async def close() -> None:
                await response.aclose()
                await owner.aclose()

            handed_over = True
            return StreamReply(upstream.status_code, _lower(upstream.headers), chunks(), close)
        except Exception as error:  # noqa: BLE001 - every failure surfaces typed, with the original text
            raise _failure(error) from error
        finally:
            if not handed_over:
                if upstream is not None:
                    await upstream.aclose()
                await client.aclose()

    async def submit(self, conn: Connection, op: str, request: CanonicalRequest) -> JobRef:
        raise UnsupportedByAdapter("Speech is answered in one request; it has no job.")

    async def poll(self, conn: Connection, ref: JobRef) -> JobStatus:
        raise UnsupportedByAdapter("Speech is answered in one request; it has no job.")

    async def events(self, conn: Connection, ref: JobRef) -> AsyncIterator[JobEvent]:
        raise UnsupportedByAdapter("Speech is answered in one request; it has no job.")
        yield  # pragma: no cover

    async def cancel(self, conn: Connection, ref: JobRef) -> None:
        raise UnsupportedByAdapter("Speech is answered in one request; it has no job.")

    async def recover(self, conn: Connection, ref: JobRef) -> JobStatus | None:
        raise UnsupportedByAdapter("Speech is answered in one request; it has no job.")

    async def fetch_assets(self, conn: Connection, ref: JobRef, status: JobStatus) -> tuple[AssetRef, ...]:
        raise UnsupportedByAdapter("Speech is answered in one request; it has no job.")
