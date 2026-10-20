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
def test_default_cleanup_releases_expired_receipt_snapshot_and_preserves_kept_record(stores, delete_path):
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
        deleted = receipt_store.prune(30 * 86400, path=stores["receipts"])
    elif delete_path == "save_retention":
        deleted = receipt_store.save_retention(30, preview, lambda: None, path=stores["receipts"])
    elif delete_path == "maintain":
        admin_store.put_operator_setting(receipt_store.RETENTION_SETTING_KEY,
            {"retention_days": 30}, updated_by="owner", path=stores["admin"])
        deleted = receipt_store.maintain(stores["admin"], path=stores["receipts"])
    else:
        app = FastAPI()
        app.include_router(create_settings_router(
            auth=lambda request: AdminContext(subject="owner", roles=frozenset({"owner"}),
                                              auth_method="session", csrf_token="csrf-fixture"),
            admin_path=stores["admin"], receipts_path=stores["receipts"], config_provider=lambda: {},
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
