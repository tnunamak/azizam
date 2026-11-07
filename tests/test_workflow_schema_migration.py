"""S3.5b: the one-time migration ``workflow-schema-2`` of stored workflow records (docs/design/CODE_TO_DATA_S3.md, D3).

Fixtures only: the documents come from the frozen schema 1 release copy and from small edits of them. Nothing here reads
a live database.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store, extension_packages, record_integrity, registry_layers as layers, service_registry  # noqa: E402
from gateway import workflow_schema_migration as migration  # noqa: E402
from gateway.workflow_convert import POLICY_KEYS, convert_1_to_2  # noqa: E402
from scripts import workflow_schema_2_report  # noqa: E402
from tests.registry_test_helpers import write_legacy_registry_for_test  # noqa: E402

SENTINEL = "sentinel-value-must-never-print"
TRUSTED_ID = "image.generate.anima-turbo"  # has a top-level ``requires``
PLAIN_ID = "image.generate.flux2-klein"


def _v1(workflow_id: str = PLAIN_ID, **edits) -> dict:
    document = copy.deepcopy(migration.frozen_release_records()[workflow_id]["manifest"])
    document.update(edits)
    return document


def _owner_record(workflow_id: str, document: dict, **beside) -> dict:
    document = {**document, "id": workflow_id}
    return {"id": workflow_id, "source": "ui", "enabled": True, "editable": True, "manifest": document, **beside}


def _stored(workflows: dict) -> dict:
    return {**layers.empty_stored(service_registry.SERVICE_REGISTRY_SCHEMA_VERSION), "workflows": workflows}


def _release_with(workflow_id: str) -> tuple[dict, dict]:
    """A release whose ``workflows`` ship ``workflow_id`` as schema 2, and the schema 1 release records it came from."""
    v1 = migration.frozen_release_records()
    document = {key: value for key, value in v1[workflow_id]["manifest"].items() if key not in POLICY_KEYS}
    converted, _notes = convert_1_to_2(document)
    record = service_registry.normalize_workflow_record(workflow_id, {
        "id": workflow_id, "source": "builtin", "manifest": converted,
        "policy": migration.v1_effective_facts(v1[workflow_id]["manifest"])})
    record.setdefault("policy", migration.v1_effective_facts(v1[workflow_id]["manifest"]))
    release = {"services": {}, "protocol_frontends": {}, "capabilities": {}, "workflows": {workflow_id: record}}
    return release, {workflow_id: v1[workflow_id]}


def _plan(workflows: dict, release: dict | None = None, **kwargs) -> migration.Plan:
    return migration.plan_workflow_schema_2(_stored(workflows), release or {"workflows": {}}, **kwargs)


def _rows(plan: migration.Plan) -> dict:
    return {row.id: row for row in plan.rows}


# -- the plan, one rule at a time -------------------------------------------------------------------------------------


def test_a_whole_owner_record_is_converted_and_states_what_the_old_readers_served():
    document = _v1(trusted=True, exposure="hidden", tags=["a", "b", "a"])
    plan = _plan({"owner.flux": _owner_record("owner.flux", document)})
    row = _rows(plan)["owner.flux"]
    assert row.action == migration.CONVERT_WHOLE
    stored = plan.stored["workflows"]["owner.flux"]
    assert stored["manifest"]["schema_version"] == 2
    assert not {"trusted", "source", *POLICY_KEYS} & set(stored["manifest"])
    assert stored["policy"] == {"exposure": "hidden", "trust_tier": "stable", "resource_tier": "batch", "tags": ["a", "b"]}
    assert {"schema_version", "trusted", "policy", "exposure", "tags"} <= set(row.fields)
    assert stored["enabled"] is True and stored["editable"] is True and stored["source"] == "ui"
    # the record is one a load accepts
    effective = service_registry.normalize_service_registry(layers.layer(plan.stored, {"workflows": {}}))
    assert effective["workflows"]["owner.flux"]["manifest"]["schema_version"] == 2


def test_an_untrusted_record_without_facts_keeps_the_experimental_default_it_served():
    plan = _plan({"owner.flux": _owner_record("owner.flux", _v1(trusted=False))})
    assert plan.stored["workflows"]["owner.flux"]["policy"] == {
        "exposure": "default", "trust_tier": "experimental", "resource_tier": "batch", "tags": []}


def test_an_inline_record_is_stored_in_the_nested_form():
    inline = {**_v1(), "id": "owner.inline", "enabled": True}
    plan = _plan({"owner.inline": inline})
    assert _rows(plan)["owner.inline"].action == migration.CONVERT_WHOLE
    stored = plan.stored["workflows"]["owner.inline"]
    assert stored["manifest"]["schema_version"] == 2 and stored["enabled"] is True and "schema_version" not in stored


def test_a_record_that_overrides_a_bundled_id_is_marked_in_the_report():
    plan = _plan({TRUSTED_ID: _owner_record(TRUSTED_ID, _v1(TRUSTED_ID))}, bundled_ids=frozenset({TRUSTED_ID}))
    assert _rows(plan)[TRUSTED_ID].overrides_bundled is True
    assert plan.report()["records"][0]["overrides_bundled"] is True


def test_an_already_v2_record_is_not_touched():
    converted = _plan({"owner.flux": _owner_record("owner.flux", _v1())}).stored["workflows"]["owner.flux"]
    plan = _plan({"owner.flux": converted})
    assert _rows(plan)["owner.flux"].action == migration.ALREADY_V2
    assert plan.stored["workflows"] == {"owner.flux": converted}
    assert not plan.changes


def test_a_second_plan_over_its_own_result_changes_nothing():
    workflows = {"owner.flux": _owner_record("owner.flux", _v1()),
                 "owner.broken": _owner_record("owner.broken", _v1(schema_version=3))}
    first = _plan(workflows)
    second = migration.plan_workflow_schema_2(first.stored, {"workflows": {}})
    assert second.stored == first.stored
    assert {row.action for row in second.rows} == {migration.ALREADY_V2, migration.CANNOT_CONVERT}


def test_a_broken_record_is_stored_disabled_with_its_errors_and_never_dropped():
    broken = _owner_record("owner.broken", _v1(typical_seconds=SENTINEL, x_note=SENTINEL, schema_version=1))
    plan = _plan({"owner.broken": broken, "owner.fine": _owner_record("owner.fine", _v1())})
    row = _rows(plan)["owner.broken"]
    assert row.action == migration.CANNOT_CONVERT and row.errors
    kept = plan.stored["workflows"]["owner.broken"]
    assert kept["enabled"] is False
    assert kept["manifest"] == broken["manifest"]  # as it was: nothing lost
    assert kept["migration"]["step"] == migration.STEP and kept["migration"]["errors"]
    assert "policy" not in kept  # no facts: it needs configuration
    assert plan.report()["needs_configuration"][0]["id"] == "owner.broken"
    # it is never offered to a run, and the others still convert
    effective, _ = service_registry.read_registry(layers.layer(plan.stored, {"workflows": {}}))
    assert "owner.broken" not in {record.id for record in service_registry.workflow_records_from_registry(effective)}
    assert _rows(plan)["owner.fine"].action == migration.CONVERT_WHOLE


def test_a_record_that_is_not_an_object_is_kept_and_listed():
    plan = _plan({"owner.odd": "not a record"})
    assert plan.stored["workflows"]["owner.odd"] == "not a record"
    assert _rows(plan)["owner.odd"].errors == (("/", "record_not_object"),)


def test_an_empty_workflows_section_changes_nothing():
    for stored in (_stored({}), {key: value for key, value in _stored({}).items() if key != "workflows"}):
        plan = migration.plan_workflow_schema_2(stored, {"workflows": {}})
        assert plan.rows == [] and not plan.changes and plan.stored == stored


# -- a patch over a release record -------------------------------------------------------------------------------------

PATCHES = {
    "an input bound": lambda v1: {"manifest": {"inputs": {"width": {"maximum": 1024}}}},
    "the requirements, which the converter moves into the binding":
        lambda v1: {"manifest": {"requires": {"custom_nodes": ["RMBG", "Other"]}}},
    "a policy fact": lambda v1: {"manifest": {"exposure": "hidden"}},
    "the author's version": lambda v1: {"manifest": {"version": "9.9.9"}},
    "a record field and a manifest field": lambda v1: {"enabled": False, "manifest": {"typical_seconds": 99}},
    "a replacement": lambda v1: {layers.REPLACE_KEY: {**_owner_record(TRUSTED_ID, v1["manifest"]),
                                                        "manifest": {**v1["manifest"], "purpose": "Another purpose"}}},
}


@pytest.mark.parametrize("name", sorted(PATCHES))
def test_a_patch_converts_to_the_same_effective_record_as_converting_the_whole_patched_document(name):
    release, v1_release = _release_with(TRUSTED_ID)
    entry = PATCHES[name](v1_release[TRUSTED_ID])
    plan = _plan({TRUSTED_ID: entry}, release, v1_release=v1_release)
    row = _rows(plan)[TRUSTED_ID]
    assert row.action == migration.CONVERT_PATCH
    # the independent computation: patch the schema 1 release record, then convert the whole result
    patched = layers.apply_entry(v1_release[TRUSTED_ID], entry)
    document = copy.deepcopy(patched["manifest"])
    facts = migration.v1_effective_facts(document)
    for key in POLICY_KEYS:
        document.pop(key, None)
    expected_manifest, _ = convert_1_to_2(document)
    effective = layers.layer(plan.stored, release)["workflows"][TRUSTED_ID]
    assert effective["manifest"] == expected_manifest
    assert effective["policy"] == facts
    assert effective["enabled"] == patched.get("enabled", True)
    # a load reads it back as it is stored (the derived identity and summary agree; the facts are read apart)
    reread = service_registry.normalize_workflow_record(TRUSTED_ID, {
        **{k: v for k, v in effective.items() if k not in ("identity", "summary")}})
    assert {k: v for k, v in effective.items() if k != "policy"} == {k: v for k, v in reread.items() if k != "policy"}
    # the record keeps the release record's own fields unless the patch changed them
    assert effective["source"] == release["workflows"][TRUSTED_ID]["source"] or "source" in entry.get(layers.REPLACE_KEY, entry)
    # what is stored is the difference from the schema 2 release record, never the whole document
    stored_entry = plan.stored["workflows"].get(TRUSTED_ID)
    assert stored_entry is None or layers.REPLACE_KEY in stored_entry or stored_entry != effective


def test_a_patch_alone_does_not_commute_with_the_converter():
    """Why the release record is needed: converting the patch and merging it into the schema 2 record is not the answer."""
    release, v1_release = _release_with(TRUSTED_ID)
    patch = {"manifest": {"requires": {"custom_nodes": ["RMBG", "Other"]}}}
    naive = layers.merge_patch(release["workflows"][TRUSTED_ID], patch)
    plan = _plan({TRUSTED_ID: patch}, release, v1_release=v1_release)
    right = layers.layer(plan.stored, release)["workflows"][TRUSTED_ID]
    assert naive["manifest"] != right["manifest"]
    assert "requires" in naive["manifest"] and "requires" not in right["manifest"]  # the converter moved it


def test_a_record_field_patch_needs_no_conversion():
    release, v1_release = _release_with(TRUSTED_ID)
    plan = _plan({TRUSTED_ID: {"enabled": False}}, release, v1_release=v1_release)
    assert _rows(plan)[TRUSTED_ID].action == migration.ALREADY_V2
    assert plan.stored["workflows"][TRUSTED_ID] == {"enabled": False}


def test_a_patch_over_a_release_record_with_no_known_schema_1_form_is_kept_disabled():
    release, _ = _release_with(TRUSTED_ID)
    entry = {"manifest": {"typical_seconds": 99}}
    plan = _plan({TRUSTED_ID: entry}, release, v1_release={})
    assert _rows(plan)[TRUSTED_ID].errors == (("/", "release_record_unknown"),)
    kept = plan.stored["workflows"][TRUSTED_ID]
    assert kept["enabled"] is False and kept["manifest"] == entry["manifest"]


# -- the frozen copy -------------------------------------------------------------------------------------------------------


def test_the_frozen_schema_1_copy_is_the_bundled_set():
    bundled = {}
    for path in sorted((ROOT / "manifests" / "workflows").glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        bundled[document["id"]] = document
    frozen = {record_id: record["manifest"] for record_id, record in migration.frozen_release_records().items()}
    assert set(frozen) == set(bundled)
    assert all(document["schema_version"] == 1 for document in frozen.values())
    for record_id, document in bundled.items():
        if document["schema_version"] == 1:  # until the files are rewritten, the copy is the file
            assert frozen[record_id] == document


def test_every_frozen_document_converts():
    for record_id, record in migration.frozen_release_records().items():
        plan = _plan({record_id: _owner_record(record_id, record["manifest"])})
        assert _rows(plan)[record_id].action == migration.CONVERT_WHOLE, record_id


# -- the report prints no value --------------------------------------------------------------------------------------------


def _sentinel_workflows() -> dict:
    document = _v1(display_name=SENTINEL, description=SENTINEL, tags=[SENTINEL], purpose="Purpose")
    document["inputs"]["prompt"]["default"] = SENTINEL
    broken = _v1(typical_seconds=SENTINEL, x_note=SENTINEL, schema_version=1)
    broken["inputs"]["prompt"]["help"] = SENTINEL
    return {"owner.fine": _owner_record("owner.fine", document, provenance={"note": SENTINEL}),
            "owner.broken": _owner_record("owner.broken", broken, provenance={"note": SENTINEL})}


def test_the_report_holds_ids_names_and_codes_and_no_stored_value():
    plan = _plan(_sentinel_workflows())
    assert SENTINEL in json.dumps(plan.stored)  # the stored document does hold it
    assert plan.report()["needs_configuration"][0]["errors"]  # and the broken record has errors to report
    assert SENTINEL not in json.dumps(plan.report())


def test_the_dry_run_prints_no_stored_value_and_writes_nothing(tmp_path, capsys):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    write_legacy_registry_for_test(_stored(_sentinel_workflows()), path=db)
    before = db.read_bytes()
    for argv in ([str(db)], [str(db), "--json"]):
        status = workflow_schema_2_report.main(argv)
        output = capsys.readouterr().out
        assert status == 1  # one record cannot convert
        assert "owner.fine" in output and "owner.broken" in output and "needs configuration" in output.lower().replace("_", " ")
        assert SENTINEL not in output
    assert db.read_bytes() == before
    assert not admin_store.migration_step_recorded(migration.STEP, path=db)


def test_the_dry_run_reports_a_done_step_and_an_unstored_registry(tmp_path, capsys):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    assert workflow_schema_2_report.main([str(db)]) == 0
    assert "no registry stored" in capsys.readouterr().out
    write_legacy_registry_for_test(_stored({}), path=db)
    admin_store.record_migration_step(migration.STEP, path=db)
    assert workflow_schema_2_report.main([str(db)]) == 0
    assert "already done" in capsys.readouterr().out


def test_the_dry_run_layers_an_old_registry_in_memory_first(tmp_path, capsys):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    old = {"schema_version": 1, "services": {}, "protocol_frontends": {}, "capabilities": {}, "updated_at": 1,
           "workflows": {"owner.flux": _owner_record("owner.flux", _v1())}}
    write_legacy_registry_for_test(old, path=db)
    assert workflow_schema_2_report.main([str(db), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["counts"]["convert-whole"] == 1 and report["state"] == "would convert"


# -- the apply, at start -----------------------------------------------------------------------------------------------------


def _db_with(tmp_path, workflows: dict) -> Path:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    write_legacy_registry_for_test(_stored(workflows), path=db)
    return db


def _row(db: Path) -> tuple[str, int]:
    record = admin_store.get_service_registry_json_with_generation(path=db)
    return record["value_json"], int(record["operator_settings_generation"])


def _audit(db: Path) -> list[dict]:
    return admin_store.list_audit(action_prefix=migration.AUDIT_ACTION, path=db)


def test_the_step_converts_stored_records_once_and_a_second_run_changes_nothing(tmp_path):
    db = _db_with(tmp_path, {"owner.flux": _owner_record("owner.flux", _v1()),
                             "owner.broken": _owner_record("owner.broken", _v1(schema_version=3))})
    _, generation_before = _row(db)
    first = migration.ensure(path=db, actor="test")
    after_first = _row(db)
    second = migration.ensure(path=db, actor="test")
    assert _row(db) == after_first  # the second run wrote nothing: no revision, no generation
    assert second == first
    assert after_first[1] == generation_before + 1
    stored = json.loads(after_first[0])["workflows"]
    assert stored["owner.flux"]["manifest"]["schema_version"] == 2
    assert stored["owner.broken"]["enabled"] is False and stored["owner.broken"]["manifest"]["schema_version"] == 3
    assert admin_store.migration_step_recorded(migration.STEP, path=db)
    events = _audit(db)
    assert len(events) == 1  # one audit entry, from the one run
    metadata = events[0]["metadata"]
    assert metadata["migration_step"] == migration.STEP and metadata["convert-whole"] == 1
    assert metadata["cannot-convert"] == 1


def test_the_audit_entry_holds_counts_and_no_stored_value(tmp_path):
    db = _db_with(tmp_path, _sentinel_workflows())
    migration.ensure(path=db, actor="test")
    events = _audit(db)
    assert len(events) == 1 and SENTINEL not in json.dumps(events)


def test_a_start_with_only_convertible_records_loads_the_converted_registry(tmp_path):
    db = _db_with(tmp_path, {"owner.flux": _owner_record("owner.flux", _v1(trusted=True))})
    registry = service_registry.ensure_service_registry(path=db, actor="test")
    assert registry["workflows"]["owner.flux"]["manifest"]["schema_version"] == 2
    assert {r.id for r in service_registry.workflow_records_from_registry(registry)} == {"owner.flux"}
    assert service_registry.ensure_service_registry(path=db, actor="test") == registry


def test_a_registry_with_nothing_to_convert_only_records_the_step(tmp_path):
    db = _db_with(tmp_path, {})
    _, generation = _row(db)
    service_registry.ensure_service_registry(path=db, actor="test")
    assert admin_store.migration_step_recorded(migration.STEP, path=db)
    assert _row(db)[1] == generation and _audit(db) == []


def test_a_fresh_database_records_the_step_and_holds_no_workflow(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    registry = service_registry.ensure_service_registry(path=db, actor="test")
    assert registry["workflows"] == {} and admin_store.migration_step_recorded(migration.STEP, path=db)


def test_an_old_unlayered_registry_is_layered_and_then_converted_in_one_start(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    old = {"schema_version": 1, "services": {}, "protocol_frontends": {}, "capabilities": {}, "updated_at": 1,
           "workflows": {"owner.flux": _owner_record("owner.flux", _v1())}}
    write_legacy_registry_for_test(old, path=db)
    registry = service_registry.ensure_service_registry(path=db, actor="test")
    assert registry["workflows"]["owner.flux"]["manifest"]["schema_version"] == 2
    stored = json.loads(_row(db)[0])
    assert registry_is_layered(stored) and stored["workflows"]["owner.flux"]["manifest"]["schema_version"] == 2


def registry_is_layered(stored: dict) -> bool:
    return layers.is_layered(stored)


def test_a_write_that_loses_a_race_is_retried_on_the_generation_it_finds(tmp_path, monkeypatch):
    db = _db_with(tmp_path, {"owner.flux": _owner_record("owner.flux", _v1())})
    real = admin_store.get_service_registry_json_with_generation
    calls = {"n": 0}

    def stale_once(*, path):
        record = real(path=path)
        calls["n"] += 1
        if calls["n"] == 1:
            record = {**record, "operator_settings_generation": int(record["operator_settings_generation"]) - 1}
        return record

    monkeypatch.setattr(admin_store, "get_service_registry_json_with_generation", stale_once)
    migrated = migration.ensure(path=db, actor="test")
    assert migrated["workflows"]["owner.flux"]["manifest"]["schema_version"] == 2
    assert calls["n"] == 2 and admin_store.migration_step_recorded(migration.STEP, path=db)


# -- packages --------------------------------------------------------------------------------------------------------------


def _install_package(db: Path, records: dict[str, dict], *, version: str = "1.0.0") -> dict:
    """A package index that holds a keyed install proof of each workflow record, as an install writes it."""
    key = record_integrity.install_key(db)
    package = {"id": "acme", "name": "acme", "version": version, "enabled": True, "package_hash": "hash-1",
               **extension_packages.installed_records_proof({"workflows": records}, key=key)}
    index = {"schema_version": 1, "sources": [], "packages": {"acme": package}, "updated_at": 1}
    admin_store.put_operator_setting(extension_packages.EXTENSION_PACKAGE_INDEX_SETTING_KEY, index, path=db)
    return index


def _package_record(workflow_id: str, **edits) -> dict:
    return _owner_record(workflow_id, _v1(**edits), source="imported", editable=False,
                         provenance={"package_id": "acme", "package_version": "1.0.0", "package_hash": "hash-1"})


def _read_index(db: Path) -> dict:
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT value_json FROM operator_settings WHERE key = ?",
            (extension_packages.EXTENSION_PACKAGE_INDEX_SETTING_KEY,)).fetchone()
    return json.loads(row[0])


def test_a_converted_package_record_keeps_matching_its_install_proof(tmp_path):
    untouched, edited = _package_record("acme.untouched"), _package_record("acme.edited")
    db = _db_with(tmp_path, {"acme.untouched": untouched, "acme.edited": edited})
    # the owner changed one record after the install: its proof must keep matching nothing
    changed = copy.deepcopy(edited)
    changed["manifest"]["display_name"] = "Changed by the owner"
    _install_package(db, {"acme.untouched": untouched, "acme.edited": edited})
    write_legacy_registry_for_test(_stored({"acme.untouched": untouched, "acme.edited": changed}), path=db)
    registry = service_registry.ensure_service_registry(path=db, actor="test")
    index = extension_packages.normalize_extension_package_index(_read_index(db))
    package, key = index["packages"]["acme"], record_integrity.install_key(db)
    assert extension_packages._matches_install(
        registry["workflows"]["acme.untouched"], package["installed_records"]["workflows"]["acme.untouched"], package, None, key)
    assert not extension_packages._matches_install(
        registry["workflows"]["acme.edited"], package["installed_records"]["workflows"]["acme.edited"], package, None, key)
