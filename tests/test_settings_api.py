"""Hermetic settings journeys: authorization, durable writes and redacted export."""
import json
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway import admin_store, media_jobs, prompt_memory
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.settings_api import create_settings_router, instance_settings
from tests.support.access import store_plan


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    config = {"proxy": {"port": 18001}, "model_aliases": {"chat": {"stable": {"default": "role"}}},
              "oidc": {"client_secret": "fixture-secret", "issuer": "https://login.example.test"},
              "nested": [{"headers": {"Authorization": "Bearer fixture-secret"}},
                         {"apiKey": "fixture-secret", "upstream": "https://user:password@host.test/v1?auth=fixture-secret"}]}

    def auth(request):
        role = request.headers.get("test-role")
        if not role:
            raise AdminAuthError()
        return AdminContext(subject="tester", roles=frozenset({role}), auth_method="session", csrf_token="csrf-fixture")

    app = FastAPI()
    app.include_router(create_settings_router(auth=auth, admin_path=path, config_provider=lambda: config,
                                             instance_defaults=lambda: {"name": "Seed", "base_url": "https://seed.test"}))
    return TestClient(app), path, config


OWNER = {"test-role": "owner", "x-azizam-csrf": "csrf-fixture"}


def test_prompt_memory_owner_kill_switch_purges_existing_prompts(settings, tmp_path, monkeypatch):
    client, _, _ = settings
    prompt_db = tmp_path / "media_jobs.db"
    monkeypatch.setattr(media_jobs, "DB_PATH", prompt_db)
    prompt_memory.put("img_one", "person-a", "private-sentinel", kept_until=None, path=prompt_db)
    defaults = client.get("/admin/api/settings/prompt-memory", headers=OWNER)
    assert defaults.status_code == 200
    assert defaults.json()["prompt_memory"] == {
        "default_remember": True, "never_keep": False, "retention_cap": "never",
    }
    denied = client.put(
        "/admin/api/settings/prompt-memory",
        json={"default_remember": True, "never_keep": True, "retention_cap": "never"},
        headers={**OWNER, "If-Match": "stale"},
    )
    assert denied.status_code == 409
    assert prompt_memory.get("img_one", "person-a", path=prompt_db)
    updated = client.put(
        "/admin/api/settings/prompt-memory",
        json={"default_remember": True, "never_keep": True, "retention_cap": "never"},
        headers={**OWNER, "If-Match": defaults.json()["revision"]},
    )
    assert updated.status_code == 200
    assert prompt_memory.get("img_one", "person-a", path=prompt_db) is None


def test_prompt_memory_owner_cap_tightens_existing_prompts(settings, tmp_path, monkeypatch):
    client, _, _ = settings
    prompt_db = tmp_path / "media_jobs.db"
    monkeypatch.setattr(media_jobs, "DB_PATH", prompt_db)
    prompt_memory.put("img_old", "person-a", "old secret", kept_until=None,
                      created_at=int(time.time()) - 100 * 86400, path=prompt_db)
    revision = client.get("/admin/api/settings/prompt-memory", headers=OWNER).json()["revision"]
    response = client.put(
        "/admin/api/settings/prompt-memory",
        json={"default_remember": True, "never_keep": False, "retention_cap": "3_months"},
        headers={**OWNER, "If-Match": revision},
    )
    assert response.status_code == 200
    assert prompt_memory.get("img_old", "person-a", path=prompt_db) is None


def test_prompt_memory_kill_switch_reports_pending_metadata_cleanup(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    auth = lambda _request: AdminContext(
        subject="owner", roles=frozenset({"owner"}), auth_method="session", csrf_token="csrf-fixture",
    )
    app = FastAPI()
    app.include_router(create_settings_router(
        auth=auth, admin_path=path, config_provider=lambda: {},
        on_prompt_memory_kill=lambda: {"metadata_stripping": {"pending": 3}},
    ))
    client = TestClient(app)
    current = client.get("/admin/api/settings/prompt-memory", headers=OWNER)
    response = client.put(
        "/admin/api/settings/prompt-memory",
        json={"default_remember": True, "never_keep": True, "retention_cap": "never"},
        headers={**OWNER, "If-Match": current.json()["revision"]},
    )
    assert response.status_code == 202
    assert response.json()["prompt_purge"]["metadata_stripping"]["pending"] == 3
    assert response.json()["prompt_purge"]["retry_scheduled"] is True


@pytest.mark.parametrize("route", ["instance", "defaults", "effective", "effective/export"])
def test_settings_reject_missing_member_and_admin(settings, route):
    client, _, _ = settings
    assert client.get(f"/admin/api/settings/{route}").status_code == 401
    for role in ("member", "admin"):
        assert client.get(f"/admin/api/settings/{route}", headers={"test-role": role}).status_code == 403


def test_instance_save_reload_and_audit(settings):
    client, path, config = settings
    body = {"name": "Our gateway", "base_url": "https://gateway.test/", "theme_default": "dark"}
    assert client.put("/admin/api/settings/instance", json=body, headers={"test-role": "owner"}).status_code == 403
    response = client.put("/admin/api/settings/instance", json=body, headers={**OWNER, "If-Match": "0"})
    assert response.status_code == 200
    assert response.json()["instance"] == {**body, "base_url": "https://gateway.test"}
    assert client.get("/admin/api/settings/instance", headers=OWNER).json() == response.json()
    assert instance_settings(path)["name"] == "Our gateway"
    assert admin_store.list_audit(path=path)[0]["action"] == "operator.instance.update"
    assert config["proxy"] == {"port": 18001}


@pytest.mark.parametrize("change", [
    {"name": ""}, {"theme_default": "automatic"}, {"base_url": "javascript:alert(1)"},
    {"base_url": "https://user:password@host.test"}, {"base_url": "https://host.test?token=secret"},
    {"base_url": "https://host.test/v1"},
    {"base_url": "https://host .test"}, {"base_url": "https://host.test\n"},
    {"base_url": "https://ho\tst.test"}, {"base_url": "https://host.test\x00"},
    {"base_url": "https://host.test\x7f"}, {"base_url": " https://host.test"},
    {"base_url": "https://host.test\x80"},
    {"name": 42}, {"bootstrap": True},
])
def test_instance_invalid_input_leaves_no_setting(settings, change):
    client, path, _ = settings
    body = {"name": "Valid", "base_url": "", "theme_default": "system", **change}
    assert client.put("/admin/api/settings/instance", json=body, headers=OWNER).status_code == 400
    assert admin_store.get_operator_setting("instance_presentation", path=path) is None


def test_removed_lan_placeholder_keys_route_is_gone(settings):
    client, _, _ = settings
    assert client.get("/admin/api/settings/lan-placeholder-keys", headers=OWNER).status_code == 404


def test_invitation_plan_default_saves_validates_and_audits(settings):
    client, path, _ = settings
    first = store_plan("First", path=path)["id"]
    second = store_plan("Second", path=path)["id"]
    assert client.get("/admin/api/settings/defaults", headers=OWNER).json()["defaults"] == {"invite_access_plan": None}
    for plan_id in (first, second, first):
        response = client.put("/admin/api/settings/defaults", json={"invite_access_plan": plan_id}, headers=OWNER)
        assert response.status_code == 200
        assert response.json()["defaults"]["invite_access_plan"] == plan_id
    for bad in ({"invite_access_plan": "missing"}, {"invite_access_plan": 1}, {"profile_id": first}, {}):
        assert client.put("/admin/api/settings/defaults", json=bad, headers=OWNER).status_code == 400
    assert admin_store.invite_access_plan(path=path) == first
    assert client.put("/admin/api/settings/defaults", json={"invite_access_plan": None}, headers=OWNER).status_code == 200
    assert admin_store.invite_access_plan(path=path) is None
    actions = [row["action"] for row in admin_store.list_audit(path=path)]
    assert actions.count("operator.invite_access_plan.update") == 4


def test_a_deleted_invitation_plan_reads_as_ask_each_time(settings):
    client, path, _ = settings
    plan_id = store_plan("Gone", path=path)["id"]
    client.put("/admin/api/settings/defaults", json={"invite_access_plan": plan_id}, headers=OWNER)
    admin_store.delete_access_plan(plan_id, path=path)
    assert client.get("/admin/api/settings/defaults", headers=OWNER).json()["defaults"]["invite_access_plan"] is None


def test_readonly_export_is_nested_secret_free_and_matches_view(settings):
    client, _, config = settings
    original = json.dumps(config)
    view = client.get("/admin/api/settings/effective", headers=OWNER)
    export = client.get("/admin/api/settings/effective/export", headers=OWNER)
    assert view.status_code == export.status_code == 200
    assert view.json() == export.json()
    assert "fixture-secret" not in export.text
    assert "user:password" not in export.text
    assert view.json()["config"]["oidc"]["issuer"] == "https://login.example.test"
    assert view.headers["cache-control"] == "no-store"
    assert "attachment" in export.headers["content-disposition"]
    assert client.put("/admin/api/settings/effective", json={}, headers=OWNER).status_code == 405
    assert json.dumps(config) == original


def test_defaults_mutation_requires_owner_and_csrf(settings):
    client, _, _ = settings
    for headers in ({}, {"test-role": "member"}, {"test-role": "admin"}, {"test-role": "owner"}):
        assert client.put("/admin/api/settings/defaults", json={"invite_access_plan": None}, headers=headers).status_code in {401, 403}


def test_saved_presentation_drives_existing_brand_and_discovery(settings, monkeypatch):
    import proxy
    _, path, _ = settings
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", path)
    admin_store.put_operator_setting("instance_presentation", {
        "name": "Saved gateway", "base_url": "https://saved.example.test", "theme_default": "dark",
    }, path=path)
    request = Request({"type": "http", "scheme": "http", "headers": [(b"host", b"request.example.test")], "path": "/"})
    assert proxy._brand_settings()[0] == "Saved gateway"
    assert proxy._public_base_url(request) == "https://saved.example.test"
    response = TestClient(proxy.app).get("/.well-known/azizam")
    assert response.status_code == 200
    assert response.json()["service"]["name"] == "Saved gateway"
    assert response.json()["presentation"] == {"theme_default": "dark"}
    assert "instance_presentation" not in response.text


def test_household_erasure_intent_survives_failure_and_reenable(settings, monkeypatch):
    client, admin_db, _ = settings
    monkeypatch.setattr(prompt_memory, "prune_all_before", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    revision = client.get("/admin/api/settings/prompt-memory", headers=OWNER).json()["revision"]
    value = {"default_remember": True, "never_keep": True, "retention_cap": "never"}
    response = client.put("/admin/api/settings/prompt-memory", json=value, headers={**OWNER, "If-Match": revision})
    assert response.status_code == 202
    assert response.json()["prompt_purge"]["database_pending"] is True
    cutoff = prompt_memory.household_erasure_cutoff(admin_path=admin_db)
    value["never_keep"] = False
    response = client.put("/admin/api/settings/prompt-memory", json=value,
                          headers={**OWNER, "If-Match": response.json()["revision"]})
    assert prompt_memory.household_erasure_cutoff(admin_path=admin_db) == cutoff
    assert response.json()["prompt_purge"]["database_pending"] is True


def test_household_intent_failure_leaves_setting_and_revision_unchanged(settings, monkeypatch):
    client, _, _ = settings
    before = client.get("/admin/api/settings/prompt-memory", headers=OWNER).json()
    monkeypatch.setattr(admin_store, "_record_prompt_erasure", lambda *a: (_ for _ in ()).throw(RuntimeError("locked")))
    response = client.put("/admin/api/settings/prompt-memory",
                          json={**before["prompt_memory"], "never_keep": True},
                          headers={**OWNER, "If-Match": before["revision"]})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "prompt_erasure_unavailable"
    after = client.get("/admin/api/settings/prompt-memory", headers=OWNER).json()
    assert after["revision"] == before["revision"]
    assert after["prompt_memory"] == before["prompt_memory"]
