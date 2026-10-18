"""Schema v2.1 contract tests. Changes to the frozen observations need a stated contract reason."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from gateway import admin_store, service_registry


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
