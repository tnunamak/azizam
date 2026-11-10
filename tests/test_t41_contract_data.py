"""Contract files refuse ambiguous or undeclared policy."""

import pytest
import yaml

from gateway.compatibility import REGISTRY, load_compatibility
from gateway.storage_kinds import load_storage_kinds


@pytest.mark.parametrize("name,loader", [("compatibility", load_compatibility), ("storage_kinds", load_storage_kinds)])
def test_contract_refuses_unknown_duplicate_and_invalid_fields(tmp_path, name, loader):
    original = (REGISTRY / f"{name}.yml").read_text()
    path = tmp_path / "contract.yml"
    for document in (original + "\nunknown: true\n", original + "\nschema_version: 1\n",
                     original.replace("schema_version: 1", "schema_version: 2"),
                     original.replace("schema_version: 1", "schema_version: true")):
        path.write_text(document)
        with pytest.raises((ValueError, yaml.YAMLError)):
            loader(path)
    value = yaml.safe_load(original)
    if name == "storage_kinds":
        value["kinds"].append(value["kinds"][0])
    else:
        value["migration"]["unknown"] = "refused"
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError):
        loader(path)


def test_storage_kind_contract_drives_validation_and_projection(tmp_path, monkeypatch):
    import importlib
    from pydantic import ValidationError
    from gateway import owner_projection as projection, storage_kinds

    path = tmp_path / "storage.yml"
    kinds = ["image", "video", "audio", "voice", "other", "archive"]
    path.write_text(yaml.safe_dump({"schema_version": 1, "kinds": kinds}))
    with monkeypatch.context() as patch:
        patch.setattr(storage_kinds, "load_storage_kinds", lambda: load_storage_kinds(path))
        importlib.reload(projection)
        try:
            for kind in kinds:
                assert projection.StorageView(category="gateway", media_kind=kind).media_kind == kind
                assert projection.project("storage", {"category": "gateway", "media_kind": kind}).media_kind == kind
            with pytest.raises(ValidationError):
                projection.StorageView(category="gateway", media_kind="undeclared")
            assert projection.project("storage", {"category": "gateway", "media_kind": "undeclared"}).media_kind is None
            assert projection.StorageView.model_json_schema()["properties"]["media_kind"]["anyOf"][0]["enum"] == kinds
        finally:
            patch.undo()
            importlib.reload(projection)


def test_compatibility_rules_are_versioned_data(tmp_path, monkeypatch):
    import json
    import sqlite3
    from gateway import admin_store, stable_aliases, stt_chunk_transform_migration as migration, video_limits

    document = yaml.safe_load((REGISTRY / "compatibility.yml").read_text())
    document["migration"]["include_capabilities"].append("unfamiliar-transcript")
    document["legacy_alias_tables"]["novel-operation"] = "novel-section"
    document["limit_key_aliases"].update({"video.generate.one": "second", "video.edit.two": "first"})
    path = tmp_path / "compatibility.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    contract = load_compatibility(path)
    monkeypatch.setattr(migration, "COMPATIBILITY", contract)
    monkeypatch.setattr(video_limits, "COMPATIBILITY", contract)
    monkeypatch.setattr(stable_aliases, "LEGACY_ALIAS_TABLES", tuple(contract.legacy_alias_tables.items()))
    cfg = {}
    assert migration.attach_timing_row(cfg, ["unfamiliar-transcript"])
    assert not migration.attach_timing_row(cfg, ["unfamiliar-transcript"])
    assert not migration.attach_timing_row({}, ["chat", "unfamiliar-transcript"])
    assert migration.attach_timing_row({"chunking": {}}, [])
    assert not migration.attach_timing_row({}, ["speech"])
    published = {"first": {"max_seconds": 1}, "second": {"max_seconds": 2}, "video-one": {"max_seconds": 99}}
    assert video_limits.limits_for_workflow(published, "video.generate.one") == {"max_seconds": 2}
    assert video_limits.limits_for_workflow(published, "video.edit.two") == {"max_seconds": 1}
    assert video_limits.limits_for_workflow(published, "video.generate.undeclared") == {}
    assert video_limits.limits_for_workflow({**published, "video.generate.one": {}}, "video.generate.one") == {}
    config = {"model_aliases": {}, **{section: {} for section in contract.legacy_alias_tables.values()}}
    for operation in contract.legacy_alias_tables:
        config["model_aliases"][operation] = {"stable": {"short": "concrete-target"}}
    stable_aliases.fold_workflow_aliases(config)
    for operation, section in contract.legacy_alias_tables.items():
        assert config[section]["aliases"]["short"] == stable_aliases.resolve_alias(config["model_aliases"], operation, "short").target
    # Exercise the real startup migration twice, including an operator removal.
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    registry = {"services": {"example": {"enabled": True, "capabilities": ["unfamiliar-transcript"], "config": {}}}}
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE schema_version SET version=5")
        conn.execute("INSERT INTO operator_settings VALUES ('service_registry', ?, 123, 'fixture')", (json.dumps(registry),))
        conn.execute("INSERT INTO operator_settings_generations VALUES ('service_registry', 1)")
    admin_store.init_db(db)
    with sqlite3.connect(db) as conn:
        updated = json.loads(conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0])
        assert updated["services"]["example"]["config"]["transforms"] == [migration.TIMING_DIAGNOSTIC_ROW]
        updated["services"]["example"]["config"]["transforms"] = []
        conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(updated),))
    admin_store.init_db(db)
    with sqlite3.connect(db) as conn:
        final = json.loads(conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0])
        assert final["services"]["example"]["config"]["transforms"] == []
