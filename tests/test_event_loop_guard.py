"""No request handler may block the event loop on slow storage.

A handler that opens SQLite, spawns a process, sleeps or opens a file on the
loop thread stalls every other request for as long as that work takes on a
slow disk. The probes record such calls by thread and assert there are none;
no wall-clock time is part of the oracle, so host load cannot flip them.
"""
import asyncio
import builtins
import contextlib
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
from gateway import accounts, admin_store, receipt_store
from gateway.runtime_auth import hash_api_key
from gateway.admin_oidc import CookieConfig, encode_session_cookie, session_from_claims
from tests.test_proxy_admin_integration import _test_backend_grid, _test_status_projection

ENDPOINTS = (
    ("/me/api/home", "cookie"),
    ("/me/api/status", "cookie"),
    ("/me/api/models/catalog", "cookie"),
    ("/admin/api/status", "cookie"),
    ("/admin/api/session", "cookie"),
    ("/v1/models", "bearer"),
    ("/v1/status", "cookie"),
)


def _go_cold(tmp_path, monkeypatch) -> None:
    """Start a test with no process-wide cache that an earlier test warmed.

    The category gate is rebuilt over a fresh store, so its first check opens
    SQLite. Every module-level memo (a ``*CACHE*``/``*MEMO*`` dict or an
    ``lru_cache``) in the gateway code is emptied. Statement tracing covers
    connections that stay open; this covers results that stay cached.
    """
    import functools
    import sys
    from gateway import category_enforcement
    from gateway.category_store import CategoryStore
    store = tmp_path / "categories.db"
    CategoryStore(store)
    monkeypatch.setattr(category_enforcement, "_GATE", category_enforcement.CategoryGate(store))
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "proxy" or name.startswith("gateway")):
            continue
        for attr, value in list(vars(module).items()):
            if isinstance(value, functools._lru_cache_wrapper):
                value.cache_clear()
            elif isinstance(value, dict) and ("CACHE" in attr.upper() or "MEMO" in attr.upper()):
                monkeypatch.setattr(module, attr, type(value)())


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", db)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    monkeypatch.setattr(proxy.status_endpoint, "get_cached_status", _test_status_projection)
    monkeypatch.setattr(proxy.status_endpoint, "_backend_grid", _test_backend_grid)
    monkeypatch.setattr(proxy, "currently_loaded_model", lambda *a, **k: None)
    receipts = tmp_path / "receipts.db"
    receipt_store.init_db(receipts)
    monkeypatch.setattr(receipt_store, "DB_PATH", receipts)
    responses = tmp_path / "responses.db"
    proxy.responses_store.init_db(responses)
    monkeypatch.setattr(proxy.responses_store, "DB_PATH", responses)
    _go_cold(tmp_path, monkeypatch)
    cookie = CookieConfig(secure=False)
    key = b"s" * 32
    monkeypatch.setattr(proxy, "ADMIN_OIDC_CONFIG", replace(proxy.ADMIN_OIDC_CONFIG, enabled=False, signing_key=key, cookie=cookie))
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", key)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "cookie", cookie)
    user = admin_store.upsert_oidc_user("https://idp.example", "owner", email="owner@example.com", path=db)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=user["id"], granted_by="test", path=db)
    accounts.initialize(db)
    admin_store.seed_first_owner_plan(user["id"], path=db)
    app = admin_store.ensure_personal_app_for_user(user["id"], path=db)
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=hash_api_key("guard-key"),
                                       prefix="masked", path=db)
    session = session_from_claims({"sub": user["id"], "email": "owner@example.com"}, role="owner", cookie=cookie)
    return {"cookies": {cookie.name: encode_session_cookie(session, signing_key=key)},
            "bearer": {"Authorization": "Bearer guard-key"}}


@pytest.mark.parametrize("path,auth", ENDPOINTS)
def test_handler_does_not_block_the_event_loop(gateway, path, auth):
    headers = gateway["bearer"] if auth == "bearer" else {}
    cookies = gateway["cookies"] if auth == "cookie" else {}
    loop_calls: list[str] = []

    async def call() -> int:
        transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://t", cookies=cookies) as client:
            return (await client.get(path, headers=headers)).status_code

    with _loop_thread_work(loop_calls):
        status = asyncio.run(call())
    assert status == 200, f"{path} answered {status}; the probe needs a working endpoint"
    assert loop_calls == [], f"{path} ran blocking work on the event loop:\n" + "\n".join(sorted(set(loop_calls)))


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


@contextlib.contextmanager
def _loop_thread_work(loop_calls: list[str]):
    """Record SQLite, subprocess, sleep and file work done on the loop thread.

    SQLite statements are traced with the connection's own trace hook, which
    fires on the thread that runs the statement. The hook goes on every
    connection already open (a store or cache warmed by an earlier test holds
    one) and on every connection opened while the probe is active, so test
    order cannot hide work.
    """
    import gc
    import subprocess
    loop_thread = threading.get_ident()

    def note(kind: str) -> None:
        if threading.get_ident() == loop_thread:
            frames = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in traceback.extract_stack()[:-2]
                      if "/site-packages/" not in f.filename and "/lib/python" not in f.filename]
            loop_calls.append(f"{kind} " + " <- ".join(reversed(frames[-7:])))

    def trace_statement(statement: str) -> None:
        note(f"sqlite3 {statement.strip()[:40]!r}")

    traced: list[sqlite3.Connection] = []

    def trace(conn: sqlite3.Connection) -> None:
        try:
            conn.set_trace_callback(trace_statement)
        except sqlite3.ProgrammingError:
            return  # closed, or bound to another thread (it cannot run on this one)
        traced.append(conn)

    real_connect, real_popen, real_sleep = sqlite3.connect, subprocess.Popen, time.sleep
    real_open = builtins.open

    def traced_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and "/site-packages/" not in str(file) and not str(file).startswith("/proc"):
            note(f"open({Path(str(file)).name})")
        return real_open(file, *args, **kwargs)

    def connect(*args, **kwargs):
        note("sqlite3.connect")
        conn = real_connect(*args, **kwargs)
        trace(conn)
        return conn

    class Popen(real_popen):
        def __init__(self, *args, **kwargs):
            note("subprocess.Popen")
            super().__init__(*args, **kwargs)

    def sleep(seconds):
        if seconds > 0.01:
            note("time.sleep")
        return real_sleep(seconds)

    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            trace(obj)
    try:
        with patch.object(sqlite3, "connect", connect), patch.object(subprocess, "Popen", Popen), \
                patch.object(time, "sleep", sleep), patch.object(builtins, "open", traced_open):
            yield
    finally:
        for conn in traced:
            try:
                conn.set_trace_callback(None)
            except sqlite3.ProgrammingError:
                pass  # closed since


@pytest.mark.parametrize("route", ROUTES)
def test_every_get_route_leaves_the_loop_free(gateway, route):
    loop_calls: list[str] = []

    async def call() -> int:
        transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", cookies=gateway["cookies"],
                                     timeout=20) as client:
            return (await client.get(_concrete(route), headers=gateway["bearer"])).status_code

    with _loop_thread_work(loop_calls):
        asyncio.run(call())
    assert not loop_calls, f"{route} ran blocking work on the event loop:\n" + "\n".join(sorted(set(loop_calls)))


def test_startup_initializes_the_stores_in_parallel_off_the_loop():
    slow = 0.3
    loop_thread = threading.get_ident()
    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "on_loop": 0}

    def sleeper(*args, **kwargs):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["on_loop"] += threading.get_ident() == loop_thread
        time.sleep(slow)
        with lock:
            state["active"] -= 1

    from gateway import providers
    with patch.object(proxy.ADMIN_STORE, "init_db", sleeper), patch.object(proxy.responses_store, "init_db", sleeper), \
            patch.object(proxy.receipt_store, "init_db", sleeper), patch.object(proxy.media_jobs, "init_db", sleeper), \
            patch.object(providers, "recover_provider_spend", sleeper):
        asyncio.run(proxy._initialize_stores())
    assert state["on_loop"] == 0, "a store initialized on the event loop thread"
    assert state["peak"] >= 2, "the stores initialized one after another"


COLD_GATE_PATHS = ("/v1/models", "/v1/models/x", "/openai/v1/models/x", "/anthropic/v1/models/x")


def test_model_routes_keep_category_reads_off_the_loop_on_a_cold_gate(gateway, tmp_path):
    """A gate built inside the probe makes the first category read hit SQLite.

    A gate warmed by an earlier test holds a connection the probe never traced,
    which hides loop-thread reads; build a fresh one so the order cannot matter.
    """
    from gateway import category_enforcement
    from gateway.category_store import CategoryStore
    store = tmp_path / "categories.db"
    CategoryStore(store)
    category_enforcement.configure(store)
    loop_calls: list[str] = []

    async def call() -> None:
        transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=20) as client:
            for path in COLD_GATE_PATHS:
                await client.get(path, headers=gateway["bearer"])

    try:
        with _loop_thread_work(loop_calls):
            asyncio.run(call())
    finally:
        category_enforcement.configure(None)
    assert not loop_calls, "model routes ran blocking work on the event loop:\n" + "\n".join(sorted(set(loop_calls)))
