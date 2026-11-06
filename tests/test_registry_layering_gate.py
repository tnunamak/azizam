"""C4: the one-time registry layering step (docs/design/CONFIG_HOMES.md, 8.3) on a registry shaped like a real one.

The fixture holds what a live registry holds: builtin frontends (some stored before they carried their routes, one
missing), the engines of the startup file stored as ``provisioned`` records, records made in the UI, a LiteLLM row with a
name, an enabled flag and a note the owner changed, and no OpenRouter row (the old boot never backfilled it). The step
must leave what the gateway serves where it was: the route table, the frontend mount table, ``/v1/models`` and the
alias table (gate of 8.3 step 4). Nothing here reads a live database.
"""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import admin_store, registry_layers, registry_migration, route_registry, service_registry  # noqa: E402
from scripts import registry_layering_report  # noqa: E402
from tests.config_homes_golden import alias_table, digest, models_view  # noqa: E402
from tests.registry_test_helpers import seed_registry_document, write_legacy_registry_for_test  # noqa: E402

SENTINEL = "sentinel-value-must-never-print"
STEP = registry_layers.LAYERING_STEP


def _live_shaped_registry() -> dict:
    """A whole registry document as an install of the previous release stored it."""
    raw = yaml.safe_load((ROOT / "proxy_config.yml").read_text(encoding="utf-8"))
    document = seed_registry_document(raw)  # provisioned engines + the release records, as the old seed built them
    document["services"].pop("openrouter", None)  # never backfilled
    litellm = document["services"]["litellm"]
    litellm.update({"name": "Operator LiteLLM", "enabled": False})
    litellm["config"]["note"] = "the owner's own note"  # where the managed row keeps a note
    for service_id in ("ui-chat", "ui-voice", "ui-notes"):  # made in the UI: no release record
        document["services"][service_id] = service_registry.normalize_service_record(service_id, {
            "name": service_id, "source": "ui", "kind": "provider", "enabled": True, "capabilities": ["chat"],
            "config": {"base_url": f"http://127.0.0.1:{9100 + len(service_id)}/v1", "note": SENTINEL},
        })
    frontends = document["protocol_frontends"]
    for frontend_id in ("anthropic", "gateway"):  # stored before they carried routes and discovery data
        frontends[frontend_id].pop("registered_routes", None)
        frontends[frontend_id].pop("public_discovery", None)
    frontends.pop("mesh", None)  # a builtin the store never held; it mounts from the release
    frontends["acme"] = service_registry.normalize_protocol_frontend("acme", {
        "name": "Acme", "protocol": "acme", "source": "ui", "routes": ["/acme/v1"], "intents": ["acme.run"]})
    return document


def _boot(db: Path, stored_registry=None) -> dict:
    raw = yaml.safe_load((ROOT / "proxy_config.yml").read_text(encoding="utf-8"))
    with patch.object(proxy, "ADMIN_DB_PATH", db):
        if stored_registry is None:
            return proxy._load_effective_runtime_config(copy.deepcopy(raw), boot=True)
        with patch.object(proxy, "_stored_service_registry", lambda: stored_registry):
            return proxy._load_effective_runtime_config(copy.deepcopy(raw), boot=True)


def _observables(effective: dict, registry: dict) -> dict:
    return {
        "routes": route_registry.registered_route_records(registry=registry, proxy_config=effective),
        "route_aliases": [dict(item) for item in route_registry.canonical_protocol_route_aliases(
            registry=registry, proxy_config=effective)],
        "mounts": route_registry.mount_frontend_records(registry=registry, proxy_config=effective),
        "models": models_view(proxy, effective),
        "aliases": alias_table(proxy, effective),
        "backends": digest(effective["backends"]),
    }


@pytest.fixture
def old_install(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    document = _live_shaped_registry()
    write_legacy_registry_for_test(document, path=db)
    admin_store.record_audit("operator.service.updated", actor_kind="user", actor_id="owner",
                             target_kind="service", target_id="litellm", path=db)
    return db, document


def test_the_step_leaves_the_route_table_mounts_models_and_aliases_where_they_were(old_install, tmp_path):
    db, document = old_install
    catalogs = service_registry.load_seed_catalogs({})
    before_registry, _carried = registry_migration.effective_before(document, catalogs)  # what the old boot served
    before_registry = json.loads(json.dumps(before_registry, sort_keys=True))  # the store keeps keys in sorted order
    before = _observables(_boot(tmp_path / "before.db", stored_registry=before_registry), before_registry)

    after_registry = service_registry.ensure_service_registry(path=db, actor="test")  # the step runs here
    after = _observables(_boot(db), after_registry)

    for name in before:
        assert after[name] == before[name], f"{name} moved"
    assert registry_migration.differing_records(before_registry, after_registry) == []


def test_the_step_is_recorded_once_and_a_second_start_writes_nothing(old_install):
    db, _document = old_install
    assert not admin_store.migration_step_recorded(STEP, path=db)

    service_registry.ensure_service_registry(path=db, actor="test")
    generation = admin_store.get_service_registry_with_generation(path=db)["operator_settings_generation"]
    service_registry.ensure_service_registry(path=db, actor="test")

    assert admin_store.migration_step_recorded(STEP, path=db)
    assert admin_store.get_service_registry_with_generation(path=db)["operator_settings_generation"] == generation
    assert len(admin_store.list_audit(action_prefix="operator.service_registry.layering", path=db)) == 1


def test_stored_after_the_step_holds_patches_tombstones_and_owner_records_only(old_install):
    db, _document = old_install

    service_registry.ensure_service_registry(path=db, actor="test")
    stored = admin_store.get_operator_setting("service_registry", path=db)["value"]

    # A patch: the release supplies the rest. Its identity keys let the code before the layer rebuild the row (rollback).
    assert set(stored["services"]["litellm"]) == {"name", "enabled", "config", "id", "source", "kind"}
    assert stored["tombstones"]["services"] == ["openrouter"]
    assert {"ui-chat", "ui-voice", "ui-notes", "kobold"} <= set(stored["services"])  # owner records, whole
    assert set(stored["protocol_frontends"]) == {"acme"}  # builtin frontends inherit; none is stored
    assert stored["layering"] == 1 and "seeded_from" not in stored


def test_a_release_change_to_another_field_reaches_a_migrated_install_without_any_backfill(old_install, tmp_path):
    db, _document = old_install
    service_registry.ensure_service_registry(path=db, actor="test")
    newer = tmp_path / "release"
    shutil.copytree(ROOT / "registry", newer)
    services = yaml.safe_load((newer / "services.yml").read_text(encoding="utf-8"))
    litellm = next(record for record in services["records"] if record["id"] == "litellm")
    litellm["config"]["health_endpoint"] = "/health/changed"
    litellm["name"] = "Release renamed it"  # the owner patched the name: theirs wins
    (newer / "services.yml").write_text(yaml.safe_dump(services), encoding="utf-8")
    frontends = yaml.safe_load((newer / "protocol_frontends.yml").read_text(encoding="utf-8"))
    mesh = next(record for record in frontends["records"] if record["id"] == "mesh")
    mesh["name"] = "Mesh, renamed by the release"
    (newer / "protocol_frontends.yml").write_text(yaml.safe_dump(frontends), encoding="utf-8")

    with patch.object(service_registry, "DEFAULT_CATALOG_DIR", newer):
        registry = service_registry.ensure_service_registry(path=db, actor="test")
        view = registry_layers.sources(admin_store.get_operator_setting("service_registry", path=db)["value"],
                                       service_registry.release_registry())

    assert registry["services"]["litellm"]["config"]["health_endpoint"] == "/health/changed"
    assert registry["services"]["litellm"]["name"] == "Operator LiteLLM"
    assert registry["protocol_frontends"]["mesh"]["name"] == "Mesh, renamed by the release"
    assert view["services"]["litellm"]["fields"] == ["config", "config.note", "enabled", "name"]


def test_a_deleted_release_record_stays_deleted_when_a_later_release_changes_it(old_install, tmp_path):
    db, _document = old_install
    service_registry.ensure_service_registry(path=db, actor="test")
    newer = tmp_path / "release"
    shutil.copytree(ROOT / "registry", newer)
    services = yaml.safe_load((newer / "services.yml").read_text(encoding="utf-8"))
    next(record for record in services["records"] if record["id"] == "openrouter")["enabled"] = True
    (newer / "services.yml").write_text(yaml.safe_dump(services), encoding="utf-8")

    with patch.object(service_registry, "DEFAULT_CATALOG_DIR", newer):
        registry = service_registry.ensure_service_registry(path=db, actor="test")

    assert "openrouter" not in registry["services"]


def test_the_report_names_records_and_fields_and_the_audit_edit_and_never_a_value(old_install, capsys):
    db, _document = old_install

    status = registry_layering_report.main([str(db), "--gate"])
    printed = capsys.readouterr().out

    assert status == 0
    assert "patch   services/litellm" in printed and "fields: config, config.note, enabled, name" in printed
    assert "owner edit in audit log: yes" in printed
    assert "tombstone" in printed and "openrouter" in printed
    assert "gate: equal before and after" in printed
    for value in (SENTINEL, "Operator LiteLLM", "the owner's own note", "127.0.0.1"):
        assert value not in printed, f"the report printed a value: {value}"


def test_a_patch_the_audit_log_does_not_show_an_edit_of_is_marked_a_probable_stale_copy(old_install):
    db, document = old_install
    document["services"]["kobold"]["name"] = "Changed outside the audit log"
    write_legacy_registry_for_test(document, path=db)

    report = json.loads(_report_json(db))
    rows = {(row["collection"], row["id"]): row for row in report["patches"]}

    assert rows[("services", "litellm")]["owner_edit_in_audit_log"] is True
    assert rows[("services", "litellm")]["probably_stale_copy"] is False


def _report_json(db: Path) -> str:
    report, _status = registry_layering_report.build_report(db, gate=False)
    return json.dumps(report)


def test_the_dry_run_writes_nothing_to_the_copy_it_reads(old_install):
    db, _document = old_install
    before = sqlite3.connect(db).execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()

    registry_layering_report.build_report(db, gate=True)
    after = sqlite3.connect(db).execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()

    assert before == after
    assert not admin_store.migration_step_recorded(STEP, path=db)


def test_an_excluded_record_is_carried_through_the_step_as_stored(old_install):
    db, document = old_install
    document["services"]["bad-engine"] = {"id": "bad-engine", "name": "Bad", "kind": "engine",
                                         "config": {"models": ["this is not a model record"]}}
    write_legacy_registry_for_test(document, path=db)

    stored_before = admin_store.get_operator_setting("service_registry", path=db)["value"]
    plan = registry_migration.plan_layering(stored_before, service_registry.release_registry(),
                                            service_registry.load_seed_catalogs({}))

    assert "services/bad-engine" in plan.carried
    assert plan.stored["services"]["bad-engine"]["config"]["models"] == ["this is not a model record"]


def test_no_code_path_reads_the_startup_files_backends_to_build_a_registry():
    """C4 drops the seed and the backfill (8.5): nothing in the gateway turns ``backends`` into registry records."""
    forbidden = ("build_service_registry_seed", "backfill_seed_catalog_metadata", "backfill_normalized_registry",
                 "MESH_UPGRADE", "servable_registry_with_backfill")
    sources = {path: path.read_text(encoding="utf-8") for path in [ROOT / "proxy.py", *(ROOT / "gateway").glob("*.py")]}
    hits = sorted(f"{path.name}: {name}" for path, text in sources.items() for name in forbidden if name in text)
    assert hits == []

