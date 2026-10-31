"""The closed multipart executor: one POST, typed failures, bounded bodies, no retries."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from gateway.http_multipart import (
    KIND_CONNECT,
    KIND_MALFORMED,
    KIND_TIMEOUT,
    KIND_UPSTREAM,
    MultipartFailure,
    post_multipart,
)
from gateway.mesh_manifest import ImageAsset
from tests.support.fake_upstream import HandlerTransport

IMAGE = ImageAsset(b"\x89PNG\r\n\x1a\n-body", "input.png", "image/png")
URL = "http://trellis.test/generate"


def run_post(handler, *, max_response_bytes: int = 1024, fields: dict | None = None) -> bytes:
    async def go() -> bytes:
        async with httpx.AsyncClient(transport=HandlerTransport(handler)) as client:
            return await post_multipart(
                client, URL, image_field="image", image=IMAGE, fields=fields or {"seed": "7", "bg_removal": "birefnet"},
                timeout_seconds=5, max_response_bytes=max_response_bytes,
            )

    return asyncio.run(go())


def test_success_returns_raw_bytes_and_sends_one_multipart_post() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"glTF\x02\x00\x00\x00raw")

    assert run_post(handler) == b"glTF\x02\x00\x00\x00raw"
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/generate"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.content
    assert b'name="image"; filename="input.png"' in body
    assert b"Content-Type: image/png" in body
    assert b'name="seed"' in body and b"\r\n\r\n7\r\n" in body
    assert b'name="bg_removal"' in body and b"birefnet" in body


def test_upstream_error_is_typed_and_never_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, content=b'{"error":"secret internal detail"}')

    with pytest.raises(MultipartFailure) as failure:
        run_post(handler)
    assert failure.value.kind == KIND_UPSTREAM
    assert failure.value.status == 500
    assert "secret" not in str(failure.value)
    assert calls == 1


def test_discarded_upstream_body_is_bounded() -> None:
    chunks_read = 0

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal chunks_read
            while True:
                chunks_read += 1
                yield b"x" * 1024
                await asyncio.sleep(0)

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, stream=Endless())

    with pytest.raises(MultipartFailure) as failure:
        run_post(handler)
    assert failure.value.kind == KIND_UPSTREAM
    assert chunks_read <= 6


def test_connect_failure_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(MultipartFailure) as failure:
        run_post(handler)
    assert failure.value.kind == KIND_CONNECT


def test_connect_timeout_is_a_connect_failure_and_read_timeout_is_timeout() -> None:
    def connect_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    def read_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(MultipartFailure) as connect:
        run_post(connect_timeout)
    assert connect.value.kind == KIND_CONNECT
    with pytest.raises(MultipartFailure) as read:
        run_post(read_timeout)
    assert read.value.kind == KIND_TIMEOUT


def test_oversized_success_body_is_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"y" * 2048)

    with pytest.raises(MultipartFailure) as failure:
        run_post(handler, max_response_bytes=1024)
    assert failure.value.kind == KIND_MALFORMED


def test_cancellation_propagates_and_is_not_swallowed() -> None:
    async def go() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise asyncio.CancelledError()

        async with httpx.AsyncClient(transport=HandlerTransport(handler)) as client:
            await post_multipart(client, URL, image_field="image", image=IMAGE, fields={},
                                 timeout_seconds=5, max_response_bytes=10)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())
