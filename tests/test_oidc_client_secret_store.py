"""The OIDC client secret is write-only and lives in the encrypted secret store."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest

from gateway import admin_store, secret_store
from gateway.admin_api import AdminContext
from gateway.admin_runtime import OIDC_PROVIDER_SETTING_KEY, AdminSQLiteStore

FAKE_SECRET = "fake-oidc-secret-for-tests-1"
OWNER = AdminContext(subject="owner", roles=frozenset({"owner"}))


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    return AdminSQLiteStore(path)


def stored_value(store):
    record = admin_store.get_operator_setting(OIDC_PROVIDER_SETTING_KEY, path=store.path)
    assert record is not None
    return record["value"]


def secret_rows(store):
    secret_store.ensure_provider_secret_schema(store.path)
    with closing(sqlite3.connect(store.path)) as conn:
        return conn.execute("SELECT id FROM provider_secrets WHERE id LIKE 'oidc-%' ORDER BY id").fetchall()


def state_bytes(store):
    return b"".join(
        candidate.read_bytes()
        for candidate in store.path.parent.iterdir()
        if candidate.name.startswith(store.path.name)
    )


def test_saved_secret_is_stored_encrypted_and_settings_keep_only_a_reference(store):
    response = store.update_oidc_config_setting(data={"enabled": False, "client_secret": FAKE_SECRET}, actor=OWNER)
    assert response["client_secret_set"] is True
    assert FAKE_SECRET not in repr(response)
    value = stored_value(store)
    assert "client_secret" not in value
    assert value["client_secret_ref"].startswith("oidc-client-secret-")
    assert FAKE_SECRET.encode() not in state_bytes(store)
    assert store.get_oidc_config_setting_raw()["client_secret"] == FAKE_SECRET
    assert "client_secret_ref" not in store.get_oidc_config_setting_raw()
    assert store.get_oidc_config_setting()["client_secret"] == "<redacted>"


def test_rotation_replaces_the_stored_secret_and_blank_keeps_it(store):
    store.update_oidc_config_setting(data={"client_secret": FAKE_SECRET}, actor=OWNER)
    first = stored_value(store)["client_secret_ref"]
    store.update_oidc_config_setting(data={"client_secret": "", "client_id": "client-b"}, actor=OWNER)
    assert stored_value(store)["client_secret_ref"] == first
    store.update_oidc_config_setting(data={"client_secret": "fake-rotated-secret-2"}, actor=OWNER)
    second = stored_value(store)["client_secret_ref"]
    assert second != first
    assert secret_rows(store) == [(second,)]
    assert store.get_oidc_config_setting_raw()["client_secret"] == "fake-rotated-secret-2"
    audits = admin_store.list_audit(action_prefix="operator.oidc_provider_config.update", path=store.path)
    assert sorted(event["metadata"]["secret_changed"] for event in audits) == [False, True, True]


def test_rejected_settings_write_keeps_the_current_secret(store):
    store.update_oidc_config_setting(data={"client_secret": FAKE_SECRET}, actor=OWNER)
    ref = stored_value(store)["client_secret_ref"]
    with patch.object(admin_store, "_record_audit_in_transaction", side_effect=RuntimeError("injected")):
        with pytest.raises(RuntimeError):
            store.update_oidc_config_setting(data={"client_secret": "fake-unsaved-secret"}, actor=OWNER)
    assert stored_value(store)["client_secret_ref"] == ref
    assert secret_rows(store) == [(ref,)]
    assert store.get_oidc_config_setting_raw()["client_secret"] == FAKE_SECRET


def test_plaintext_written_behind_the_store_is_not_used(store):
    admin_store.put_operator_setting(OIDC_PROVIDER_SETTING_KEY, {"client_secret": FAKE_SECRET}, path=store.path)
    assert "client_secret" not in store.get_oidc_config_setting_raw()


def test_startup_moves_a_plaintext_secret_into_the_store_once(store):
    admin_store.put_operator_setting(
        OIDC_PROVIDER_SETTING_KEY,
        {"enabled": True, "issuer": "https://idp.example.test", "client_secret": FAKE_SECRET},
        path=store.path,
    )
    admin_store.init_db(store.path)
    value = stored_value(store)
    assert value == {"enabled": True, "issuer": "https://idp.example.test", "client_secret_ref": "oidc-client-secret"}
    assert store.get_oidc_config_setting_raw()["client_secret"] == FAKE_SECRET
    assert store.get_oidc_config_setting()["client_secret_set"] is True
    assert FAKE_SECRET.encode() not in state_bytes(store)
    revision = store.setting_revision(OIDC_PROVIDER_SETTING_KEY)

    admin_store.init_db(store.path)
    assert stored_value(store) == value
    assert store.setting_revision(OIDC_PROVIDER_SETTING_KEY) == revision
    moved = admin_store.list_audit(action_prefix="operator.oidc_provider_config.secret_moved", path=store.path)
    assert len(moved) == 1 and moved[0]["metadata"] == {"secret_ref": "oidc-client-secret"}


def test_startup_keeps_an_empty_secret_override(store):
    admin_store.put_operator_setting(OIDC_PROVIDER_SETTING_KEY, {"client_secret": ""}, path=store.path)
    admin_store.init_db(store.path)
    assert stored_value(store) == {"client_secret": ""}
    assert secret_rows(store) == []
    assert store.get_oidc_config_setting()["client_secret_set"] is False
