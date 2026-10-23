"""Owner visibility step 2: an App's keys and trusted addresses are added by its accountable person only.

The owner role is no longer enough to add a binding to an App someone else answers for. Owner powers
over plans and over disabling Apps are unchanged (tests/test_admin_cannot_act_as_owner.py). A
control-plane key holds no person, so it adds no binding for any App.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import accounts, admin_store, trusted_addresses, user_runtime  # noqa: E402
from gateway.access_plans import ALL, CAPABILITIES  # noqa: E402
from gateway.admin_api import AdminContext, create_admin_router  # noqa: E402
from gateway.admin_oidc import AdminSession  # noqa: E402
from gateway.admin_runtime import AdminSQLiteStore  # noqa: E402
from gateway.user_runtime import UserPortalError  # noqa: E402

PLAN = {
    "name": "Local only",
    "can_use": {capability: {"items": ALL} for capability in CAPABILITIES},
    "direct_engine_access": {"engines": [], "operations": []},
    "paid_providers": [],
    "provider_fallback": False,
    "caps": {"usd_per_request": None, "usd_per_day": None, "usd_per_month": None},
    "can_load_models": True,
    "receipt_detail": "basic",
    "media_metadata": "none",
}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    return path


def _person(path: Path, email: str) -> dict:
    user = admin_store.upsert_oidc_user("https://sso.test", uuid.uuid4().hex, email=email,
                                        display_name=email, path=path)
    plan = admin_store.create_access_plan({**PLAN, "name": f"plan {uuid.uuid4().hex[:6]}"}, path=path)
    accounts.change_person(user["id"], "test", plan_id=plan["id"], path=path)
    return user


def _app(path: Path, owner_id: str, *, kind: str = "personal") -> dict:
    return admin_store.create_app("Desk", kind=kind, owner_user_id=owner_id, access_plan="person", path=path)


def _person_actor(user: dict, *roles: str) -> AdminContext:
    return AdminContext(subject=user["id"], email=user["email"], actor_kind="user", actor_id=user["id"],
                        roles=frozenset(roles), auth_method="bearer")


def _control_plane_actor() -> AdminContext:
    return AdminContext(subject="svc_owner_automation", actor_kind="service_account",
                        actor_id="svc_owner_automation", roles=frozenset({"owner"}), auth_method="bearer")


def _client(path: Path, actor: AdminContext) -> TestClient:
    api = FastAPI()
    api.include_router(create_admin_router(AdminSQLiteStore(path), auth=lambda request: actor))
    return TestClient(api)


def _app_keys(path: Path, app_id: str) -> list[dict]:
    return [row for row in admin_store.list_credentials(path=path)
            if row.get("subject_kind") == "app" and row.get("subject_id") == app_id]


def _session(user: dict) -> AdminSession:
    return AdminSession(subject=user["id"], email=user["email"], role="none",
                        issued_at=1, expires_at=4_000_000_000)


def test_the_owner_role_cannot_issue_a_key_for_an_app_someone_else_answers_for(db):
    owner, member = _person(db, "owner@example.test"), _person(db, "member@example.test")
    app = _app(db, member["id"])
    client = _client(db, _person_actor(owner, "owner"))

    response = client.post(f"/admin/api/apps/{app['id']}/keys", json={"label": "Desktop"})

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "not_your_app"
    assert _app_keys(db, app["id"]) == []


def test_the_accountable_person_issues_a_key_for_their_app_with_themselves_as_creator(db):
    member = _person(db, "member@example.test")
    app = _app(db, member["id"])
    client = _client(db, _person_actor(member, "admin"))

    response = client.post(f"/admin/api/apps/{app['id']}/keys", json={"label": "Desktop"})

    assert response.status_code == 201, response.text
    [key] = _app_keys(db, app["id"])
    assert key["created_by"] == member["id"]


def test_a_control_plane_key_issues_no_app_key_even_for_the_owners_own_app(db):
    owner = _person(db, "owner@example.test")
    app = _app(db, owner["id"])
    client = _client(db, _control_plane_actor())

    response = client.post(f"/admin/api/apps/{app['id']}/keys", json={"label": "Automation"})

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "not_your_app"
    assert _app_keys(db, app["id"]) == []


def test_the_owner_role_cannot_trust_an_address_for_an_app_someone_else_answers_for(db):
    owner, member = _person(db, "owner@example.test"), _person(db, "member@example.test")
    app = _app(db, member["id"])
    client = _client(db, _person_actor(owner, "owner"))

    response = client.post(f"/admin/api/apps/{app['id']}/trusted-addresses", json={"address": "10.0.0.1"})

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "not_your_app"
    assert admin_store.list_trusted_addresses(app["id"], path=db) == []


def test_the_accountable_person_trusts_an_address_for_their_app_with_themselves_as_creator(db):
    member = _person(db, "member@example.test")
    app = _app(db, member["id"])
    client = _client(db, _person_actor(member, "admin"))

    response = client.post(f"/admin/api/apps/{app['id']}/trusted-addresses", json={"address": "10.0.0.1"})

    assert response.status_code == 201, response.text
    [entry] = admin_store.list_trusted_addresses(app["id"], path=db)
    assert entry["created_by"] == member["id"]


def test_a_control_plane_key_trusts_no_address(db):
    owner = _person(db, "owner@example.test")
    app = _app(db, owner["id"])
    client = _client(db, _control_plane_actor())

    response = client.post(f"/admin/api/apps/{app['id']}/trusted-addresses", json={"address": "10.0.0.1"})

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "not_your_app"
    assert admin_store.list_trusted_addresses(app["id"], path=db) == []


def test_a_person_trusts_an_address_for_their_own_app_from_their_signed_in_session(db):
    member = _person(db, "member@example.test")
    app = _app(db, member["id"])

    record = user_runtime.add_user_trusted_address(
        _session(member), app["id"], {"address": "10.0.0.1", "note": "den"}, path=db,
    )

    assert record["address"] == "10.0.0.1"
    assert record["created_by"] == member["id"]


def test_a_person_cannot_trust_an_address_for_an_app_they_do_not_answer_for(db):
    owner, member = _person(db, "owner@example.test"), _person(db, "member@example.test")
    app = _app(db, owner["id"])

    with pytest.raises(UserPortalError) as refused:
        user_runtime.add_user_trusted_address(_session(member), app["id"], {"address": "10.0.0.1"}, path=db)

    assert refused.value.code == "not_your_app"
    assert refused.value.status_code == 403
    assert admin_store.list_trusted_addresses(app["id"], path=db) == []


def test_the_address_is_normalized_the_same_way_as_the_admin_route(db):
    member = _person(db, "member@example.test")
    app = _app(db, member["id"])

    record = user_runtime.add_user_trusted_address(_session(member), app["id"], {"address": "10.0.0.1"}, path=db)

    assert record["address"] == trusted_addresses.normalize_entry("10.0.0.1")
