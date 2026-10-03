"""Hermetic settings journeys: authorization, durable writes and redacted export."""
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway import admin_store
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.settings_api import create_settings_router, instance_settings


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


OWNER = {"test-role": "owner", "x-ai-gateway-csrf": "csrf-fixture"}


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
    response = client.put("/admin/api/settings/instance", json=body, headers=OWNER)
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


def test_default_plan_replace_is_one_grant_and_preserves_specific_mapping(settings):
    client, path, _ = settings
    first = admin_store.create_profile("First", {"id": "first"}, path=path)["id"]
    second = admin_store.create_profile("Second", {"id": "second"}, path=path)["id"]
    specific = admin_store.create_runtime_profile_mapping(source_kind="email", source_value="special@example.test", profile_id=first, path=path)
    for profile_id in (first, second, first):
        response = client.put("/admin/api/settings/defaults", json={"profile_id": profile_id}, headers=OWNER)
        assert response.status_code == 200
        assert response.json()["defaults"]["profile_id"] == profile_id
        assert len([m for m in admin_store.list_runtime_profile_mappings(path=path) if m["source_kind"] == "all_users"]) == 1
    assert admin_store.resolve_runtime_profile_mapping(email="special@example.test", gateway_role="none", path=path) == first
    assert any(m["id"] == specific["id"] for m in admin_store.list_runtime_profile_mappings(path=path))
    assert client.put("/admin/api/settings/defaults", json={"profile_id": "missing"}, headers=OWNER).status_code == 400
    assert client.get("/admin/api/settings/defaults", headers=OWNER).json()["defaults"]["profile_id"] == first
    assert client.put("/admin/api/settings/defaults", json={"profile_id": None}, headers=OWNER).status_code == 200
    assert admin_store.resolve_runtime_profile_mapping(email="ordinary@example.test", gateway_role="none", path=path) is None


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
        assert client.put("/admin/api/settings/defaults", json={"profile_id": None}, headers=headers).status_code in {401, 403}


def test_default_grant_and_audit_rollback_together(settings, monkeypatch):
    _, path, _ = settings
    first = admin_store.create_profile("First", {"id": "first"}, path=path)["id"]
    admin_store.create_runtime_profile_mapping(source_kind="all_users", source_value="*", profile_id=first, path=path)
    def fail_audit(*args, **kwargs):
        raise RuntimeError("fixture audit failure")
    monkeypatch.setattr(admin_store, "_record_audit_in_transaction", fail_audit)
    with pytest.raises(RuntimeError):
        admin_store.replace_default_runtime_profile_mapping(profile_id=None, actor=AdminContext(subject="tester"), path=path)
    assert admin_store.resolve_runtime_profile_mapping(email="ordinary@example.test", gateway_role="none", path=path) == first


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
    response = TestClient(proxy.app).get("/.well-known/ai-gateway")
    assert response.status_code == 200
    assert response.json()["presentation"] == {"theme_default": "dark"}
    assert "instance_presentation" not in response.text
