"""Authorization and state-permission regressions; all databases are temporary."""
from __future__ import annotations

import importlib.util
from contextlib import closing
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import admin_store
from gateway.admin_api import AdminContext, create_admin_router
from gateway.admin_oidc import CookieConfig, encode_session_cookie, session_from_claims
from gateway.admin_runtime import (
    AdminAuthenticator, AdminSQLiteStore, create_admin_service_account_key,
    service_account_session_subject,
)
from tests.support.access import store_plan


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state" / "admin.db"
    admin_store.init_db(path)
    return path


def client_for(db: Path, role: str) -> TestClient:
    key, _, _ = create_admin_service_account_key(f"issuer-{role}", role=role, path=db)
    store = AdminSQLiteStore(db)
    app = FastAPI()
    app.include_router(create_admin_router(store, auth=AdminAuthenticator(store)))
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {key}"
    return client


def test_sec05_name_collision_cannot_issue_owner_principal_key(db):
    create_admin_service_account_key("release-bot", role="owner", path=db)
    client = client_for(db, "admin")
    before_credentials = admin_store.list_credentials(path=db)
    before_roles = admin_store.list_admin_roles(path=db)
    response = client.post("/admin/api/admin-keys", json={"name": "release-bot", "role": "admin"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "admin_key_name_taken"
    assert admin_store.list_credentials(path=db) == before_credentials
    assert admin_store.list_admin_roles(path=db) == before_roles


def test_sec05_runtime_authorization_precedes_new_identity_mutation(db):
    actor = AdminContext(subject="admin", roles=frozenset({"admin"}))
    with pytest.raises(PermissionError):
        AdminSQLiteStore(db).create_admin_automation_key(data={"name": "new-owner", "role": "owner"}, actor=actor)
    with admin_store._conn(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM service_accounts").fetchone()[0] == 0
    assert not admin_store.list_credentials(path=db)


def test_sec05_nobody_reuses_an_existing_key_name_not_even_the_owner(db):
    """A dashboard key always gets its own account; reusing one by name was how a key was taken over."""
    create_admin_service_account_key("release-bot", role="owner", path=db)
    response = client_for(db, "owner").post("/admin/api/admin-keys", json={"name": "release-bot", "role": "admin"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "admin_key_name_taken"


def test_sec06_oidc_trust_changes_require_owner(db):
    admin = client_for(db, "admin")
    settings = admin.get("/admin/api/oidc-config")
    assert settings.status_code == 200
    headers = {"If-Match": settings.json()["revision"]}
    assert admin.put("/admin/api/oidc-config", json={"enabled": False}, headers=headers).status_code == 403
    assert AdminSQLiteStore(db).get_oidc_config_setting() is None
    owner = client_for(db, "owner")
    assert owner.put("/admin/api/oidc-config", json={"enabled": False}, headers=headers).status_code == 200


def test_sec08_oidc_grants_are_removed_but_manual_grants_survive(db):
    user = admin_store.upsert_oidc_user("https://idp.test/realm", "identity", path=db)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=user["id"], path=db)
    admin_store.reconcile_oidc_admin_roles(user["id"], {"admin"}, now=100, path=db)
    rows = admin_store.list_admin_roles(subject_id=user["id"], path=db)
    assert {(r["role"], r["provenance"]) for r in rows} == {("owner", "manual"), ("admin", "oidc")}
    rows = admin_store.reconcile_oidc_admin_roles(user["id"], set(), now=101, path=db)
    assert {(r["role"], r["provenance"]) for r in rows} == {("owner", "manual")}
    rows = admin_store.reconcile_oidc_admin_roles(user["id"], {"admin"}, now=102, path=db)
    assert {(r["role"], r["provenance"]) for r in rows} == {("owner", "manual"), ("admin", "oidc")}


def test_sec08_existing_owner_identity_survives_idempotent_migration_and_login(tmp_path):
    db = tmp_path / "admin.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(admin_store._SCHEMA.replace("  provenance TEXT NOT NULL DEFAULT 'manual',\n", ""))
        conn.execute("INSERT INTO schema_version VALUES (3)")
        conn.execute("INSERT INTO users(id,issuer,subject,email,created_at) VALUES ('existing-owner','https://idp.test/realm','owner-sub','owner@example.test',1)")
        conn.execute("INSERT INTO admin_role_assignments(role,subject_kind,subject_id,granted_at) VALUES ('owner','user','existing-owner',1)")
    admin_store.init_db(db)
    admin_store.init_db(db)
    user = admin_store.upsert_oidc_user("https://idp.test/realm", "owner-sub", email="changed@example.test", path=db)
    assert user["id"] == "existing-owner"
    rows = admin_store.reconcile_oidc_admin_roles(user["id"], set(), path=db)
    assert [(r["role"], r["provenance"]) for r in rows] == [("owner", "manual")]
    signing_key = b"s" * 32
    cookie = CookieConfig(secure=False)
    session = session_from_claims({"sub": user["id"], "email": ""}, role="owner", cookie=cookie)
    app = FastAPI()
    store = AdminSQLiteStore(db)
    app.include_router(create_admin_router(store, auth=AdminAuthenticator(store, signing_key=signing_key, cookie=cookie)))
    client = TestClient(app)
    client.cookies.set(cookie.name, encode_session_cookie(session, signing_key=signing_key))
    assert client.get("/admin/api/session").status_code == 200


def test_sec14_db_and_new_sidecars_are_private(db):
    assert db.parent.stat().st_mode & 0o777 == 0o700
    assert db.stat().st_mode & 0o777 == 0o600
    with admin_store._conn(db) as conn:
        conn.execute("INSERT INTO schema_version VALUES (4)")
        conn.commit()
        for sidecar in (Path(f"{db}-wal"), Path(f"{db}-shm")):
            assert sidecar.exists()
            assert sidecar.stat().st_mode & 0o777 == 0o600


def test_sec14_existing_state_permissions_are_tightened(db):
    db.parent.chmod(0o755)
    db.chmod(0o644)
    with admin_store._conn(db) as conn:
        conn.execute("INSERT INTO schema_version VALUES (4)")
        conn.commit()
        for sidecar in (Path(f"{db}-wal"), Path(f"{db}-shm")):
            sidecar.chmod(0o644)
        admin_store.secure_state_path(db)
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in (db, Path(f"{db}-wal"), Path(f"{db}-shm")))
    assert db.parent.stat().st_mode & 0o777 == 0o700


def test_sec14_registry_backup_is_private(db):
    script = Path(__file__).resolve().parents[1] / "scripts/local/register_service_from_config.py"
    spec = importlib.util.spec_from_file_location("azizam_register_service", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backup = module._backup(db)
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("service_account", [False, True])
def test_sec10_admin_auth_rejects_revoked_legacy_and_service_sessions(db, service_account):
    from gateway import accounts

    accounts.initialize(db)
    if service_account:
        principal = admin_store.ensure_service_account("browser-bot", path=db)
        kind = "service_account"
        subject = service_account_session_subject(principal["id"])
    else:
        principal = admin_store.upsert_oidc_user("https://idp.test", "legacy", path=db)
        kind = "user"
        subject = principal["id"]
    admin_store.assign_admin_role(role="owner", subject_kind=kind, subject_id=principal["id"], path=db)
    signing_key = b"s" * 32
    cookie = CookieConfig(secure=False)
    session = session_from_claims({"sub": subject, "email": ""}, role="owner", cookie=cookie)
    app = FastAPI()
    store = AdminSQLiteStore(db)
    app.include_router(create_admin_router(store, auth=AdminAuthenticator(store, signing_key=signing_key, cookie=cookie)))
    client = TestClient(app)
    client.cookies.set(cookie.name, encode_session_cookie(session, signing_key=signing_key))
    assert client.get("/admin/api/session").status_code == 200
    accounts.revoke_session(session.session_id, session.expires_at, path=db)
    assert client.get("/admin/api/session").status_code == 401


def test_sec07_migration_discards_folded_claims_and_keeps_fresh_exact_claims(tmp_path):
    db = tmp_path / "admin.db"
    schema_v6 = (Path(__file__).resolve().parent / "fixtures" / "admin_db_schema_v6.sql").read_text(encoding="utf-8")
    with sqlite3.connect(db) as conn:
        conn.executescript(schema_v6)
        conn.execute("INSERT INTO schema_version VALUES (3)")
        conn.execute("INSERT INTO users(id,issuer,subject,email,created_at) VALUES ('member','https://idp.test','member-sub','member@example.test',1)")
        # The old mapper stored both the full group path and its leaf alias.
        conn.executemany("INSERT INTO user_identity_claims VALUES ('member','oidc_group',?,1)",
                         [("untrusted/admins",), ("admins",)])
    admin_store.init_db(db)
    assert admin_store.list_user_identity_claims("member", path=db) == []
    admin_store.replace_user_identity_claims("member", {"oidc_group": ["/untrusted/admins"]}, path=db)
    admin_store.init_db(db)  # A repeated migration must not remove fresh exact claims.
    claims = admin_store.list_user_identity_claims("member", path=db)
    assert [row["claim_value"] for row in claims] == ["/untrusted/admins"]


def _grants_table_exists(db):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_identity_profile_grants'").fetchone() is not None


def _add_grants_table(db, *rows):
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE legacy_identity_profile_grants (user_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL)")
        conn.executemany("INSERT INTO legacy_identity_profile_grants VALUES (?,?)", rows)
        conn.commit()


def test_new_database_has_no_identity_profile_grants_table(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    assert not _grants_table_exists(db)


def test_empty_identity_profile_grants_table_is_dropped(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    _add_grants_table(db)
    admin_store.init_db(db)
    assert not _grants_table_exists(db)
    admin_store.init_db(db)  # Idempotent once the table is gone.
    from gateway import accounts
    accounts.initialize(db)
    user = admin_store.upsert_oidc_user("https://idp.test", "member", email="member@example.test", path=db)
    plan = store_plan("Plan", path=db)
    assert accounts.plan_for_user(user["id"], path=db) is None
    accounts.change_person(user["id"], "owner", plan_id=plan["id"], path=db)
    assert accounts.plan_for_user(user["id"], path=db) == plan["id"]
    assert accounts.people(path=db)[0]["plan_id"] == plan["id"]
    with pytest.raises(ValueError, match=r"1 person \(member@example.test\)"):
        admin_store.delete_access_plan(plan["id"], path=db)
    accounts.change_person(user["id"], "owner", remove=True, path=db)
    assert admin_store.delete_access_plan(plan["id"], path=db)["id"] == plan["id"]


def test_identity_profile_grants_with_rows_stop_startup_and_stay_intact(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    _add_grants_table(db, ("member", "plan"))
    with pytest.raises(sqlite3.DatabaseError, match="still holds 1 access-plan grant"):
        admin_store.init_db(db)
    assert _grants_table_exists(db)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT user_id, profile_id FROM legacy_identity_profile_grants").fetchall() == [("member", "plan")]
