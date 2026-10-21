import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import proxy


def test_receipt_startup_maintenance_compacts_activity(monkeypatch, tmp_path):
    compact = Mock(return_value=4)
    monkeypatch.setattr(proxy.firehose_metrics, "compact_log", compact)
    monkeypatch.setattr(proxy.receipt_store, "maintain", Mock(return_value=0))
    monkeypatch.setattr(proxy, "ADMIN_STORE", type("Store", (), {"path": tmp_path / "admin.db"})())

    async def scenario():
        stop = asyncio.Event()
        stop.set()
        await (await proxy._start_receipts_maintenance(stop))

    asyncio.run(scenario())
    compact.assert_called_once_with()


def test_startup_maintenance_persists_only_valid_window_rows(monkeypatch, tmp_path):
    now = 1000
    path = tmp_path / "activity.jsonl"
    rows = [
        {"kind": "request", "ts": 899, "path": "/old"},
        {"kind": "request", "ts": 900, "path": "/cutoff"},
        "{malformed row",
        {"kind": "request", "ts": 999, "path": "/recent"},
        {"kind": "request", "ts": 1300, "path": "/future-limit"},
        {"kind": "request", "ts": 1301, "path": "/too-far-future"},
    ]
    path.write_text("".join(
        row + "\n" if isinstance(row, str) else json.dumps(row) + "\n"
        for row in rows
    ), encoding="utf-8")
    monkeypatch.setattr(proxy.firehose_metrics, "_DEFAULT_CACHE_PATH", path)
    monkeypatch.setattr(proxy.firehose_metrics, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(proxy.receipt_store, "maintain", Mock(return_value=0))
    monkeypatch.setattr(proxy, "ADMIN_STORE", type("Store", (), {"path": tmp_path / "admin.db"})())

    async def scenario():
        stop = asyncio.Event()
        stop.set()
        await (await proxy._start_receipts_maintenance(stop))

    asyncio.run(scenario())

    persisted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [(row["ts"], row["path"]) for row in persisted] == [
        (900, "/cutoff"), (999, "/recent"), (1300, "/future-limit"),
    ]
    assert path.stat().st_mode & 0o777 == 0o600


def test_startup_compaction_runs_when_receipt_maintenance_fails(monkeypatch, tmp_path):
    compact = Mock(return_value=0)

    def fail_receipt_maintenance(*_args, **_kwargs):
        raise RuntimeError("receipt store unavailable")

    monkeypatch.setattr(proxy.firehose_metrics, "compact_log", compact)
    monkeypatch.setattr(proxy.receipt_store, "maintain", fail_receipt_maintenance)
    monkeypatch.setattr(proxy, "ADMIN_STORE", type("Store", (), {"path": tmp_path / "admin.db"})())

    async def scenario():
        stop = asyncio.Event()
        stop.set()
        await (await proxy._start_receipts_maintenance(stop))

    asyncio.run(scenario())

    compact.assert_called_once_with()


def test_hourly_maintenance_compacts_after_receipt_maintenance(monkeypatch, tmp_path):
    calls = []
    compact = Mock(side_effect=lambda: calls.append("compact") or 2)
    maintain = Mock(side_effect=lambda *_a, **_kw: calls.append("receipts") or 0)
    monkeypatch.setattr(proxy.firehose_metrics, "compact_log", compact)
    monkeypatch.setattr(proxy.receipt_store, "maintain", maintain)
    monkeypatch.setattr(proxy, "ADMIN_STORE", type("Store", (), {"path": tmp_path / "admin.db"})())

    async def controlled_wait(awaitable, timeout):
        awaitable.close()
        assert timeout == 3600
        if calls:
            stop.set()
            return True
        raise asyncio.TimeoutError

    async def inline_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def scenario():
        nonlocal stop
        stop = asyncio.Event()
        monkeypatch.setattr(proxy.asyncio, "wait_for", controlled_wait)
        monkeypatch.setattr(proxy.asyncio, "to_thread", inline_thread)
        await proxy._receipts_maintenance_loop(stop)

    stop = None
    asyncio.run(scenario())
    assert calls == ["receipts", "compact"]
    maintain.assert_called_once_with(proxy.ADMIN_STORE.path, fact_store_path=proxy.media_jobs.DB_PATH)
    compact.assert_called_once_with()
