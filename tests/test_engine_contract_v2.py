"""Schema v2.1 contract tests. Changes to the frozen observations need a stated contract reason."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from gateway import admin_store, service_registry
from gateway.engine_admin import engine_view
from gateway.engine_definitions import load_engine_catalog
from gateway.litellm_substrate import reaches_engine_directly


def _service(*, source: str = "ui", kind: str = "provider") -> dict:
    lifecycle = {"driver": "command", "driver_config": {"start": ["start"], "status": ["status"]}}
    return {
        "id": "engine-a", "name": "Engine A", "kind": kind, "source": source,
        "enabled": True, "capabilities": ["chat"],
        "config": {
            "address": "http://engine.invalid", "protocol": "openai-compatible",
            "api_key_env": "TEST_ENGINE_KEY", "credential_origin": "http://engine.invalid",
            "models": [
                {"id": "first", "model_group": "shared", "upstream_model": "upstream-a"},
                {"id": "second", "model_group": "shared", "upstream_model": "upstream-b"},
            ],
            "lifecycle": copy.deepcopy(lifecycle),
        },
        "lifecycle": {**copy.deepcopy(lifecycle), "type": "command", "managed": True, "service_name": ""},
        "editable": True,
    }


def test_v2_view_keeps_offering_identity_separate_from_public_names() -> None:
    view = service_registry.service_record_v2(_service())
    assert view["schema_version"] == 2
    assert view["id"] == "engine-a"
    assert [row["id"] for row in view["offerings"]] == ["first", "second"]
    assert [row["upstream"] for row in view["offerings"]] == ["upstream-a", "upstream-b"]
    assert all(row["selection"]["mode"] == "request_field" for row in view["offerings"])
    assert all(row["tasks"] == [{"task": "chat", "evidence": "declared"}] for row in view["offerings"])
    assert "public_name" not in view["offerings"][0]
    assert view["connection"]["credentials"]["inference"]["env"] == "TEST_ENGINE_KEY"
    assert view["connection"]["credentials"]["control"] is None
    assert view["process"]["driver"] == "command"
    assert view["observations"] == {}
    assert view["residency"] == {}
    assert view["resources"] == {}


def test_v2_view_preserves_record_permission_and_marks_substrate_as_system() -> None:
    ordinary = _service(source="provisioned")
    ordinary["editable"] = True
    assert service_registry.service_record_v2(ordinary)["editable"] is True
    system = _service(source="builtin", kind="llm-substrate")
    system["id"] = "litellm"
    system["editable"] = False
    view = service_registry.service_record_v2(system)
    assert view["record_type"] == "system"
    assert view["editable"] is False
    assert view["derived"] is True


@pytest.mark.parametrize("source", sorted(service_registry.SERVICE_SOURCES))
def test_v2_view_accepts_all_legacy_sources(source: str) -> None:
    view = service_registry.service_record_v2(_service(source=source))
    assert view["provenance"]["source"] == source
    assert view["offerings"][0]["tasks"][0]["evidence"] == "declared"


def test_v2_view_maps_systemd_profile_and_legacy_resources() -> None:
    record = _service()
    lifecycle = {"driver": "systemd", "driver_config": {
        "unit": "example.service", "scope": "user", "dropin": "/synthetic/dropin.conf"}}
    record["lifecycle"] = copy.deepcopy(lifecycle)
    record["config"]["lifecycle"] = copy.deepcopy(lifecycle)
    record["config"]["resources"] = {"gpu": {"devices": [0]}}
    view = service_registry.service_record_v2(record)
    assert view["process"]["driver"] == "command"
    assert view["process"]["start"] == ["systemctl", "--user", "start", "example.service"]
    assert view["process"]["profile_apply"]["kind"] == "write_env_file_then_restart"
    assert view["resources"] == {"gpu": {"devices": [0]}}


@pytest.mark.parametrize("driver,expected", [("docker", "command"), ("http-load-unload", "none"), ("future-driver", "none")])
def test_v2_view_never_promotes_legacy_driver_or_shell_string(driver: str, expected: str) -> None:
    record = _service()
    lifecycle = {"driver": driver, "driver_config": {"start": "unsafe shell string"}}
    record["lifecycle"] = copy.deepcopy(lifecycle)
    record["config"]["lifecycle"] = copy.deepcopy(lifecycle)
    view = service_registry.service_record_v2(record)
    assert view["process"]["driver"] == expected
    assert "start" not in view["process"]
    assert view["process"]["needs_owner_conversion"] is True
    if driver == "http-load-unload":
        assert view["residency"]["needs_owner_conversion"] is True


def test_engine_admin_exposes_v2_beside_existing_fields_and_uses_record_permission(tmp_path: Path) -> None:
    record = _service()
    record["config"]["gateway_lifecycle"] = True
    record["config"]["headers"] = [{"name": "Authorization", "value": "test-secret-value"}]
    record["config"]["api_key_secret_id"] = "test-secret-reference"
    assert not reaches_engine_directly("engine-a", record)
    view = engine_view(load_engine_catalog(), record, db_path=tmp_path / "admin.db")
    # The final hop now reads the edited record, so its routing path does not
    # impose a second permission rule on the form.
    assert view["editable"] is True
    assert view["v2"]["editable"] is True
    assert view["v2"]["connection"]["headers"] == [{"name": "Authorization", "value": "configured"}]
    assert view["v2"]["connection"]["credentials"]["inference"]["secret_ref"] == "configured"
    assert "test-secret-reference" not in json.dumps(view["v2"])
    assert "legacy_lifecycle" not in (view["v2"]["process"] or {})
    assert "values" in view and "models" in view and "key" in view


def test_v2_migration_collapses_agreeing_lifecycle_copies_and_preserves_runtime() -> None:
    old = _service()
    before = service_registry.runtime_backend_from_service(old)
    registry = {"schema_version": 1, "services": {"engine-a": old}}
    changed = service_registry.migrate_engine_records_v2(registry)
    assert changed == ["engine-a"]
    assert registry["schema_version"] == 2
    migrated = registry["services"]["engine-a"]
    assert "lifecycle" not in migrated
    assert "lifecycle" not in migrated["config"]
    assert migrated["process"]["driver"] == "command"
    assert service_registry.runtime_backend_from_service(migrated) == before
    assert service_registry.migrate_engine_records_v2(registry) == []


def test_v2_record_stays_collapsed_after_normalization_and_legacy_edit() -> None:
    registry = {"schema_version": 1, "services": {"engine-a": _service()}}
    service_registry.migrate_engine_records_v2(registry)
    normalized = service_registry.normalize_service_registry(registry)
    record = normalized["services"]["engine-a"]
    assert "lifecycle" not in record
    assert "lifecycle" not in record["config"]

    raw = copy.deepcopy(record["config"])
    raw.update(name="Renamed", lifecycle=copy.deepcopy(record["process"]["legacy_lifecycle"]))
    edited, record = service_registry.upsert_service_in_registry(
        normalized, service_id="engine-a", raw=raw, replace=True,
    )
    assert edited["schema_version"] == 2
    assert record["name"] == "Renamed"
    assert "lifecycle" not in record
    assert "lifecycle" not in record["config"]
    assert record["process"]["legacy_lifecycle"]["driver"] == "command"


def test_v2_direct_normalization_and_import_stay_collapsed() -> None:
    registry = {"schema_version": 1, "services": {"engine-a": _service()}}
    service_registry.migrate_engine_records_v2(registry)
    record = service_registry.normalize_service_record("engine-a", registry["services"]["engine-a"])
    assert "lifecycle" not in record

    imported = service_registry.merge_imported_records(
        {"schema_version": 2, "services": {}},
        services={"engine-a": _service(source="imported")},
    )
    record = imported["services"]["engine-a"]
    assert "lifecycle" not in record
    assert "lifecycle" not in record["config"]
    assert record["process"]["driver"] == "command"


def test_v2_migration_refuses_conflicting_copies_without_partial_change() -> None:
    old = _service()
    old["config"]["lifecycle"]["driver_config"]["start"] = ["other"]
    registry = {"schema_version": 1, "services": {"engine-a": old}}
    before = copy.deepcopy(registry)
    with pytest.raises(ValueError, match=r"engine-a.*lifecycle.*driver_config.*conflict"):
        service_registry.migrate_engine_records_v2(registry)
    assert registry == before


def test_v2_migration_refuses_nested_only_driver_and_preserves_normalized_runtime() -> None:
    record = _service()
    record["lifecycle"].pop("driver")
    record["lifecycle"].pop("driver_config")
    registry = {"schema_version": 1, "services": {"engine-a": record}}
    with pytest.raises(ValueError, match=r"engine-a.*lifecycle.*driver.*conflict"):
        service_registry.migrate_engine_records_v2(registry)

    record["config"].pop("lifecycle")
    record["lifecycle"]["status"] = "not-a-runtime-field"
    before = service_registry.runtime_backends(service_registry.normalize_service_registry(registry))
    service_registry.migrate_engine_records_v2(registry)
    after = service_registry.runtime_backends(service_registry.normalize_service_registry(registry))
    assert after == before


def test_v2_command_changes_still_require_owner() -> None:
    old = {"schema_version": 2, "services": {}}
    new = {"schema_version": 2, "services": {"engine-a": _service()}}
    assert "lifecycle" in service_registry.credential_binding_changes(old, new)["engine-a"]
    migrated = {"schema_version": 1, "services": {"engine-a": _service()}}
    service_registry.migrate_engine_records_v2(migrated)
    edited = copy.deepcopy(migrated)
    edited["services"]["engine-a"]["process"]["start"] = ["different"]
    assert "lifecycle" in service_registry.credential_binding_changes(migrated, edited)["engine-a"]


def test_owner_gate_catches_legacy_driver_beside_process_marker() -> None:
    record = _service()
    record["process"] = {"driver": "none"}
    before = {"schema_version": 1, "services": {}}
    after = {"schema_version": 1, "services": {"engine-a": record}}
    assert "lifecycle" in service_registry.credential_binding_changes(before, after)["engine-a"]


def test_v2_record_readers_keep_legacy_availability_and_admin_shape() -> None:
    from gateway.availability import _is_idle_startable
    from gateway.engine_admin import redacted_record
    from gateway.model_files import _host_managed_lifecycle

    record = _service()
    record["lifecycle"]["driver"] = "systemd"
    record["config"]["lifecycle"]["driver"] = "systemd"
    before = (_is_idle_startable(record), _host_managed_lifecycle(record))
    registry = {"schema_version": 1, "services": {"engine-a": record}}
    service_registry.migrate_engine_records_v2(registry)
    migrated = registry["services"]["engine-a"]
    assert (_is_idle_startable(migrated), _host_managed_lifecycle(migrated)) == before == (True, True)
    assert redacted_record(migrated)["lifecycle"]["driver"] == "systemd"


def test_v2_migration_preserves_external_and_system_runtime_wrappers() -> None:
    ordinary = _service()
    ordinary["lifecycle"] = {"type": "external", "managed": False, "service_name": ""}
    ordinary["config"]["lifecycle"] = "external"
    system = copy.deepcopy(ordinary)
    system.update(id="litellm", kind="llm-substrate", source="builtin", editable=False)
    system["lifecycle"]["service_name"] = "managed-sidecar"
    original = {"engine-a": ordinary, "litellm": system}
    before = {key: service_registry.runtime_backend_from_service(row) for key, row in original.items()}
    registry = {"schema_version": 1, "services": copy.deepcopy(original)}
    service_registry.migrate_engine_records_v2(registry)
    after = {key: service_registry.runtime_backend_from_service(row) for key, row in registry["services"].items()}
    assert after == before


def test_admin_schema_migration_refuses_conflict_atomically(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    service = _service()
    service["config"]["lifecycle"]["driver"] = "systemd"
    registry = {"schema_version": 1, "services": {"engine-a": service}}
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE schema_version SET version=9")
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES ('service_registry',?,0)", (json.dumps(registry),))
    with pytest.raises(ValueError, match=r"engine-a.*lifecycle.*driver.*conflict"):
        admin_store.init_db(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9
        assert json.loads(conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0]) == registry
