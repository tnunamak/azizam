"""Try discovers only models its own App key may use."""
from unittest.mock import AsyncMock

import pytest

import proxy
from gateway import accounts, admin_store, receipt_store
from gateway.capabilities import Capability
from tests.support.access import store_plan
from tests.test_try_session_keys import browser, member  # noqa: F401 - fixtures


@pytest.fixture
def catalog(browser, member, monkeypatch, tmp_path):
    wide = store_plan("A and B", path=member.db, models=["model-a", "model-b"])
    narrow = store_plan("A only", path=member.db, models=["model-a"])
    accounts.change_person(member.user["id"], "test", plan_id=wide["id"], path=member.db)
    admin_store.create_app("Other", kind="personal", owner_user_id=member.user["id"],
                           access_plan=wide["id"], path=member.db)
    receipts = tmp_path / "receipts.db"
    receipt_store.init_db(receipts)
    monkeypatch.setattr(receipt_store, "DB_PATH", receipts)
    monkeypatch.setattr(proxy, "_gateway_catalog_base", lambda: ({}, [
        Capability(intent="chat.completions", backend="svc", model=model)
        for model in ("model-a", "model-b")
    ]))
    monkeypatch.setattr(proxy, "_gateway_catalog_finish", lambda registry, base, remote: base)
    monkeypatch.setattr(proxy, "_gateway_aliases_provider", lambda: {})
    monkeypatch.setattr(proxy, "_models_catalog_health_evidence", AsyncMock(return_value={}))
    monkeypatch.setattr(proxy.status_endpoint, "get_cached_status", AsyncMock(return_value={}))
    monkeypatch.setattr(proxy, "_status_payload_with_service_rows", AsyncMock(return_value={}))
    monkeypatch.setattr(proxy, "_runtime_availability_evidence", lambda *args, **kw: {})
    return narrow


@pytest.mark.parametrize("has_try_app", [True, False])
def test_try_catalog_matches_its_app_or_the_plan_a_new_app_will_follow(catalog, browser, member, has_try_app):
    if has_try_app:
        admin_store.create_app("Try", kind="personal", owner_user_id=member.user["id"],
                               access_plan=catalog["id"], path=member.db)
    else:
        other = admin_store.list_apps_for_user(member.user["id"], path=member.db)[0]
        admin_store.set_app_access_plan(other["id"], catalog["id"], path=member.db)
    union = browser.client.get("/me/api/models/catalog")
    assert union.status_code == 200, union.text
    assert {row["id"] for row in union.json()["models"]} == ({"model-a", "model-b"} if has_try_app else {"model-a"})
    scoped = browser.client.get("/me/api/models/catalog?for=try")
    assert scoped.status_code == 200, scoped.text
    expected = {"model-a"} if has_try_app else {"model-a", "model-b"}
    assert {row["id"] for row in scoped.json()["models"]} == expected
    if not has_try_app:
        assert [app["name"] for app in admin_store.list_apps_for_user(member.user["id"], path=member.db)] == ["Other"]
        minted = browser.client.post("/me/api/try-key", headers=browser.headers, json={})
        assert minted.status_code == 201, minted.text
        assert browser.client.get("/me/api/models/catalog?for=try").content == scoped.content


def test_unknown_catalog_scope_is_a_json_error(browser):
    response = browser.client.get("/me/api/models/catalog?for=bogus")
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_catalog_scope"
