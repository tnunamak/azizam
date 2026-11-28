"""Named step loaded-model-probe: a stored service's state_type becomes the loaded_model_probe it meant."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from gateway import admin_store, loaded_model_probe_migration

PROPS = {"endpoint": "/props", "model_key": "model_path"}


def _stored(db: Path) -> dict:
    with closing(sqlite3.connect(db)) as conn:
        return json.loads(conn.execute(
            "SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0])


def _store_with(tmp_path: Path, registry: dict) -> Path:
    """A real admin database whose stored registry predates the step."""
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT OR REPLACE INTO operator_settings(key, value_json, updated_at, updated_by) "
                     "VALUES ('service_registry', ?, 1, 'owner')", (json.dumps(registry),))
        conn.execute("DELETE FROM schema_migration_steps WHERE name=?", (loaded_model_probe_migration.STEP_NAME,))
        conn.commit()
    return db


def _config(sid: str, **fields) -> dict:
    return {"id": sid, "enabled": True, "config": {"port": 5051, **fields}}


def test_each_stored_record_keeps_the_probe_it_was_read_through(tmp_path):
    registry = {"services": {
        "path-llm": _config("path-llm", state_type="llama_cpp", loaded_model_identity="path", aux={"flag": "--x"}),
        "path-default": _config("path-default", loaded_model_identity="path"),
        "served-id": _config("served-id", state_type="vllm"),
        "compatible": _config("compatible", state_type="openai_compatible"),
        "odd": _config("odd", state_type="something-else"),
        "declared": _config("declared", state_type="llama_cpp", loaded_model_probe={"endpoint": "/v", "model_key": "k"}),
        "plain": _config("plain"),
        "aux-only": _config("aux-only", aux={"flag": "--x"}),
        "no-config": {"id": "no-config", "enabled": False},
    }}
    db = _store_with(tmp_path, registry)
    admin_store.init_db(db)
    services = _stored(db)["services"]
    configs = {sid: record.get("config") for sid, record in services.items()}
    assert configs["path-llm"]["loaded_model_probe"] == PROPS
    assert configs["path-default"]["loaded_model_probe"] == PROPS
    assert configs["declared"]["loaded_model_probe"] == {"endpoint": "/v", "model_key": "k"}
    for sid in ("served-id", "compatible", "odd", "plain", "aux-only"):
        assert "loaded_model_probe" not in configs[sid], sid
    assert all("state_type" not in (config or {}) for config in configs.values())
    assert configs["path-llm"]["aux"] == {"flag": "--x"} and configs["plain"] == {"port": 5051}
    assert services["no-config"] == {"id": "no-config", "enabled": False}
    with closing(sqlite3.connect(db)) as conn:
        audit = conn.execute("SELECT metadata_json FROM audit_events WHERE action='operator.loaded_model_probe.migrate'"
                             ).fetchall()
    assert len(audit) == 1
    assert json.loads(audit[0][0])["services"] == {
        "path-llm": "probe", "path-default": "probe", "served-id": "default", "compatible": "default",
        "odd": "unknown-state-type", "declared": "kept-declared-probe"}


def test_the_step_runs_once(tmp_path):
    db = _store_with(tmp_path, {"services": {"a": _config("a", state_type="vllm")}})
    admin_store.init_db(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'",
                     (json.dumps({"services": {"a": _config("a", loaded_model_identity="path")}}),))
        conn.commit()
    admin_store.init_db(db)
    assert "loaded_model_probe" not in _stored(db)["services"]["a"]["config"]


def test_a_new_database_records_the_step_without_a_registry(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT 1 FROM schema_migration_steps WHERE name=?",
                            (loaded_model_probe_migration.STEP_NAME,)).fetchone()
