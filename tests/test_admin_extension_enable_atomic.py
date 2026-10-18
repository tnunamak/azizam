"""Enabling an extension package is all-or-nothing in the admin store."""
import copy

import pytest

from gateway import admin_runtime, admin_store
from gateway.admin_api import AdminContext
from gateway.admin_runtime import AdminSQLiteStore, EXTENSION_PACKAGE_INDEX_SETTING_KEY


OWNER = AdminContext(subject="fixture-owner", roles=frozenset({"owner"}))
ENABLE_ACTION = "operator.extension_package.enable"


@pytest.fixture
def sqlite_store(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    return AdminSQLiteStore(path)


@pytest.fixture
def fake_package(sqlite_store, monkeypatch):
    review = {
        "ok": True,
        "package": {"id": "fixture-package", "version": "1.0.0"},
        "package_hash": "sha256:" + "a" * 64,
    }
    imported = {
        "services": {}, "protocol_frontends": {},
        "workflows": {}, "capabilities": {},
    }
    ep = admin_runtime.extension_packages
    monkeypatch.setattr(ep, "validate_extension_package_source",
                        lambda *a, **kw: copy.deepcopy(review))
    monkeypatch.setattr(ep, "imported_registry_records",
                        lambda *a, **kw: copy.deepcopy(imported))
    monkeypatch.setattr(ep, "merge_package_index", lambda *a, **kw: {
        "schema_version": 1, "sources": [],
        "packages": {"fixture-package": {"id": "fixture-package"}},
        "updated_at": 1,
    })
    monkeypatch.setattr(sqlite_store, "write_litellm_config",
                        lambda registry: sqlite_store.path.parent / "unused.yaml")


def snapshot(store):
    return (
        admin_store.get_service_registry_with_generation(path=store.path),
        admin_store.get_operator_setting(
            EXTENSION_PACKAGE_INDEX_SETTING_KEY, path=store.path
        ),
        admin_store.list_audit(path=store.path),
    )


def enable(store):
    return store.enable_extension_package(
        source={"type": "local_directory", "path": "/not-accessed"},
        actor=OWNER, expected_generation=0,
    )


EMPTY_RECORDS = {"capabilities": {}, "protocol_frontends": {}, "services": {}, "workflows": {}}

def test_extension_enable_commits_registry_index_and_both_audits_together(
    sqlite_store, fake_package
):
    result = enable(sqlite_store)

    registry, index, audits = snapshot(sqlite_store)
    assert registry["operator_settings_generation"] == 1
    # The install snapshot is present even when the package owns no records yet.
    assert index["value"]["packages"] == {"fixture-package": {
        "id": "fixture-package", "installed_records_version": 2, "installed_records": EMPTY_RECORDS,
    }}
    assert result["operator_settings_generation"] == 1
    assert result["extension_package"] == {
        "id": "fixture-package", "installed_records_version": 2, "installed_records": EMPTY_RECORDS,
    }
    assert sorted(event["action"] for event in audits) == [
        ENABLE_ACTION, "operator.service_registry.update",
    ]


@pytest.mark.parametrize("failure_stage", ["index", "audit"])
def test_extension_enable_failure_leaves_no_partial_durable_state(
    sqlite_store, fake_package, monkeypatch, failure_stage
):
    injected = []
    original_upsert = admin_store._upsert_setting_in_transaction
    original_audit = admin_store._record_audit_in_transaction

    def upsert(conn, key, *args, **kwargs):
        if failure_stage == "index" and key == EXTENSION_PACKAGE_INDEX_SETTING_KEY:
            injected.append("index")
            raise OSError("injected index failure")
        return original_upsert(conn, key, *args, **kwargs)

    def audit(conn, action, **kwargs):
        if failure_stage == "audit" and action == ENABLE_ACTION:
            injected.append("audit")
            raise OSError("injected audit failure")
        return original_audit(conn, action, **kwargs)

    monkeypatch.setattr(admin_store, "_upsert_setting_in_transaction", upsert)
    monkeypatch.setattr(admin_store, "_record_audit_in_transaction", audit)

    before = snapshot(sqlite_store)
    with pytest.raises(OSError, match="injected"):
        enable(sqlite_store)
    assert injected == [failure_stage]
    assert snapshot(sqlite_store) == before
