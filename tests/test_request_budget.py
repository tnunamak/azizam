"""Per-request SQLite connection and latency budgets for the hot read endpoints.

Each hot endpoint may open at most MAX_OPENS SQLite connections per request
(on any thread) and must answer within P95_LIMIT_S without injected delay and
within P95_SLOW_LIMIT_S when every open costs 100 ms.
"""
import asyncio
import sqlite3
import statistics
import time
import traceback
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import proxy
from gateway import media_jobs
from tests.test_event_loop_guard import gateway  # noqa: F401 - fixture

MAX_OPENS = 3
P95_LIMIT_S = 0.05
P95_SLOW_LIMIT_S = 0.3
SAMPLES = 20
ENDPOINTS = (
    ("/me/api/home", "cookie"),
    ("/me/api/status", "cookie"),
    ("/me/api/models/catalog", "cookie"),
    ("/v1/models", "bearer"),
    ("/me/api/media", "cookie"),
    ("/me/api/jobs", "cookie"),
    ("/me/api/usage/summary", "cookie"),
    ("/me/api/activity", "cookie"),
)


def _counting_connect(real, delay, counter, opened):
    def connect(*args, **kwargs):
        counter[0] += 1
        frames = [f for f in traceback.extract_stack()[:-1] if ("/gateway/" in f.filename or f.filename.endswith("proxy.py"))
                  and not f.filename.endswith(("sqlite_startup.py", "request_connections.py"))]
        where = " < ".join(f"{Path(f.filename).stem}.{f.name}" for f in reversed(frames[-3:]))
        opened.append(f"{Path(str(args[0])).name}@{where}")
        if delay:
            time.sleep(delay)
        return real(*args, **kwargs)
    return connect


async def _measure(path, headers, cookies, delay):
    opens = [0]
    opened = []
    times = []
    transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://t", cookies=cookies) as client:
        with patch.object(sqlite3, "connect", _counting_connect(sqlite3.connect, delay, opens, opened)):
            counts = []
            for _ in range(SAMPLES if not delay else 5):
                before = opens[0]
                started = time.perf_counter()
                response = await client.get(path, headers=headers)
                times.append(time.perf_counter() - started)
                counts.append(opens[0] - before)
                assert response.status_code == 200, (path, response.status_code, response.text[:200])
    return times, counts, Counter(opened)


def _p(times, q):
    ordered = sorted(times)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


@pytest.fixture(autouse=True)
def media_store(tmp_path, monkeypatch):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    monkeypatch.setattr(media_jobs, "DB_PATH", db)


@pytest.mark.parametrize("path,auth", ENDPOINTS)
def test_endpoint_budget(gateway, path, auth):  # noqa: F811
    headers = gateway["bearer"] if auth == "bearer" else {}
    cookies = gateway["cookies"] if auth == "cookie" else {}
    fast, counts, opened = asyncio.run(_measure(path, headers, cookies, 0))
    warm = counts[2:]  # the first requests may fill caches
    slow, _, _ = asyncio.run(_measure(path, headers, cookies, 0.1))
    p50, p95, slow_p95 = _p(fast[2:], .5), _p(fast[2:], .95), max(slow[1:])
    note = f"{path}: opens={max(warm)} {dict(opened)} p50={p50*1000:.0f}ms p95={p95*1000:.0f}ms slow_p95={slow_p95*1000:.0f}ms"
    assert max(warm) <= MAX_OPENS, f"PERF {note}"
    assert p95 <= P95_LIMIT_S, f"PERF {note}"
    assert slow_p95 <= P95_SLOW_LIMIT_S, f"PERF {note}"
