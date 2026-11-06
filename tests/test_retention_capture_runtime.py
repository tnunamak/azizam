"""Capture uses configured state and keeps optional evidence off the hot path."""
import logging
import sqlite3
from contextlib import contextmanager

import pytest

import proxy
from gateway import admin_store, media_jobs, receipt_store, retention_facts, receipts as receipt_helpers
from gateway.receipts import ReceiptLifecycleOwner, RouteReceipt


@pytest.fixture
def capture_stores(tmp_path, monkeypatch):
    admin = tmp_path / "configured-admin.db"
    media = tmp_path / "media.db"
    receipts = tmp_path / "receipts.db"
    admin_store.init_db(admin)
    media_jobs.init_db(media)
    receipt_store.init_db(receipts)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    monkeypatch.setattr(admin_store, "DB_PATH", tmp_path / "unused-default-admin.db")
    monkeypatch.setattr(media_jobs, "DB_PATH", media)
    monkeypatch.setattr(receipt_store, "begin", lambda *_a, **_kw: None)
    monkeypatch.setattr(receipt_store, "finalize", lambda *_a, **_kw: None)
    registry = {"services": {"capture-destination": {"name": "Configured destination", "config": {
        "connection": {"protocol": "openai", "base_url": "http://127.0.0.1:18080"},
        "retention_facts": [],
    }}}}
    admin_store.write_service_registry(registry, expected_generation=0, path=admin)
    cached = getattr(receipt_helpers, "_captured_retention_generation", None)
    if cached is not None:
        cached.cache_clear()
    return admin, media, registry


def test_lifecycle_capture_reads_gateway_configured_admin_path(capture_stores):
    row = RouteReceipt(protocol="test", intent="test", backend="capture-destination")
    ReceiptLifecycleOwner(row)
    captured = getattr(row, "_retention_fact_snapshot", None)
    assert captured is not None
    assert any(d["id"] == "capture-destination" for d in captured[1]["destinations"])
    assert not admin_store.DB_PATH.exists()


def test_capture_memoizes_validated_hashed_generation_and_invalidates(capture_stores, monkeypatch):
    _, _, registry = capture_stores
    calls = []
    real_ledger = retention_facts.ledger
    def counted(*args, **kwargs):
        calls.append(1)
        return real_ledger(*args, **kwargs)
    monkeypatch.setattr(retention_facts, "ledger", counted)
    overrides = {}
    def capture():
        row = RouteReceipt(protocol="test", intent="test", backend="capture-destination")
        row.capture_retention_facts(registry=registry, overrides=overrides)
        return row
    first = capture()
    capture()
    assert len(calls) == 1
    registry["services"]["capture-destination"]["name"] = "Changed destination"
    second = capture()
    assert len(calls) == 2
    assert first._retention_fact_snapshot[1]["destinations"] != second._retention_fact_snapshot[1]["destinations"]
    overrides[("engine", "capture-destination")] = []
    capture()
    assert len(calls) == 3
    # A captured generation must also skip the validation/hash preparation at persist.
    monkeypatch.setattr(retention_facts, "validate_facts", lambda *_a, **_kw: pytest.fail("revalidated cached facts"))
    monkeypatch.setattr(retention_facts, "revision", lambda *_a, **_kw: pytest.fail("rehashed cached facts"))
    first.persist_retention_facts()
    assert first.retention_facts


def test_request_fact_persistence_has_bounded_busy_wait(capture_stores, monkeypatch):
    _, media, registry = capture_stores
    row = RouteReceipt(protocol="test", intent="test", backend="capture-destination")
    row.capture_retention_facts(registry=registry, overrides={})
    waits, pauses = [], []
    real_conn = media_jobs._conn

    @contextmanager
    def observed_conn(*args, **kwargs):
        with real_conn(*args, **kwargs) as conn:
            waits.append(conn.execute("PRAGMA busy_timeout").fetchone()[0])
            yield conn

    monkeypatch.setattr(media_jobs, "_conn", observed_conn)
    monkeypatch.setattr(receipt_helpers, "sleep", pauses.append)
    with sqlite3.connect(media) as locked:
        locked.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            row.persist_retention_facts()
    # Check the 620 ms SQLite wait/backoff budget independently of host scheduling.
    assert waits == [200, 200, 200]
    assert pauses == [0.01, 0.01]
    assert row.retention_facts == []
    assert media_jobs.fact_revisions_for_source("receipt", row.request_id, path=media) == {}


@pytest.mark.parametrize("stage", ["capture", "persist"])
def test_proxy_fact_failure_logs_request_and_counter(capture_stores, monkeypatch, caplog, capsys, stage):
    _, _, registry = capture_stores
    row = RouteReceipt(protocol="test", intent="test", backend="capture-destination")
    def unavailable(*_a, **_kw):
        raise sqlite3.OperationalError("test unavailable")
    monkeypatch.setattr(row, stage + "_retention_facts", unavailable)
    caplog.set_level(logging.WARNING)
    if stage == "capture":
        proxy._capture_retention_facts(row)
    else:
        row.capture_retention_facts(registry=registry, overrides={})
        proxy._snapshot_retention_facts(row)
    output = caplog.text + capsys.readouterr().out
    assert row.request_id in output
    assert "count=" in output
    assert stage in output
