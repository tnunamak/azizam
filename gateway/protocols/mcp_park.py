"""Private loopback MCP transport for tools owned by a plan-route caller.

Use the module's async register/close for the process-wide server. A ParkRun's
synchronous methods belong to the same event loop as its server.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from typing import Any

import h11

from gateway import transport_budgets

MAX_BODY_BYTES = transport_budgets.section("mcp_park")["max_body_bytes"]   # registry/transport_budgets.yml
_PREFIX = "mcp__caller__"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _result(content: Any, is_error: bool = False) -> dict:
    parts = []
    for part in content if isinstance(content, list) else [content]:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append({"type": "text", "text": str(part.get("text", ""))})
        elif isinstance(part, dict) and part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url", "") if isinstance(image, dict) else ""
            match = re.fullmatch(r"data:([^;,]+);base64,([A-Za-z0-9+/=\s]*)", url) if isinstance(url, str) else None
            if match:
                parts.append({"type": "image", "data": match[2], "mimeType": match[1]})
            else:
                parts.append({"type": "text", "text": _canonical(part)})
        else:
            parts.append({"type": "text", "text": part if isinstance(part, str) else _canonical(part)})
    return {"content": parts, "isError": is_error}


class ParkRun:
    """One binary run; first result wins, including results received before calls."""

    def __init__(self, server: ParkServer, tools: list[dict]):
        self._server = server
        self.id = secrets.token_urlsafe(24)
        self._bearer = secrets.token_urlsafe(32)
        self.calls: asyncio.Queue[tuple[str, str, Any]] = asyncio.Queue()
        self._waiting: dict[str, asyncio.Future] = {}
        self._early: dict[str, dict] = {}
        self._finished: set[str] = set()
        self._expected: dict[str, tuple[str, str]] = {}
        self._failure: str | None = None
        self._names: dict[str, str] = {}
        functions = [t["function"] for t in tools if t.get("type") == "function"]
        # Reserve unchanged names first, so a caller name cannot collide with an alias.
        reserved = {f["name"] for f in functions if re.fullmatch(r"[A-Za-z0-9_-]{1,51}", f["name"])}
        aliases = {}
        for name in sorted({f["name"] for f in functions}):
            alias = name
            if name not in reserved:
                prefix = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:16] or "tool"
                salt = 0
                while True:
                    digest = hashlib.sha256(f"{name}:{salt}".encode()).hexdigest()[:32]
                    alias = f"{prefix}_{digest}"
                    if alias not in reserved and alias not in self._names:
                        break
                    salt += 1
            aliases[name] = alias
            self._names[alias] = name
        self.tools = [{"name": aliases[f["name"]], "description": f.get("description", ""),
                       "inputSchema": f.get("parameters") or {"type": "object"},
                       "annotations": {"readOnlyHint": True},
                       "_meta": {"anthropic/maxResultSizeChars": 2000000, "anthropic/alwaysLoad": True}}
                      for f in functions]

    def mcp_config(self, timeout_ms: int) -> dict:
        return {"mcpServers": {"caller": {"type": "http", "url": f"{self._server.url}/mcp/{self.id}",
                                          "headers": {"Authorization": f"Bearer {self._bearer}"},
                                          "timeout": timeout_ms}}}

    def to_caller_name(self, mcp_name: str) -> str:
        """Translate the binary stream's names (including mcp__caller__)."""
        name = mcp_name.removeprefix(_PREFIX)
        return self._names[name]

    @property
    def pending_count(self) -> int:
        return len(self._waiting)

    def expect(self, tool_use_id: str, caller_name: str, arguments: Any) -> None:
        if self._failure is None and tool_use_id not in self._finished and tool_use_id not in self._waiting:
            self._expected.setdefault(tool_use_id, (caller_name, _canonical(arguments)))

    def resolve(self, tool_use_id: str, content: Any, is_error: bool = False) -> bool:
        if self._failure is not None or tool_use_id in self._finished or tool_use_id in self._early:
            return False
        result = _result(content, is_error)
        future = self._waiting.get(tool_use_id)
        if future is not None:
            if future.done():
                return False
            future.set_result(result)
        else:
            self._early[tool_use_id] = result
        return True

    def fail_all(self, message: str) -> None:
        self._failure = message
        self._early.clear()
        self._expected.clear()
        for future in self._waiting.values():
            if not future.done():
                future.set_result(_result(message, True))

    def close(self) -> None:
        self._server._runs.pop(self.id, None)
        self.fail_all("Tool run closed")
        self._finished.clear()
        # Retain waiting futures until their handlers' finally blocks run.

    async def _call(self, params: dict) -> dict:
        if self._failure is not None:
            return _result(self._failure, True)
        try:
            # MCP requests carry the bare alias; only the binary stream adds a prefix.
            name = self._names[params["name"]]
        except (KeyError, TypeError, AttributeError):
            return _result("Unknown tool", True)
        arguments = params.get("arguments", {})
        meta = params.get("_meta") or {}
        call_id = meta.get("claudecode/toolUseId") if isinstance(meta, dict) else None
        if not call_id:
            match = (name, _canonical(arguments))
            call_id = next((key for key, value in self._expected.items() if value == match), None)
        if not isinstance(call_id, str) or not call_id:
            return _result("No matching tool-use expectation", True)
        if call_id in self._waiting or call_id in self._finished:
            return _result("Duplicate tool-use id", True)
        self._expected.pop(call_id, None)
        future = asyncio.get_running_loop().create_future()
        self._waiting[call_id] = future
        if call_id in self._early:
            future.set_result(self._early.pop(call_id))
        self.calls.put_nowait((call_id, name, arguments))
        try:
            return await future
        finally:
            self._waiting.pop(call_id, None)
            self._finished.add(call_id)


class ParkServer:
    """An asyncio HTTP/1.1 listener, separate from the public application."""

    def __init__(self):
        self._listener: asyncio.Server | None = None
        self._start_lock = asyncio.Lock()
        self._runs: dict[str, ParkRun] = {}
        self._connections: dict[asyncio.Task, asyncio.StreamWriter] = {}
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closing = False
        self.url = ""

    async def register(self, tools: list[dict]) -> ParkRun:
        async with self._start_lock:
            if self._closing:
                raise RuntimeError("MCP server closed")
            if self._listener is None:
                self._listener = await asyncio.start_server(self._connection, "127.0.0.1", 0)
                port = self._listener.sockets[0].getsockname()[1]
                self.url = f"http://127.0.0.1:{port}"
        run = ParkRun(self, tools)
        self._runs[run.id] = run
        return run

    async def wait_idle(self) -> None:
        """Wait until request handlers have sent their responses or disconnected."""
        await self._idle.wait()

    async def close(self) -> None:
        async with self._start_lock:
            self._closing = True
            if self._listener is not None:
                self._listener.close()
        for run in list(self._runs.values()):
            run.close()
        await self.wait_idle()
        for writer in list(self._connections.values()):
            writer.close()
        await asyncio.gather(*list(self._connections), return_exceptions=True)
        if self._listener is not None:
            await self._listener.wait_closed()

    async def _dispatch(self, request: h11.Request, body: bytes) -> tuple[int, Any]:
        path = request.target.decode("ascii", errors="replace")
        if request.method != b"POST" or not re.fullmatch(r"/mcp/[A-Za-z0-9_-]+", path):
            return 404, {}
        run = self._runs.get(path[5:])
        if run is None:
            return 404, {}
        authorization = [value for key, value in request.headers if key == b"authorization"]
        expected = f"Bearer {run._bearer}".encode()
        if len(authorization) != 1 or not hmac.compare_digest(authorization[0], expected):
            return 401, {}
        try:
            message = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return 400, {}
        if not isinstance(message, dict):
            return 400, {}
        method, rid = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            return 400, {}
        if method.startswith("notifications/"):
            return 202, None
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                      "serverInfo": {"name": "caller", "version": "1"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": run.tools}
        elif method == "tools/call":
            result = await run._call(params)
        else:
            return 200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
        return 200, {"jsonrpc": "2.0", "id": rid, "result": result}

    async def _exchange(self, conn, reader, writer, request, body):
        # Read concurrently while parked: EOF must cancel the waiting call. Bytes
        # pipelined by a client remain in h11 until this response ends its cycle.
        async def watch_disconnect():
            while data := await reader.read(65536):
                conn.receive_data(data)
                if len(conn.trailing_data[0]) > MAX_BODY_BYTES:
                    return

        dispatch = asyncio.create_task(self._dispatch(request, body))
        peer = asyncio.create_task(watch_disconnect())
        self._active += 1
        self._idle.clear()
        try:
            done, _ = await asyncio.wait((dispatch, peer), return_when=asyncio.FIRST_COMPLETED)
            if peer in done:
                peer.result()
                return False
            # Stop reading before sending the response, so no bytes from the next
            # request can be consumed by a task we are about to discard.
            peer.cancel()
            await asyncio.gather(peer, return_exceptions=True)
            status, payload = dispatch.result()
            await self._send(conn, writer, status, payload)
            return True
        finally:
            dispatch.cancel()
            peer.cancel()
            await asyncio.gather(dispatch, peer, return_exceptions=True)
            self._active -= 1
            if not self._active:
                self._idle.set()

    @staticmethod
    async def _send(conn, writer, status, payload):
        body = b"" if payload is None else json.dumps(payload).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        writer.write(conn.send(h11.Response(status_code=status, headers=headers)))
        writer.write(conn.send(h11.Data(data=body)))
        writer.write(conn.send(h11.EndOfMessage()))
        await writer.drain()

    async def _connection(self, reader, writer):
        task = asyncio.current_task()
        self._connections[task] = writer
        conn = h11.Connection(h11.SERVER)
        request = None
        body = bytearray()
        oversized = False
        try:
            while not self._closing:
                event = conn.next_event()
                if event is h11.NEED_DATA:
                    data = await reader.read(65536)
                    if not data:
                        break
                    conn.receive_data(data)
                elif isinstance(event, h11.Request):
                    request = event
                    length = dict(event.headers).get(b"content-length", b"0")
                    oversized = int(length) > MAX_BODY_BYTES
                    if oversized:
                        await self._send(conn, writer, 413, {})
                elif isinstance(event, h11.Data):
                    if not oversized:
                        body.extend(event.data)
                        if len(body) > MAX_BODY_BYTES:
                            oversized = True
                            body.clear()
                            await self._send(conn, writer, 413, {})
                elif isinstance(event, h11.EndOfMessage):
                    # Drain rejected bodies without storing them: closing with unread
                    # bytes can reset TCP and hide the 413 from an uploading client.
                    if not oversized and not await self._exchange(conn, reader, writer, request, bytes(body)):
                        break
                    if conn.our_state is h11.MUST_CLOSE or conn.their_state is h11.MUST_CLOSE:
                        break
                    conn.start_next_cycle()
                    request, body, oversized = None, bytearray(), False
                elif isinstance(event, h11.ConnectionClosed):
                    break
                else:
                    break
        except (ConnectionError, h11.ProtocolError, ValueError):
            # Malformed HTTP and client disconnects carry no application data to logs.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self._connections.pop(task, None)


_server: ParkServer | None = None


async def register(tools: list[dict]) -> ParkRun:
    """Register with the lazily started process-wide loopback listener."""
    global _server
    if _server is None:
        _server = ParkServer()
    return await _server.register(tools)


async def close() -> None:
    """Close the process-wide listener and release every parked handler."""
    global _server
    if _server is not None:
        await _server.close()
        _server = None
