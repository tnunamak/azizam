"""Receipt expiry releases its complete fact snapshot through every public path."""
import copy
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import admin_store, media_jobs, receipt_store
from gateway.admin_api import AdminContext
from gateway.retention_facts import ledger, revision
from gateway.settings_api import create_settings_router

NOW = 1_800_000_000


@pytest.fixture
def stores(tmp_path, monkeypatch):
    paths = {name: tmp_path / f"{name}.db" for name in ("receipts", "facts", "admin")}
    receipt_store.init_db(paths["receipts"])
    media_jobs.init_db(paths["facts"])
    admin_store.init_db(paths["admin"])
    monkeypatch.setattr(media_jobs, "DB_PATH", paths["facts"])
    monkeypatch.setattr(receipt_store.time, "time", lambda: NOW)
    return paths


def _fact():
    fact = copy.deepcopy(ledger({}, kinds={})["destinations"][0]["facts"][0])
    return {key: value for key, value in fact.items()
            if key not in {"revision", "stale", "stale_label", "source"}}


def _save(paths, source_kind, source_id, fact):
    return media_jobs.save_fact_revisions(
        [fact], source_kind=source_kind, source_id=source_id, destination_id="remote",
        destination={"name": "Captured provider", "kind": "provider"}, path=paths["facts"],
    )[0]


@pytest.mark.parametrize("delete_path", ["prune", "save_retention", "maintain", "settings_router"])
def test_configured_cleanup_releases_expired_receipt_snapshot_and_preserves_kept_record(stores, delete_path):
    fact = _fact()
    shared = _save(stores, "receipt", "expired", fact)
    orphan = _save(stores, "receipt", "expired", {**fact, "how_long": "expired only"})
    _save(stores, "kept_record", "kept", fact)
    survivor = _save(stores, "receipt", "recent", {**fact, "how_long": "recent only"})
    receipt_store.record({"request_id": "expired", "retention_facts": [shared, orphan]},
                         created_at=NOW - 40 * 86400, path=stores["receipts"])
    receipt_store.record({"request_id": "recent", "retention_facts": [survivor]},
                         created_at=NOW, path=stores["receipts"])
    preview = receipt_store.retention_preview(30, path=stores["receipts"])
    if delete_path == "prune":
        deleted = receipt_store.prune(30 * 86400, path=stores["receipts"], fact_store_path=stores["facts"])
    elif delete_path == "save_retention":
        deleted = receipt_store.save_retention(30, preview, lambda: None, path=stores["receipts"], fact_store_path=stores["facts"])
    elif delete_path == "maintain":
        admin_store.put_operator_setting(receipt_store.RETENTION_SETTING_KEY,
            {"retention_days": 30}, updated_by="owner", path=stores["admin"])
        deleted = receipt_store.maintain(stores["admin"], path=stores["receipts"], fact_store_path=stores["facts"])
    else:
        app = FastAPI()
        app.include_router(create_settings_router(
            auth=lambda request: AdminContext(subject="owner", roles=frozenset({"owner"}),
                                              auth_method="session", csrf_token="csrf-fixture"),
            admin_path=stores["admin"], receipts_path=stores["receipts"], fact_store_path=stores["facts"], config_provider=lambda: {},
        ))
        response = TestClient(app).put("/admin/api/settings/receipts",
            json={"retention_days": 30, "preview": preview},
            headers={"If-Match": "0", "x-azizam-csrf": "csrf-fixture"})
        assert response.status_code == 200, response.text
        deleted = response.json()["deleted_count"]
    assert deleted == 1
    assert receipt_store.get("expired", path=stores["receipts"]) is None
    assert receipt_store.get("recent", path=stores["receipts"]) is not None
    assert media_jobs.fact_revisions_for_source("receipt", "expired", path=stores["facts"]) == {}
    assert media_jobs.fact_destinations_for_source("receipt", "expired", path=stores["facts"]) == {}
    assert media_jobs.fact_revisions_for_source("kept_record", "kept", path=stores["facts"])["remote"][0]["revision"] == shared
    assert media_jobs.fact_destinations_for_source("kept_record", "kept", path=stores["facts"])
    assert media_jobs.fact_revisions_for_source("receipt", "recent", path=stores["facts"])["remote"][0]["revision"] == survivor
    with sqlite3.connect(stores["facts"]) as conn:
        assert {row[0] for row in conn.execute("SELECT revision FROM retention_fact_revisions")} == {shared, survivor}


def test_snapshot_saves_all_destinations_and_strips_display_fields(stores):
    fact = _fact()
    decorated = {**fact, "revision": "display", "stale": True, "stale_label": "old", "source": "seed"}
    destinations = [{"id": name, "name": name, "kind": "provider", "facts": [decorated]}
                    for name in ("first", "second")]
    destinations.append({"id": "empty", "name": "Empty", "facts": []})
    hashes = media_jobs.save_fact_snapshot(destinations, source_kind="receipt", source_id="r", path=stores["facts"])
    assert hashes == [revision(fact)]
    assert set(media_jobs.fact_revisions_for_source("receipt", "r", path=stores["facts"])) == {"first", "second"}
    assert set(media_jobs.fact_destinations_for_source("receipt", "r", path=stores["facts"])) == {"first", "second", "empty"}
    assert media_jobs.fact_revisions(hashes, path=stores["facts"]) == [{**fact, "revision": revision(fact)}]
    assert destinations[0]["facts"][0] == decorated


def test_snapshot_rolls_back_all_destinations_on_later_write_failure(stores):
    with sqlite3.connect(stores["facts"]) as conn:
        conn.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON retention_fact_destinations
                        WHEN NEW.destination_id = 'second' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        media_jobs.save_fact_snapshot(
            [{"id": name, "name": name, "facts": [_fact()]} for name in ("first", "second")],
            source_kind="receipt", source_id="r", path=stores["facts"],
        )
    with sqlite3.connect(stores["facts"]) as conn:
        for table in ("retention_fact_revisions", "retention_fact_references", "retention_fact_destinations"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["prune", "save", "maintain", "settings"])
@pytest.mark.parametrize("damage", ["garbage", "tableless"])
def test_expiry_commits_before_damaged_facts_and_sweeps_after_recovery(stores, operation, damage, caplog, monkeypatch):
    fact = _fact()
    _save(stores, "receipt", "expired", fact)
    _save(stores, "kept_record", "kept", fact)
    receipt_store.record({"request_id": "expired"}, created_at=NOW - 40 * 86400, path=stores["receipts"])
    admin_store.put_operator_setting(receipt_store.RETENTION_SETTING_KEY, {"retention_days": 30}, path=stores["admin"])
    broken = stores["facts"].with_name("broken.db")
    if damage == "garbage":
        broken.write_bytes(b"not a database" * 100)
    else:
        with sqlite3.connect(broken) as conn:
            conn.execute("CREATE TABLE unrelated (id)")
    preview = receipt_store.retention_preview(30, path=stores["receipts"])
    if operation == "prune":
        assert receipt_store.prune(30 * 86400, path=stores["receipts"], fact_store_path=broken) == 1
    elif operation == "save":
        assert receipt_store.save_retention(30, preview, lambda: None, path=stores["receipts"], fact_store_path=broken) == 1
    elif operation == "maintain":
        assert receipt_store.maintain(stores["admin"], path=stores["receipts"], fact_store_path=broken) == 1
    else:
        app = FastAPI()
        app.include_router(create_settings_router(
            auth=lambda request: AdminContext(subject="owner", roles=frozenset({"owner"}), auth_method="session", csrf_token="csrf-fixture"),
            admin_path=stores["admin"], receipts_path=stores["receipts"], fact_store_path=broken, config_provider=lambda: {},
        ))
        result = TestClient(app).put("/admin/api/settings/receipts", json={"retention_days": 30, "preview": preview},
                                   headers={"If-Match": "1", "x-azizam-csrf": "csrf-fixture"})
        assert result.status_code == 202, result.text
        assert result.json()["cleanup_pending"] is True
        assert result.json()["deleted_count"] == 1
    assert receipt_store.get("expired", path=stores["receipts"]) is None
    assert "fact" in caplog.text.lower()
    assert media_jobs.fact_revisions_for_source("receipt", "expired", path=stores["facts"])
    monkeypatch.setattr(receipt_store.time, "time", lambda: NOW + 301)
    assert receipt_store.maintain(stores["admin"], path=stores["receipts"], fact_store_path=stores["facts"]) == 0
    assert media_jobs.fact_revisions_for_source("receipt", "expired", path=stores["facts"]) == {}
    assert media_jobs.fact_destinations_for_source("receipt", "expired", path=stores["facts"]) == {}
    assert media_jobs.fact_revisions_for_source("kept_record", "kept", path=stores["facts"])


def test_cleanup_batches_media_transactions_after_releasing_receipt_lock(stores, monkeypatch):
    from gateway import request_connections
    for index in range(12):
        _save(stores, "receipt", str(index), _fact())
        receipt_store.record({"request_id": str(index)}, created_at=NOW - 40 * 86400, path=stores["receipts"])
    calls = []
    release = media_jobs.release_fact_reference_batch
    def checked_release(kind, ids, **kwargs):
        with sqlite3.connect(stores["receipts"], timeout=0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        calls.append(len(ids))
        return release(kind, ids, **kwargs)
    monkeypatch.setattr(media_jobs, "release_fact_reference_batch", checked_release)
    with request_connections.request_scope():
        assert receipt_store.prune(30 * 86400, path=stores["receipts"], fact_store_path=stores["facts"]) == 12
    assert calls == [12]


def test_startup_schedules_maintenance_before_initial_failure(monkeypatch):
    import asyncio
    import proxy
    async def run():
        calls = []
        loop = asyncio.get_running_loop()
        def fail(*args, **kwargs):
            calls.append(any(t.get_name() == "receipt-maintenance" for t in asyncio.all_tasks(loop)))
            raise sqlite3.DatabaseError("broken receipts")
        monkeypatch.setattr(receipt_store, "maintain", fail)
        stop = asyncio.Event()
        task = await proxy._start_receipts_maintenance(stop)
        assert not task.done()
        stop.set()
        await task
        assert calls == [True]
    asyncio.run(run())


def test_hourly_maintenance_survives_a_failed_run(monkeypatch):
    import asyncio
    import proxy
    async def run():
        stop = asyncio.Event()
        attempts = []
        async def tick(*args, **kwargs):
            args[0].close()
            raise asyncio.TimeoutError
        def maintain(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.DatabaseError("broken")
            stop.set()
        monkeypatch.setattr(proxy.asyncio, "wait_for", tick)
        monkeypatch.setattr(receipt_store, "maintain", maintain)
        await proxy._receipts_maintenance_loop(stop)
        assert len(attempts) == 2
    asyncio.run(run())
