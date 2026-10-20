"""Regression contracts from the third retention review."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import logging
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

import proxy
from gateway import admin_store, media_ingestion, media_jobs, receipt_store, retention_facts
from gateway.receipts import ReceiptLifecycleOwner, RouteReceipt


@pytest.mark.parametrize("entry", ["lifecycle", "media_stream"])
def test_terminal_fact_writes_leave_loop_and_precede_finalize(monkeypatch, entry):
    async def scenario():
        loop_thread = threading.get_ident()
        writes = []
        row = RouteReceipt(protocol="test", intent="test")

        def persist(self):
            assert threading.get_ident() != loop_thread, "fact fsync runs on event loop"
            writes.append("facts")
            self.retention_facts = ["saved-revision"]

        def finalize(receipt, **_kw):
            assert receipt.retention_facts == ["saved-revision"]
            writes.append("finalize")

        monkeypatch.setattr(RouteReceipt, "persist_retention_facts", persist)
        monkeypatch.setattr(receipt_store, "finalize", finalize)
        if entry == "lifecycle":
            async with ReceiptLifecycleOwner(row, begin=lambda _r: None, finalize=finalize):
                pass
        else:
            async def body():
                yield b"data"
            response = media_ingestion.finish_response(row, StreamingResponse(body()))
            assert [chunk async for chunk in response.body_iterator] == [b"data"]
        assert writes == ["facts", "finalize"]

    asyncio.run(scenario())


def test_request_facts_survive_concurrent_large_cleanup(tmp_path, monkeypatch):
    """A writer arriving during cleanup waits for its batch and loses no facts."""
    path = tmp_path / "facts.db"
    media_jobs.init_db(path)
    monkeypatch.setattr(media_jobs, "DB_PATH", path)
    fact = next(f for d in retention_facts.ledger({}, kinds={})["destinations"] for f in d["facts"])
    prepared = media_jobs.prepare_fact_snapshot([{"id": "destination", "name": "Destination", "facts": [fact]}])
    # A single transaction seeds many sources without thousands of fsync calls.
    with sqlite3.connect(path) as conn:
        conn.executemany("INSERT INTO retention_fact_destinations VALUES('receipt', ?, 'destination', '{}', 0)",
                         [(f"expired-{i}",) for i in range(1500)])
    locked = threading.Event()
    release = threading.Event()
    cleanup_errors = []
    original_conn = media_jobs._conn

    @contextmanager
    def slow_cleanup_batch(*a, **kw):
        with original_conn(*a, **kw) as conn:
            if threading.current_thread().name == "test-cleanup" and not locked.is_set():
                conn.execute("BEGIN IMMEDIATE")
                locked.set()
                assert release.wait(5)
                conn.rollback()
            yield conn

    monkeypatch.setattr(media_jobs, "_conn", slow_cleanup_batch)
    def cleanup():
        try:
            receipt_store._release_expired_fact_refs([f"expired-{i}" for i in range(1500)], path)
        except BaseException as error:
            cleanup_errors.append(error)
    worker = threading.Thread(target=cleanup, name="test-cleanup")
    worker.start()
    timer = None
    try:
        assert locked.wait(5)
        timer = threading.Timer(0.04, release.set)
        timer.start()
        for i in range(40):
            row = RouteReceipt(protocol="test", intent="test", backend="destination", request_id=f"new-{i}")
            row._retention_fact_snapshot = ("destination", None, prepared)
            row.persist_retention_facts()
    finally:
        release.set()
        worker.join(5)
        if timer is not None:
            timer.join(5)
    assert not worker.is_alive() and not cleanup_errors
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM retention_fact_destinations WHERE source_id LIKE 'new-%'").fetchone()[0] == 40
        assert conn.execute("SELECT count(*) FROM retention_fact_references WHERE source_id LIKE 'new-%'").fetchone()[0] == 40
    for i in range(40):
        saved = media_jobs.fact_revisions_for_source("receipt", f"new-{i}", path=path)
        assert saved["destination"][0]["holds"] == fact["holds"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM retention_fact_destinations WHERE source_id LIKE 'expired-%'").fetchone()[0] == 0


def test_invalid_legacy_facts_stay_unavailable_in_saved_ledger(tmp_path, monkeypatch):
    admin = tmp_path / "admin.db"
    facts = tmp_path / "facts.db"
    receipts = tmp_path / "receipts.db"
    admin_store.init_db(admin)
    media_jobs.init_db(facts)
    receipt_store.init_db(receipts)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    monkeypatch.setattr(media_jobs, "DB_PATH", facts)
    registry = {"services": {"destination": {"name": "Destination", "config": {"remote_provider": True}}}}
    admin_store.write_service_registry(registry, expected_generation=0, path=admin)
    with sqlite3.connect(admin) as conn:
        conn.execute("INSERT INTO retention_fact_overrides(target_type,target_id,facts_json,updated_at) VALUES(?,?,?,?)",
                     ("provider", "destination", json.dumps([{"id": "bad", "holds": "x"}]), 1))
    row = RouteReceipt(protocol="test", intent="test", backend="destination")
    row.capture_retention_facts(registry=registry, overrides=admin_store.retention_fact_overrides(path=admin))
    row.persist_retention_facts()
    receipt_store.record(row, path=receipts)
    original_get = receipt_store.get
    monkeypatch.setattr(receipt_store, "get", lambda *a, **kw: original_get(*a, **{**kw, "path": receipts}))
    # Current facts becoming valid must not rewrite historical uncertainty.
    admin_store.put_retention_fact_override("provider", "destination", [], path=admin)
    from starlette.requests import Request
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    view = proxy._retention_ledger_view(request, row.request_id, viewer_person_id="owner")
    saved = next(d for d in view["destinations"] if d["id"] == "destination")
    assert saved["facts_unavailable"] is True
    assert "Some destination facts are unavailable" in view["fact_basis"]
    assert "Destination" in view["never_save_text"] and "unavailable" in view["never_save_text"]


def test_media_fact_failure_logs_request_and_counter(monkeypatch, caplog):
    row = RouteReceipt(protocol="test", intent="test")
    def fail(_self):
        raise sqlite3.OperationalError("private content must not be logged")
    monkeypatch.setattr(RouteReceipt, "persist_retention_facts", fail)
    monkeypatch.setattr(receipt_store, "finalize", lambda *_a, **_kw: None)
    with caplog.at_level(logging.WARNING):
        media_ingestion.finish_receipt(row, status_code=200)
    assert row.request_id in caplog.text and "count=" in caplog.text
    assert "private content" not in caplog.text


def test_cleanup_missing_fact_path_is_observable(caplog):
    with caplog.at_level(logging.WARNING):
        assert receipt_store._release_expired_fact_refs(["expired"], None) is True
    assert "fact" in caplog.text.lower() and "path" in caplog.text.lower()


def test_startup_reports_pruned_count(monkeypatch, capsys):
    monkeypatch.setattr(receipt_store, "maintain", lambda *_a, **_kw: 17)
    async def scenario():
        stop = asyncio.Event()
        stop.set()
        await (await proxy._start_receipts_maintenance(stop))
    asyncio.run(scenario())
    assert "17" in capsys.readouterr().out


def test_cancellation_drains_ordered_writes_before_return(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    writes = []

    def persist(_self):
        entered.set()
        assert release.wait(5)
        writes.append("facts")

    def finalize(_receipt):
        writes.append("finalize")

    monkeypatch.setattr(RouteReceipt, "persist_retention_facts", persist)

    async def scenario():
        async def request():
            async with ReceiptLifecycleOwner(RouteReceipt(protocol="test", intent="test"),
                                             begin=lambda _r: None, finalize=finalize):
                pass
        task = asyncio.create_task(request())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done(), "cancellation detached the terminal write"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert writes == ["facts", "finalize"]
        finally:
            release.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_native_refusal_fact_commit_is_off_loop(monkeypatch):
    from types import SimpleNamespace
    from starlette.requests import Request
    from gateway import native_lane
    row = RouteReceipt(protocol="test", intent="test", backend="destination")
    monkeypatch.setattr(proxy, "_auth_context_from_scope", lambda _r: SimpleNamespace(identity=None))
    monkeypatch.setattr(proxy, "_native_passthrough_receipt", lambda **_kw: row)
    monkeypatch.setattr(receipt_store, "record", lambda *_a, **_kw: None)
    threads = []
    monkeypatch.setattr(row, "persist_retention_facts", lambda: threads.append(threading.get_ident()))
    async def scenario():
        loop_thread = threading.get_ident()
        request = Request({"type": "http", "method": "POST", "path": "/native/destination/unknown", "headers": []})
        response = await proxy._forward_provider_native(
            request, provider_key="destination", target={}, native_path="unknown", gateway_path=request.url.path,
            native_policy=native_lane.policy_for({}),
        )
        assert response.status_code == 403
        assert threads and all(thread != loop_thread for thread in threads)
    asyncio.run(scenario())
