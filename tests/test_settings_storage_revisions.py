"""Regressions for settings concurrency, grant audits and effective People plans."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from gateway import accounts, admin_store
from gateway.admin_api import AdminContext
from gateway.admin_oidc import CookieConfig, session_from_claims
from gateway.admin_runtime import AdminSQLiteStore


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    return path


def write_setting(path, value, revision):
    return admin_store.put_operator_setting_with_audit(
        "oidc_provider", value, expected_revision=revision, updated_by="operator",
        audit_action="operator.oidc.update", audit_actor_kind="service_account",
        audit_actor_id="operator", audit_actor_label="Operator",
        audit_target_kind="operator_setting", audit_target_id="oidc_provider", path=path,
    )


def test_settings_revision_serializes_competing_writers_and_rolls_back_audit(db):
    def save(value):
        try:
            return write_setting(db, {"issuer": value}, "0")
        except admin_store.SettingsConflictError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["first", "second"]))
    assert results.count("conflict") == 1
    saved = admin_store.get_operator_setting("oidc_provider", path=db)
    assert saved["revision"] == "1"
    assert len(admin_store.list_audit(path=db)) == 1
    write_setting(db, saved["value"], "1")
    with pytest.raises(admin_store.SettingsConflictError):
        write_setting(db, {"issuer": "stale"}, "1")
    assert admin_store.get_operator_setting("oidc_provider", path=db)["revision"] == "2"
    assert len(admin_store.list_audit(path=db)) == 2


def test_settings_generation_migration_is_idempotent_and_all_writers_advance(db):
    with accounts.connection(db) as conn:
        conn.execute("INSERT INTO operator_settings VALUES('instance','{}',1,NULL)")
    admin_store.init_db(db)
    assert admin_store.get_operator_setting("instance", path=db)["revision"] == "1"
    admin_store.init_db(db)
    assert admin_store.get_operator_setting("instance", path=db)["revision"] == "1"
    admin_store.put_operator_setting("instance", {"name": "New"}, path=db)
    assert admin_store.get_operator_setting("instance", path=db)["revision"] == "2"


