"""A partial service edit keeps every field it does not name.

The edit path rebuilds a record from flat form fields. ``secret_refs``,
``lifecycle``, ``kind`` and ``provenance`` are not form fields, so a note edit
used to drop a key held in ``secret_refs`` and turn a managed lifecycle back
into ``external``. Both break a running service without any error.
"""
from gateway import admin_store, service_registry
from gateway.litellm_substrate import compile_litellm_substrate_config
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

LIFECYCLE = {"type": "systemd", "managed": True, "service_name": "engine.service"}


def _install(gateway):
    registry = service_registry.load_service_registry(path=gateway.path)
    registry["services"]["engine"] = {
        "id": "engine", "name": "Engine", "kind": "provider", "source": "ui", "capabilities": ["openai"],
        "config": {"protocol": "openai-compatible", "host": "192.168.1.60", "port": 8000,
                   "models": [{"id": "engine-m", "upstream_model": "engine-m"}]},
        "secret_refs": {"api_key": "<external-secret>"},
        "lifecycle": dict(LIFECYCLE),
        "provenance": {"imported_from": "fixture"},
    }
    current = admin_store.get_service_registry_with_generation(path=gateway.path)
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry(registry),
        expected_generation=current["operator_settings_generation"], updated_by="test", audit_action="test.install",
        audit_actor_kind="test", audit_actor_id="test", audit_actor_label="test",
        audit_target_kind="service", audit_target_id="engine", path=gateway.path)


def _engine(gateway):
    return service_registry.load_service_registry(path=gateway.path)["services"]["engine"]


def _patch(client, body):
    etag = client.get("/admin/api/operator/service-registry").headers["etag"]
    return client.patch("/admin/api/operator/services/engine", headers={**csrf_for(client), "If-Match": etag}, json=body)


def _compiled(record):
    return compile_litellm_substrate_config({"engine": record})


def test_a_note_edit_keeps_secret_refs_lifecycle_kind_and_provenance(gateway):
    owner, _, _, admin = owner_and_admin(gateway)
    _install(gateway)
    before = _engine(gateway)
    assert before["secret_refs"] == {"api_key": "<external-secret>"} and before["lifecycle"] == LIFECYCLE

    for client in (admin, owner):
        assert _patch(client, {"note": f"edited by {id(client)}"}).status_code == 200

    after = _engine(gateway)
    for field in ("secret_refs", "lifecycle", "kind", "provenance"):
        assert after[field] == before[field], field
    assert _compiled(after) == _compiled(before)


def test_naming_one_lifecycle_field_changes_only_that_field(gateway):
    owner, _, _, _ = owner_and_admin(gateway)
    _install(gateway)

    assert _patch(owner, {"service_name": "renamed.service"}).status_code == 200

    assert _engine(gateway)["lifecycle"] == {**LIFECYCLE, "service_name": "renamed.service"}
    assert _engine(gateway)["secret_refs"] == {"api_key": "<external-secret>"}


def test_a_lifecycle_block_the_owner_sends_replaces_the_stored_one(gateway):
    owner, _, _, _ = owner_and_admin(gateway)
    _install(gateway)

    assert _patch(owner, {"lifecycle": {"type": "external"}}).status_code == 200

    assert _engine(gateway)["lifecycle"]["type"] == "external"
    assert _engine(gateway)["secret_refs"] == {"api_key": "<external-secret>"}
