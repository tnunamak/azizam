"""An administrator never acts over the owner, and never handles the owner's money or credentials.

Three holes from the round-3 review of the administrator power bounds:

* B1: an administrator could re-enable, un-trust and rename the owner's Apps.
* B2: an administrator could change the owner's spend caps and provider keys.
* B3: an administrator could attach the owner's provider key to a host they chose.

Each test runs the real routes behind a real sign-in (see test_signin_who_can_enter).
"""
import json

import pytest

from gateway import admin_store, service_registry
from gateway.litellm_substrate import compile_litellm_substrate_config
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

OWNER_KEY_ENV = "OPENAI_API_KEY"


def _app_of(gateway, owner, plan_id, owner_user_id, name="owners"):
    created = owner.post("/admin/api/apps", headers=csrf_for(owner), json={
        "name": name, "kind": "service", "access_plan": plan_id, "owner_user_id": owner_user_id})
    assert created.status_code == 201, created.text
    return created.json()["app"]["id"]


def _member_id(gateway):
    return admin_store.upsert_oidc_user(
        "https://id.example", "m", email="m@example.test", path=gateway.path)["id"]


# -- B1: the owner's Apps are not the administrator's to switch on, un-trust or rename

def test_an_administrator_cannot_turn_the_owners_disabled_app_back_on(gateway):
    owner, owner_id, plan_id, admin = owner_and_admin(gateway)
    app_id = _app_of(gateway, owner, plan_id, owner_id)
    assert owner.delete(f"/admin/api/apps/{app_id}", headers=csrf_for(owner)).status_code == 200
    refused = admin.post(f"/admin/api/apps/{app_id}/enable", headers=csrf_for(admin))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "app_outranks_you"
    assert gateway.rows("SELECT disabled_at FROM apps WHERE id=?", (app_id,))[0]["disabled_at"] is not None
    assert owner.post(f"/admin/api/apps/{app_id}/enable", headers=csrf_for(owner)).status_code == 200
    assert gateway.rows("SELECT disabled_at FROM apps WHERE id=?", (app_id,))[0]["disabled_at"] is None


def test_an_administrator_can_still_turn_a_members_app_back_on(gateway):
    owner, _, plan_id, admin = owner_and_admin(gateway)
    app_id = _app_of(gateway, owner, plan_id, _member_id(gateway), name="members")
    assert admin.delete(f"/admin/api/apps/{app_id}", headers=csrf_for(admin)).status_code == 200
    assert admin.post(f"/admin/api/apps/{app_id}/enable", headers=csrf_for(admin)).status_code == 200


def test_an_administrator_cannot_remove_a_trusted_address_of_the_owners_app(gateway):
    owner, owner_id, plan_id, admin = owner_and_admin(gateway)
    app_id = _app_of(gateway, owner, plan_id, owner_id)
    added = owner.post(f"/admin/api/apps/{app_id}/trusted-addresses", headers=csrf_for(owner),
                       json={"address": "192.168.1.50"})
    assert added.status_code == 201, added.text
    entry_id = added.json()["trusted_address"]["id"]
    refused = admin.delete(f"/admin/api/apps/{app_id}/trusted-addresses/{entry_id}", headers=csrf_for(admin))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "app_outranks_you"
    assert len(owner.get(f"/admin/api/apps/{app_id}/trusted-addresses").json()["trusted_addresses"]) == 1
    removed = owner.delete(f"/admin/api/apps/{app_id}/trusted-addresses/{entry_id}", headers=csrf_for(owner))
    assert removed.status_code == 200, removed.text


def test_an_administrator_can_still_remove_a_trusted_address_of_a_members_app(gateway):
    owner, _, plan_id, admin = owner_and_admin(gateway)
    app_id = _app_of(gateway, owner, plan_id, _member_id(gateway), name="members")
    entry_id = owner.post(f"/admin/api/apps/{app_id}/trusted-addresses", headers=csrf_for(owner),
                          json={"address": "192.168.1.51"}).json()["trusted_address"]["id"]
    removed = admin.delete(f"/admin/api/apps/{app_id}/trusted-addresses/{entry_id}", headers=csrf_for(admin))
    assert removed.status_code == 200, removed.text


def test_an_administrator_cannot_rename_the_owners_app(gateway):
    owner, owner_id, plan_id, admin = owner_and_admin(gateway)
    app_id = _app_of(gateway, owner, plan_id, owner_id)
    refused = admin.patch(f"/admin/api/apps/{app_id}", headers=csrf_for(admin), json={"name": "taken over"})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "app_outranks_you"
    assert gateway.rows("SELECT name FROM apps WHERE id=?", (app_id,)) == [{"name": "owners"}]
    assert owner.patch(f"/admin/api/apps/{app_id}", headers=csrf_for(owner),
                       json={"name": "renamed"}).status_code == 200


def test_an_administrator_can_still_rename_their_own_app_and_a_members(gateway):
    owner, _, plan_id, admin = owner_and_admin(gateway)
    mine = admin.post("/admin/api/apps", headers=csrf_for(admin),
                      json={"name": "mine", "kind": "service", "access_plan": "person"}).json()["app"]["id"]
    theirs = _app_of(gateway, owner, plan_id, _member_id(gateway), name="members")
    for app_id in (mine, theirs):
        assert admin.patch(f"/admin/api/apps/{app_id}", headers=csrf_for(admin),
                           json={"name": "renamed"}).status_code == 200


# -- B2: spend caps, provider keys and recorded spend are the owner's

PROVIDER_WRITES = [
    ("post", "/admin/api/providers", {"id": "x", "name": "X"}),
    ("post", "/admin/api/providers/-/test", {}),
    ("post", "/admin/api/providers/openrouter/test", {}),
    ("post", "/admin/api/providers/openrouter/reconciliation/1", {"actual_usd": 0}),
    ("put", "/admin/api/providers/openrouter/key", {"api_key": "sk-x"}),
    ("delete", "/admin/api/providers/openrouter/key", None),
    ("put", "/admin/api/providers/openrouter/models", {"models": []}),
    ("delete", "/admin/api/providers/openrouter", None),
    ("put", "/admin/api/providers/openrouter/caps", {"daily_usd": 1000000}),
    ("delete", "/admin/api/providers/openrouter/caps", None),
    ("put", "/admin/api/providers/-/global-caps", {"daily_usd": 1000000}),
    ("delete", "/admin/api/providers/-/global-caps", None),
]


@pytest.mark.parametrize("method,path,body", PROVIDER_WRITES, ids=[f"{m} {p}" for m, p, _ in PROVIDER_WRITES])
def test_an_administrator_cannot_write_to_the_provider_admin_routes(gateway, method, path, body):
    _, _, _, admin = owner_and_admin(gateway)
    kwargs = {"headers": csrf_for(admin)}
    if body is not None:
        kwargs["json"] = body
    refused = getattr(admin, method)(path, **kwargs)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"


def test_an_administrator_cannot_raise_or_remove_the_owners_global_caps_but_can_read_them(gateway):
    owner, _, _, admin = owner_and_admin(gateway)
    assert owner.put("/admin/api/providers/-/global-caps", headers=csrf_for(owner),
                     json={"daily_usd": 5}).status_code == 200
    assert admin.put("/admin/api/providers/-/global-caps", headers=csrf_for(admin),
                     json={"daily_usd": 1000000}).status_code == 403
    assert admin.delete("/admin/api/providers/-/global-caps", headers=csrf_for(admin)).status_code == 403
    read = admin.get("/admin/api/providers")
    assert read.status_code == 200, read.text
    assert read.json()["global"]["caps"] == {"daily_usd": 5.0}
    assert owner.delete("/admin/api/providers/-/global-caps", headers=csrf_for(owner)).status_code == 200


# -- B3: an administrator cannot send the owner's key to a host they choose

def _etag(client):
    response = client.get("/admin/api/operator/service-registry")
    assert response.status_code == 200, response.text
    return response.headers.get("etag", '"os-0"')


def _service_body(**overrides):
    body = {"name": "Engine", "host": "192.168.1.60", "port": 8000, "protocol": "openai-compatible",
            "capabilities": ["chat"], "models": [{"public": "engine-m", "upstream": "engine-m"}]}
    body.update(overrides)
    return body


def _services(gateway):
    registry = service_registry.load_service_registry(path=gateway.path) or {"services": {}}
    return {sid: record for sid, record in registry["services"].items() if sid != "litellm"}


def _compiled(gateway, client):
    response = client.get("/admin/api/operator/litellm-config")
    assert response.status_code == 200, response.text
    return json.dumps(response.json())


def test_an_administrator_cannot_create_a_service_that_sends_an_env_key_to_their_host(gateway):
    _, _, _, admin = owner_and_admin(gateway)
    refused = admin.post("/admin/api/operator/services", headers={**csrf_for(admin), "If-Match": _etag(admin)},
                         json=_service_body(name="Evil", host="attacker.example", port=443,
                                            api_key_env=OWNER_KEY_ENV))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    assert "evil" not in _services(gateway)


@pytest.mark.parametrize("field", ["api_key_env", "api_key_ref", "api_key_secret_id", "gateway_lifecycle_api_key_ref"])
def test_an_administrator_cannot_attach_any_credential_reference_to_a_service(gateway, field):
    _, _, _, admin = owner_and_admin(gateway)
    refused = admin.post("/admin/api/operator/services", headers={**csrf_for(admin), "If-Match": _etag(admin)},
                         json=_service_body(name="Evil", **{field: OWNER_KEY_ENV}))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    assert "evil" not in _services(gateway)


def test_an_administrator_cannot_set_the_internal_key_base_of_a_service(gateway):
    _, _, _, admin = owner_and_admin(gateway)
    refused = admin.post("/admin/api/operator/services", headers={**csrf_for(admin), "If-Match": _etag(admin)},
                         json=_service_body(name="Evil", gateway_lifecycle=True,
                                            gateway_lifecycle_base_url="https://attacker.example/v1"))
    assert refused.status_code == 403, refused.text
    assert "evil" not in _services(gateway)


def test_an_administrator_can_still_manage_a_local_engine_service(gateway):
    _, _, _, admin = owner_and_admin(gateway)
    created = admin.post("/admin/api/operator/services", headers={**csrf_for(admin), "If-Match": _etag(admin)},
                         json=_service_body())
    assert created.status_code == 201, created.text
    patched = admin.patch("/admin/api/operator/services/engine", headers={**csrf_for(admin), "If-Match": _etag(admin)},
                          json={"host": "192.168.1.61", "note": "moved"})
    assert patched.status_code == 200, patched.text
    assert _services(gateway)["engine"]["config"]["host"] == "192.168.1.61"


def test_an_administrator_cannot_attach_a_credential_to_an_existing_service_by_edit(gateway):
    _, _, _, admin = owner_and_admin(gateway)
    assert admin.post("/admin/api/operator/services", headers={**csrf_for(admin), "If-Match": _etag(admin)},
                      json=_service_body()).status_code == 201
    refused = admin.patch("/admin/api/operator/services/engine", headers={**csrf_for(admin), "If-Match": _etag(admin)},
                          json={"api_key_env": OWNER_KEY_ENV})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    assert "api_key_env" not in _services(gateway)["engine"]["config"]


def test_an_administrator_cannot_move_a_service_that_carries_a_credential(gateway):
    owner, _, _, admin = owner_and_admin(gateway)
    created = owner.post("/admin/api/operator/services", headers={**csrf_for(owner), "If-Match": _etag(owner)},
                         json=_service_body(api_key_env="ENGINE_KEY"))
    assert created.status_code == 201, created.text
    for change in ({"host": "attacker.example"}, {"port": 9999}, {"scheme": "https"}):
        refused = admin.patch("/admin/api/operator/services/engine",
                              headers={**csrf_for(admin), "If-Match": _etag(admin)}, json=change)
        assert refused.status_code == 403, (change, refused.text)
        assert refused.json()["error"]["code"] == "owner_required"
    assert _services(gateway)["engine"]["config"]["host"] == "192.168.1.60"
    # Edits that leave the credential and the host alone still work, and the key stays attached.
    allowed = admin.patch("/admin/api/operator/services/engine",
                          headers={**csrf_for(admin), "If-Match": _etag(admin)}, json={"note": "fine"})
    assert allowed.status_code == 200, allowed.text
    assert _services(gateway)["engine"]["config"]["api_key_env"] == "ENGINE_KEY"
    # The owner may move it, and the key follows the owner's choice.
    moved = owner.patch("/admin/api/operator/services/engine",
                        headers={**csrf_for(owner), "If-Match": _etag(owner)}, json={"host": "192.168.1.62"})
    assert moved.status_code == 200, moved.text
    assert "ENGINE_KEY" in _compiled(gateway, owner)


def test_an_administrator_cannot_replace_the_whole_registry_with_a_credentialed_record(gateway):
    _, _, _, admin = owner_and_admin(gateway)
    registry = service_registry.normalize_service_registry({"services": {"evil": {
        "id": "evil", "name": "Evil", "kind": "provider",
        "config": {"protocol": "openai-compatible", "host": "attacker.example", "port": 443,
                   "api_key_env": OWNER_KEY_ENV, "models": [{"id": "m", "upstream_model": "m"}]}}}})
    refused = admin.put("/admin/api/operator/service-registry",
                        headers={**csrf_for(admin), "If-Match": _etag(admin)}, json=registry)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    assert "evil" not in _services(gateway)


def _plant(gateway, **config):
    """Write a service record straight into the store, past every route."""
    record = {"id": "planted", "name": "Planted", "kind": "provider", "capabilities": ["openai"],
              "config": {"protocol": "openai-compatible", "models": [{"id": "m", "upstream_model": "m"}], **config}}
    value = service_registry.normalize_service_registry({"services": {"planted": record}})
    current = admin_store.get_service_registry_with_generation(path=gateway.path)
    admin_store.write_service_registry_with_audit(
        value, expected_generation=current["operator_settings_generation"] if current else 0,
        updated_by="test", audit_action="test.plant", audit_actor_kind="test", audit_actor_id="test",
        audit_actor_label="test", audit_target_kind="service", audit_target_id="planted", path=gateway.path)


def test_a_record_planted_in_the_store_compiles_without_the_key_for_a_host_it_was_not_made_for(gateway):
    owner, _, _, admin = owner_and_admin(gateway)
    _plant(gateway, host="attacker.example", port=443, scheme="https", api_key_env=OWNER_KEY_ENV)
    compiled = _compiled(gateway, admin)
    assert "attacker.example" in compiled  # the record is still served ...
    assert OWNER_KEY_ENV not in compiled  # ... but the owner's key is not sent to it
    # A harmless edit by an administrator rewrites the config file; it must not carry the key either.
    toggled = admin.post("/admin/api/operator/services/planted/enabled",
                         headers={**csrf_for(admin), "If-Match": _etag(admin)}, json={"enabled": True})
    assert toggled.status_code == 200, toggled.text
    assert OWNER_KEY_ENV not in _compiled(gateway, admin)


def test_a_record_planted_with_a_credential_origin_for_another_host_compiles_without_the_key(gateway):
    _plant(gateway, host="attacker.example", port=443, scheme="https", api_key_env=OWNER_KEY_ENV,
           credential_origin="https://api.openai.com:443")
    services = service_registry.load_service_registry(path=gateway.path)["services"]
    assert OWNER_KEY_ENV not in json.dumps(compile_litellm_substrate_config(services))


def test_a_record_whose_host_matches_the_origin_of_its_credential_keeps_the_key(gateway):
    _plant(gateway, host="engine.example", port=443, scheme="https", api_key_env="ENGINE_KEY",
           credential_origin="https://engine.example:443")
    services = service_registry.load_service_registry(path=gateway.path)["services"]
    assert "os.environ/ENGINE_KEY" in json.dumps(compile_litellm_substrate_config(services))


def test_the_owner_can_attach_a_credential_and_it_is_bound_to_the_host_they_chose(gateway):
    owner, _, _, _ = owner_and_admin(gateway)
    created = owner.post("/admin/api/operator/services", headers={**csrf_for(owner), "If-Match": _etag(owner)},
                         json=_service_body(name="Keyed", host="192.168.1.70", api_key_env="KEYED_KEY"))
    assert created.status_code == 201, created.text
    assert "os.environ/KEYED_KEY" in _compiled(gateway, owner)
