"""Request ledgers use real management credentials through the running ASGI app."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import accounts, admin_store, media_jobs, receipt_store
from gateway.admin_oidc import AdminSession, encode_session_cookie, session_from_claims
from gateway.admin_runtime import create_admin_service_account_key
from gateway.receipts import RouteReceipt
from gateway.user_runtime import mint_user_app_key
from tests.support.access import store_plan


@pytest.fixture
def ledger_credentials(tmp_path, monkeypatch):
    admin = tmp_path / "admin.db"
    receipts = tmp_path / "receipts.db"
    facts = tmp_path / "facts.db"
    admin_store.init_db(admin)
    receipt_store.init_db(receipts)
    media_jobs.init_db(facts)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    monkeypatch.setattr(media_jobs, "DB_PATH", facts)
    original_get = receipt_store.get
    monkeypatch.setattr(receipt_store, "get", lambda *a, **kw: original_get(*a, **{**kw, "path": receipts}))
    accounts.initialize(admin)
    owner = admin_store.upsert_oidc_user("https://idp.example", "ledger-owner", email="ledger@example.test", path=admin)
    plan = store_plan("Ledger owner", path=admin, engines="all", operations=["*"])
    accounts.change_person(owner["id"], "owner", path=admin, plan_id=plan["id"])
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=owner["id"], path=admin)
    app = admin_store.create_app("Ledger app", kind="personal", owner_user_id=owner["id"], access_plan="person", path=admin)
    app_key = mint_user_app_key(AdminSession(subject=owner["id"], email=owner["email"], role="owner",
                                            issued_at=1, expires_at=4_000_000_000), app["id"], path=admin)["api_key"]
    admin_key, _, _ = create_admin_service_account_key("ledger-admin", role="admin", path=admin)
    cookie = proxy.ADMIN_AUTH.cookie
    session = session_from_claims({"sub": owner["id"], "email": owner["email"]}, role="owner", cookie=cookie)
    signed_cookie = encode_session_cookie(session, signing_key=proxy.ADMIN_AUTH.signing_key)
    return receipts, owner["id"], admin_key, app_key, (cookie.name, signed_cookie)


@pytest.mark.parametrize("viewer", ["admin_bearer", "owner_browser", "maker_browser", "owner_app"])
def test_request_ledger_real_auth_and_viewer_mask(ledger_credentials, viewer):
    receipts, owner, admin_key, app_key, cookie = ledger_credentials
    row = RouteReceipt(protocol="test", intent="test", backend="former-service",
                       maker_person_id=owner if viewer == "maker_browser" else "another-person")
    row.set_retention(kind="library_prompt", decision="kept", reason="kept", kept_until_ms=123456789,
                      parts=["accepted"], limited_by="member_choice")
    receipt_store.record(row, path=receipts)
    client = TestClient(proxy.app, base_url="https://testserver", client=("127.0.0.1", 50000))
    headers = {}
    if viewer == "admin_bearer":
        headers["Authorization"] = f"Bearer {admin_key}"
    elif viewer == "owner_app":
        headers["Authorization"] = f"Bearer {app_key}"
    else:
        client.cookies.set(*cookie)
    response = client.get(f"/admin/api/requests/{row.request_id}/retention/ledger", headers=headers)
    assert response.status_code == (403 if viewer == "owner_app" else 200)
    if viewer == "owner_app":
        return
    assert response.headers["cache-control"] == "no-store"
    ledger = response.json()["ledger"]
    assert ledger["request_id"] == row.request_id
    prompt = next(kind for kind in ledger["kinds"] if kind["id"] == "library_prompt")
    assert prompt["decision"] == "kept"
    assert ("kept_until_ms" in prompt) is (viewer == "maker_browser")
    assert ("reason" in prompt) is (viewer == "maker_browser")
    client.close()


def test_request_ledger_old_route_is_removed():
    assert not any(getattr(route, "path", None) == "/gateway/v1/requests/{request_id}/ledger"
                   for route in proxy.app.routes)


@pytest.mark.parametrize("viewer", ["admin_bearer", "owner_browser", "owner_app", "anonymous"])
def test_current_ledger_real_management_auth(ledger_credentials, viewer):
    _, _, admin_key, app_key, cookie = ledger_credentials
    client = TestClient(proxy.app, base_url="https://testserver", client=("127.0.0.1", 50000))
    headers = {}
    if viewer == "admin_bearer":
        headers["Authorization"] = f"Bearer {admin_key}"
    elif viewer == "owner_app":
        headers["Authorization"] = f"Bearer {app_key}"
    elif viewer == "owner_browser":
        client.cookies.set(*cookie)
    response = client.get("/admin/api/me/retention/ledger", headers=headers)
    assert response.status_code == (200 if viewer in {"admin_bearer", "owner_browser"} else 403)
    if response.status_code == 200:
        assert response.headers["cache-control"] == "no-store"
        assert "destinations" in response.json()["ledger"]
    # The runtime path must be gone for every credential, with no shim.
    assert not any(getattr(route, "path", None) == "/gateway/v1/me/retention/ledger" for route in proxy.app.routes)
    client.close()
