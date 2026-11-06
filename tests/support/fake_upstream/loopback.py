"""Loopback mode: serve a fake upstream on 127.0.0.1 for wire-level faults.

The server is a small HTTP/1.1 server on h11 in its own thread and event
loop. Each body part is written and drained as its own chunk with
TCP_NODELAY. ``FakeDisconnect`` aborts the socket instead of sending an error
response, so the client sees the stream end without the HTTP end-of-message.
Real sockets are what prove connect and read timeouts, chunk boundaries on
the wire, and a peer drop mid-stream.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import typing
from urllib.parse import unquote_to_bytes

import h11

from .app import FakeDisconnect


class LoopbackServer:
    def __init__(self, app: typing.Callable) -> None:
        self.app = app
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-upstream-loopback", daemon=True)
        self._connections: set[asyncio.Task] = set()
        self._error: BaseException | None = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "LoopbackServer":
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            raise self._error
        return self

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result()
        self._thread.join()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._server = self._loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
            self.port = self._server.sockets[0].getsockname()[1]
        except BaseException as error:  # noqa: BLE001 - reported to start()
            self._error = error
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    async def _shutdown(self) -> None:
        assert self._server is not None
        self._server.close()
        for task in list(self._connections):
            task.cancel()
        await asyncio.gather(*self._connections, return_exceptions=True)
        await self._server.wait_closed()
        asyncio.get_running_loop().call_soon(asyncio.get_running_loop().stop)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection = h11.Connection(h11.SERVER)
        try:
            while await self._serve_one(connection, reader, writer):
                connection.start_next_cycle()
        except (FakeDisconnect, ConnectionError, h11.ProtocolError):
            writer.transport.abort()
        except asyncio.CancelledError:
            writer.transport.abort()
        else:
            writer.close()
        finally:
            self._connections.discard(task)

    async def _serve_one(self, connection: h11.Connection, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        request = await _next_event(connection, reader)
        if request is None:
            return False
        assert isinstance(request, h11.Request)
        body = bytearray()
        while True:
            event = await _next_event(connection, reader)
            if event is None:
                return False
            if isinstance(event, h11.Data):
                body += event.data
            elif isinstance(event, h11.EndOfMessage):
                break
        target = request.target.decode("latin-1")
        path, _, query = target.partition("?")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method.decode("ascii"),
            "scheme": "http",
            # ASGI: ``path`` is percent-decoded UTF-8, ``raw_path`` keeps the wire bytes.
            "path": unquote_to_bytes(path).decode("utf-8", "replace"),
            "raw_path": path.encode("latin-1"),
            "query_string": query.encode("latin-1"),
            "root_path": "",
            "server": ("127.0.0.1", self.port),
            "client": writer.get_extra_info("peername"),
            "headers": [(k.lower(), v) for k, v in request.headers],
        }
        peer_gone = asyncio.Event()
        delivered = False

        async def receive() -> dict:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            await peer_gone.wait()
            return {"type": "http.disconnect"}

        async def watch_peer() -> None:
            # EOF means the client left. Bytes mean the client already sent
            # its next keep-alive request; h11 buffers them for the next cycle.
            data = await reader.read(65536)
            if data:
                connection.receive_data(data)
            else:
                peer_gone.set()

        watcher = asyncio.create_task(watch_peer())
        async def send(message: dict) -> None:
            if peer_gone.is_set():
                return
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"content-length"]
                headers.append((b"transfer-encoding", b"chunked"))
                writer.write(connection.send(h11.Response(status_code=message["status"], headers=headers, reason=b"")))
            elif message["type"] == "http.response.body":
                if message.get("body"):
                    writer.write(connection.send(h11.Data(data=message["body"])))
                if not message.get("more_body", False):
                    writer.write(connection.send(h11.EndOfMessage()))
            await writer.drain()

        try:
            await self.app(scope, receive, send)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        return connection.our_state is h11.DONE and connection.their_state is h11.DONE


async def _next_event(connection: h11.Connection, reader: asyncio.StreamReader) -> typing.Any:
    while True:
        event = connection.next_event()
        if event is h11.NEED_DATA:
            data = await reader.read(65536)
            connection.receive_data(data)
            if not data and connection.their_state is h11.IDLE:
                return None
            continue
        if isinstance(event, h11.ConnectionClosed):
            return None
        return event
