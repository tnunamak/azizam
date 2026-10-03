"""Release gate: two members sharing one access plan retain separate data.

HTTP tests use the production app, authentication and durable stores. Every
store connection is redirected to a per-test database, including providers
whose default path was captured when the production router was registered.
No lifespan is started and no backend request is made.
"""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import proxy
from gateway import admin_store, media_jobs, receipt_store
from gateway.admin_oidc import AdminSession
from gateway.api import create_gateway_router
from gateway.discovery import WorkflowRegistry
from gateway.receipts import RouteReceipt
from gateway.user_runtime import mint_user_app_key


@pytest.fixture
def people(monkeypatch, tmp_path):
    paths = {}
    for module, name in (
        (admin_store, "admin"), (receipt_store, "receipts"), (media_jobs, "jobs")
    ):
        path = tmp_path / f"{name}.db"
        module.init_db(path)
        connect = module._conn
        monkeypatch.setattr(module, "_conn", lambda _path, fn=connect, db=path: fn(db))
        monkeypatch.setattr(module, "DB_PATH", path)
        paths[name] = path

    monkeypatch.setattr(proxy.ADMIN_STORE, "path", paths["admin"])
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "_client_is_lan", lambda _request: False)
    monkeypatch.setattr(proxy, "_gateway_audience_resolver", lambda _request: "client")
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "assets")
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"people-isolation-test")
    monkeypatch.setattr(proxy, "_user_self_service_enabled", lambda: True)
    monkeypatch.setattr(proxy, "_default_profile_id_for_session", lambda _session: None)
    monkeypatch.setattr(proxy, "_csrf_token_for_oidc_session", lambda _session: "fixture-csrf")
    # Policy is exercised by its own suite. This gate checks ownership even
    # when both members have exactly the same permissions.
    monkeypatch.setattr(proxy, "_enforce_runtime_route_policy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(proxy, "_media_job_content_preflight_response", lambda _auth: None)

    members = {}
    for name, color in (("alice", (10, 20, 30)), ("bob", (40, 50, 60))):
        user = admin_store.upsert_oidc_user(
            "https://people.test", name, email=f"{name}@example.test", path=paths["admin"]
        )
        session = AdminSession(
            subject=user["id"], email=user["email"], role="none",
            issued_at=1, expires_at=4_000_000_000,
        )
        # Matching App names and plans are intentional: neither is an owner id.
        app = admin_store.create_app(
            "Phone", kind="personal", owner_user_id=user["id"], path=paths["admin"]
        )
        key = mint_user_app_key(session, app["id"], path=paths["admin"])
        bearer = {"Authorization": f"Bearer {key['api_key']}"}
        browser = {
            "X-Test-Member": name, "X-AI-Gateway-User-Action": "1",
            "X-AI-Gateway-Csrf": "fixture-csrf",
        }
        receipt = RouteReceipt(
            protocol="openai.images", intent="image.generate", app_id=app["id"],
            app_profile="default-local", api_key_name="Phone", status="succeeded",
        )
        receipt_store.record(receipt, path=paths["receipts"])
        output = io.BytesIO()
        Image.new("RGB", (2, 2), color).save(output, format="PNG")
        raw = output.getvalue()
        asset_id = proxy.store_image_bytes(raw, owner_key_hash=proxy.hash_api_key(key["api_key"]))
        job = media_jobs.create(media_jobs.MediaJob(
            intent="image.generate", request_id=receipt.request_id,
            parent_request_id=receipt.request_id, api_key_name="Phone",
            app_profile="default-local", app_id=app["id"], assets=[{"asset_id": asset_id}],
        ), path=paths["jobs"])
        media_jobs.append_event(job.job_id, "queued", {"progress": 0}, path=paths["jobs"])
        members[name] = SimpleNamespace(
            user=user, session=session, app=app, key=key, bearer=bearer,
            browser=browser, receipt_id=receipt.request_id, job_id=job.job_id,
            asset_id=asset_id, raw=raw,
        )

    monkeypatch.setattr(proxy, "_oidc_session_from_request", lambda request: (
        members[request.headers["x-test-member"]].session
        if request.headers.get("x-test-member") in members else None
    ))
    return SimpleNamespace(
        **members, client=TestClient(proxy.app), paths=paths, monkeypatch=monkeypatch
    )


def test_member_account_lists_only_owned_apps_and_keys(people):
    response = people.client.get("/me/api/session", headers=people.alice.browser)
    assert response.status_code == 200
    apps = response.json()["apps"]
    assert {app["id"] for app in apps} == {people.alice.app["id"]}
    assert {key["id"] for app in apps for key in app["credentials"]} == {people.alice.key["id"]}
    assert people.bob.app["id"] not in response.text
    assert people.bob.key["id"] not in response.text
    assert "hash" not in response.text


@pytest.mark.parametrize("action", ["delete_app", "mint_key", "read_key", "revoke_key"])
def test_member_cannot_read_or_modify_another_members_apps_or_keys(people, action):
    client, bob, alice = people.client, people.bob, people.alice
    before_app = admin_store.get_app(bob.app["id"])
    before_key = admin_store.get_credential(bob.key["id"])
    before_credentials = admin_store.list_active_credentials(app_id=bob.app["id"])
    if action == "delete_app":
        response = client.delete(f"/me/api/apps/{bob.app['id']}", headers=alice.browser)
    elif action == "mint_key":
        response = client.post(f"/me/api/apps/{bob.app['id']}/keys", headers=alice.browser, json={})
    elif action == "read_key":
        response = client.get(f"/me/api/credentials/{bob.key['id']}/access", headers=alice.browser)
    else:
        response = client.delete(f"/me/api/credentials/{bob.key['id']}", headers=alice.browser)
    assert response.status_code == 404, response.text
    assert admin_store.get_app(bob.app["id"]) == before_app
    assert admin_store.get_credential(bob.key["id"]) == before_key
    assert admin_store.list_active_credentials(app_id=bob.app["id"]) == before_credentials


def test_member_browser_activity_and_receipt_detail_are_owner_scoped(people):
    response = people.client.get("/me/api/activity", headers=people.alice.browser)
    assert response.status_code == 200
    assert {row["request_id"] for row in response.json()["receipts"]} == {people.alice.receipt_id}
    own = people.client.get(f"/me/api/receipts/{people.alice.receipt_id}", headers=people.alice.browser)
    assert own.status_code == 200
    foreign = people.client.get(f"/me/api/receipts/{people.bob.receipt_id}", headers=people.alice.browser)
    missing = people.client.get("/me/api/receipts/req_unknown", headers=people.alice.browser)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


@pytest.mark.parametrize("resource,id_field", [("receipts", "request_id"), ("media/jobs", "job_id")])
def test_gateway_lists_and_details_do_not_cross_member_boundary(people, resource, id_field):
    alice, bob = people.alice, people.bob
    own_id = alice.receipt_id if resource == "receipts" else alice.job_id
    foreign_id = bob.receipt_id if resource == "receipts" else bob.job_id
    response = people.client.get(f"/gateway/v1/{resource}", headers=alice.bearer)
    assert response.status_code == 200, response.text
    list_field = "receipts" if resource == "receipts" else "jobs"
    assert {row[id_field] for row in response.json()[list_field]} == {own_id}
    assert "api_key_name" not in response.text
    assert "metadata" not in response.text
    assert people.client.get(f"/gateway/v1/{resource}/{own_id}", headers=alice.bearer).status_code == 200
    for record_id in (foreign_id, "unknown"):
        denied = people.client.get(f"/gateway/v1/{resource}/{record_id}", headers=alice.bearer)
        assert denied.status_code == 404, denied.text
    # A caller-supplied plan filter only narrows scope. Shared plans must not
    # grant access to another member's records.
    filtered = people.client.get(
        f"/gateway/v1/{resource}?app_profile=default-local", headers=alice.bearer
    )
    assert {row[id_field] for row in filtered.json()[list_field]} == {own_id}


@pytest.mark.parametrize("action", ["read_events", "append_event", "cancel", "content"])
def test_member_cannot_read_modify_or_mint_urls_for_another_members_job(people, action):
    path = f"/gateway/v1/media/jobs/{people.bob.job_id}"
    before = media_jobs.get(people.bob.job_id, audience="internal")
    before_events = media_jobs.events(people.bob.job_id, audience="internal")
    if action == "read_events":
        response = people.client.get(path + "/events", headers=people.alice.bearer)
    elif action == "append_event":
        response = people.client.post(path + "/events", headers=people.alice.bearer,
                                      json={"event_type": "progress", "payload": {"progress": 99}})
    elif action == "cancel":
        response = people.client.post(path + "/cancel", headers=people.alice.bearer, json={})
    else:
        media_jobs.update_status(people.bob.job_id, media_jobs.STATUS_RUNNING)
        media_jobs.update_status(people.bob.job_id, media_jobs.STATUS_SUCCEEDED)
        before = media_jobs.get(people.bob.job_id, audience="internal")
        response = people.client.get(path + "/content", headers=people.alice.bearer)
    assert response.status_code == 404, response.text
    assert "sig=" not in response.text
    assert media_jobs.get(people.bob.job_id, audience="internal") == before
    assert media_jobs.events(people.bob.job_id, audience="internal") == before_events


def test_member_can_read_and_cancel_owned_job(people):
    path = f"/gateway/v1/media/jobs/{people.alice.job_id}"
    events = people.client.get(path + "/events", headers=people.alice.bearer)
    assert events.status_code == 200
    assert events.json()["events"]
    response = people.client.post(path + "/cancel", headers=people.alice.bearer, json={})
    assert response.status_code == 200, response.text
    assert media_jobs.get(people.alice.job_id)["status"] == media_jobs.STATUS_CANCELED
    assert media_jobs.get(people.bob.job_id)["status"] == media_jobs.STATUS_QUEUED


def test_owned_job_access_survives_receipt_retention_and_status_updates(people):
    media_jobs.update_status(people.alice.job_id, media_jobs.STATUS_RUNNING)
    with receipt_store._conn(people.paths["receipts"]) as connection:
        connection.execute("DELETE FROM receipts WHERE request_id = ?", (people.alice.receipt_id,))
        connection.commit()
    stored = media_jobs.get(people.alice.job_id, audience="internal")
    assert stored["app_id"] == people.alice.app["id"]
    path = f"/gateway/v1/media/jobs/{people.alice.job_id}"
    assert people.client.get(path, headers=people.alice.bearer).status_code == 200
    assert people.client.get(path, headers=people.bob.bearer).status_code == 404
    canceled = people.client.post(path + "/cancel", headers=people.alice.bearer, json={})
    assert canceled.status_code == 200
    assert media_jobs.get(people.alice.job_id, audience="internal")["app_id"] == people.alice.app["id"]


def test_member_cannot_forge_job_ownership_with_raw_creation(people):
    before = media_jobs.recent(audience="internal")
    response = people.client.post("/gateway/v1/media/jobs", headers=people.alice.bearer, json={
        "intent": "unknown.generate", "parent_request_id": people.bob.receipt_id,
        "api_key_name": "Phone", "assets": [{"asset_id": people.bob.asset_id}],
    })
    assert response.status_code == 400, response.text
    assert media_jobs.recent(audience="internal") == before


def test_member_cannot_mint_another_members_asset_url_or_reuse_asset_input(people):
    own = people.client.get(f"/gateway/v1/assets/{people.alice.asset_id}", headers=people.alice.bearer)
    assert own.status_code == 200, own.text
    foreign = people.client.get(f"/gateway/v1/assets/{people.bob.asset_id}", headers=people.alice.bearer)
    assert foreign.status_code in (403, 404), foreign.text
    assert "sig=" not in foreign.text
    identity = proxy.runtime_identity_for_app_credential_hash(
        proxy.hash_api_key(people.alice.key["api_key"]), path=people.paths["admin"]
    )
    context = proxy.AuthContext(identity=identity)
    own_bytes, _ = asyncio.run(proxy._resolve_input_reference(
        {"asset_id": people.alice.asset_id}, context
    ))
    assert own_bytes == people.alice.raw
    with pytest.raises(proxy._InputReferenceError, match="not accessible"):
        asyncio.run(proxy._resolve_input_reference({"asset_id": people.bob.asset_id}, context))


def test_signed_url_cannot_be_changed_to_another_members_asset(people):
    own = people.client.get(f"/gateway/v1/assets/{people.alice.asset_id}", headers=people.alice.bearer)
    assert own.status_code == 200, own.text
    url = own.json()["content_url"]
    # Signed URLs are bearer capabilities by contract. Holding the whole URL
    # grants access, but guessing an id or reusing another asset's signature
    # must not grant access to that asset.
    assert people.client.get(url).status_code == 200
    forged = url.replace(people.alice.asset_id, people.bob.asset_id)
    assert people.client.get(forged).status_code == 403
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query))
    query["expires"] = "1"
    query["sig"] = proxy._sign_asset_token(people.alice.asset_id, 1, query.get("md"))
    expired = urlunsplit(parsed._replace(query=urlencode(query)))
    assert people.client.get(expired).status_code == 403


def test_shared_plan_and_app_name_do_not_authorize_unattributed_legacy_records(people):
    orphan = RouteReceipt(protocol="openai.images", intent="image.generate",
                          api_key_name="Phone", app_profile="default-local", status="succeeded")
    receipt_store.record(orphan)
    job = media_jobs.create(media_jobs.MediaJob(
        intent="image.generate", request_id=orphan.request_id,
        parent_request_id=orphan.request_id, api_key_name="Phone", app_profile="default-local",
    ))
    for path in (f"receipts/{orphan.request_id}", f"media/jobs/{job.job_id}"):
        response = people.client.get(f"/gateway/v1/{path}", headers=people.alice.bearer)
        assert response.status_code == 404, response.text


def test_configured_authorizer_failure_does_not_fall_through_to_all_records(people):
    def fail(_request, _kind, _record):
        raise RuntimeError("authorization unavailable")

    app = FastAPI()
    app.include_router(create_gateway_router(
        WorkflowRegistry(), record_authorizer=fail,
        receipt_store_path=people.paths["receipts"], media_job_store_path=people.paths["jobs"],
    ))
    client = TestClient(app)
    for path in ("receipts", "media/jobs"):
        response = client.get(f"/gateway/v1/{path}")
        assert response.status_code == 500
        assert people.alice.receipt_id not in response.text
        assert people.alice.job_id not in response.text


@pytest.mark.parametrize("authorized", [False, True])
def test_audience_visibility_and_record_authorization_are_independent(people, authorized):
    app = FastAPI()
    app.include_router(create_gateway_router(
        WorkflowRegistry(), audience_resolver=lambda _request: "internal",
        record_authorizer=lambda _request, _kind, _record: authorized,
        receipt_store_path=people.paths["receipts"], media_job_store_path=people.paths["jobs"],
    ))
    client = TestClient(app)
    for path, field in (("receipts", "receipts"), ("media/jobs", "jobs")):
        response = client.get(f"/gateway/v1/{path}")
        assert response.status_code == 200
        assert len(response.json()[field]) == (2 if authorized else 0)


@pytest.mark.parametrize("resource,list_field,id_field", [
    ("receipts", "receipts", "request_id"), ("media/jobs", "jobs", "job_id"),
])
def test_owner_filter_precedes_limit_when_another_member_is_more_active(people, resource, list_field, id_field):
    for index in range(5):
        receipt = RouteReceipt(protocol="openai.images", intent="image.generate", app_id=people.bob.app["id"])
        receipt_store.record(receipt)
        media_jobs.create(media_jobs.MediaJob(intent="image.generate", app_id=people.bob.app["id"],
                                             request_id=receipt.request_id, updated_at=4_000_000_000 + index))
    response = people.client.get(f"/gateway/v1/{resource}?limit=1", headers=people.alice.bearer)
    assert response.status_code == 200
    expected = people.alice.receipt_id if resource == "receipts" else people.alice.job_id
    assert [row[id_field] for row in response.json()[list_field]] == [expected]


def test_legacy_runtime_key_owns_its_new_queued_jobs_without_app_identity(people, monkeypatch):
    from gateway.runtime_auth import RuntimeIdentity
    identity = RuntimeIdentity(name="legacy-key", key_hash="a" * 64)
    monkeypatch.setattr(proxy, "_auth_context_from_scope", lambda request: proxy.AuthContext(identity=identity))
    job = media_jobs.create(media_jobs.MediaJob(intent="image.generate", owner_key_hash=identity.key_hash))
    request = SimpleNamespace()
    assert proxy._gateway_record_authorizer(request, "media_job", job.to_dict("internal"))
    assert "owner_key_hash" not in job.to_dict("client")
    assert "owner_key_hash" not in job.to_dict("admin")
    assert media_jobs.recent(owner_key_hashes=[identity.key_hash], limit=1)[0]["job_id"] == job.job_id
    monkeypatch.setattr(proxy, "_auth_context_from_scope", lambda request: proxy.AuthContext(identity=RuntimeIdentity(name="same-label", key_hash="b" * 64)))
    assert not proxy._gateway_record_authorizer(request, "media_job", job.to_dict("internal"))
    assert media_jobs.recent(owner_key_hashes=["b" * 64]) == []


def test_historical_client_supplied_receipt_and_asset_references_do_not_prove_job_ownership(people):
    job = media_jobs.create(media_jobs.MediaJob(intent="image.generate", parent_request_id=people.alice.receipt_id,
                                             assets=[{"asset_id": people.alice.asset_id}]))
    response = people.client.get(f"/gateway/v1/media/jobs/{job.job_id}", headers=people.alice.bearer)
    assert response.status_code == 404
    listed = people.client.get("/gateway/v1/media/jobs", headers=people.alice.bearer)
    assert job.job_id not in [row["job_id"] for row in listed.json()["jobs"]]


def test_native_passthrough_receipt_retains_legacy_caller_ownership(people, monkeypatch):
    from gateway.runtime_auth import RuntimeIdentity
    identity = RuntimeIdentity(name="legacy-key", key_hash="c" * 64)
    context = proxy.AuthContext(identity=identity)
    receipt = proxy._native_passthrough_receipt(auth_context=context, provider="example",
                                               gateway_path="/example/v1/models", native_path="models")
    monkeypatch.setattr(proxy, "_auth_context_from_scope", lambda request: context)
    assert proxy._gateway_record_authorizer(SimpleNamespace(), "receipt", receipt.to_dict("internal"))
    assert receipt.owner_key_hash == identity.key_hash
    assert "owner_key_hash" not in receipt.to_dict("admin")
    assert "owner_key_hash" not in receipt.to_dict("client")
