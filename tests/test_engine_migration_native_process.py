"""Schema 2 migration keeps a native process block and refuses to guess between two sources."""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from gateway import admin_store, service_registry

NATIVE = {"driver": "process", "start": ["runner"], "port": {"fixed": 8080}, "env": {"MODE": "prod"}}
LEGACY_COMMAND = {"driver": "command", "driver_config": {"start": ["legacy-start"], "status": ["legacy-status"]}}


def _record(**extra) -> dict:
    return {"id": "engine-a", "name": "Engine A", "kind": "provider", "source": "ui", "enabled": True,
            "capabilities": ["chat"], "editable": True,
            "config": {"address": "http://engine.invalid", "protocol": "openai-compatible",
                       "models": [{"id": "first", "upstream_model": "upstream-a"}]}, **extra}


def _legacy(lifecycle: dict, **extra) -> dict:
    record = _record(**extra)
    record["lifecycle"] = {**copy.deepcopy(lifecycle), "type": lifecycle["driver"], "managed": True, "service_name": ""}
    record["config"]["lifecycle"] = copy.deepcopy(lifecycle)
    return record


def _migrate(*records: dict) -> dict:
    registry = {"schema_version": 1, "services": {row["id"]: row for row in records}}
    service_registry.migrate_engine_records_v2(registry)
    return registry


def test_a_native_process_block_survives_migration_whole() -> None:
    registry = _migrate(_record(process=copy.deepcopy(NATIVE)))
    process = registry["services"]["engine-a"]["process"]
    assert {key: process[key] for key in NATIVE} == NATIVE
    assert process["driver"] == "process"


def test_a_native_process_beside_a_default_external_lifecycle_keeps_the_runtime_backend() -> None:
    record = _record(process=copy.deepcopy(NATIVE))
    record["lifecycle"] = {"type": "external", "managed": False, "service_name": ""}
    before = service_registry.runtime_backend_from_service(record)
    registry = _migrate(record)
    assert service_registry.runtime_backend_from_service(registry["services"]["engine-a"]) == before
    assert "lifecycle" not in registry["services"]["engine-a"]


def test_a_native_process_in_config_is_not_shadowed_by_the_default() -> None:
    record = _record()
    record["config"]["process"] = copy.deepcopy(NATIVE)
    registry = _migrate(record)
    migrated = registry["services"]["engine-a"]
    assert {key: migrated["process"][key] for key in NATIVE} == NATIVE
    assert "process" not in migrated["config"]


def test_a_native_process_and_a_conflicting_legacy_lifecycle_are_refused_before_any_change() -> None:
    mixed = _legacy(LEGACY_COMMAND, process=copy.deepcopy(NATIVE))
    plain = _legacy(LEGACY_COMMAND)
    plain["id"] = "engine-b"
    registry = {"schema_version": 1, "services": {"engine-a": mixed, "engine-b": plain}}
    before = copy.deepcopy(registry)
    with pytest.raises(ValueError, match=r"engine-a.*both.*process.*lifecycle"):
        service_registry.migrate_engine_records_v2(registry)
    assert registry == before


def test_the_refusal_names_the_service_and_both_blocks_and_says_to_keep_one() -> None:
    registry = {"schema_version": 1, "services": {"engine-a": _legacy(LEGACY_COMMAND, process=copy.deepcopy(NATIVE))}}
    with pytest.raises(ValueError) as refused:
        service_registry.migrate_engine_records_v2(registry)
    message = str(refused.value)
    assert "'engine-a'" in message and "process" in message and "lifecycle" in message
    assert "keep one" in message.lower() and "delete" in message.lower()
    assert "legacy-start" not in message and "runner" not in message


def test_a_top_level_process_and_a_different_config_process_are_refused_before_any_change() -> None:
    mixed = _record(process=copy.deepcopy(NATIVE))
    mixed["config"]["process"] = {**NATIVE, "start": ["other-runner"]}
    registry = {"schema_version": 1, "services": {"engine-a": mixed}}
    before = copy.deepcopy(registry)
    with pytest.raises(ValueError) as refused:
        service_registry.migrate_engine_records_v2(registry)
    message = str(refused.value)
    assert "'engine-a'" in message and "config.process" in message and "keep one" in message.lower()
    assert "other-runner" not in message
    assert registry == before


def test_equal_top_level_and_config_process_blocks_migrate_to_one() -> None:
    record = _record(process=copy.deepcopy(NATIVE))
    record["config"]["process"] = copy.deepcopy(NATIVE)
    migrated = _migrate(record)["services"]["engine-a"]
    assert {key: migrated["process"][key] for key in NATIVE} == NATIVE
    assert "process" not in migrated["config"]


def test_a_process_marker_beside_a_legacy_lifecycle_still_takes_the_legacy_command() -> None:
    registry = _migrate(_legacy(LEGACY_COMMAND, process={"driver": "none"}))
    process = registry["services"]["engine-a"]["process"]
    assert process["driver"] == "command" and process["start"] == ["legacy-start"]


def test_mixed_records_each_migrate_by_their_own_source() -> None:
    legacy = _legacy(LEGACY_COMMAND)
    native = _record(process=copy.deepcopy(NATIVE))
    native["id"] = "engine-b"
    plain = _record()
    plain["id"] = "engine-c"
    registry = _migrate(legacy, native, plain)
    rows = registry["services"]
    assert rows["engine-a"]["process"]["driver"] == "command"
    assert rows["engine-b"]["process"]["start"] == ["runner"] and rows["engine-b"]["process"]["port"] == {"fixed": 8080}
    assert rows["engine-c"]["process"]["driver"] == "none"


def test_migrating_twice_equals_migrating_once_byte_for_byte() -> None:
    legacy = _legacy(LEGACY_COMMAND)
    native = _record(process=copy.deepcopy(NATIVE))
    native["id"] = "engine-b"
    plain = _record()
    plain["id"] = "engine-c"
    once = _migrate(legacy, native, plain)
    twice = copy.deepcopy(once)
    assert service_registry.migrate_engine_records_v2(twice) == []
    assert json.dumps(twice, sort_keys=True) == json.dumps(once, sort_keys=True)
    # Forced through the per-record path a second time: the same bytes again.
    forced = copy.deepcopy(once)
    forced["schema_version"] = 1
    service_registry.migrate_engine_records_v2(forced)
    assert json.dumps(forced, sort_keys=True) == json.dumps(once, sort_keys=True)
    assert json.dumps(service_registry.normalize_service_registry(once), sort_keys=True) == json.dumps(
        service_registry.normalize_service_registry(service_registry.normalize_service_registry(once)), sort_keys=True)


def test_the_admin_schema_migration_keeps_a_native_process_and_refuses_a_mixed_record(tmp_path) -> None:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    registry = {"schema_version": 1, "services": {"engine-a": _record(process=copy.deepcopy(NATIVE))}}
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE schema_version SET version=9")
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES ('service_registry',?,0)", (json.dumps(registry),))
    admin_store.init_db(db)
    stored = admin_store.get_service_registry_with_generation(path=db)["value"]["services"]["engine-a"]
    assert stored["process"]["start"] == ["runner"]

    mixed = {"schema_version": 1, "services": {"engine-a": _legacy(LEGACY_COMMAND, process=copy.deepcopy(NATIVE))}}
    other = tmp_path / "mixed.db"
    admin_store.init_db(other)
    with sqlite3.connect(other) as conn:
        conn.execute("UPDATE schema_version SET version=9")
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES ('service_registry',?,0)", (json.dumps(mixed),))
    admin_store.init_db(other)  # a refused record no longer stops the gateway from starting
    with sqlite3.connect(other) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
        stored = json.loads(conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0])
    assert stored["services"]["engine-a"]["migration_refused"]
