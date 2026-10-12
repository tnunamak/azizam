"""In-process transports: reach an ASGI app with httpx, chunk by chunk.

``httpx.ASGITransport`` buffers the whole response body, so a client never
sees a partial stream. ``StreamingASGITransport`` hands each body message to
the client as one chunk, reports ``http.disconnect`` to the app only when the
client closes the response, and turns an app-side drop into the same
``httpx.RemoteProtocolError`` a real peer close produces.

``HostRouter`` sends requests for registered ``host[:port]`` names to an ASGI
app and leaves every other request on the real network transport. It patches
``httpx.AsyncHTTPTransport`` (the process's HTTP boundary), not gateway code.
"""

from __future__ import annotations

import asyncio
import typing

import httpx
import pytest

from .app import FakeDisconnect

_END = object()


class _Abort:
    def __init__(self, error: BaseException) -> None:
        self.error = error


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, queue: asyncio.Queue, task: asyncio.Task, disconnected: asyncio.Event) -> None:
        self._queue = queue
        self._task = task
        self._disconnected = disconnected

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        while True:
            item = await self._queue.get()
            if item is _END:
                return
            if isinstance(item, _Abort):
                if isinstance(item.error, FakeDisconnect):
                    raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
                raise item.error
            yield item

    async def aclose(self) -> None:
        self._disconnected.set()
        if not self._task.done():
            await asyncio.wait({self._task})


class StreamingASGITransport(httpx.AsyncBaseTransport):
    def __init__(self, app: typing.Callable, client: tuple[str, int] = ("127.0.0.1", 40000)) -> None:
        self.app = app
        self.client = client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request_chunks = request.stream.__aiter__()  # type: ignore[union-attr]
        request_done = False
        disconnected = asyncio.Event()
        started = asyncio.Event()
        queue: asyncio.Queue = asyncio.Queue()
        start: dict[str, typing.Any] = {}
        ended = False

        async def receive() -> dict:
            nonlocal request_done
            if not request_done:
                try:
                    chunk = await request_chunks.__anext__()
                except StopAsyncIteration:
                    request_done = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return {"type": "http.request", "body": chunk, "more_body": True}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            nonlocal ended
            if disconnected.is_set():
                return
            if message["type"] == "http.response.start":
                start.update(message)
                started.set()
            elif message["type"] == "http.response.body" and not ended:
                if message.get("body"):
                    queue.put_nowait(message["body"])
                if not message.get("more_body", False):
                    ended = True
                    queue.put_nowait(_END)

        async def run() -> None:
            try:
                await self.app(scope, receive, send)
            except BaseException as error:  # noqa: BLE001 - handed to the reader
                queue.put_nowait(_Abort(error))
                if not started.is_set():
                    raise
            else:
                if not ended:
                    queue.put_nowait(_END)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "root_path": "",
            "server": (request.url.host, request.url.port or (443 if request.url.scheme == "https" else 80)),
            "client": self.client,
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "extensions": {},
        }
        task = asyncio.create_task(run())
        waiter = asyncio.ensure_future(started.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if not started.is_set():
            waiter.cancel()
            error = task.exception()
            if isinstance(error, FakeDisconnect):
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            if error is not None:
                raise error
            raise httpx.RemoteProtocolError("ASGI app returned without sending a response.")
        return httpx.Response(
            start["status"],
            headers=start.get("headers", []),
            stream=_ResponseStream(queue, task, disconnected),
            request=request,
        )


class HostRouter:
    """Route ``host`` or ``host:port`` names to in-process ASGI apps."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._routes: dict[str, StreamingASGITransport] = {}
        original = httpx.AsyncHTTPTransport.handle_async_request
        router = self

        async def routed(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
            target = router._routes.get(f"{request.url.host}:{request.url.port}") or router._routes.get(request.url.host)
            if target is None:
                return await original(transport, request)
            return await target.handle_async_request(request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", routed)

    def add(self, name: str, app: typing.Callable) -> None:
        if name in self._routes:
            raise ValueError(f"{name!r} is already routed")
        self._routes[name] = StreamingASGITransport(app)
