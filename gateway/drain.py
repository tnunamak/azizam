"""In-flight request tracking and the restart drain flag.

A restart of the gateway kills every response that is still being written.
This module gives operators two things:

* ``InflightMiddleware`` counts requests from the first ASGI message until the
  app has finished sending the response. It is a pure ASGI middleware on
  purpose: a ``BaseHTTPMiddleware`` returns when the response *headers* are
  ready, so it would report a streaming (SSE) reply as finished while tokens
  are still being sent.
* ``DrainState`` holds the draining flag. While it is set, new work-starting
  requests (POST/PUT/PATCH) get ``503`` with ``Retry-After`` and an
  OpenAI-shaped error with code ``gateway_draining``. Reads, health checks,
  the admin plane, job polling and cancel calls keep working.

The state is in memory only. A restart always clears it, and the flag expires
by itself (``ttl_s``) so a crashed operator script cannot leave the gateway
refusing work forever.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

DRAINING_ERROR_CODE = "gateway_draining"

DEFAULT_TTL_S = 1800.0
MAX_TTL_S = 7200.0
DEFAULT_RETRY_AFTER_S = 30

# Paths that are never counted and never rejected. Health and the admin plane
# must work during a drain; the UI shells are static.
EXEMPT_EXACT = frozenset({"/health", "/status", "/v1/status", "/favicon.ico", "/metrics"})
EXEMPT_PREFIXES = ("/admin", "/ui", "/login", "/setup", "/static", "/.well-known")

# Methods that start new work. GET/HEAD/OPTIONS are reads: model lists, job
# polls and downloads of finished results must keep working while we drain.
REJECTED_METHODS = frozenset({"POST", "PUT", "PATCH"})

CHAT_PATHS = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/responses",
    "/v1/messages",
    "/api/chat",
    "/api/generate",
    "/v1/beta",
)
MEDIA_PREFIXES = (
    "/v1/images",
    "/v1/audio",
    "/v1/videos",
    "/gateway/v1/media",
    "/gateway/v1/workflows",
)

MAX_LISTED = 50


def _retry_after_s() -> int:
    raw = os.environ.get("AI_GATEWAY_DRAIN_RETRY_AFTER_S", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RETRY_AFTER_S
    return value if 1 <= value <= 3600 else DEFAULT_RETRY_AFTER_S


def is_exempt(path: str) -> bool:
    return path in EXEMPT_EXACT or any(
        path == prefix or path.startswith(prefix + "/") for prefix in EXEMPT_PREFIXES
    )


def is_cancel_path(path: str) -> bool:
    """Cancels free resources; they must go through during a drain."""
    return path.rstrip("/").endswith("/cancel")


def classify(path: str) -> str:
    if any(path == p or path.startswith(p) for p in CHAT_PATHS):
        return "chat"
    if any(path.startswith(p) for p in MEDIA_PREFIXES):
        return "media"
    return "other"


@dataclass
class _Entry:
    method: str
    path: str
    kind: str
    started: float
    stream: bool = False


class DrainState:
    """Active-request registry plus the draining flag. Thread-safe."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._wall = wall_clock
        self._lock = threading.Lock()
        self._active: dict[int, _Entry] = {}
        self._next_id = 0
        self._drain_until: float | None = None  # monotonic deadline
        self._drain_since_wall: float | None = None
        self._reason = ""
        # name -> callable returning the live count of in-process async jobs.
        self._job_counters: dict[str, Callable[[], int]] = {}

    # -- async media jobs ---------------------------------------------------

    def register_job_counter(self, name: str, counter: Callable[[], int]) -> None:
        self._job_counters[name] = counter

    def _job_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name, counter in self._job_counters.items():
            try:
                counts[name] = max(0, int(counter()))
            except Exception:  # noqa: BLE001 - a broken counter must not break status
                counts[name] = 0
        return counts

    # -- request tracking ---------------------------------------------------

    def begin(self, method: str, path: str) -> int:
        with self._lock:
            self._next_id += 1
            token = self._next_id
            self._active[token] = _Entry(method, path, classify(path), self._clock())
            return token

    def mark_stream(self, token: int) -> None:
        with self._lock:
            entry = self._active.get(token)
            if entry is not None:
                entry.stream = True

    def end(self, token: int) -> None:
        with self._lock:
            self._active.pop(token, None)

    # -- drain flag ---------------------------------------------------------

    def set_draining(
        self, draining: bool, *, ttl_s: float | None = None, reason: str = ""
    ) -> None:
        with self._lock:
            if not draining:
                self._drain_until = None
                self._drain_since_wall = None
                self._reason = ""
                return
            ttl = DEFAULT_TTL_S if ttl_s is None else float(ttl_s)
            ttl = min(max(ttl, 1.0), MAX_TTL_S)
            now = self._clock()
            if self._drain_until is None:
                self._drain_since_wall = self._wall()
            self._drain_until = now + ttl
            self._reason = reason[:200]

    def is_draining(self) -> bool:
        with self._lock:
            return self._is_draining_locked()

    def _is_draining_locked(self) -> bool:
        if self._drain_until is None:
            return False
        if self._clock() >= self._drain_until:
            self._drain_until = None
            self._drain_since_wall = None
            self._reason = ""
            return False
        return True

    # -- reporting ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        jobs = self._job_counts()
        with self._lock:
            now = self._clock()
            draining = self._is_draining_locked()
            entries = sorted(self._active.values(), key=lambda e: e.started)
            listed = [
                {
                    "method": e.method,
                    "path": e.path,
                    "kind": e.kind,
                    "stream": e.stream,
                    "age_s": round(now - e.started, 1),
                }
                for e in entries[:MAX_LISTED]
            ]
            requests = len(entries)
            streams = sum(1 for e in entries if e.stream)
            chat = sum(1 for e in entries if e.kind == "chat")
            media = sum(1 for e in entries if e.kind == "media")
            expires_in = (
                round(self._drain_until - now, 1)
                if draining and self._drain_until is not None
                else None
            )
            since = self._drain_since_wall if draining else None
            reason = self._reason if draining else ""
        async_jobs = sum(jobs.values())
        return {
            "draining": draining,
            "since": since,
            "expires_in_s": expires_in,
            "reason": reason,
            "retry_after_s": _retry_after_s(),
            "active": {
                "requests": requests,
                "streams": streams,
                "chat": chat,
                "media": media,
                "async_media_jobs": jobs,
                "async_media_jobs_total": async_jobs,
                "total": requests + async_jobs,
            },
            "inflight": listed,
            "inflight_truncated": requests > len(listed),
            "idle": requests + async_jobs == 0,
        }


def draining_response_body() -> bytes:
    return json.dumps(
        {
            "error": {
                "message": (
                    "The gateway is draining for a restart and is not accepting "
                    "new work. Retry shortly."
                ),
                "type": "server_error",
                "param": None,
                "code": DRAINING_ERROR_CODE,
            }
        }
    ).encode("utf-8")


Scope = Mapping[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class InflightMiddleware:
    """Pure ASGI middleware: count requests and enforce the drain flag."""

    def __init__(self, app: Callable[..., Awaitable[None]], state: DrainState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if is_exempt(path):
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET").upper()

        if (
            method in REJECTED_METHODS
            and not is_cancel_path(path)
            and self.state.is_draining()
        ):
            await self._reject(send)
            return

        token = self.state.begin(method, path)

        async def tracking_send(message: Message) -> None:
            if message.get("type") == "http.response.start":
                for name, value in message.get("headers") or ():
                    if name.lower() == b"content-type" and value.lower().startswith(
                        b"text/event-stream"
                    ):
                        self.state.mark_stream(token)
                        break
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        finally:
            self.state.end(token)

    async def _reject(self, send: Send) -> None:
        body = draining_response_body()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(_retry_after_s()).encode()),
                    (b"x-gateway-draining", b"1"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


DRAIN_STATE = DrainState()
