"""D52 privacy permission is per delegated credential and is granted from a fresh browser session."""
from __future__ import annotations

import sqlite3
import time

from fastapi.responses import JSONResponse

from gateway import admin_runtime, admin_store, content_access, mcp_oauth_store
from gateway.runtime_auth import hash_api_key
from tests.test_mcp_oauth import RESOURCE, World
from tests.test_signin_who_can_enter import OWNER, csrf_for, gateway, signed_in  # noqa: F401

ACTION = {"x-azizam-user-action": "1"}


def _headers(client):
    return {**csrf_for(client), **ACTION}


def _make_app_key(client, gateway):
    headers = _headers(client)
    app = client.post("/me/api/apps", headers=headers, json={"name": "History client"})
    assert app.status_code == 201, app.text
    app_id = app.json()["app"]["id"]
    key = client.post(f"/me/api/apps/{app_id}/keys", headers=headers, json={"label": "History key"})
    assert key.status_code == 201, key.text
    return app_id, key.json()["credential"]


def test_only_the_accountable_person_can_turn_on_an_app_keys_privacy_permission(gateway):
    owner, signed_in_response = signed_in(gateway, sub="owner", email=OWNER)
    assert signed_in_response.status_code == 302
    app_id, key = _make_app_key(owner, gateway)
    assert key["privacy_permission"] is False

    no_action = owner.put(f"/me/api/credentials/{key['id']}/privacy-permission", headers=csrf_for(owner),
                          json={"enabled": True})
    assert no_action.status_code == 403
    assert no_action.json()["error"]["code"] == "user_action_header_required"

    invalid = owner.put(f"/me/api/credentials/{key['id']}/privacy-permission", headers=_headers(owner),
                        json={"enabled": 1})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_privacy_permission"

    enabled = owner.put(f"/me/api/credentials/{key['id']}/privacy-permission", headers=_headers(owner),
                        json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["credential"]["privacy_permission"] is True
    saved = admin_store.get_credential(key["id"], path=gateway.path)
    identity = admin_runtime.runtime_identity_for_app_credential_hash(saved["hash"], path=gateway.path)
    viewer = content_access.viewer_from_auth(identity, owner_keys=())
    assert viewer.privacy_permission is True

    disabled = owner.put(f"/me/api/credentials/{key['id']}/privacy-permission", headers=_headers(owner),
                         json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["credential"]["privacy_permission"] is False


def test_an_owner_role_does_not_let_someone_set_another_persons_key_permission(gateway):
    owner, response = signed_in(gateway, sub="owner", email=OWNER)
    assert response.status_code == 302
    other = admin_store.upsert_oidc_user("https://id.example/realms/home", "other",
                                         email="other@example.test", path=gateway.path)
    app = admin_store.create_app("Other person's App", kind="personal", owner_user_id=other["id"],
                                 access_plan="person", path=gateway.path)
    key = admin_store.create_credential_hash(
        subject_kind="app", subject_id=app["id"], app_id=app["id"], hash=hash_api_key("not-a-real-secret"),
        prefix="opk_test", created_by=other["id"], path=gateway.path,
    )

    refused = owner.put(f"/me/api/credentials/{key['id']}/privacy-permission", headers=_headers(owner),
                        json={"enabled": True})
    assert refused.status_code == 404
    assert admin_store.get_credential(key["id"], path=gateway.path)["privacy_permission"] is False


def test_stale_browser_sign_in_cannot_change_an_app_keys_privacy_permission(gateway):
    client = gateway.client()
    response = gateway.sign_in(client, sub="owner", email=OWNER, auth_time=int(time.time()) - 3_600)
    assert response.status_code == 302
    owner_id = gateway.user(OWNER)["id"]
    app = admin_store.create_app("Stale sign-in App", kind="personal", owner_user_id=owner_id,
                                 access_plan="person", path=gateway.path)
    key = admin_store.create_credential_hash(
        subject_kind="app", subject_id=app["id"], app_id=app["id"], hash=hash_api_key("stale-key-value"),
        prefix="opk_test", created_by=owner_id, path=gateway.path,
    )

    refused = client.put(f"/me/api/credentials/{key['id']}/privacy-permission", headers=_headers(client),
                         json={"enabled": True})
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "reauth_required"
    assert admin_store.get_credential(key["id"], path=gateway.path)["privacy_permission"] is False


def test_existing_app_keys_gain_a_privacy_permission_column_defaulted_off(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    person = admin_store.upsert_oidc_user("https://idp.test", "old-key", path=db)
    app = admin_store.create_app("Old App", kind="personal", owner_user_id=person["id"],
                                 access_plan="person", path=db)
    key = admin_store.create_credential_hash(
        subject_kind="app", subject_id=app["id"], app_id=app["id"], hash=hash_api_key("old-key-value"),
        prefix="opk_test", created_by=person["id"], path=db,
    )
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE credentials DROP COLUMN privacy_permission")
        conn.execute("UPDATE schema_version SET version=17")

    admin_store.init_db(db)

    assert admin_store.schema_version(db) == admin_store.SCHEMA_VERSION >= 18
    assert admin_store.get_credential(key["id"], path=db)["privacy_permission"] is False


def test_existing_mcp_grants_gain_a_privacy_permission_column_defaulted_off(tmp_path):
    db = tmp_path / "admin.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE mcp_oauth_grants (id TEXT PRIMARY KEY, person_id TEXT NOT NULL, client_id TEXT NOT NULL, "
            "client_name TEXT NOT NULL, redirect_uri TEXT NOT NULL, resource TEXT NOT NULL, scope TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, last_used_at INTEGER, revoked_at INTEGER, revoked_reason TEXT)"
        )
        conn.execute("INSERT INTO mcp_oauth_grants VALUES ('grant_old','person','client','Client','https://client/cb',"
                     "'https://gw/mcp','mcp',1,NULL,NULL,NULL)")

    grant = mcp_oauth_store.Store(lambda: db).grant("grant_old")

    assert grant is not None and grant.privacy_permission is False


def test_mcp_connection_permission_is_session_only_and_is_returned_by_connection_list(tmp_path, monkeypatch):
    world = World(tmp_path)
    client_id, tokens = world.connect(person="usr_a")
    grant = world.oauth.verify_access_token(tokens["access_token"], RESOURCE)
    assert grant is not None
    assert grant.privacy_permission is False
    updates = []

    def set_permission(grant_id, *, person_id, enabled, session):
        assert session.user_id == person_id
        with sqlite3.connect(world.db) as conn:
            changed = conn.execute(
                "UPDATE mcp_oauth_grants SET privacy_permission=? WHERE id=? AND person_id=? AND revoked_at IS NULL",
                (int(enabled), grant_id, person_id),
            ).rowcount
        if changed == 1:
            updates.append((grant_id, person_id, enabled))
        return changed == 1

    monkeypatch.setattr(world.store, "set_privacy_permission", set_permission)
    path = f"/me/api/mcp/connections/{grant.grant_id}/privacy-permission"
    no_csrf = world.client.put(path, headers={"x-test-person": "usr_a"}, json={"enabled": True})
    assert no_csrf.status_code == 403
    assert no_csrf.json()["error"]["code"] == "user_csrf_required"

    world.oauth.deps.require_recent_sign_in = lambda _request, _session: JSONResponse(
        {"error": {"code": "reauth_required"}}, status_code=401)
    stale = world.client.put(path, headers={"x-test-person": "usr_a", "x-test-csrf": "ok"},
                             json={"enabled": True})
    assert stale.status_code == 401 and stale.json()["error"]["code"] == "reauth_required"
    world.oauth.deps.require_recent_sign_in = lambda _request, _session: None

    enabled = world.client.put(path, headers={"x-test-person": "usr_a", "x-test-csrf": "ok"},
                               json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["connection"]["privacy_permission"] is True
    listed = world.client.get("/me/api/mcp/connections", headers={"x-test-person": "usr_a"}).json()["connections"]
    assert next(row for row in listed if row["id"] == grant.grant_id)["privacy_permission"] is True
    assert world.oauth.verify_access_token(tokens["access_token"], RESOURCE).privacy_permission is True

    foreign = world.client.put(path, headers={"x-test-person": "usr_b", "x-test-csrf": "ok"},
                               json={"enabled": False})
    assert foreign.status_code == 404
    assert updates == [(grant.grant_id, "usr_a", True)]
