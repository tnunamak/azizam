import asyncio
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
