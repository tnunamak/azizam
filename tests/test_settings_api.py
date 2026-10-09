"""Hermetic settings journeys: authorization, durable writes and redacted export."""
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway import admin_store, lan_auth_policy, media_jobs, prompt_memory
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


@pytest.mark.parametrize("route", ["instance", "defaults", "lan-placeholder-keys", "effective", "effective/export"])
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


def test_lan_placeholder_keys_default_response_is_owner_only_and_not_persisted(settings):
    client, path, _ = settings
    response = client.get("/admin/api/settings/lan-placeholder-keys", headers=OWNER)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "lan_placeholder_keys": lan_auth_policy.DEFAULT_LAN_PLACEHOLDER_KEYS,
        "source": "admin.db",
        "apply_mode": "live",
    }
    assert admin_store.get_operator_setting(lan_auth_policy.LAN_PLACEHOLDER_KEYS_SETTING_KEY, path=path) is None


def test_lan_placeholder_keys_mutation_requires_owner_and_csrf(settings):
    client, path, _ = settings
    for headers in ({}, {"test-role": "member"}, {"test-role": "admin"}, {"test-role": "owner"}):
        response = client.put(
            "/admin/api/settings/lan-placeholder-keys",
            json={"lan_placeholder_keys": ["fixture-placeholder"]},
            headers=headers,
        )
        assert response.status_code in {401, 403}
    assert admin_store.get_operator_setting(lan_auth_policy.LAN_PLACEHOLDER_KEYS_SETTING_KEY, path=path) is None


def test_lan_placeholder_keys_persist_empty_and_custom_policy_live(settings):
    client, path, _ = settings
    empty = client.put("/admin/api/settings/lan-placeholder-keys", json={"lan_placeholder_keys": []}, headers=OWNER)
    assert empty.status_code == 200
    assert empty.json()["lan_placeholder_keys"] == []
    assert lan_auth_policy.lan_placeholder_keys(path) == []

    custom = client.put(
        "/admin/api/settings/lan-placeholder-keys",
        json={"lan_placeholder_keys": ["custom-lan", " sk-local ", "EMPTY"]},
        headers=OWNER,
    )
    assert custom.status_code == 200
    assert custom.json() == {
        "lan_placeholder_keys": ["custom-lan", "sk-local", "EMPTY"],
        "source": "admin.db",
        "apply_mode": "live",
    }
    assert client.get("/admin/api/settings/lan-placeholder-keys", headers=OWNER).json() == custom.json()
    assert lan_auth_policy.lan_placeholder_keys(path) == ["custom-lan", "sk-local", "EMPTY"]
    audit = admin_store.list_audit(action_prefix="operator.lan_placeholder_keys", path=path)
    assert sorted(event["metadata"]["count"] for event in audit) == [0, 3]
    assert "custom-lan" not in json.dumps(audit)


@pytest.mark.parametrize("body", [
    {},
    {"lan_placeholder_keys": "lan"},
    {"lan_placeholder_keys": ["ok"], "extra": []},
    {"lan_placeholder_keys": [1]},
    {"lan_placeholder_keys": [""]},
    {"lan_placeholder_keys": ["   "]},
    {"lan_placeholder_keys": ["has\ncontrol"]},
    {"lan_placeholder_keys": ["x" * 129]},
    {"lan_placeholder_keys": ["item"] * 65},
    {"lan_placeholder_keys": ["opk_live"]},
    {"lan_placeholder_keys": ["oak_live"]},
    {"lan_placeholder_keys": ["sk-live"]},
])
def test_lan_placeholder_keys_invalid_input_leaves_policy_unchanged(settings, body):
    client, path, _ = settings
    assert client.put(
        "/admin/api/settings/lan-placeholder-keys",
        json={"lan_placeholder_keys": ["before"]},
        headers=OWNER,
    ).status_code == 200
    response = client.put("/admin/api/settings/lan-placeholder-keys", json=body, headers=OWNER)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_lan_placeholder_keys"
    assert lan_auth_policy.lan_placeholder_keys(path) == ["before"]


@pytest.mark.parametrize("stored", [
    {"lan_placeholder_keys": "lan"},
    {"lan_placeholder_keys": ["ok", "sk-real"]},
    {"lan_placeholder_keys": [""]},
    {"wrong_key": ["lan"]},
])
def test_lan_placeholder_keys_malformed_persisted_policy_fails_closed(settings, stored):
    client, path, _ = settings
    admin_store.put_operator_setting(lan_auth_policy.LAN_PLACEHOLDER_KEYS_SETTING_KEY, stored, path=path)
    assert lan_auth_policy.lan_placeholder_keys(path) == []
    response = client.get("/admin/api/settings/lan-placeholder-keys", headers=OWNER)
    assert response.status_code == 200
    assert response.json()["lan_placeholder_keys"] == []


def test_lan_placeholder_keys_effective_snapshot_redacts_policy_values(settings):
    client, _, _ = settings
    assert client.put(
        "/admin/api/settings/lan-placeholder-keys",
        json={"lan_placeholder_keys": ["custom-lan-secret"]},
        headers=OWNER,
    ).status_code == 200
    response = client.get("/admin/api/settings/effective", headers=OWNER)
    assert response.status_code == 200
    assert response.json()["config"]["auth_policy"]["lan_placeholder_keys"] == "<redacted>"
    assert "custom-lan-secret" not in response.text


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
    response = TestClient(proxy.app).get("/.well-known/azizam")
    assert response.status_code == 200
    assert response.json()["service"]["name"] == "Saved gateway"
    assert response.json()["presentation"] == {"theme_default": "dark"}
    assert "instance_presentation" not in response.text
