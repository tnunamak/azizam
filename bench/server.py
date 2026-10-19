"""Child-only gateway/engine entrypoint with a fail-closed Unix socket boundary."""
import argparse
import asyncio
import cProfile
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import time


def install_boundary(state: Path, os_boundary=False):
    def inside(raw):
        return Path(os.fsdecode(raw)).resolve().is_relative_to(state)

    def audit(event, args):
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
            raise RuntimeError("benchmark blocked a host process")
        if event == "socket.getaddrinfo":
            raise RuntimeError("benchmark blocked DNS")
        if event in {"socket.connect", "socket.bind", "socket.sendto"}:
            sock, address = args[:2]
            if sock.family != socket.AF_UNIX or not isinstance(address, (str, bytes)) or not inside(address):
                raise RuntimeError("benchmark blocked a non-fixture socket")
        if os_boundary:
            # Namespaces/read-only mounts enforce file isolation without an
            # extra path resolution on every production database operation.
            return
        if event == "sqlite3.connect" and not inside(args[0]):
            raise RuntimeError("benchmark blocked a non-fixture database")
        if event == "open" and isinstance(args[0], (str, bytes)):
            flags = args[2] or 0
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC) and not inside(args[0]):
                raise RuntimeError("benchmark blocked a non-fixture write")
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename"}:
            paths = args[:2] if event == "os.rename" else args[:1]
            if any(isinstance(p, (str, bytes)) and not inside(p) for p in paths):
                raise RuntimeError(f"benchmark blocked a non-fixture mutation: {paths}")

    sys.addaudithook(audit)


def route_http_over_unix(state: Path):
    """Keep real HTTPX construction/pooling, replace only socket establishment.

    The pinned httpcore backend API is also checked by the smoke test. Logical
    loopback origins all target the fake except the gateway's protocol loopback.
    """
    import httpcore
    import httpx
    from httpcore._backends.auto import AutoBackend

    def path(host, port):
        if host != "127.0.0.1":
            raise RuntimeError("benchmark rejected an unconfigured backend host")
        return str(state / ("gateway.sock" if port == 18991 else "engine.sock"))

    class AsyncUnix(AutoBackend):
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            return await self.connect_unix_socket(path(host, port), timeout, socket_options)

    class SyncUnix(httpcore.SyncBackend):
        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            return self.connect_unix_socket(path(host, port), timeout, socket_options)

    async_init = httpx.AsyncHTTPTransport.__init__
    sync_init = httpx.HTTPTransport.__init__

    def init_async(self, *args, **kwargs):
        async_init(self, *args, **kwargs)
        self._pool._network_backend = AsyncUnix()

    def init_sync(self, *args, **kwargs):
        sync_init(self, *args, **kwargs)
        self._pool._network_backend = SyncUnix()

    httpx.AsyncHTTPTransport.__init__ = init_async
    httpx.HTTPTransport.__init__ = init_sync


def gateway_app(state: Path):
    route_http_over_unix(state)
    import proxy
    import status_endpoint
    from .host_fixtures import ffmpeg_metadata
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from gateway import admin_store, media_jobs, receipt_store
    from gateway.admin_runtime import create_admin_service_account_key
    from gateway.runtime_auth import hash_api_key

    for module in (proxy.metrics_store, proxy.responses_store, receipt_store, media_jobs):
        module.init_db()
    writer = proxy.metrics_sampler.start_writer()
    writer.wait_ready()
    def patch(owner, name, value):
        if not hasattr(owner, name):
            raise RuntimeError(f"benchmark fixture boundary changed: {owner.__name__}.{name}")
        setattr(owner, name, value)
    profile = admin_store.create_profile("Benchmark plan", {"id": "benchmark", "protocols": ["openai"]}, path=proxy.ADMIN_DB_PATH)
    owner = admin_store.ensure_service_account("benchmark-client", path=proxy.ADMIN_DB_PATH)
    app = admin_store.create_app("Benchmark client", kind="service", owner_service_account_id=owner["id"],
                                 default_profile_id=profile["id"], path=proxy.ADMIN_DB_PATH)
    # Fresh fixture-only keys remain in private state and are never in reports.
    app_key = "benchmark-fixture-app-key"
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"],
                                       hash=hash_api_key(app_key), prefix="bench", profile_id=profile["id"], path=proxy.ADMIN_DB_PATH)
    admin_key, _, _ = create_admin_service_account_key("benchmark-owner", path=proxy.ADMIN_DB_PATH)
    (state / "auth.json").write_text(json.dumps({"app": app_key, "admin": admin_key}))
    os.chmod(state / "auth.json", 0o600)

    # Host observations are the other fake backend. Keep the real status build,
    # database reads, fan-out, thread offloads, cache single-flight and renderer.
    # No host subprocess, /proc scan, GPU query or disk census is timed.
    patch(status_endpoint, "_run", lambda *a, **k: (1, "", "fixture: no host commands"))
    patch(status_endpoint, "_ram_attribution", lambda: {"total_bytes": 17179869184, "per_service": []})
    patch(status_endpoint, "_load_average", lambda: [0.0, 0.0, 0.0])
    patch(status_endpoint, "_repo_state", lambda: {"commit": "fixture", "dirty": False})
    patch(status_endpoint, "_HOST_TELEMETRY_DISABLED", True)
    patch(status_endpoint, "_HOTSPOT_DIRS", [("fixture", state / "images")])
    patch(status_endpoint, "_CACHE_TTL_S", 0)  # Every batch exercises a build.
    patch(proxy.node_info, "DISK_CACHE_DIR", state / "object_info")
    patch(proxy.media_delivery, "DEFAULT_WORKDIR", state / "media_metadata")
    patch(proxy.media_metadata, "_run", ffmpeg_metadata)

    cpu_start = 0
    lag = []
    probe = None
    before = None
    request_path = None
    active = False
    # Control is outside production middleware, including request accounting.
    # All benchmark requests still traverse the complete production app.
    bench_app = FastAPI()
    bench_app.state.profiler = None

    def snapshot(path):
        with sqlite3.connect(state / "metrics.db") as conn:
            metrics = conn.execute("SELECT COUNT(*) FROM samples WHERE metric='request' AND json_extract(tags, '$.path')=? AND json_extract(tags, '$.status')=200", (path,)).fetchone()[0]
        with sqlite3.connect(state / "receipts.db") as conn:
            receipts = conn.execute("SELECT COUNT(*) FROM receipts WHERE path=? AND status IN ('succeeded','ok')", (path,)).fetchone()[0]
        with sqlite3.connect(state / "responses.db") as conn:
            responses = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        return {"metrics": metrics, "receipts": receipts, "responses": responses}

    async def probe_loop():
        loop = asyncio.get_running_loop()
        while True:
            due = loop.time() + 0.001
            await asyncio.sleep(0.001)
            lag.append(max(0, loop.time() - due) * 1000)

    @bench_app.post("/__bench/control")
    async def control(request: Request):
        nonlocal cpu_start, probe, lag, before, request_path, active
        body = await request.json()
        if body["action"] == "profile_dump":
            profiler = bench_app.state.profiler
            if profiler is None or active:
                return JSONResponse({"error": "profile unavailable or measurement active"}, status_code=400)
            profiler.disable()
            profiler.dump_stats(state / "gateway.pstats")
            return {"saved": True}
        if body["action"] == "start":
            await asyncio.to_thread(writer.flush)
            request_path = body["path"]
            before = await asyncio.to_thread(snapshot, request_path)
            lag = []
            probe = asyncio.create_task(probe_loop()) if body.get("probe", True) else None
            active = True
            cpu_start = time.process_time_ns()
            return {"started": True}
        if body["action"] != "stop" or not active:
            return JSONResponse({"error": "invalid control transition"}, status_code=400)
        await asyncio.to_thread(writer.flush)
        cpu_ms = (time.process_time_ns() - cpu_start) / 1e6
        if probe:
            probe.cancel()
            try:
                await probe
            except asyncio.CancelledError:
                pass
        probe = None
        active = False
        after = await asyncio.to_thread(snapshot, request_path)
        payload = status_endpoint._CACHE.get("data") or {}
        return {"cpu_ms": cpu_ms, "loop_lag_ms": lag, "work_delta": {k: after[k] - before[k] for k in after},
                "status_errors": payload.get("errors", {}), "status_sections": sorted(payload),
                "status_backends": [b.get("key") for b in payload.get("backends", [])]}

    bench_app.mount("/", proxy.app)
    return bench_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["gateway", "engine"])
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    if os.environ.get("BENCH_CPU"):
        os.sched_setaffinity(0, [int(os.environ["BENCH_CPU"])])
    state = args.state.resolve()
    install_boundary(state, os_boundary=bool(os.environ.get("BENCH_OS_BOUNDARY")))
    import uvicorn
    from .fixtures import create_engine
    app = gateway_app(state) if args.kind == "gateway" else create_engine()
    profiler = cProfile.Profile() if args.kind == "gateway" and os.environ.get("BENCH_PROFILE") else None
    if profiler:
        app.state.profiler = profiler
        profiler.enable()
    try:
        uvicorn.run(app, uds=str(state / f"{args.kind}.sock"), log_level="error", access_log=False,
                    loop="auto", http="auto", lifespan="off", timeout_graceful_shutdown=2)
    finally:
        if profiler:
            profiler.disable()
            profiler.dump_stats(state / "gateway.pstats")


if __name__ == "__main__":
    main()
