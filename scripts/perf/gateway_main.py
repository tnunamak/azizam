"""Instrumented gateway entry for the real-data performance harness.

Runs `proxy:app` under uvicorn and stamps every response with the work the
request caused: SQLite statements (`x-perf-sql`), SQLite connections
(`x-perf-connects`), spawned processes (`x-perf-subproc`), server time
(`x-perf-ms`) and the process's CPU time (`x-perf-cpu-ms`). Counters are process-wide deltas, so the caller sends requests
one at a time and takes the minimum over repeats to drop background noise.
It also writes the GET route table to PERF_ROUTES_FILE for the bench.

Harness code only. Nothing here runs in production.
"""
from __future__ import annotations

import collections
import json
import os
import re
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
    depth = 9 if kind == "proc" else 3  # a process is rarer and its caller further up
    chain = " < ".join(f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in reversed(frames[-depth:]))
    with _LOCK:
        WHERE[f"{kind} {detail[:70]!r} @ {chain}"] += 1


PLANS = os.environ.get("PERF_PLANS") == "1"
_STATEMENTS: dict[tuple[str, str], str] = {}  # (database, statement with literals removed) -> one real example
_LITERALS = re.compile(r"'(?:[^']|'')*'|\b\d+(?:\.\d+)?\b")

PROFILE = os.environ.get("PERF_PROFILE") == "1"
_INCLUSIVE: collections.Counter = collections.Counter()
_LEAF: collections.Counter = collections.Counter()
_SAMPLES = [0]


def _sampler() -> None:
    """Every 4 ms, charge each busy repo frame on every other thread (inclusive) and its leaf."""
    me = threading.get_ident()
    while True:
        time.sleep(0.004)
        for ident, frame in sys._current_frames().items():
            if ident == me:
                continue
            chain, leaf = [], None
            while frame is not None:
                name = frame.f_code.co_filename
                if "/site-packages/" not in name and "/lib/python" not in name and "/scripts/perf/" not in name:
                    chain.append(f"{os.path.basename(name)}:{frame.f_code.co_name}")
                elif leaf is None:
                    leaf = f"{os.path.basename(name)}:{frame.f_code.co_name}"  # the library call at the top
                frame = frame.f_back
            if not chain or chain[0] in ("selectors.py:select", "threading.py:wait", "queue.py:get"):
                continue
            with _LOCK:
                _SAMPLES[0] += 1
                _LEAF[f"{chain[0]}{' -> ' + leaf if leaf else ''}"] += 1
                for entry in set(chain):
                    _INCLUSIVE[entry] += 1


def install_counters() -> None:
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        _bump("connects")
        connection = real_connect(*args, **kwargs)
        database = str(args[0]) if args else ""
        try:
            def trace(statement):
                _bump("sql")
                if ATTRIBUTE:
                    _where("sql", " ".join(str(statement).split()))
                if PLANS and len(_STATEMENTS) < 4000:
                    text = " ".join(str(statement).split())
                    _STATEMENTS.setdefault((database, _LITERALS.sub("?", text)), text)

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
        if PLANS and scope["path"] == "/__perf/statements":
            with _LOCK:
                body = json.dumps([[db, example] for (db, _key), example in _STATEMENTS.items()]).encode()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        if PROFILE and scope["path"] == "/__perf/prof":
            with _LOCK:
                body = json.dumps({"samples": _SAMPLES[0], "inclusive": _INCLUSIVE.most_common(70),
                                   "leaf": _LEAF.most_common(40)}).encode()
                if scope.get("query_string") == b"reset=1":
                    _INCLUSIVE.clear(), _LEAF.clear()
                    _SAMPLES[0] = 0
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
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
        cpu_before = time.process_time()

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
                    # Process CPU, all threads: unlike wall time it does not grow with host load.
                    (b"x-perf-cpu-ms", f"{(time.process_time() - cpu_before) * 1000:.1f}".encode()),
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
    if PROFILE:
        threading.Thread(target=_sampler, daemon=True, name="perf-sampler").start()
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
