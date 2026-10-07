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


@pytest.mark.parametrize("kind,value,source", [
    ("email", "person@example.test", "email"), ("domain", "example.test", "domain"),
    ("oidc_group", "/Engineering", "group"), ("oidc_role", "Reader", "role"),
    ("gateway_role", "none", "role"), ("all_users", "*", "gateway_default"),
])
def test_people_effective_plan_matches_session_and_app_authorization(db, monkeypatch, kind, value, source):
    import proxy
    monkeypatch.setattr(proxy, "ADMIN_STORE", AdminSQLiteStore(db))
    monkeypatch.setattr(proxy, "_user_self_service_enabled", lambda: True)
    user = admin_store.upsert_oidc_user("issuer", "subject", email="person@example.test", path=db)
    plan = admin_store.create_profile("Mapped plan", {}, path=db)
    admin_store.put_operator_setting("user_access_defaults", {}, path=db)
    admin_store.replace_user_identity_claims(user["id"], {"oidc_group": ["/Engineering"], "oidc_role": ["Reader"]}, path=db)
    admin_store.create_runtime_profile_mapping(source_kind=kind, source_value=value, profile_id=plan["id"], path=db)
    person = accounts.people(path=db, effective_plan_resolver=proxy._effective_plan_for_person)[0]
    session = session_from_claims({"sub": user["id"], "email": user["email"]}, role="none", cookie=CookieConfig())
    app = admin_store.create_app("App", owner_user_id=user["id"], path=db)
    assert person["profile_id"] == proxy._default_profile_id_for_session(session) == proxy._inherited_profile_id_for_app(app) == plan["id"]
    assert person["plan_source"] == source
    assert person["plan_name"] == "Mapped plan"


def test_assigned_plan_precedes_mappings_and_people_shows_both_grant_sources(db, monkeypatch):
    import proxy
    monkeypatch.setattr(proxy, "ADMIN_STORE", AdminSQLiteStore(db))
    user = admin_store.upsert_oidc_user("issuer", "subject", email="person@example.test", path=db)
    assigned = admin_store.create_profile("Assigned plan", {}, path=db)
    mapped = admin_store.create_profile("Mapped plan", {}, path=db)
    with accounts.connection(db) as conn:
        conn.execute("INSERT INTO account_access(user_id,profile_id) VALUES(?,?)", (user["id"], assigned["id"]))
    admin_store.create_runtime_profile_mapping(source_kind="email", source_value=user["email"], profile_id=mapped["id"], path=db)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=user["id"], path=db)
    admin_store.reconcile_oidc_admin_roles(user["id"], ["admin"], path=db)
    person = accounts.people(path=db, effective_plan_resolver=proxy._effective_plan_for_person)[0]
    assert (person["profile_id"], person["plan_name"], person["plan_source"]) == (assigned["id"], "Assigned plan", "assigned")
    assert {(g["role"], g["source"]) for g in person["grants"]} == {("owner", "manual"), ("admin", "oidc-claim")}


def test_people_without_mapping_reports_gateway_default(db, monkeypatch):
    import proxy
    monkeypatch.setattr(proxy, "ADMIN_STORE", AdminSQLiteStore(db))
    admin_store.upsert_oidc_user("issuer", "subject", path=db)
    admin_store.put_operator_setting("user_access_defaults", {}, path=db)
    person = accounts.people(path=db, effective_plan_resolver=proxy._effective_plan_for_person)[0]
    assert (person["profile_id"], person["plan_source"]) == (None, "gateway_default")
