"""No request handler may block the event loop on slow storage.

Every SQLite connection sleeps 100 ms, as a disk under pressure would. A
handler that opens one on the loop thread stalls every other request for that
long; the same work on a worker thread leaves the loop ticking. The probe
fails any endpoint whose worst loop stall exceeds STALL_LIMIT_S.
"""
import asyncio
import builtins
import os
import sqlite3
import threading
import traceback
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import proxy
from gateway import admin_store, receipt_store
from gateway.runtime_auth import hash_api_key
from gateway.admin_oidc import CookieConfig, encode_session_cookie, session_from_claims
from tests.test_proxy_admin_integration import _test_backend_grid, _test_status_projection

DB_DELAY_S = 0.1
STALL_LIMIT_S = 0.08
ENDPOINTS = (
    ("/me/api/home", "cookie"),
    ("/me/api/status", "cookie"),
    ("/me/api/models/catalog", "cookie"),
    ("/admin/api/status", "cookie"),
    ("/admin/api/session", "cookie"),
    ("/v1/models", "bearer"),
    ("/v1/status", "cookie"),
)


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", db)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy.status_endpoint, "get_cached_status", _test_status_projection)
    monkeypatch.setattr(proxy.status_endpoint, "_backend_grid", _test_backend_grid)
    monkeypatch.setattr(proxy, "currently_loaded_kobold_model", lambda *a, **k: None)
    receipts = tmp_path / "receipts.db"
    receipt_store.init_db(receipts)
    monkeypatch.setattr(receipt_store, "DB_PATH", receipts)
    cookie = CookieConfig(secure=False)
    key = b"s" * 32
    monkeypatch.setattr(proxy, "ADMIN_OIDC_CONFIG", replace(proxy.ADMIN_OIDC_CONFIG, enabled=False, signing_key=key, cookie=cookie))
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", key)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "cookie", cookie)
    user = admin_store.upsert_oidc_user("https://idp.example", "owner", email="owner@example.com", path=db)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=user["id"], granted_by="test", path=db)
    app = admin_store.ensure_personal_app_for_user(user["id"], path=db)
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=hash_api_key("guard-key"),
                                       prefix="masked", path=db)
    session = session_from_claims({"sub": user["id"], "email": "owner@example.com"}, role="owner", cookie=cookie)
    return {"cookies": {cookie.name: encode_session_cookie(session, signing_key=key)},
            "bearer": {"Authorization": "Bearer guard-key"}}


def slow_connect(real, loop_calls):
    loop_thread = threading.get_ident()

    def connect(*args, **kwargs):
        if threading.get_ident() == loop_thread:
            frames = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in traceback.extract_stack()[:-1]
                      if "/site-packages/" not in f.filename and "/lib/python" not in f.filename]
            loop_calls.append(" <- ".join(reversed(frames[-9:])))
        time.sleep(DB_DELAY_S)
        return real(*args, **kwargs)
    return connect


async def worst_loop_stall(path: str, headers, cookies) -> tuple[float, int, float]:
    worst, stop = 0.0, False

    async def ticker():
        nonlocal worst
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            worst, last = max(worst, now - last), now

    transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://t", cookies=cookies) as client:
        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        response = await client.get(path, headers=headers)
        elapsed = time.perf_counter() - started
        stop = True
        await task
    return worst, response.status_code, elapsed


@pytest.mark.parametrize("path,auth", ENDPOINTS)
def test_handler_does_not_block_the_event_loop(gateway, path, auth):
    headers = gateway["bearer"] if auth == "bearer" else {}
    cookies = gateway["cookies"] if auth == "cookie" else {}
    loop_calls: list[str] = []
    with patch.object(sqlite3, "connect", slow_connect(sqlite3.connect, loop_calls)):
        worst, status, elapsed = asyncio.run(worst_loop_stall(path, headers, cookies))
    assert status == 200, f"{path} answered {status}; the probe needs a working endpoint"
    assert worst < STALL_LIMIT_S, (
        f"{path} blocked the event loop for {worst:.3f}s (limit {STALL_LIMIT_S}s) opening SQLite on the loop at:\n"
        + "\n".join(sorted(set(loop_calls))))


def _get_routes() -> list[str]:
    paths = []
    for route in proxy.app.routes:
        path = getattr(route, "path", "")
        if "GET" in (getattr(route, "methods", None) or ()) and path.startswith(("/me/api/", "/admin/api/", "/v1/")):
            paths.append(path)
    return sorted(set(paths))


def _concrete(path: str) -> str:
    out = []
    for part in path.split("/"):
        out.append("x" if part.startswith("{") and "path" not in part else part)
    return "/".join(out).replace("{rest:path}", "x").replace("{model_id:path}", "x")


# Content and download routes stream bytes from disk by design.
_STREAMING = ("/content", "/download", "/sample")
ROUTES = [route for route in _get_routes() if not route.endswith(_STREAMING)]


def test_route_table_is_covered():
    assert len(ROUTES) > 60, "the route table walk found too few GET routes"


def _loop_thread_work(loop_calls: list[str]):
    """Patch SQLite, subprocess and sleeps so a call on the loop thread is recorded."""
    import subprocess
    loop_thread = threading.get_ident()

    def note(kind: str) -> None:
        if threading.get_ident() == loop_thread:
            frames = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in traceback.extract_stack()[:-2]
                      if "/site-packages/" not in f.filename and "/lib/python" not in f.filename]
            loop_calls.append(f"{kind} " + " <- ".join(reversed(frames[-7:])))

    real_connect, real_popen, real_sleep = sqlite3.connect, subprocess.Popen, time.sleep
    real_open = builtins.open

    def traced_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and "/site-packages/" not in str(file) and not str(file).startswith("/proc"):
            note(f"open({Path(str(file)).name})")
        return real_open(file, *args, **kwargs)

    def connect(*args, **kwargs):
        note("sqlite3.connect")
        return real_connect(*args, **kwargs)

    class Popen(real_popen):
        def __init__(self, *args, **kwargs):
            note("subprocess.Popen")
            super().__init__(*args, **kwargs)

    def sleep(seconds):
        if seconds > 0.01:
            note("time.sleep")
        return real_sleep(seconds)

    return (patch.object(sqlite3, "connect", connect), patch.object(subprocess, "Popen", Popen),
            patch.object(time, "sleep", sleep), patch.object(builtins, "open", traced_open))


@pytest.mark.parametrize("route", ROUTES)
def test_every_get_route_leaves_the_loop_free(gateway, route):
    loop_calls: list[str] = []

    async def call() -> int:
        transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", cookies=gateway["cookies"],
                                     timeout=20) as client:
            return (await client.get(_concrete(route), headers=gateway["bearer"])).status_code

    patches = _loop_thread_work(loop_calls)
    with patches[0], patches[1], patches[2], patches[3]:
        asyncio.run(call())
    assert not loop_calls, f"{route} ran blocking work on the event loop:\n" + "\n".join(sorted(set(loop_calls)))


def test_startup_initializes_the_stores_in_parallel_off_the_loop():
    slow = 0.2

    def sleeper(*args, **kwargs):
        time.sleep(slow)

    async def run() -> tuple[float, float]:
        worst, stop = 0.0, False

        async def ticker():
            nonlocal worst
            last = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                worst, last = max(worst, now - last), now

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        await proxy._initialize_stores()
        elapsed = time.perf_counter() - started
        stop = True
        await task
        return worst, elapsed

    from gateway import providers
    with patch.object(proxy.ADMIN_STORE, "init_db", sleeper), patch.object(proxy.responses_store, "init_db", sleeper), \
            patch.object(proxy.receipt_store, "init_db", sleeper), patch.object(proxy.media_jobs, "init_db", sleeper), \
            patch.object(providers, "recover_provider_spend", sleeper):
        worst, elapsed = asyncio.run(run())
    assert worst < STALL_LIMIT_S, f"startup stalled the event loop for {worst:.3f}s"
    assert elapsed < slow * 3, f"the stores initialized one after another ({elapsed:.2f}s)"
