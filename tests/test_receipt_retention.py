"""Receipt history stays durable unless an owner chooses expiry."""
import sqlite3
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway import admin_store, receipt_store
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.settings_api import create_settings_router

OWNER = {"test-role": "owner", "x-azizam-csrf": "csrf-fixture"}


def test_upgrade_seeds_history_floor_from_surviving_receipts(tmp_path, monkeypatch):
    path = tmp_path / "receipts.db"
    now = 1_800_000_000
    monkeypatch.setattr(receipt_store.time, "time", lambda: now)
    # An old v2 store has already pruned the first 14 days of its history.
    with sqlite3.connect(path) as conn:
        conn.executescript(receipt_store._SCHEMA)
        conn.execute("INSERT INTO schema_version VALUES (2)")
    receipt_store.record({"request_id": "survivor"}, created_at=now - 14 * 86400, path=path)
    receipt_store.init_db(path)
    assert receipt_store.history_floor(path) == now - 14 * 86400
    receipt_store.init_db(path)
    assert receipt_store.history_floor(path) == now - 14 * 86400


@pytest.mark.parametrize("existing", [False, True])
def test_empty_store_history_starts_at_initialization(tmp_path, monkeypatch, existing):
    path = tmp_path / "receipts.db"
    if existing:
        with sqlite3.connect(path) as conn:
            conn.executescript(receipt_store._SCHEMA)
            conn.execute("INSERT INTO schema_version VALUES (2)")
    monkeypatch.setattr(receipt_store.time, "time", lambda: 1_800_000_000)
    receipt_store.init_db(path)
    assert receipt_store.history_floor(path) == 1_800_000_000
    monkeypatch.setattr(receipt_store.time, "time", lambda: 1_800_001_000)
    receipt_store.init_db(path)
    assert receipt_store.history_floor(path) == 1_800_000_000


@pytest.mark.parametrize("deletion", ["prune", "save", "maintain"])
def test_history_floor_never_retreats_after_retention_increase(tmp_path, monkeypatch, deletion):
    path = tmp_path / "receipts.db"
    admin_path = tmp_path / "admin.db"
    admin_store.init_db(admin_path)
    now = 1_800_000_000
    monkeypatch.setattr(receipt_store.time, "time", lambda: now - 60 * 86400)
    receipt_store.init_db(path)
    receipt_store.record({"request_id": "old"}, created_at=now - 40 * 86400, path=path)
    receipt_store.record({"request_id": "kept"}, created_at=now, path=path)
    monkeypatch.setattr(receipt_store.time, "time", lambda: now)
    save = lambda days: admin_store.put_operator_setting(receipt_store.RETENTION_SETTING_KEY,
        {"retention_days": days}, path=admin_path)
    if deletion == "prune":
        deleted = receipt_store.prune(30 * 86400, path=path)
    elif deletion == "save":
        preview = receipt_store.retention_preview(30, path=path)
        deleted = receipt_store.save_retention(30, preview, lambda: save(30), path=path)
    else:
        save(30)
        deleted = receipt_store.maintain(admin_path, path=path)
    assert deleted == 1
    assert receipt_store.history_floor(path) == now - 30 * 86400
    for days in (90, 365, None):
        preview = receipt_store.retention_preview(days, path=path)
        assert receipt_store.save_retention(days, preview, lambda: save(days), path=path) == 0
        assert receipt_store.maintain(admin_path, path=path) == 0
        receipt_store.init_db(path)
        assert receipt_store.history_floor(path) == now - 30 * 86400


def test_compaction_busy_reader_reports_pending_and_physical_bytes(tmp_path):
    path = tmp_path / "receipts.db"
    receipt_store.init_db(path)
    for i in range(40):
        receipt_store.record({"request_id": str(i), "metadata": {"diagnostic": "a" * 4000}},
                             created_at=i, path=path)
    reader = sqlite3.connect(path)
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 40
        receipt_store.prune(int(time.time()) - 39, path=path)
        result = receipt_store.compact(path)
        assert result["checkpoint_pending"] is True
        assert result["after_logical_bytes"] < result["before_logical_bytes"]
        assert result["after_main_bytes"] == path.stat().st_size
        assert result["after_wal_bytes"] == Path(str(path) + "-wal").stat().st_size > 0
        assert result["after_physical_bytes"] == result["after_main_bytes"] + result["after_wal_bytes"]
        assert result["after_physical_bytes"] > result["after_logical_bytes"]
        assert reader.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 40
    finally:
        reader.close()
    assert receipt_store.count(path) == 1
    result = receipt_store.compact(path)
    assert result["checkpoint_pending"] is False
    assert result["after_wal_bytes"] == 0
    assert receipt_store.get("39", path=path) is not None


def test_compaction_cli_exits_nonzero_until_reader_is_idle(tmp_path):
    path = tmp_path / "receipts.db"
    receipt_store.init_db(path)
    receipt_store.record({"request_id": "kept"}, path=path)
    script = Path(__file__).resolve().parents[1] / "scripts/compact-receipts"
    cmd = [sys.executable, str(script), "--db", str(path)]
    reader = sqlite3.connect(path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM receipts").fetchall()
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode != 0
        assert json.loads(result.stdout)["checkpoint_pending"] is True
        assert "retry" in result.stderr.lower() and "idle" in result.stderr.lower()
    finally:
        reader.close()
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["checkpoint_pending"] is False


def test_no_pruning_by_default(tmp_path):
    path = tmp_path / "receipts.db"
    receipt_store.init_db(path)
    receipt_store.record({"request_id": "old", "app_id": "mine"},
                         created_at=int(time.time()) - 400 * 86400, path=path)
    assert receipt_store.prune(path=path) == 0
    assert receipt_store.get("old", path=path) is not None


@pytest.fixture
def history(tmp_path, monkeypatch):
    admin_path = tmp_path / "admin.db"
    receipts = tmp_path / "receipts.db"
    admin_store.init_db(admin_path)
    receipt_store.init_db(receipts)
    monkeypatch.setattr(receipt_store.time, "time", lambda: 1_800_000_000)
    for days in (0, 29, 30, 31, 89, 90, 91, 364, 365, 366, 400):
        receipt_store.record({"request_id": str(days), "app_id": "mine", "status": "succeeded"},
                             created_at=1_800_000_000 - days * 86400, path=receipts)
    # Child receipts also count toward deletion, even though Usage lists outer rows.
    receipt_store.record({"request_id": "child", "app_id": "theirs", "parent_request_id": "400"},
                         created_at=1_800_000_000 - 400 * 86400, path=receipts)

    def auth(request: Request):
        role = request.headers.get("test-role")
        if not role:
            raise AdminAuthError()
        return AdminContext(subject="tester", roles=frozenset({role}), auth_method="session", csrf_token="csrf-fixture")

    def new_client():
        app = FastAPI()
        app.include_router(create_settings_router(auth=auth, admin_path=admin_path,
            receipts_path=receipts, config_provider=lambda: {}))
        return TestClient(app)

    return new_client(), admin_path, receipts, new_client


def _save(client, days, revision="0", preview=None):
    return client.put("/admin/api/settings/receipts", json={"retention_days": days, "preview": preview},
                      headers={**OWNER, "If-Match": revision})


@pytest.mark.parametrize("days,deleted", [(None, 0), (365, 3), (90, 6), (30, 9)])
def test_preview_save_restart_and_maintenance(history, days, deleted):
    client, admin_path, path, new_client = history
    assert client.get("/admin/api/settings/receipts", headers=OWNER).json()["retention_days"] is None
    preview = client.get("/admin/api/settings/receipts/preview", params={"retention_days": days or "forever"}, headers=OWNER).json()
    assert preview["delete_count"] == deleted
    assert receipt_store.count(path, outer_only=False) == 12  # Preview never deletes.
    response = _save(client, days, preview=preview)
    assert response.status_code == 200
    assert response.json()["deleted_count"] == deleted
    assert receipt_store.count(path, outer_only=False) == 12 - deleted
    if days is not None:
        assert receipt_store.get(str(days), path=path) is not None  # Exact cutoff survives.
        assert receipt_store.get(str(days + 1), path=path) is None
    admin_store.init_db(admin_path)
    receipt_store.init_db(path)
    restored = new_client().get("/admin/api/settings/receipts", headers=OWNER).json()
    assert restored["retention_days"] == days
    assert restored["revision"] == response.json()["revision"]
    receipt_store.record({"request_id": "late-old", "app_id": "mine"}, created_at=1_800_000_000 - 500 * 86400, path=path)
    assert receipt_store.maintain(admin_path, path=path) == (0 if days is None else 1)
    assert bool(receipt_store.get("late-old", path=path)) == (days is None)
    assert admin_store.list_audit(path=admin_path)[0]["action"] == "operator.receipt_retention.update"


@pytest.mark.parametrize("role", [None, "member", "admin"])
def test_retention_and_preview_are_owner_only(history, role):
    client, _, path, _ = history
    headers = {"test-role": role, "x-azizam-csrf": "csrf-fixture"} if role else {}
    for route in ("receipts", "receipts/preview?retention_days=30"):
        assert client.get(f"/admin/api/settings/{route}", headers=headers).status_code in (401, 403)
    assert client.put("/admin/api/settings/receipts", json={"retention_days": 30, "preview": None}, headers=headers).status_code in (401, 403)
    assert receipt_store.count(path, outer_only=False) == 12


def test_save_requires_csrf_and_revision(history):
    client, _, path, _ = history
    body = {"retention_days": None, "preview": None}
    assert client.put("/admin/api/settings/receipts", json=body, headers={"test-role": "owner", "If-Match": "0"}).status_code == 403
    assert client.put("/admin/api/settings/receipts", json=body, headers=OWNER).status_code == 428
    assert receipt_store.count(path, outer_only=False) == 12


@pytest.mark.parametrize("days", [True, False, "30", 14, 45, 30.0, {}, []])
def test_reject_invalid_retention(history, days):
    client, _, path, _ = history
    assert _save(client, days).status_code == 400
    assert receipt_store.count(path, outer_only=False) == 12


def test_conflicts_do_not_save_or_delete(history, monkeypatch):
    client, admin_path, path, _ = history
    assert _save(client, 30).status_code == 409
    preview = client.get("/admin/api/settings/receipts/preview?retention_days=30", headers=OWNER).json()
    assert _save(client, 30, revision="stale", preview=preview).status_code == 409
    receipt_store.record({"request_id": "backfill"}, created_at=1_800_000_000 - 400 * 86400, path=path)
    assert _save(client, 30, preview=preview).status_code == 409
    assert receipt_store.retention_days(admin_path) is None
    preview = client.get("/admin/api/settings/receipts/preview?retention_days=30", headers=OWNER).json()
    monkeypatch.setattr(receipt_store.time, "time", lambda: 1_800_001_000)
    assert _save(client, 30, preview=preview).status_code == 409
    assert receipt_store.count(path, outer_only=False) == 13
    assert admin_store.list_audit(path=admin_path) == []


def test_save_uses_preview_cutoff_even_when_time_advances(history, monkeypatch):
    client, _, path, _ = history
    preview = client.get("/admin/api/settings/receipts/preview?retention_days=30", headers=OWNER).json()
    monkeypatch.setattr(receipt_store.time, "time", lambda: 1_800_000_030)
    response = _save(client, 30, preview=preview)
    assert response.status_code == 200
    assert response.json()["deleted_count"] == preview["delete_count"]
    assert receipt_store.get("30", path=path) is not None


def test_setting_failure_preserves_history(history, monkeypatch):
    client, _, path, _ = history
    preview = client.get("/admin/api/settings/receipts/preview?retention_days=30", headers=OWNER).json()
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(admin_store, "put_operator_setting_with_audit", fail)
    assert _save(client, 30, preview=preview).status_code == 503
    assert receipt_store.count(path, outer_only=False) == 12


def test_default_maintenance_keeps_history_and_visibility(history):
    _, admin_path, path, _ = history
    assert receipt_store.maintain(admin_path, path=path) == 0
    assert receipt_store.count(path, outer_only=False) == 12
    assert {r["request_id"] for r in receipt_store.recent(app_ids=["mine"], path=path)} == {str(d) for d in (0, 29, 30, 31, 89, 90, 91, 364, 365, 366, 400)}
    assert receipt_store.recent(app_ids=[], path=path) == []
    assert receipt_store.recent(app_ids=["theirs"], path=path) == []  # Child hidden by default.
    assert [r["request_id"] for r in receipt_store.recent(app_ids=["theirs"], outer_only=False, path=path)] == ["child"]


def test_compaction_preserves_rows_enables_incremental_reclaim(tmp_path, monkeypatch):
    monkeypatch.setattr(receipt_store.time, "time", lambda: 1_800_000_000)
    path = tmp_path / "receipts.db"
    receipt_store.init_db(path)
    # Exercise conversion of an existing file whose auto_vacuum was disabled.
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA auto_vacuum=NONE")
        conn.execute("VACUUM")
    for i in range(100):
        receipt_store.record({"request_id": str(i), "metadata": {"diagnostic": "a" * 4000}}, created_at=i, path=path)
    receipt_store.prune(retain_s=int(time.time()) - 99, path=path)
    before = receipt_store.get("99", path=path)
    assert before is not None
    result = receipt_store.compact(path)
    assert result["after_bytes"] < result["before_bytes"]
    assert receipt_store.get("99", path=path) == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    admin_path = tmp_path / "admin.db"
    admin_store.init_db(admin_path)
    for i in range(100):
        receipt_store.record({"request_id": f"more-{i}", "metadata": {"diagnostic": "b" * 4000}}, created_at=i, path=path)
    receipt_store.prune(retain_s=int(time.time()) - 99, path=path)
    with sqlite3.connect(path) as conn:
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
    receipt_store.maintain(admin_path, path=path)
    with sqlite3.connect(path) as conn:
        after_pages = conn.execute("PRAGMA page_count").fetchone()[0]
        assert 0 < pages - after_pages <= 256


def test_saved_setting_retries_after_deletion_failure(history, monkeypatch):
    client, admin_path, path, _ = history
    original = receipt_store.save_retention
    def fail_after_save(days, preview, save_setting, *, path):
        save_setting()
        raise sqlite3.OperationalError("receipt database busy")
    monkeypatch.setattr(receipt_store, "save_retention", fail_after_save)
    preview = client.get("/admin/api/settings/receipts/preview?retention_days=30", headers=OWNER).json()
    response = _save(client, 30, preview=preview)
    assert response.status_code == 202
    assert response.json()["cleanup_pending"] is True
    assert response.json()["retention_days"] == 30
    assert response.json()["revision"] == "1"
    assert receipt_store.count(path, outer_only=False) == 12
    monkeypatch.setattr(receipt_store, "save_retention", original)
    assert receipt_store.maintain(admin_path, path=path) == preview["delete_count"]
    assert receipt_store.count(path, outer_only=False) == 3
