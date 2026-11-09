"""S3.5b: the one-time migration ``workflow-schema-2`` of stored workflow records (docs/design/CODE_TO_DATA_S3.md, D3).

Fixtures only: the documents come from the frozen schema 1 release copy and from small edits of them. Nothing here reads
a live database.
"""
from __future__ import annotations

import copy
import itertools
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

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


def with_class(facts):
    """The facts a load states for a record of this file's image intent: the fifth fact is completed from the intent."""
    return {**facts, "resource_class": "image"}


def test_a_whole_owner_record_is_converted_and_states_what_the_old_readers_served():
    document = _v1(trusted=True, exposure="hidden", tags=["a", "b", "a"])
    plan = _plan({"owner.flux": _owner_record("owner.flux", document)})
    row = _rows(plan)["owner.flux"]
    assert row.action == migration.CONVERT_WHOLE
    stored = plan.stored["workflows"]["owner.flux"]
    assert stored["manifest"]["schema_version"] == 2
    assert not {"trusted", "source", *POLICY_KEYS} & set(stored["manifest"])
    assert stored["policy"] == with_class({"exposure": "hidden", "trust_tier": "stable", "resource_tier": "batch", "tags": ["a", "b"]})
    assert {"schema_version", "trusted", "policy", "exposure", "tags"} <= set(row.fields)
    assert stored["enabled"] is True and stored["editable"] is True and stored["source"] == "ui"
    # the record is one a load accepts
    effective = service_registry.normalize_service_registry(layers.layer(plan.stored, {"workflows": {}}))
    assert effective["workflows"]["owner.flux"]["manifest"]["schema_version"] == 2


def test_an_untrusted_record_without_facts_keeps_the_experimental_default_it_served():
    plan = _plan({"owner.flux": _owner_record("owner.flux", _v1(trusted=False))})
    assert plan.stored["workflows"]["owner.flux"]["policy"] == with_class({
        "exposure": "default", "trust_tier": "experimental", "resource_tier": "batch", "tags": []})


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
    assert kept["enabled"] is True  # the owner's own value; the migration block marks the quarantine
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
    assert effective["policy"] == with_class(facts)
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
    assert "enabled" not in kept and kept["manifest"] == entry["manifest"]


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
    assert stored["owner.broken"]["enabled"] is True and stored["owner.broken"]["manifest"]["schema_version"] == 3
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


def test_a_start_with_only_convertible_records_loads_the_converted_registry(tmp_path, monkeypatch):
    ticks = itertools.count(1_700_000_000)  # the clock moves a second at every read: a stored document with no updated_at
    monkeypatch.setattr(service_registry.time, "time", lambda: next(ticks))  # gets the read time, which is not part of the registry
    db = _db_with(tmp_path, {"owner.flux": _owner_record("owner.flux", _v1(trusted=True))})
    registry = service_registry.ensure_service_registry(path=db, actor="test")
    assert registry["workflows"]["owner.flux"]["manifest"]["schema_version"] == 2
    assert {r.id for r in service_registry.workflow_records_from_registry(registry)} == {"owner.flux"}
    again = service_registry.ensure_service_registry(path=db, actor="test")
    assert again["updated_at"] != registry["updated_at"]  # the reads differ in the read time only
    assert {**again, "updated_at": 0} == {**registry, "updated_at": 0}


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


# -- review findings: the plan is total ----------------------------------------------------------------------------------


def _with_stated_capability(**edits) -> dict:
    document = _v1(**edits)
    document.setdefault("capability", "image.generate")
    return document


MALFORMED = {
    "input type is a list": lambda d: d["inputs"].setdefault("x", {}).update(type=[]),
    "input type is a mapping": lambda d: d["inputs"].setdefault("x", {}).update(type={"a": 1}),
    "input type is a number": lambda d: d["inputs"].setdefault("x", {}).update(type=7),
    "input type is null": lambda d: d["inputs"].setdefault("x", {}).update(type=None),
    "bindings is a list": lambda d: d.update(bindings=[SENTINEL]),
    "bindings is a number": lambda d: d.update(bindings=5),
    "bindings is null": lambda d: d.update(bindings=None),
    "inputs is a list": lambda d: d.update(inputs=[SENTINEL]),
    "outputs is a string": lambda d: d.update(outputs=SENTINEL),
    "tags is a mapping": lambda d: d.update(tags={SENTINEL: 1}),
    "intent is a list": lambda d: d.update(intent=[SENTINEL]),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_one_malformed_record_does_not_stop_the_plan(name):
    bad = _with_stated_capability()
    MALFORMED[name](bad)
    plan = _plan({"owner.bad": _owner_record("owner.bad", bad), "owner.fine": _owner_record("owner.fine", _v1())})
    rows = _rows(plan)
    assert rows["owner.fine"].action == migration.CONVERT_WHOLE
    assert plan.stored["workflows"]["owner.fine"]["manifest"]["schema_version"] == 2
    # a malformed record is never dropped; if it cannot convert it is kept as it was, with codes only in the report
    kept = plan.stored["workflows"]["owner.bad"]
    if rows["owner.bad"].action == migration.CANNOT_CONVERT:
        assert kept["enabled"] is True and kept["manifest"] == _owner_record("owner.bad", bad)["manifest"]
        assert all(len(code) < 60 for _pointer, code in rows["owner.bad"].errors)
    assert SENTINEL not in json.dumps(plan.report())


def test_a_converter_exception_marks_the_record_cannot_convert_with_a_bounded_code(monkeypatch):
    real = migration.convert_and_check

    def raising(document):
        if document.get("display_name") == "boom":
            raise KeyError(SENTINEL)  # an error text that quotes the record
        return real(document)

    monkeypatch.setattr(migration, "convert_and_check", raising)
    plan = _plan({"owner.bad": _owner_record("owner.bad", _v1(display_name="boom")),
                  "owner.fine": _owner_record("owner.fine", _v1())})
    row = _rows(plan)["owner.bad"]
    assert row.action == migration.CANNOT_CONVERT
    assert row.errors == (("/", "conversion_failed"),)
    kept = plan.stored["workflows"]["owner.bad"]
    assert kept["enabled"] is True and SENTINEL not in json.dumps(kept["migration"]) and SENTINEL not in json.dumps(plan.report())
    assert _rows(plan)["owner.fine"].action == migration.CONVERT_WHOLE


def test_a_list_for_an_input_type_is_not_a_converter_crash():
    document = _with_stated_capability()
    document["inputs"]["x"] = {"type": []}
    converted, _notes = convert_1_to_2(document)
    assert converted["inputs"]["x"]["type"] == []


def test_a_malformed_record_does_not_abort_the_start_up_migration(tmp_path):
    bad = _with_stated_capability()
    bad["inputs"]["x"] = {"type": []}
    db = _db_with(tmp_path, {"owner.bad": _owner_record("owner.bad", bad), "owner.fine": _owner_record("owner.fine", _v1())})
    registry = service_registry.ensure_service_registry(path=db, actor="test")
    stored = json.loads(_row(db)[0])["workflows"]
    assert stored["owner.fine"]["manifest"]["schema_version"] == 2
    assert stored["owner.bad"]["enabled"] is True
    assert admin_store.migration_step_recorded(migration.STEP, path=db)
    assert "owner.fine" in registry["workflows"]


# -- the no-change race and the stale snapshot -------------------------------------------------------------------------------


def _write_stored(db: Path, workflows: dict) -> None:
    """A write by another process: it bumps the registry generation, as any registry write does."""
    _, generation = _row(db)
    admin_store.write_service_registry(
        _stored(workflows), updated_by="other", expected_generation=generation, path=db, stored_form=True)


def _after_first_registry_read(monkeypatch, action):
    """Run ``action`` once, right after the first registry read of ``ensure``, and hand back what was read before it."""
    real = admin_store.get_service_registry_json_with_generation
    state = {"n": 0}

    def hooked(*, path):
        record = real(path=path)
        state["n"] += 1
        if state["n"] == 1:
            action()
        return record

    monkeypatch.setattr(admin_store, "get_service_registry_json_with_generation", hooked)
    return state


def test_a_record_written_between_the_read_and_the_no_change_step_is_not_skipped_for_ever(tmp_path, monkeypatch):
    db = _db_with(tmp_path, {})
    _after_first_registry_read(monkeypatch, lambda: _write_stored(db, {"owner.flux": _owner_record("owner.flux", _v1())}))
    migrated = migration.ensure(path=db, actor="test")
    assert migrated["workflows"]["owner.flux"]["manifest"]["schema_version"] == 2
    assert json.loads(_row(db)[0])["workflows"]["owner.flux"]["manifest"]["schema_version"] == 2
    assert admin_store.migration_step_recorded(migration.STEP, path=db)


def test_the_no_change_step_is_not_recorded_when_the_generation_moved(tmp_path):
    db = _db_with(tmp_path, {})
    _, generation = _row(db)
    _write_stored(db, {})
    with pytest.raises(admin_store.OperatorSettingsGenerationConflict):
        admin_store.record_migration_step_at_registry_generation(migration.STEP, expected_generation=generation, path=db)
    assert not admin_store.migration_step_recorded(migration.STEP, path=db)


def test_a_true_no_op_bumps_no_generation_and_writes_no_audit_entry(tmp_path):
    db = _db_with(tmp_path, {"owner.flux": migration.plan_workflow_schema_2(
        _stored({"owner.flux": _owner_record("owner.flux", _v1())}), {"workflows": {}}).stored["workflows"]["owner.flux"]})
    before = _row(db)
    migration.ensure(path=db, actor="test")
    assert _row(db) == before and _audit(db) == []
    assert admin_store.migration_step_recorded(migration.STEP, path=db)


def test_a_snapshot_from_before_a_concurrent_completion_is_not_returned(tmp_path, monkeypatch):
    db = _db_with(tmp_path, {"owner.flux": _owner_record("owner.flux", _v1())})
    real_ensure = migration.ensure

    def other_start_finishes():
        monkeypatch.undo()  # the other start sees the real functions
        real_ensure(path=db, actor="other")

    _after_first_registry_read(monkeypatch, other_start_finishes)
    migrated = migration.ensure(path=db, actor="test")
    assert migrated["workflows"]["owner.flux"]["manifest"]["schema_version"] == 2


def test_a_start_that_finishes_between_the_marker_read_and_the_registry_read_is_not_migrated_again(tmp_path, monkeypatch):
    # The broken record stays schema 1 after the first run, so a second plan over the converted registry would write again.
    db = _db_with(tmp_path, {"owner.flux": _owner_record("owner.flux", _v1()),
                             "owner.broken": _owner_record("owner.broken", _v1(schema_version=3))})
    real_ensure = migration.ensure
    real_read = admin_store.get_service_registry_json_with_generation
    state = {"n": 0}

    def other_start_finishes_first(*, path):
        state["n"] += 1
        if state["n"] == 1:
            monkeypatch.undo()  # the other start sees the real functions
            real_ensure(path=db, actor="other")
        return real_read(path=path)

    _value, generation_before = _row(db)
    monkeypatch.setattr(admin_store, "get_service_registry_json_with_generation", other_start_finishes_first)
    migrated = migration.ensure(path=db, actor="test")
    assert migrated == service_registry._stored_document(_row(db)[0])
    assert _row(db)[1] == generation_before + 1
    assert [entry["actor_id"] for entry in _audit(db)] == ["other"]


def test_a_normalizer_error_is_stored_as_its_kind_only_and_the_other_records_go_on(monkeypatch):
    real = service_registry.normalize_workflow_record

    def refuses_one(record_id, record):
        if record_id == "owner.bad":
            raise ValueError(f"refused because of {SENTINEL}")
        return real(record_id, record)

    monkeypatch.setattr(service_registry, "normalize_workflow_record", refuses_one)
    plan = _plan({"owner.bad": _owner_record("owner.bad", _v1()), "owner.fine": _owner_record("owner.fine", _v1())})
    bad = plan.stored["workflows"]["owner.bad"]
    assert bad["enabled"] is True
    assert [error["code"] for error in bad["migration"]["errors"]] == ["conversion_failed"]
    assert SENTINEL not in json.dumps(plan.stored) and SENTINEL not in json.dumps(plan.report())
    assert plan.stored["workflows"]["owner.fine"]["manifest"]["schema_version"] == 2


def test_the_retries_are_bounded_and_exhausting_them_raises(tmp_path, monkeypatch):
    for workflows in ({"owner.flux": _owner_record("owner.flux", _v1())}, {}):  # a write conflicts; a no-change record conflicts
        db = _db_with(tmp_path / str(len(workflows)), workflows) if (tmp_path / str(len(workflows))).mkdir() is None else None
        real = admin_store.get_service_registry_json_with_generation
        calls = {"n": 0}

        def always_stale(*, path, real=real, calls=calls):
            calls["n"] += 1
            record = real(path=path)
            return {**record, "operator_settings_generation": int(record["operator_settings_generation"]) - 1}

        monkeypatch.setattr(admin_store, "get_service_registry_json_with_generation", always_stale)
        with pytest.raises(RuntimeError, match="bounded startup retry"):
            migration.ensure(path=db, actor="test")
        assert calls["n"] == 3
        assert not admin_store.migration_step_recorded(migration.STEP, path=db)
        monkeypatch.undo()


# -- null and other non-object records, invalid manifests, failed replacements ---------------------------------------------


@pytest.mark.parametrize("value", [None, "text", 7, ["a"]], ids=["null", "string", "number", "list"])
def test_a_record_that_is_not_an_object_is_kept_as_stored_and_listed(value):
    plan = _plan({"owner.odd": value, "owner.fine": _owner_record("owner.fine", _v1())})
    assert "owner.odd" in plan.stored["workflows"] and plan.stored["workflows"]["owner.odd"] == value
    assert _rows(plan)["owner.odd"].errors == (("/", "record_not_object"),)
    assert [item["id"] for item in plan.report()["needs_configuration"]] == ["owner.odd"]
    assert _rows(plan)["owner.fine"].action == migration.CONVERT_WHOLE


def test_a_null_record_survives_the_apply_and_a_second_run(tmp_path):
    db = _db_with(tmp_path, {"owner.null": None, "owner.fine": _owner_record("owner.fine", _v1())})
    migration.ensure(path=db, actor="test")
    stored = json.loads(_row(db)[0])["workflows"]
    assert "owner.null" in stored and stored["owner.null"] is None


def test_a_patch_that_now_equals_the_release_record_is_still_dropped():
    release, v1_release = _release_with(TRUSTED_ID)
    same = {"manifest": {"typical_seconds": v1_release[TRUSTED_ID]["manifest"]["typical_seconds"]}}
    plan = _plan({TRUSTED_ID: same}, release, v1_release=v1_release)
    assert _rows(plan)[TRUSTED_ID].action == migration.CONVERT_PATCH
    assert TRUSTED_ID not in plan.stored["workflows"]


@pytest.mark.parametrize("manifest", [None, "text", 3, ["a"]], ids=["null", "string", "number", "list"])
def test_a_patch_with_an_invalid_manifest_value_cannot_convert(manifest):
    release, v1_release = _release_with(TRUSTED_ID)
    plan = _plan({TRUSTED_ID: {"manifest": manifest}}, release, v1_release=v1_release)
    row = _rows(plan)[TRUSTED_ID]
    assert row.action == migration.CANNOT_CONVERT and row.errors == (("/manifest", "manifest_not_object"),)
    kept = plan.stored["workflows"][TRUSTED_ID]
    assert "enabled" not in kept and kept["manifest"] == manifest and kept["migration"]["errors"]


def test_a_patch_with_no_manifest_key_is_still_already_v2():
    release, v1_release = _release_with(TRUSTED_ID)
    plan = _plan({TRUSTED_ID: {"enabled": False}}, release, v1_release=v1_release)
    assert _rows(plan)[TRUSTED_ID].action == migration.ALREADY_V2


def test_a_failed_replacement_stays_a_replacement_and_is_disabled_inside_it():
    release, v1_release = _release_with(TRUSTED_ID)
    broken = _owner_record(TRUSTED_ID, _v1(TRUSTED_ID, typical_seconds=SENTINEL, x_note=SENTINEL))
    plan = _plan({TRUSTED_ID: {layers.REPLACE_KEY: broken}}, release, v1_release=v1_release)
    assert _rows(plan)[TRUSTED_ID].action == migration.CANNOT_CONVERT
    entry = plan.stored["workflows"][TRUSTED_ID]
    assert set(entry) == {layers.REPLACE_KEY}  # still the sole key, so a read takes it as a replacement
    replaced = entry[layers.REPLACE_KEY]
    assert replaced["enabled"] is True and replaced["migration"]["errors"] and replaced["manifest"] == broken["manifest"]
    # through layering: the effective record is the failed replacement, disabled, not a merge over the release record
    effective = layers.layer(plan.stored, release)["workflows"][TRUSTED_ID]
    assert effective == replaced
    assert "migration" in effective and effective["manifest"] == broken["manifest"]
    # and the plan over its own result changes nothing
    again = migration.plan_workflow_schema_2(plan.stored, release, v1_release=v1_release)
    assert again.stored["workflows"][TRUSTED_ID][layers.REPLACE_KEY]["enabled"] is True


def test_a_failed_replacement_survives_the_apply_and_the_readback(tmp_path):
    release, v1_release = _release_with(TRUSTED_ID)
    broken = _owner_record(TRUSTED_ID, _v1(TRUSTED_ID, typical_seconds=SENTINEL))
    db = _db_with(tmp_path, {TRUSTED_ID: {layers.REPLACE_KEY: broken}})
    migration.ensure(path=db, actor="test", release=release)
    entry = json.loads(_row(db)[0])["workflows"][TRUSTED_ID]
    assert set(entry) == {layers.REPLACE_KEY} and entry[layers.REPLACE_KEY]["enabled"] is True


# -- the dry run -----------------------------------------------------------------------------------------------------------


def _files(db: Path) -> dict[str, bytes | None]:
    return {suffix: (Path(str(db) + suffix).read_bytes() if Path(str(db) + suffix).exists() else None)
            for suffix in ("", "-wal", "-shm", "-journal")}


def test_the_dry_run_leaves_the_database_and_its_sidecars_as_they_were(tmp_path, capsys):
    db = _db_with(tmp_path, _sentinel_workflows())
    before = _files(db)
    for argv in ([str(db)], [str(db), "--json"]):
        workflow_schema_2_report.main(argv)
        capsys.readouterr()
        assert _files(db) == before


def test_the_dry_run_layers_an_old_registry_before_it_plans(tmp_path, capsys, monkeypatch):
    """A fixture whose answer depends on the layering: against a release that ships the record, the old whole record is a
    patch once layered (convert-patch), and a whole record if the layering call were skipped (convert-whole)."""
    release, _v1_release = _release_with(TRUSTED_ID)
    monkeypatch.setattr(service_registry, "release_registry", lambda: release)
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    old = {"schema_version": 1, "services": {}, "protocol_frontends": {}, "capabilities": {}, "updated_at": 1,
           "workflows": {TRUSTED_ID: _owner_record(TRUSTED_ID, _v1(TRUSTED_ID, typical_seconds=99))}}
    write_legacy_registry_for_test(old, path=db)
    assert not layers.is_layered(service_registry._stored_document(_row(db)[0]))
    workflow_schema_2_report.main([str(db), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["counts"]["convert-patch"] == 1 and report["counts"]["convert-whole"] == 0

    calls = []
    real = workflow_schema_2_report.registry_migration.plan_layering
    monkeypatch.setattr(workflow_schema_2_report.registry_migration, "plan_layering",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    workflow_schema_2_report.main([str(db), "--json"])
    capsys.readouterr()
    assert calls == [1]


# -- package proofs --------------------------------------------------------------------------------------------------------


def test_a_package_proof_refresh_touches_only_the_records_it_converted(tmp_path):
    untouched, edited = _package_record("acme.untouched"), _package_record("acme.edited")
    db = _db_with(tmp_path, {"acme.untouched": untouched, "acme.edited": edited})
    key = record_integrity.install_key(db)
    other = {"id": "other", "name": "other", "version": "2.0.0", "enabled": True, "package_hash": "hash-2",
             **extension_packages.installed_records_proof({"workflows": {"other.gone": _package_record("other.gone")}}, key=key)}
    index = _install_package(db, {"acme.untouched": untouched, "acme.edited": edited})
    index["packages"]["other"] = other
    admin_store.put_operator_setting(extension_packages.EXTENSION_PACKAGE_INDEX_SETTING_KEY, index, path=db)
    changed = copy.deepcopy(edited)
    changed["manifest"]["display_name"] = "Changed by the owner"
    write_legacy_registry_for_test(_stored({"acme.untouched": untouched, "acme.edited": changed}), path=db)
    before = _read_index(db)
    service_registry.ensure_service_registry(path=db, actor="test")
    after = _read_index(db)
    proofs = lambda document, package, name: document["packages"][package]["installed_records"]["workflows"][name]  # noqa: E731
    assert json.dumps(proofs(after, "acme", "acme.edited"), sort_keys=True) == json.dumps(
        proofs(before, "acme", "acme.edited"), sort_keys=True)  # the edited record's proof is byte-identical
    assert proofs(after, "acme", "acme.untouched") != proofs(before, "acme", "acme.untouched")  # the converted one moved
    assert json.dumps(after["packages"]["other"], sort_keys=True) == json.dumps(before["packages"]["other"], sort_keys=True)


def test_a_failing_proof_refresh_rolls_back_the_registry_write_with_it(tmp_path, monkeypatch):
    record = _package_record("acme.one")
    db = _db_with(tmp_path, {"acme.one": record})
    _install_package(db, {"acme.one": record})
    registry_before, index_before = _row(db), _read_index(db)

    def failing(conn, key, payload, now, actor):
        raise RuntimeError("the hook failed")

    monkeypatch.setattr(admin_store, "_upsert_setting_in_transaction", failing)
    with pytest.raises(RuntimeError, match="the hook failed"):
        migration.ensure(path=db, actor="test")
    assert _row(db) == registry_before and _read_index(db) == index_before
    assert not admin_store.migration_step_recorded(migration.STEP, path=db) and _audit(db) == []


# -- integration: the release's named defect fix, the marked record, the policy facts --------------------------------------

ACE_ID = "audio.generate.ace-step-1.5-xl-turbo"


def _ace_release() -> tuple[dict, dict]:
    """The real release (schema 2) record of the ACE workflow, and its frozen schema 1 record."""
    from gateway.discovery import bundled_workflow_manifests
    from gateway.workflow_policy import release_facts

    facts = release_facts(ACE_ID)
    record = service_registry.normalize_workflow_record(ACE_ID, {
        "id": ACE_ID, "source": "builtin", "manifest": bundled_workflow_manifests()[ACE_ID],
        "policy": {name: list(getattr(facts, name)) if name == "tags" else getattr(facts, name) for name in facts.__dataclass_fields__}})
    return {"workflows": {ACE_ID: record}}, {ACE_ID: migration.frozen_release_records()[ACE_ID]}


def test_every_frozen_release_document_converts_to_the_bundled_schema_2_file():
    from gateway.discovery import bundled_workflow_manifests

    bundled = bundled_workflow_manifests()
    frozen = migration.frozen_release_records()
    assert set(frozen) <= set(bundled)
    for record_id, record in frozen.items():
        plan = _plan({record_id: _owner_record(record_id, record["manifest"])})
        assert _rows(plan)[record_id].action == migration.CONVERT_WHOLE, record_id
        assert plan.stored["workflows"][record_id]["manifest"] == bundled[record_id], record_id


def test_the_ace_fix_is_data_and_removes_only_a_value_the_record_still_holds_from_the_release():
    fixes = json.loads(migration.FROZEN_FIXES_PATH.read_text(encoding="utf-8"))["fixes"]
    assert ACE_ID in fixes
    document = _v1(ACE_ID)
    migration.apply_release_fixes(ACE_ID, document)
    assert "prompt" not in document["inputs"] and "prompt" not in document["bindings"]["comfyui"]["map"]
    untouched = _v1(PLAIN_ID)
    migration.apply_release_fixes(PLAIN_ID, untouched)
    assert untouched == _v1(PLAIN_ID)


def test_an_owner_patch_over_ace_on_another_input_converts():
    release, v1_release = _ace_release()
    entry = {"manifest": {"inputs": {"bpm": {"default": 120}}}}
    plan = _plan({ACE_ID: entry}, release, v1_release=v1_release)
    assert _rows(plan)[ACE_ID].action == migration.CONVERT_PATCH
    effective = layers.layer(plan.stored, release)["workflows"][ACE_ID]
    assert effective["manifest"]["inputs"]["bpm"]["default"] == 120
    assert "prompt" not in effective["manifest"]["inputs"]


def test_an_owner_who_edited_the_ace_prompt_cannot_convert():
    release, v1_release = _ace_release()
    patch = {"manifest": {"inputs": {"prompt": {"default": "mine"}}}}
    plan = _plan({ACE_ID: patch}, release, v1_release=v1_release)
    assert _rows(plan)[ACE_ID].action == migration.CANNOT_CONVERT
    assert plan.stored["workflows"][ACE_ID].get("enabled") is None
    whole = _v1(ACE_ID)
    whole["inputs"]["prompt"]["label"] = "mine"
    plan = _plan({ACE_ID: _owner_record(ACE_ID, whole)})
    assert _rows(plan)[ACE_ID].action == migration.CANNOT_CONVERT
    # Editing only the second listed path (the binding) keeps the first too: the fix is all or none.
    patch = {"manifest": {"bindings": {"comfyui": {"map": {"prompt": "mine"}}}}}
    plan = _plan({ACE_ID: patch}, release, v1_release=v1_release)
    assert _rows(plan)[ACE_ID].action == migration.CANNOT_CONVERT
    whole = _v1(ACE_ID)
    whole["bindings"]["comfyui"]["map"]["prompt"] = "mine"
    document = copy.deepcopy(whole)
    migration.apply_release_fixes(ACE_ID, document)
    assert document == whole  # no part of the fix was made


def test_the_release_fix_compares_as_json_does_so_true_is_not_one():
    frozen = migration._frozen_documents()
    paths = migration._frozen_fixes()[ACE_ID]
    document = copy.deepcopy(frozen[ACE_ID])
    migration.apply_release_fixes(ACE_ID, document)
    assert migration._at(document, paths[0]) is migration._OMIT
    assert migration._json_equal(True, 1) is False and migration._json_equal(1, 1.0) is False
    assert migration._json_equal({"a": [1, "x"]}, {"a": [1, "x"]}) is True


# -- a record the migration marked does not abort the start ---------------------------------------------------------------


def _marked_db(tmp_path) -> Path:
    bad = _with_stated_capability()
    bad["inputs"]["x"] = {"type": []}
    db = _db_with(tmp_path, {"owner.bad": _owner_record("owner.bad", bad), "owner.fine": _owner_record("owner.fine", _v1())})
    service_registry.ensure_service_registry(path=db, actor="test")
    return db


def test_a_marked_record_is_listed_as_needing_configuration_and_never_served(tmp_path):
    db = _marked_db(tmp_path)
    stored_before = json.loads(_row(db)[0])["workflows"]["owner.bad"]
    assert stored_before["manifest"]["schema_version"] == 1 and stored_before["migration"]["step"] == migration.STEP
    registry = service_registry.ensure_service_registry(path=db, actor="test")  # a second start reads it again
    assert "owner.bad" not in registry["workflows"] and "owner.fine" in registry["workflows"]
    assert "owner.bad" not in [r.id for r in service_registry.workflow_records_from_registry(registry)]
    excluded = service_registry.load_service_registry_exclusions(path=db)
    (item,) = [e for e in excluded if e.id == "owner.bad"]
    assert item.section == "workflows" and item.error.startswith("needs_configuration")
    assert item.record["migration"] == stored_before["migration"]
    assert json.loads(_row(db)[0])["workflows"]["owner.bad"] == stored_before


def _store_of(db):
    from gateway import admin_runtime

    store = admin_runtime.AdminSQLiteStore(db)
    store.write_litellm_config = lambda registry: None  # the substrate config is not under test here
    return store



def _owner_actor():
    from gateway.admin_api import AdminContext

    return AdminContext(subject="owner-test", roles=frozenset({"owner"}))


def test_a_marked_record_keeps_its_block_through_a_normal_write(tmp_path):
    db = _marked_db(tmp_path)
    stored_before = json.loads(_row(db)[0])["workflows"]["owner.bad"]
    store = _store_of(db)
    value, generation = store.get_service_registry_snapshot()
    value["workflows"]["owner.fine"]["manifest"]["display_name"] = "Renamed by an owner save"  # an ordinary edit of another record
    saved = store.update_service_registry(data=value, actor=_owner_actor(), expected_generation=generation)
    assert [item.id for item in saved["excluded"]] == ["owner.bad"]
    persisted = json.loads(_row(db)[0])["workflows"]
    assert persisted["owner.bad"] == stored_before  # read back from the database, block included
    assert persisted["owner.fine"]["manifest"]["display_name"] == "Renamed by an owner save"
    assert [e.id for e in service_registry.load_service_registry_exclusions(path=db)] == ["owner.bad"]


def _fixed_record(db, *, inline: bool) -> dict:
    fine = json.loads(_row(db)[0])["workflows"]["owner.fine"]
    manifest = {**copy.deepcopy(fine["manifest"]), "id": "owner.bad"}
    if inline:  # the fields beside nothing: the document's own keys sit in the record, with the stale block copied along
        stale = json.loads(_row(db)[0])["workflows"]["owner.bad"]["migration"]
        return {**manifest, "enabled": True, "migration": stale}
    return {"id": "owner.bad", "source": "ui", "enabled": True, "manifest": manifest}


@pytest.mark.parametrize("inline", [False, True], ids=["nested", "inline"])
def test_an_owner_save_of_a_valid_schema_2_manifest_clears_the_block(tmp_path, inline):
    db = _marked_db(tmp_path)
    assert json.loads(_row(db)[0])["workflows"]["owner.bad"]["migration"]["step"] == migration.STEP
    store = _store_of(db)
    generation = _row(db)[1]
    store.replace_excluded_record(section="workflows", record_id="owner.bad", record=_fixed_record(db, inline=inline),
                                  actor=_owner_actor(), expected_generation=generation)
    saved = json.loads(_row(db)[0])["workflows"]["owner.bad"]
    assert "migration" not in saved and saved["enabled"] is True
    assert service_registry.load_service_registry_exclusions(path=db) == []
    registry = service_registry.load_service_registry(path=db)
    assert "owner.bad" in [r.id for r in service_registry.workflow_records_from_registry(registry)]


def test_a_record_that_fails_the_contract_without_the_mark_still_fails_the_strict_read():
    registry = _stored({"owner.bad": _owner_record("owner.bad", _v1(schema_version=1))})
    with pytest.raises(ValueError):
        service_registry.normalize_service_registry(registry)
    marked = _owner_record("owner.bad", _v1(schema_version=1), migration={"step": "another-step", "errors": []}, enabled=False)
    with pytest.raises(ValueError):
        service_registry.normalize_service_registry(_stored({"owner.bad": marked}))
    marked["migration"]["step"] = migration.STEP
    for enabled in (True, False):  # the migration block alone marks the quarantine; the owner's own ``enabled`` is not read for it
        marked["enabled"] = enabled
        registry, left_out = service_registry.read_registry(_stored({"owner.bad": marked}))
        assert [item.id for item in left_out] == ["owner.bad"] and "owner.bad" not in registry["workflows"]


# -- the policy facts survive the migration -------------------------------------------------------------------------------


def test_policy_facts_that_differ_from_the_defaults_survive_ensure_a_readback_and_a_write_cycle(tmp_path):
    facts = {"exposure": "hidden", "trust_tier": "stable", "resource_tier": "heavy", "tags": ["mine"]}
    release, _ = _release_with(TRUSTED_ID)
    release_facts = dict(release["workflows"][TRUSTED_ID]["policy"])
    assert release_facts != facts
    owner = _owner_record("owner.flux", _v1(**facts))
    patch = {"manifest": {"exposure": "hidden", "resource_tier": "heavy", "tags": ["mine"], "trust_tier": "stable"}}
    db = _db_with(tmp_path, {"owner.flux": owner})
    migrated = migration.ensure(path=db, actor="test", release={"workflows": {}})
    assert migrated["workflows"]["owner.flux"]["policy"] == with_class(facts)
    stored = json.loads(_row(db)[0])["workflows"]["owner.flux"]
    assert stored["policy"] == with_class(facts)
    reread = service_registry.normalize_workflow_record("owner.flux", stored)
    assert reread["policy"] == with_class(facts)
    rewritten = service_registry.normalize_workflow_record("owner.flux", {k: v for k, v in reread.items() if k not in ("identity", "summary")})
    assert rewritten["policy"] == with_class(facts)
    # a patch over a release record whose stated facts differ from the release's
    v1_release = {TRUSTED_ID: migration.frozen_release_records()[TRUSTED_ID]}
    plan = _plan({TRUSTED_ID: patch}, release, v1_release=v1_release)
    effective = layers.layer(plan.stored, release)["workflows"][TRUSTED_ID]
    assert effective["policy"] == with_class(facts)
    again = service_registry.normalize_workflow_record(TRUSTED_ID, {k: v for k, v in effective.items() if k not in ("identity", "summary")})
    assert again["policy"] == with_class(facts)


# -- the quarantine text trusts no stored value -------------------------------------------------------------------------


def _marked_registry(*codes) -> dict:
    block = {"step": migration.STEP, "errors": [{"pointer": "/", "code": code, "message": "m"} for code in codes]}
    record = _owner_record("owner.bad", _v1(schema_version=1), migration=block, enabled=False)
    return _stored({"owner.bad": record})


def _left_out_text(*codes) -> str:
    registry, excluded = service_registry.read_registry(_marked_registry(*codes))
    assert "owner.bad" not in registry["workflows"]
    (item,) = excluded
    return item.error


def test_forged_error_codes_never_reach_the_exclusion_text():
    forged = {"nested": SENTINEL}
    long_code = "c" * 1000
    secret_like = "opk_" + "A1b2C3d4" * 6 + " extra words"  # not an identifier: spaces and length
    text = _left_out_text("invalid_value", forged, long_code, secret_like, 42, ["a"])
    assert text.startswith("needs_configuration") and "invalid_value" in text
    assert SENTINEL not in text and "nested" not in text and long_code[:80] not in text and "opk_" not in text
    assert len(text) <= 300


def test_a_code_equal_to_a_value_the_record_holds_is_redacted():
    registry = _marked_registry("Opk_Abc123Secret")
    registry["workflows"]["owner.bad"]["manifest"]["display_name"] = "Opk_Abc123Secret"
    (item,) = service_registry.read_registry(registry)[1]
    assert "Opk_Abc123Secret" not in item.error and "<redacted>" in item.error


def test_the_exclusion_text_is_capped_and_scrubbed_like_every_other_exclusion():
    codes = [f"code_{index}_" + "x" * 50 for index in range(8)]
    assert len(_left_out_text(*codes)) <= 300
    assert "no reason" not in _left_out_text({"a": 1}) and "none stored" in _left_out_text({"a": 1})


def test_the_log_line_for_a_marked_record_holds_the_bounded_text_only(tmp_path, capsys):
    db = _db_with(tmp_path, {})
    registry = _marked_registry("c" * 1000, {"nested": SENTINEL})
    write_legacy_registry_for_test(registry, path=db)
    service_registry.load_service_registry_exclusions(path=db)
    out = capsys.readouterr().out
    assert "owner.bad" in out and SENTINEL not in out and "c" * 100 not in out


# -- an owner record that overrides a bundled id --------------------------------------------------------------------------


def test_a_quarantined_override_of_a_bundled_id_is_served_by_the_bundled_file_and_the_listing_says_so(tmp_path, monkeypatch):
    import proxy
    from gateway.discovery import bundled_workflow_manifests

    bad = _with_stated_capability()
    bad["inputs"]["x"] = {"type": []}
    bad["display_name"] = "Owner's version"
    db = _db_with(tmp_path, {PLAIN_ID: _owner_record(PLAIN_ID, bad, overrides=PLAIN_ID)})
    service_registry.ensure_service_registry(path=db, actor="test")
    assert json.loads(_row(db)[0])["workflows"][PLAIN_ID]["migration"]["step"] == migration.STEP
    (item,) = [e for e in service_registry.load_service_registry_exclusions(path=db) if e.id == PLAIN_ID]
    assert item.error.startswith("needs_configuration") and "bundled version of this workflow is served" in item.error
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    served = proxy._load_gateway_workflow_registry().get(PLAIN_ID)
    assert served is not None and served.manifest == bundled_workflow_manifests()[PLAIN_ID]  # deliberate: the id is not closed
    # an owner's own id (no bundled file) is said to need a document and is served by nothing
    other = service_registry.read_registry(_marked_registry("invalid_value"))[1][0].error
    assert "bundled version" not in other


# -- a policy the record states beside its manifest wins ------------------------------------------------------------------

RECORD_FACTS = {"exposure": "hidden", "trust_tier": "stable", "resource_tier": "heavy", "tags": ["mine"]}


def test_a_whole_record_stating_policy_beside_a_manifest_without_it_keeps_the_record_facts():
    plan = _plan({"owner.flux": _owner_record("owner.flux", _v1(), policy=dict(RECORD_FACTS))})
    assert plan.stored["workflows"]["owner.flux"]["policy"] == with_class(RECORD_FACTS)


def test_record_facts_win_over_manifest_facts_which_win_over_the_defaults():
    manifest_facts = {"exposure": "default", "trust_tier": "stable", "resource_tier": "batch", "tags": ["m"]}
    both = _plan({"owner.a": _owner_record("owner.a", _v1(**manifest_facts), policy=dict(RECORD_FACTS))})
    assert both.stored["workflows"]["owner.a"]["policy"] == with_class(RECORD_FACTS)
    manifest_only = _plan({"owner.b": _owner_record("owner.b", _v1(**manifest_facts))})
    assert manifest_only.stored["workflows"]["owner.b"]["policy"] == with_class(manifest_facts)
    neither = _plan({"owner.c": _owner_record("owner.c", _v1(trusted=False))})
    assert neither.stored["workflows"]["owner.c"]["policy"] == with_class({
        "exposure": "default", "trust_tier": "experimental", "resource_tier": "batch", "tags": []})
    partial = _plan({"owner.d": _owner_record("owner.d", _v1(**manifest_facts), policy={"exposure": "hidden"})})
    assert partial.stored["workflows"]["owner.d"]["policy"] == with_class(manifest_facts)  # a record that states some facts states none


def test_a_patch_over_a_release_record_stating_policy_keeps_the_stated_facts():
    release, _ = _release_with(TRUSTED_ID)
    v1_release = {TRUSTED_ID: migration.frozen_release_records()[TRUSTED_ID]}
    patch = {"policy": dict(RECORD_FACTS), "manifest": {"display_name": "Mine"}}
    plan = _plan({TRUSTED_ID: patch}, release, v1_release=v1_release)
    assert _rows(plan)[TRUSTED_ID].action == migration.CONVERT_PATCH
    effective = layers.layer(plan.stored, release)["workflows"][TRUSTED_ID]
    assert effective["policy"] == with_class(RECORD_FACTS)


# -- the exclusion text: scrub the whole text, then cap it ----------------------------------------------------------------


def test_a_code_that_straddles_the_cap_boundary_is_redacted_whole():
    secret = "Opk_Abc123SecretValueOfTheOwner"  # an identifier-shaped code that is also a value the record holds
    for pad in range(1, 65):  # moves the start of the secret code across character 300 of the unscrubbed text
        registry = _marked_registry("a" * 64, "b" * 64, "c" * pad, secret)
        registry["workflows"]["owner.bad"]["manifest"]["display_name"] = secret
        (item,) = service_registry.read_registry(registry)[1]
        assert "Opk_" not in item.error and len(item.error) <= 300, pad


def test_a_scrub_that_makes_the_text_longer_is_still_capped():
    for pad in range(1, 65):  # the unscrubbed text sits at 290..300 for some pad; "<redacted>" is longer than the value
        registry = _marked_registry("a" * 64, "b" * 64, "c" * pad, "d" * 9)
        registry["workflows"]["owner.bad"]["manifest"]["display_name"] = "disabled"  # a word of the fixed text
        (item,) = service_registry.read_registry(registry)[1]
        assert len(item.error) <= 300, pad


# -- a quarantined override: the bundled version serves, whatever its enabled value ----------------------------------------


def _bundled_override_db(tmp_path, *, enabled: Any) -> Path:
    bad = _with_stated_capability()
    bad["inputs"]["x"] = {"type": []}
    record = _owner_record(PLAIN_ID, bad, overrides=PLAIN_ID, enabled=enabled)
    db = _db_with(tmp_path, {PLAIN_ID: record})
    service_registry.ensure_service_registry(path=db, actor="test")
    return db


@pytest.mark.parametrize("enabled", [True, False, "off", 0], ids=repr)
def test_a_quarantined_record_keeps_the_owners_own_enabled_value(tmp_path, enabled):
    db = _bundled_override_db(tmp_path, enabled=enabled)
    record = json.loads(_row(db)[0])["workflows"][PLAIN_ID]
    assert record["enabled"] == enabled and type(record["enabled"]) is type(enabled)
    assert record["migration"]["step"] == migration.STEP and "was_enabled" not in record["migration"]


_BOOT_SCRIPT = """
import asyncio, json, os
import proxy
registry = proxy.GATEWAY_WORKFLOW_REGISTRY
booted = {key: registry.get(key) is not None for key in json.loads(os.environ["BOOT_IDS"])}
reloaded_error = None
try:
    proxy.reload_runtime_config()
except Exception as error:  # reported, not raised: the test reads it
    reloaded_error = f"{type(error).__name__}: {error}"
reloaded = {key: registry.get(key) is not None for key in json.loads(os.environ["BOOT_IDS"])}
evidence = asyncio.run(proxy._live_probe_evidence())
print("RESULT" + json.dumps({"booted": booted, "reloaded": reloaded, "reload_error": reloaded_error,
                             "mesh": proxy.current_mesh_workflow().id, "evidence": evidence}))
"""


def _boot(tmp_path: Path, db: Path, ids: list[str]) -> dict:
    """Start the gateway module in a fresh process over the stored registry in ``db``, then run a real reload."""
    import os
    import subprocess

    env = dict(os.environ)
    for name, filename in [("AZIZAM_METRICS_DB", "metrics.db"), ("AZIZAM_RECEIPTS_DB", "receipts.db"),
                           ("AZIZAM_MEDIA_JOBS_DB", "media_jobs.db"), ("AZIZAM_RESPONSES_DB", "responses.db")]:
        env[name] = str(tmp_path / filename)
    env.update(AZIZAM_ADMIN_DB=str(db), PYTHONPATH=str(ROOT), HOME=str(tmp_path), BOOT_IDS=json.dumps(ids))
    done = subprocess.run([sys.executable, "-c", _BOOT_SCRIPT], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(next(line for line in done.stdout.splitlines() if line.startswith("RESULT"))[len("RESULT"):])


@pytest.mark.parametrize("enabled", [False, "off", True], ids=repr)
def test_a_quarantined_override_of_a_bundled_id_gets_the_bundled_version_at_boot_and_after_a_real_reload(tmp_path, enabled):
    db = _bundled_override_db(tmp_path, enabled=enabled)
    (item,) = [e for e in service_registry.load_service_registry_exclusions(path=db) if e.id == PLAIN_ID]
    assert "is served in the meantime" in item.error and "not served either" not in item.error
    result = _boot(tmp_path, db, [PLAIN_ID])
    assert result["booted"] == {PLAIN_ID: True} and result["reload_error"] is None and result["reloaded"] == {PLAIN_ID: True}


def test_a_quarantined_record_with_no_bundled_counterpart_is_not_served(tmp_path):
    bad = _with_stated_capability()
    bad["inputs"]["x"] = {"type": []}
    db = _db_with(tmp_path, {"owner.bad": _owner_record("owner.bad", bad)})
    service_registry.ensure_service_registry(path=db, actor="test")
    (item,) = [e for e in service_registry.load_service_registry_exclusions(path=db) if e.id == "owner.bad"]
    assert "bundled version" not in item.error
    result = _boot(tmp_path, db, ["owner.bad"])
    assert result["booted"] == {"owner.bad": False} and result["reload_error"] is None and result["reloaded"] == {"owner.bad": False}


def _mesh_override_db(tmp_path) -> tuple[Path, str]:
    mesh_id = str(yaml.safe_load((ROOT / "manifests" / "workflows" / "mesh.generate.trellis-2.yml").read_text(encoding="utf-8"))["id"])
    document = {**yaml.safe_load((ROOT / "manifests" / "workflows" / "mesh.generate.trellis-2.yml").read_text(encoding="utf-8")),
                "schema_version": 1}
    document.setdefault("inputs", {})["x"] = {"type": []}  # cannot convert, so the migration quarantines the record
    record = {"id": mesh_id, "source": "ui", "enabled": False, "editable": True, "overrides": mesh_id, "manifest": document}
    db = _db_with(tmp_path, {mesh_id: record})
    service_registry.ensure_service_registry(path=db, actor="test")
    return db, mesh_id


def test_a_disabled_quarantined_mesh_override_does_not_stop_the_gateway_and_the_bundled_mesh_serves(tmp_path):
    db, mesh_id = _mesh_override_db(tmp_path)
    assert json.loads(_row(db)[0])["workflows"][mesh_id]["migration"]["step"] == migration.STEP  # quarantined, owner-disabled
    result = _boot(tmp_path, db, [mesh_id])
    assert result["booted"] == {mesh_id: True} and result["reload_error"] is None and result["reloaded"] == {mesh_id: True}
    assert result["mesh"] == mesh_id  # whether the engine behind it is up is another question: the workflow record is there


# -- the loader reads the stored registry once ------------------------------------------------------------------------------


def test_the_loader_reads_the_stored_registry_once_so_a_writer_between_reads_cannot_split_it(tmp_path, monkeypatch):
    import proxy

    db = _bundled_override_db(tmp_path, enabled=True)  # quarantined: the bundled version is served meanwhile
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    stored = json.loads(_row(db)[0])
    off = copy.deepcopy(stored)
    off["workflows"][PLAIN_ID]["enabled"] = False  # the writer turns the quarantined override off
    real, reads = admin_store.get_service_registry_json_with_generation, []

    def read_then_write(**kwargs):
        value = real(**kwargs)
        reads.append(value["operator_settings_generation"])
        if len(reads) == 1:
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE operator_settings SET value_json = ? WHERE key = 'service_registry'", (json.dumps(off),))
                conn.execute("UPDATE operator_settings_generations SET operator_settings_generation = operator_settings_generation + 1 "
                             "WHERE setting_key = 'service_registry'")
        return value

    monkeypatch.setattr(admin_store, "get_service_registry_json_with_generation", read_then_write)
    served = proxy._load_gateway_workflow_registry().get(PLAIN_ID)
    assert len(reads) == 1  # one read of the stored document
    assert served is not None  # the bundled version serves in both states of the writer's race


# -- a replacement that does not validate changes nothing ------------------------------------------------------------------


def test_an_invalid_replacement_leaves_the_row_the_block_and_the_generation_unchanged(tmp_path):
    db = _marked_db(tmp_path)
    before = _row(db)
    store = _store_of(db)
    still_bad = _fixed_record(db, inline=False)
    still_bad["manifest"]["schema_version"] = 1
    still_bad["manifest"]["inputs"] = {"x": {"type": []}}
    with pytest.raises(ValueError):
        store.replace_excluded_record(section="workflows", record_id="owner.bad", record=still_bad,
                                      actor=_owner_actor(), expected_generation=before[1])
    assert _row(db) == before
    assert json.loads(before[0])["workflows"]["owner.bad"]["migration"]["step"] == migration.STEP
    assert [e.id for e in service_registry.load_service_registry_exclusions(path=db)] == ["owner.bad"]


def test_an_invalid_schema_2_replacement_leaves_the_row_the_block_and_the_generation_unchanged(tmp_path):
    db = _marked_db(tmp_path)
    before = _row(db)
    store = _store_of(db)
    for breakage in ({"inputs": {"x": {"type": []}}}, {"typical_seconds": SENTINEL}, {"inputs": [SENTINEL]}):
        still_bad = _fixed_record(db, inline=False)
        assert still_bad["manifest"]["schema_version"] == 2  # a schema 2 document with invalid content
        still_bad["manifest"].update(breakage)
        with pytest.raises(ValueError):
            store.replace_excluded_record(section="workflows", record_id="owner.bad", record=still_bad,
                                          actor=_owner_actor(), expected_generation=before[1])
        assert _row(db) == before
    assert json.loads(before[0])["workflows"]["owner.bad"]["migration"]["step"] == migration.STEP
    assert [e.id for e in service_registry.load_service_registry_exclusions(path=db)] == ["owner.bad"]


# -- the release fix compares as JSON does, through apply_release_fixes -----------------------------------------------------


def test_apply_release_fixes_keeps_a_path_whose_value_changed_type(monkeypatch):
    monkeypatch.setattr(migration, "_frozen_documents", lambda: {"x.y": {"a": {"flag": True}, "b": "s"}})
    monkeypatch.setattr(migration, "_frozen_fixes", lambda: {"x.y": (("a",), ("b",))})
    same = {"a": {"flag": True}, "b": "s"}
    migration.apply_release_fixes("x.y", same)
    assert same == {}
    edited = {"a": {"flag": 1}, "b": "s"}  # True became 1: equal in Python, different in JSON
    migration.apply_release_fixes("x.y", edited)
    assert edited == {"a": {"flag": 1}, "b": "s"}
