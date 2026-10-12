"""Deny by default at the HTTP layer (design v4 §2.5 step 1-3, §2.8, §5.3).

These requests go through the real key lookup in the test-owned admin.db:
no stubbed identity. A request runs only with an App key whose App is on,
whose person is on, and where both have an access plan.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import accounts, admin_store  # noqa: E402
from gateway.access_plans import ALL, CAPABILITIES  # noqa: E402
from gateway.runtime_auth import hash_api_key  # noqa: E402

LOCAL_ONLY = {
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
CHAT = "/v1/chat/completions"
BODY = {"model": "default", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}


@pytest.fixture
def store():
    path = proxy.ADMIN_STORE.path
    admin_store.init_db(path)
    accounts.initialize(path)
    return path


def _person(store, *, plan_id=None):
    user = admin_store.upsert_oidc_user("https://sso.test", uuid.uuid4().hex, email="p@example.test",
                                        display_name="Person", path=store)
    if plan_id is not None:
        accounts.change_person(user["id"], "test", plan_id=plan_id, path=store)
    return user["id"]


def _key(store, person_id, *, access_plan="person", kind="personal"):
    app = admin_store.create_app("test app", kind=kind, owner_user_id=person_id, access_plan=access_plan, path=store)
    raw = f"opk_{uuid.uuid4().hex}"
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=hash_api_key(raw),
                                       prefix=raw[:18], created_by=person_id, path=store)
    return app["id"], {"Authorization": f"Bearer {raw}"}


def _plan(store, **overrides):
    return admin_store.create_access_plan({**LOCAL_ONLY, "name": f"plan {uuid.uuid4().hex[:6]}", **overrides},
                                          path=store)["id"]


def _error(response):
    return response.json()["error"]


@pytest.mark.parametrize("client_ip", ["127.0.0.1", "::1", "192.168.1.50", "10.0.0.5", "100.64.1.1",
                                       "fd00::1", "203.0.113.9"])
def test_no_key_from_any_address_is_key_required(store, client_ip):
    client = TestClient(proxy.app, client=(client_ip, 4242))
    response = client.post(CHAT, json=BODY)
    assert response.status_code == 401
    assert _error(response) == {"message": proxy.ACCESS_ERROR_MESSAGES["key_required"],
                                "type": "access_error", "code": "key_required"}


@pytest.mark.parametrize("placeholder", ["lan", "sk-local", "EMPTY", "not-needed", "sk-litellm-virtual"])
def test_placeholder_or_unknown_key_from_the_lan_is_key_invalid(store, placeholder):
    client = TestClient(proxy.app, client=("192.168.1.50", 4242))
    response = client.post(CHAT, json=BODY, headers={"Authorization": f"Bearer {placeholder}"})
    assert response.status_code == 401 and _error(response)["code"] == "key_invalid"


def test_person_without_a_plan_is_denied(store):
    person = _person(store)
    _, headers = _key(store, person)
    response = TestClient(proxy.app).post(CHAT, json=BODY, headers=headers)
    assert response.status_code == 403
    error = _error(response)
    assert error["code"] == "no_access_plan" and error["details"]["side"] == "person"
    assert error["message"].startswith("You don't have an access plan yet.")


def test_person_without_a_plan_sees_no_models(store):
    person = _person(store)
    _, headers = _key(store, person)
    response = TestClient(proxy.app).get("/v1/models", headers=headers)
    assert response.status_code == 403 and _error(response)["code"] == "no_access_plan"


def test_app_whose_assignment_was_never_written_is_denied(store):
    person = _person(store, plan_id=_plan(store))
    app_id, headers = _key(store, person)
    conn = admin_store.sqlite3.connect(store)
    conn.execute("UPDATE apps SET access_plan = NULL WHERE id = ?", (app_id,))
    conn.commit()
    conn.close()
    response = TestClient(proxy.app).post(CHAT, json=BODY, headers=headers)
    assert response.status_code == 403
    assert _error(response)["code"] == "no_access_plan" and _error(response)["details"]["side"] == "app"


def test_app_plan_narrower_than_the_person_denies_on_the_app_side(store):
    person = _person(store, plan_id=_plan(store))
    embed_only = _plan(store, can_use={"embed": {"items": ALL}})
    _, headers = _key(store, person, access_plan=embed_only)
    response = TestClient(proxy.app).post(CHAT, json=BODY, headers=headers)
    assert response.status_code == 403
    error = _error(response)
    assert error["code"] == "not_in_access_plan" and error["details"]["side"] == "app"


def test_disabled_app_and_disabled_person_are_named(store):
    person = _person(store, plan_id=_plan(store))
    app_id, headers = _key(store, person)
    admin_store.disable_app(app_id, path=store)
    response = TestClient(proxy.app).post(CHAT, json=BODY, headers=headers)
    assert response.status_code == 403 and _error(response)["code"] == "app_disabled"

    other = _person(store, plan_id=_plan(store))
    _, other_headers = _key(store, other)
    accounts.change_person(other, "test", disabled=True, path=store)
    response = TestClient(proxy.app).post(CHAT, json=BODY, headers=other_headers)
    # Disabling a person revokes their Apps' keys; the key no longer exists as active.
    assert response.status_code == 401 and _error(response)["code"] == "key_invalid"


def test_app_whose_person_row_is_missing_is_denied(store):
    person = _person(store, plan_id=_plan(store))
    _, headers = _key(store, person)
    conn = admin_store.sqlite3.connect(store)
    conn.execute("DELETE FROM account_access WHERE user_id = ?", (person,))
    conn.commit()
    conn.close()
    response = TestClient(proxy.app).post(CHAT, json=BODY, headers=headers)
    assert response.status_code == 403 and _error(response)["code"] == "person_disabled"


def test_health_stays_public_and_runs_nothing(store):
    response = TestClient(proxy.app, client=("203.0.113.9", 1)).get("/health")
    assert response.status_code == 200 and "auth_enabled" not in response.json()


def test_former_path_token_prefix_is_just_an_unknown_path(store):
    response = TestClient(proxy.app).get("/tok_livingroom/v1/models")
    assert response.status_code == 401 and _error(response)["code"] == "key_required"


@pytest.mark.parametrize("setting", ["enabled", "lan_bypass_enabled", "lan_bypass_cidrs", "path_tokens",
                                     "ollama_compat_discovery"])
def test_startup_refuses_removed_keyless_settings(setting):
    found = proxy.removed_auth_settings({setting: False}, {})
    assert found and found[0].startswith(f"auth.{setting}:")


@pytest.mark.parametrize("variable", ["AZIZAM_AUTH_ENABLED", "AZIZAM_LAN_BYPASS", "OLLAMA_COMPAT_DISCOVERY"])
def test_startup_refuses_removed_keyless_environment(variable):
    assert proxy.removed_auth_settings({}, {variable: "true"})


def test_shipped_config_has_no_keyless_settings():
    assert proxy.removed_auth_settings(proxy.auth_cfg, {}) == []
