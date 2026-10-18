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
    assert not reaches_engine_directly("engine-a", record)
    view = engine_view(load_engine_catalog(), record, db_path=tmp_path / "admin.db")
    # The current form still gates fields it cannot apply; the v2 contract
    # carries the independent record permission for the later editor.
    assert view["editable"] is False
    assert view["v2"]["editable"] is True
    assert view["v2"]["connection"]["headers"] == [{"name": "Authorization", "value": "configured"}]
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


def test_v2_migration_refuses_conflicting_copies_without_partial_change() -> None:
    old = _service()
    old["config"]["lifecycle"]["driver_config"]["start"] = ["other"]
    registry = {"schema_version": 1, "services": {"engine-a": old}}
    before = copy.deepcopy(registry)
    with pytest.raises(ValueError, match=r"engine-a.*lifecycle.*driver_config.*conflict"):
        service_registry.migrate_engine_records_v2(registry)
    assert registry == before


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
