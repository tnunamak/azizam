"""Reuse one SQLite connection per store for the length of a read request.

A hot read endpoint resolves identity, registry, policy and capabilities, and
each step used to open its own connection. Inside ``request_scope`` the first
``scoped`` call opens a connection and later calls in the same request reuse
it; leaving the scope closes them all. Outside a scope ``scoped`` opens and
closes one connection per call, as before.

A connection is shared by key (store kind and path). Its lock is re-entrant, so
a nested call on the same thread works, and a call on another thread waits for
the first. If the wait runs out, the caller gets a private connection instead
of blocking the request.
"""
from __future__ import annotations

import contextvars
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator

LOCK_WAIT_S = 2.0
_SCOPE: ContextVar["_Scope | None"] = ContextVar("azizam_request_connections", default=None)


class _Scope:
    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.entries: dict[tuple[str, str], tuple[sqlite3.Connection, threading.RLock]] = {}
        self.closed = False

    def close(self) -> None:
        with self.guard:
            self.closed = True
            entries, self.entries = list(self.entries.values()), {}
        for conn, _lock in entries:
            try:
                conn.close()
            except sqlite3.Error:
                pass


@contextmanager
def request_scope() -> Iterator[None]:
    scope = _Scope()
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)
        scope.close()


def active() -> bool:
    return _SCOPE.get() is not None


@contextmanager
def scoped(kind: str, path: object, opener: Callable[[], sqlite3.Connection]) -> Iterator[sqlite3.Connection]:
    scope = _SCOPE.get()
    if scope is None:
        conn = opener()
        try:
            yield conn
        finally:
            conn.close()
        return
    key = (kind, str(path))
    with scope.guard:
        entry = None if scope.closed else scope.entries.get(key)
        if entry is None and not scope.closed:
            entry = (opener(), threading.RLock())
            scope.entries[key] = entry
    if entry is None or not entry[1].acquire(timeout=LOCK_WAIT_S):
        conn = opener()
        try:
            yield conn
        finally:
            conn.close()
        return
    try:
        yield entry[0]
    finally:
        entry[1].release()


class RequestConnectionsMiddleware:
    """Open a ``request_scope`` around read requests to the hot API prefixes."""

    PREFIXES = ("/me/api/", "/admin/api/", "/v1/")
    BULK_SUFFIXES = ("/content", "/download", "/sample", "/stream")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (scope["type"] != "http" or scope.get("method") != "GET"
                or not path.startswith(self.PREFIXES) or path.endswith(self.BULK_SUFFIXES)):
            return await self.app(scope, receive, send)
        with request_scope():
            await self.app(scope, receive, send)


_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="request-io")


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
    """Run ``fn`` on a helper thread inside the caller's request scope.

    Use it to overlap two independent store reads in one request: each store
    has its own shared connection, so their opens no longer add up.
    """
    return _EXECUTOR.submit(contextvars.copy_context().run, lambda: fn(*args, **kwargs))
