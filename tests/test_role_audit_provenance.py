"""Role changes commit their audit evidence and preserve the real actor."""
import sqlite3
from tests.support.app_keys import accountable_person

import pytest

from gateway import accounts, admin_store
from gateway.admin_api import AdminContext
from gateway.admin_runtime import AdminSQLiteStore, runtime_identity_for_app_credential_hash


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    return path


def test_claim_grants_revocations_have_transactional_provenance_and_no_duplicate_events(db):
    user = admin_store.upsert_oidc_user("issuer", "subject", path=db)
    manual = admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=user["id"], path=db)
    admin_store.reconcile_oidc_admin_roles(user["id"], ["owner", "admin"], path=db)
    admin_store.reconcile_oidc_admin_roles(user["id"], ["owner", "admin"], path=db)
    admin_store.reconcile_oidc_admin_roles(user["id"], [], path=db)
    admin_store.reconcile_oidc_admin_roles(user["id"], [], path=db)
    events = admin_store.list_audit(path=db)
    assert sorted((e["action"], e["metadata"]["provenance"]) for e in events) == [
        ("admin_role.grant", "manual"), ("admin_role.grant", "oidc-claim"),
        ("admin_role.revoke", "oidc-claim"),
    ]
    assert admin_store.list_admin_roles(path=db)[0]["id"] == manual["id"]
    with accounts.connection(db) as conn:
        conn.execute("CREATE TRIGGER fail_role_audit BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT,'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        admin_store.reconcile_oidc_admin_roles(user["id"], ["admin"], path=db)
    assert len(admin_store.list_admin_roles(path=db)) == 1
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        admin_store.revoke_admin_role(manual["id"], path=db)
    assert len(admin_store.list_admin_roles(path=db)) == 1


def test_manual_role_audit_preserves_service_actor_and_rolls_back_failed_grant(db):
    user = admin_store.upsert_oidc_user("issuer", "subject", path=db)
    actor = AdminContext(subject="svc_operator", roles=frozenset({"owner"}), actor_kind="service_account", actor_label="Automation")
    grant = admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=user["id"], actor=actor, path=db)
    admin_store.revoke_admin_role(grant["id"], actor=actor, path=db)
    assert {(e["actor_kind"], e["actor_id"], e["actor_label"]) for e in admin_store.list_audit(path=db)} == {
        ("service_account", "svc_operator", "Automation")}
    with accounts.connection(db) as conn:
        conn.execute("CREATE TRIGGER fail_role_audit BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT,'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=user["id"], actor=actor, path=db)
    assert admin_store.list_admin_roles(path=db) == []


def test_new_app_credential_for_suspended_owner_cannot_authenticate(db):
    store = AdminSQLiteStore(db)
    user = admin_store.upsert_oidc_user('fixture', 'suspended', path=store.path)
    app = admin_store.create_app(kind='personal', name='fixture', owner_user_id=user['id'], access_plan="person", path=store.path)
    admin_store.create_credential_hash(subject_kind='app', subject_id=app['id'], app_id=app['id'], hash='fixture-key-hash', prefix='fixture', path=store.path, created_by=accountable_person(store.path, app['id']))
    assert runtime_identity_for_app_credential_hash('fixture-key-hash', path=store.path) is not None
    with admin_store._conn(store.path) as conn:
        conn.execute('UPDATE account_access SET disabled_at = ? WHERE user_id = ?', (123, user['id']))
        conn.commit()
    refused = runtime_identity_for_app_credential_hash('fixture-key-hash', path=store.path)
    assert refused is not None and refused.denial == 'person_disabled'
    assert admin_store.get_app(app['id'], path=store.path)['disabled_at'] is None
