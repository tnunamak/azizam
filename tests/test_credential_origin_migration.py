"""Schema 8 records, once, the host each existing credential was configured for.

Before schema 8 a service record named a credential and the compiler sent it
to whatever host the record named. Now the compiler sends a local record's key
only to the origin recorded with it. The owner configured every record that
exists today, so the migration trusts today's destination and records it.
Records made afterwards by an administrator get no such trust.
"""
import json
import sqlite3

from gateway import admin_store, service_registry
from gateway.litellm_substrate import compile_litellm_substrate_config
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

REMOTE_BASE = "https://api.typesafe.example/v1"


def _legacy_registry():
    """The three shapes the live registry holds, plus a local engine that names an env key."""
    return service_registry.normalize_service_registry({"services": {
        "litellm": {
            "id": "litellm", "name": "LiteLLM", "kind": "llm-substrate", "source": "builtin",
            "config": {"execution_substrate": "litellm", "protocol": "litellm",
                       "default_base_url": "http://127.0.0.1:4011", "api_key_env": "AZIZAM_LITELLM_API_KEY"}},
        "typesafe": {
            "id": "typesafe", "name": "TypeSafe", "kind": "provider", "source": "ui",
            "capabilities": ["openai"],
            "config": {"provider_type": "paid-remote", "remote_provider": True, "provider_id": "typesafe",
                       "protocol": "openai-compatible", "base_url": REMOTE_BASE,
                       "api_key_secret_id": "provider:typesafe:api_key",
                       "models": [{"id": "ts-model", "upstream_model": "ts-model"}]}},
        "engine": {
            "id": "engine", "name": "Engine", "kind": "provider", "source": "ui", "capabilities": ["openai"],
            "config": {"protocol": "openai-compatible", "host": "192.168.1.60", "port": 8000,
                       "api_key_env": "ENGINE_KEY", "models": [{"id": "engine-m", "upstream_model": "engine-m"}]}},
    }})


def _install_legacy_registry(path):
    """Put the registry in a database that is still at schema 7."""
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version=7")
        conn.execute("INSERT OR REPLACE INTO operator_settings VALUES ('service_registry', ?, 123, 'fixture')",
                     (json.dumps(_legacy_registry()),))
        conn.execute("INSERT OR REPLACE INTO operator_settings_generations VALUES ('service_registry', 5)")


def _emitted(path):
    """Per service: what the compiler emits, as (api_base, api_key) pairs."""
    services = service_registry.load_service_registry(path=path)["services"]
    out = {}
    for service_id, record in services.items():
        entries = (compile_litellm_substrate_config({service_id: record}) or {}).get("model_list", [])
        out[service_id] = [(e["litellm_params"].get("api_base"), e["litellm_params"].get("api_key")) for e in entries]
    return out


def test_the_migration_keeps_every_existing_key_going_to_the_host_it_went_to_before(gateway):
    _install_legacy_registry(gateway.path)
    before = _emitted(gateway.path)
    # Before: a local record with a key but no recorded origin is compiled with no key.
    assert before["engine"] == [("http://192.168.1.60:8000/v1", "sk-local")]
    assert before["typesafe"][0][1].startswith("os.environ/AZIZAM_PROVIDER_SECRET_")

    admin_store.init_db(gateway.path)

    after = _emitted(gateway.path)
    assert after["engine"] == [("http://192.168.1.60:8000/v1", "os.environ/ENGINE_KEY")]
    # The other records emit exactly what they emitted before.
    assert after["typesafe"] == before["typesafe"]
    assert after["litellm"] == before["litellm"] == []
    assert admin_store.schema_version(gateway.path) == admin_store.SCHEMA_VERSION == 8


def test_the_migration_records_the_current_destination_and_names_the_records_in_one_audit_entry(gateway):
    _install_legacy_registry(gateway.path)
    admin_store.init_db(gateway.path)
    services = service_registry.load_service_registry(path=gateway.path)["services"]
    assert services["engine"]["config"]["credential_origin"] == "http://192.168.1.60:8000"
    assert services["typesafe"]["config"]["credential_origin"] == "https://api.typesafe.example:443"
    assert services["litellm"]["config"]["credential_origin"] == "http://127.0.0.1:4011"
    audit = gateway.rows("SELECT metadata_json FROM audit_events WHERE action='operator.credential_origin.migrate'")
    assert len(audit) == 1
    assert json.loads(audit[0]["metadata_json"])["services"] == ["engine", "litellm", "typesafe"]
    assert "ENGINE_KEY" not in audit[0]["metadata_json"]


def test_the_migration_runs_once_and_leaves_records_without_a_credential_alone(gateway):
    _install_legacy_registry(gateway.path)
    with sqlite3.connect(gateway.path) as conn:
        value = json.loads(conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0])
        value["services"]["plain"] = {"id": "plain", "name": "Plain", "kind": "provider", "capabilities": ["openai"],
                                      "config": {"protocol": "openai-compatible", "host": "10.0.0.5", "port": 80,
                                                 "models": [{"id": "m", "upstream_model": "m"}]}}
        conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(value),))
    admin_store.init_db(gateway.path)
    first = gateway.rows("SELECT operator_settings_generation AS g FROM operator_settings_generations WHERE setting_key='service_registry'")
    admin_store.init_db(gateway.path)
    admin_store.init_db(gateway.path)
    assert gateway.rows("SELECT operator_settings_generation AS g FROM operator_settings_generations WHERE setting_key='service_registry'") == first
    assert len(gateway.rows("SELECT id FROM audit_events WHERE action='operator.credential_origin.migrate'")) == 1
    services = service_registry.load_service_registry(path=gateway.path)["services"]
    assert "credential_origin" not in services["plain"]["config"]


def test_an_origin_the_owner_already_recorded_is_not_replaced(gateway):
    _install_legacy_registry(gateway.path)
    with sqlite3.connect(gateway.path) as conn:
        value = json.loads(conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0])
        value["services"]["engine"]["config"]["credential_origin"] = "http://192.168.1.99:9000"
        conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(value),))
    admin_store.init_db(gateway.path)
    services = service_registry.load_service_registry(path=gateway.path)["services"]
    assert services["engine"]["config"]["credential_origin"] == "http://192.168.1.99:9000"


def test_after_the_migration_an_administrator_still_cannot_attach_a_key_or_move_a_keyed_record(gateway):
    owner, _, _, admin = owner_and_admin(gateway)
    _install_legacy_registry(gateway.path)
    admin_store.init_db(gateway.path)

    def headers(client):
        etag = client.get("/admin/api/operator/service-registry").headers["etag"]
        return {**csrf_for(client), "If-Match": etag}

    created = admin.post("/admin/api/operator/services", headers=headers(admin), json={
        "name": "Evil", "host": "attacker.example", "port": 443, "protocol": "openai-compatible",
        "api_key_env": "OPENAI_API_KEY", "models": [{"public": "m", "upstream": "m"}]})
    assert created.status_code == 403, created.text
    assert created.json()["error"]["code"] == "owner_required"
    moved = admin.patch("/admin/api/operator/services/engine", headers=headers(admin), json={"host": "attacker.example"})
    assert moved.status_code == 403, moved.text
    # A harmless edit still works, and the migrated key keeps going to its own host.
    noted = admin.patch("/admin/api/operator/services/engine", headers=headers(admin), json={"note": "fine"})
    assert noted.status_code == 200, noted.text
    compiled = owner.get("/admin/api/operator/litellm-config").json()["litellm_config"]
    engine = [e for e in compiled["model_list"] if e["model_name"] == "engine-m"]
    assert [(e["litellm_params"]["api_base"], e["litellm_params"]["api_key"]) for e in engine] == [
        ("http://192.168.1.60:8000/v1", "os.environ/ENGINE_KEY")]


def test_a_record_planted_after_the_migration_compiles_without_a_key(gateway):
    _install_legacy_registry(gateway.path)
    admin_store.init_db(gateway.path)
    registry = service_registry.load_service_registry(path=gateway.path)
    registry["services"]["planted"] = {
        "id": "planted", "name": "Planted", "kind": "provider", "capabilities": ["openai"],
        "config": {"protocol": "openai-compatible", "host": "attacker.example", "port": 443, "scheme": "https",
                   "api_key_env": "OPENAI_API_KEY", "models": [{"id": "pm", "upstream_model": "pm"}]}}
    current = admin_store.get_service_registry_with_generation(path=gateway.path)
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry(registry),
        expected_generation=current["operator_settings_generation"], updated_by="test", audit_action="test.plant",
        audit_actor_kind="test", audit_actor_id="test", audit_actor_label="test",
        audit_target_kind="service", audit_target_id="planted", path=gateway.path)
    assert _emitted(gateway.path)["planted"] == [("https://attacker.example:443/v1", "sk-local")]
