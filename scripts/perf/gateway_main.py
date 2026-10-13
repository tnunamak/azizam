"""Instrumented gateway entry for the real-data performance harness.

Runs `proxy:app` under uvicorn and stamps every response with the work the
request caused: SQLite statements (`x-perf-sql`), SQLite connections
(`x-perf-connects`), spawned processes (`x-perf-subproc`) and server time
(`x-perf-ms`). Counters are process-wide deltas, so the caller sends requests
one at a time and takes the minimum over repeats to drop background noise.
It also writes the GET route table to PERF_ROUTES_FILE for the bench.

Harness code only. Nothing here runs in production.
"""
from __future__ import annotations

import collections
import json
import os
import sqlite3
import sys
import threading
import time
import traceback

_LOCK = threading.Lock()
COUNTS = {"sql": 0, "connects": 0, "subproc": 0}
_SPAWN_EVENTS = {"subprocess.Popen", "os.posix_spawn", "os.system", "os.exec", "os.fork"}


def _bump(key: str) -> None:
    with _LOCK:
        COUNTS[key] += 1


ATTRIBUTE = os.environ.get("PERF_ATTRIBUTE") == "1"
WHERE: collections.Counter = collections.Counter()


def _where(kind: str, detail: str) -> None:
    """Charge one statement or process to the innermost repo frames that caused it."""
    frames = [f for f in traceback.extract_stack()[:-2]
              if ("/gateway/" in f.filename or f.filename.endswith(("proxy.py", "status_endpoint.py")))
              and "/scripts/perf/" not in f.filename]
    chain = " < ".join(f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in reversed(frames[-3:]))
    with _LOCK:
        WHERE[f"{kind} {detail[:70]!r} @ {chain}"] += 1


def install_counters() -> None:
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        _bump("connects")
        connection = real_connect(*args, **kwargs)
        try:
            def trace(statement):
                _bump("sql")
                if ATTRIBUTE:
                    _where("sql", " ".join(str(statement).split()))

            connection.set_trace_callback(trace)
        except sqlite3.Error:
            pass
        return connection

    sqlite3.connect = connect  # type: ignore[assignment]

    def hook(event, args):
        if event in _SPAWN_EVENTS:
            _bump("subproc")
            if ATTRIBUTE:
                _where("proc", str(args[0] if args else event))

    sys.addaudithook(hook)


class PerfStamp:
    """Pure ASGI middleware: adds the counter deltas to the response headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if ATTRIBUTE and scope["path"] == "/__perf/where":
            with _LOCK:
                body = json.dumps(WHERE.most_common(60)).encode()
                if scope.get("query_string") == b"reset=1":
                    WHERE.clear()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        with _LOCK:
            before = dict(COUNTS)
        started = time.perf_counter()

        async def stamped(message):
            if message["type"] == "http.response.start":
                with _LOCK:
                    now = dict(COUNTS)
                headers = list(message.get("headers") or [])
                headers += [
                    (b"x-perf-sql", str(now["sql"] - before["sql"]).encode()),
                    (b"x-perf-connects", str(now["connects"] - before["connects"]).encode()),
                    (b"x-perf-subproc", str(now["subproc"] - before["subproc"]).encode()),
                    (b"x-perf-ms", f"{(time.perf_counter() - started) * 1000:.1f}".encode()),
                ]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, stamped)


def dump_routes(app, path: str) -> None:
    from fastapi.routing import APIRoute

    rows = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        if "GET" in methods and isinstance(route, APIRoute):
            rows.append(route.path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(sorted(set(rows)), handle)


def main() -> None:
    port = int(sys.argv[1])
    install_counters()
    import proxy
    import uvicorn

    proxy.app.add_middleware(PerfStamp)
    routes_file = os.environ.get("PERF_ROUTES_FILE")
    if routes_file:
        dump_routes(proxy.app, routes_file)
    uvicorn.run(proxy.app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio",
                timeout_keep_alive=3600)


if __name__ == "__main__":
    main()
