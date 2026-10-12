"""The fake upstream ASGI app: serves one scenario and records every call.

The app fails closed. A request that no exchange expects gets HTTP 599 with
the request in the body, and the violation is latched. The 599 alone is not
the oracle, because the gateway may absorb it; the teardown check reads the
latch, the call counts and the call order.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import parse_qsl

from .clock import ClockClosed, VirtualClock
from .scenario import Exchange, Scenario, matches

VIOLATION_STATUS = 599


class FakeDisconnect(Exception):
    """Raised by the app to drop the connection; the server turns it into an abort."""


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    body: bytes
    exchange: str | None

    @property
    def json(self) -> Any:
        return json.loads(self.body)

    def summary(self) -> dict[str, Any]:
        try:
            body: Any = self.json
        except ValueError:
            body = self.body.decode("utf-8", "replace")
        return {"method": self.method, "path": self.path, "query": dict(self.query), "body": body}


@dataclass
class FakeUpstream:
    """One wire-protocol fake serving one scenario. ``base_url`` is set on registration."""

    scenario: Scenario
    clock: VirtualClock
    name: str
    base_url: str = ""
    requests: list[RecordedRequest] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    # Exchange ids whose client closed the connection before the response ended.
    client_closed: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = {exchange.id: 0 for exchange in self.scenario.exchanges}
        self._ordered = [e for e in self.scenario.exchanges if not e.repeat]
        self._background = [e for e in self.scenario.exchanges if e.repeat]
        self._pointer = 0
        self._waived: set[str] = set()

    def calls(self, exchange_id: str) -> list[RecordedRequest]:
        if exchange_id not in self._counts:
            raise KeyError(exchange_id)
        return [request for request in self.requests if request.exchange == exchange_id]

    def waive(self, *exchange_ids: str) -> None:
        """Do not require these exchanges. Only a known-bug path calls this, for the
        calls the bug stops the gateway from making; violations stay latched."""
        for exchange_id in exchange_ids:
            if exchange_id not in self._counts:
                raise KeyError(exchange_id)
        self._waived.update(exchange_ids)

    def problems(self) -> list[str]:
        """Latched violations plus required exchanges not consumed as declared."""
        out = [f"{self.name}: {violation}" for violation in self.violations]
        for exchange in self.scenario.exchanges:
            count = self._counts[exchange.id]
            if exchange.required and exchange.id not in self._waived and count < (1 if exchange.repeat else exchange.times):
                want = "at least once" if exchange.repeat else f"{exchange.times} time(s)"
                out.append(f"{self.name}: required exchange {exchange.id!r} was called {count} time(s), expected {want}")
        return out

    def _select(self, request: RecordedRequest) -> tuple[Exchange | None, str | None]:
        actual = _request_view(request)
        with self._lock:
            index = self._pointer
            while index < len(self._ordered):
                exchange = self._ordered[index]
                if matches(exchange.match, actual):
                    self._counts[exchange.id] += 1
                    self._pointer = index + 1 if self._counts[exchange.id] == exchange.times else index
                    return exchange, None
                if exchange.required and self._counts[exchange.id] < exchange.times:
                    break
                index += 1
            for exchange in self._background:
                if matches(exchange.match, actual):
                    self._counts[exchange.id] += 1
                    return exchange, None
            for exchange in self._ordered:
                if matches(exchange.match, actual):
                    if self._counts[exchange.id] >= exchange.times:
                        return None, f"exchange {exchange.id!r} called more than {exchange.times} time(s)"
                    return None, f"exchange {exchange.id!r} called out of order"
            return None, f"unmatched request {request.method} {request.path}"

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        query = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True))
        probe = RecordedRequest(scope["method"], scope["path"], query, headers, bytes(body), None)
        exchange, violation = self._select(probe)
        recorded = RecordedRequest(probe.method, probe.path, query, headers, probe.body, exchange.id if exchange else None)
        with self._lock:
            self.requests.append(recorded)
            if violation:
                self.violations.append(f"{violation}: {json.dumps(recorded.summary(), sort_keys=True)}")
        if exchange is None:
            payload = json.dumps({"error": {"type": "fake_upstream_violation", "message": violation}, "request": recorded.summary()}).encode()
            await send({"type": "http.response.start", "status": VIOLATION_STATUS, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": payload})
            return
        disconnected = asyncio.Event()

        async def watch() -> None:
            while (await receive())["type"] != "http.disconnect":
                pass
            disconnected.set()

        watcher = asyncio.create_task(watch())
        try:
            await self._respond(exchange.response, send, disconnected)
        except _ClientGone:
            with self._lock:
                self.client_closed.append(exchange.id)
            return
        except ClockClosed:
            return
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def _wait(self, seconds: Any, disconnected: asyncio.Event) -> None:
        duration = float("inf") if seconds == "forever" else float(seconds)
        sleeper = asyncio.ensure_future(self.clock.sleep(duration))
        gone = asyncio.ensure_future(disconnected.wait())
        try:
            await asyncio.wait({sleeper, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, gone):
                task.cancel()
            await asyncio.gather(sleeper, gone, return_exceptions=True)
        if disconnected.is_set():
            raise _ClientGone()
        if not sleeper.cancelled() and sleeper.exception() is not None:
            raise sleeper.exception()

    async def _respond(self, response: Mapping[str, Any], send, disconnected: asyncio.Event) -> None:
        protocol = self.scenario.protocol
        if "wait_before_headers" in response:
            await self._wait(response["wait_before_headers"], disconnected)
        if response.get("disconnect_before_headers"):
            raise FakeDisconnect()
        headers = {k.lower(): str(v) for k, v in (response.get("headers") or {}).items()}
        parts = list(response.get("body", ()))
        if "json" in response:
            headers.setdefault("content-type", "application/json")
            parts = [{"text": json.dumps(response["json"], ensure_ascii=False)}]
        elif any("event" in part or "done" in part for part in parts):
            headers.setdefault("content-type", protocol.stream_media_type)
        await send({
            "type": "http.response.start",
            "status": int(response.get("status", 200)),
            "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        })
        for part in parts:
            if "wait" in part:
                await self._wait(part["wait"], disconnected)
                continue
            if "disconnect" in part:
                raise FakeDisconnect()
            if "text" in part:
                chunk = part["text"].encode("utf-8")
            elif "hex" in part:
                chunk = bytes.fromhex(part["hex"])
            elif "event" in part:
                chunk = protocol.frame(part["event"])
            else:
                chunk = protocol.stream_end
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


class _ClientGone(Exception):
    pass


def _request_view(request: RecordedRequest) -> dict[str, Any]:
    view: dict[str, Any] = {
        "method": request.method,
        "path": request.path,
        "query": dict(request.query),
        "headers": dict(request.headers),
    }
    try:
        view["json"] = json.loads(request.body) if request.body else None
    except ValueError:
        view["json"] = None
    return view
