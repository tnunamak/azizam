"""Named step workflow-overrides-off: a stored record that replaced a bundled contract becomes run control only.

Owner overrides of bundled ids are going away. A stored override whose contract and facts equal the release's becomes
the run-control patch the store already writes when the owner turns a bundled workflow on or off, so a later release
shows through. An override that differs is left as stored and named, never changed by guesswork. A record whose id
the release no longer ships loses only its ``overrides`` field and becomes a plain owner record.
"""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from gateway import admin_store, registry_layers, registry_snapshot, service_registry, workflow_overrides_migration
from gateway.workflow_registry_store import facts_record

SAME = "video.generate.ltx-2.5-t2v"
DIFFERS = "image.generate.krea2-turbo"
RETIRED = "image.generate.retired-demo"
REPLACE = registry_layers.REPLACE_KEY
PATCH_OF = registry_layers.RUN_CONTROL_PATCH_KEY


def _bundled(wid: str) -> tuple[dict, dict]:
    """The release's contract and facts record for ``wid``."""
    entry = registry_snapshot.build({}, registry_snapshot.bundled_release(), generation=1).get(wid)
    return copy.deepcopy(service_registry.bundled_workflow_manifests()[wid]), facts_record(entry.facts)


def _override(wid: str, manifest: dict, policy: dict, **control) -> dict:
    record = {"manifest": manifest, "policy": policy, "overrides": wid, "source": "local", "editable": True,
              "enabled": True, "state": "active", "published": True, "incarnation": f"inc-{wid}", "record_revision": 3}
    record.update(control)
    return {REPLACE: record}


def _registry() -> dict:
    same_manifest, same_policy = _bundled(SAME)
    differs_manifest, differs_policy = _bundled(DIFFERS)
    differs_manifest["display_name"] = "Owner's own name"
    retired_manifest = copy.deepcopy(same_manifest)
    retired_manifest["id"] = RETIRED
    return {"workflows": {
        SAME: _override(SAME, same_manifest, same_policy, published=False),
        DIFFERS: _override(DIFFERS, differs_manifest, differs_policy),
        RETIRED: _override(RETIRED, retired_manifest, dict(same_policy, exposure="disabled"), state="disabled"),
    }}


def _stored(db: Path) -> dict:
    with closing(sqlite3.connect(db)) as conn:
        return json.loads(conn.execute(
            "SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0])


def _generation(db: Path) -> int:
    with closing(sqlite3.connect(db)) as conn:
        return int(conn.execute("SELECT operator_settings_generation FROM operator_settings_generations "
                                "WHERE setting_key='service_registry'").fetchone()[0])


def _store_with(tmp_path: Path, registry: dict) -> Path:
    """A real admin database whose stored registry predates the step."""
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT OR REPLACE INTO operator_settings(key, value_json, updated_at, updated_by) "
                     "VALUES ('service_registry', ?, 1, 'owner')", (json.dumps(registry),))
        conn.execute("INSERT OR REPLACE INTO operator_settings_generations(setting_key, operator_settings_generation) "
                     "VALUES ('service_registry', 5)")
        conn.execute("DELETE FROM schema_migration_steps WHERE name=?", (workflow_overrides_migration.STEP_NAME,))
        conn.commit()
    return db


def test_each_stored_override_becomes_run_control_a_named_kept_override_or_a_plain_owner_record(tmp_path):
    registry = _registry()
    db = _store_with(tmp_path, registry)
    before = _generation(db)

    admin_store.init_db(db)   # the startup path runs the named step

    workflows = _stored(db)["workflows"]
    assert workflows[SAME] == {REPLACE: {PATCH_OF: SAME, "state": "active", "published": False, "record_revision": 4}}
    assert workflows[DIFFERS] == registry["workflows"][DIFFERS]   # differs: left exactly as stored
    retired = dict(registry["workflows"][RETIRED][REPLACE])
    del retired["overrides"]
    retired["record_revision"] = 4
    assert workflows[RETIRED] == retired   # not bundled: a plain owner record, as the store writes one
    assert _generation(db) == before + 1
    with closing(sqlite3.connect(db)) as conn:
        audit = conn.execute("SELECT action, metadata_json FROM audit_events WHERE action=?",
                             ("operator.workflow_overrides.migrate",)).fetchall()
    assert len(audit) == 1
    assert json.loads(audit[0][1])["workflows"] == {SAME: "run-control", DIFFERS: "kept-differs", RETIRED: "owner-record"}


def test_the_migrated_bundled_record_serves_the_release_contract_and_facts_with_the_owner_run_control(tmp_path):
    db = _store_with(tmp_path, _registry())
    admin_store.init_db(db)

    release = registry_snapshot.build({}, registry_snapshot.bundled_release(), generation=1).get(SAME)
    document, _excluded = service_registry.effective_registry(_stored(db))   # the layering the store reads through
    served = registry_snapshot.build(document, registry_snapshot.bundled_release(), generation=2).get(SAME)
    assert served.contract_digest == release.contract_digest
    assert served.facts == release.facts
    assert (served.state, served.published) == ("active", False)


def test_the_step_runs_once_and_a_second_run_changes_nothing(tmp_path):
    db = _store_with(tmp_path, _registry())
    admin_store.init_db(db)
    once, generation = _stored(db), _generation(db)

    with closing(sqlite3.connect(db)) as conn:
        # Run directly again: only the differing override is still an override, and it is named, not rewritten.
        assert workflow_overrides_migration.migrate_registry(conn) == {DIFFERS: "kept-differs"}
        conn.commit()
    admin_store.init_db(db)   # recorded by name: the startup path does not run it again

    assert _stored(db) == once and _generation(db) == generation


def _served(db: Path, wid: str):
    document, _excluded = service_registry.effective_registry(_stored(db))   # the layering the store reads through
    return registry_snapshot.build(document, registry_snapshot.bundled_release(), generation=9).get(wid)


def test_the_retired_record_serves_as_a_plain_owner_record_with_its_own_run_control(tmp_path):
    db = _store_with(tmp_path, _registry())
    admin_store.init_db(db)

    assert "overrides" not in _stored(db)["workflows"][RETIRED]
    served = _served(db, RETIRED)
    assert served is not None
    assert (served.state, served.published) == ("disabled", True)


def test_a_run_of_only_kept_overrides_names_them_and_writes_nothing(tmp_path):
    registry = _registry()
    del registry["workflows"][SAME], registry["workflows"][RETIRED]
    db = _store_with(tmp_path, registry)
    before = _generation(db)

    admin_store.init_db(db)

    assert _stored(db) == registry and _generation(db) == before
    with closing(sqlite3.connect(db)) as conn:
        audit = conn.execute("SELECT metadata_json FROM audit_events WHERE action=?",
                             ("operator.workflow_overrides.migrate",)).fetchall()
    assert [json.loads(row[0])["workflows"] for row in audit] == [{DIFFERS: "kept-differs"}]


def test_state_and_publication_follow_the_snapshot_rules_and_no_policy_means_the_release_facts(tmp_path):
    """A record with no state follows ``enabled``; one with no ``published`` is unpublished; no policy is the release's."""
    manifest, _policy = _bundled(SAME)
    record = {"manifest": manifest, "overrides": SAME, "enabled": False, "record_revision": 2}
    db = _store_with(tmp_path, {"workflows": {SAME: {REPLACE: record}}})

    admin_store.init_db(db)

    assert _stored(db)["workflows"][SAME] == {REPLACE: {PATCH_OF: SAME, "state": "disabled", "published": False,
                                                        "record_revision": 3}}


def _raw(db: Path) -> tuple[str, int]:
    with closing(sqlite3.connect(db)) as conn:
        value = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0]
    return value, _generation(db)


def _step_recorded(db: Path) -> bool:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT 1 FROM schema_migration_steps WHERE name=?",
                            (workflow_overrides_migration.STEP_NAME,)).fetchone() is not None


def test_a_kept_override_whose_run_control_differs_is_kept_logged_and_leaves_the_step_unrecorded(tmp_path, caplog):
    registry = _registry()
    registry["workflows"][DIFFERS][REPLACE]["published"] = False   # the release publishes it
    db = _store_with(tmp_path, registry)
    before = _generation(db)

    with caplog.at_level("WARNING", logger=workflow_overrides_migration.__name__):
        admin_store.init_db(db)   # no raise: the start goes on

    workflows = _stored(db)["workflows"]
    assert workflows[DIFFERS] == registry["workflows"][DIFFERS]   # kept exactly as stored
    assert workflows[SAME] == {REPLACE: {PATCH_OF: SAME, "state": "active", "published": False, "record_revision": 4}}
    assert "overrides" not in workflows[RETIRED]   # the other rewrites committed
    assert _generation(db) == before + 1
    assert not _step_recorded(db)   # runs again on the next start
    with closing(sqlite3.connect(db)) as conn:
        audit = conn.execute("SELECT metadata_json FROM audit_events WHERE action=?",
                             ("operator.workflow_overrides.migrate",)).fetchall()
    assert [json.loads(row[0])["workflows"] for row in audit] == [
        {SAME: "run-control", DIFFERS: "kept-run-control-differs", RETIRED: "owner-record"}]
    line = next(r.getMessage() for r in caplog.records if DIFFERS in r.getMessage())
    assert "stored state='active' published=False" in line and "release state='active' published=True" in line
    assert "Delete the kept record" in line


def test_a_kept_override_with_the_release_run_control_stays_stored_and_named(tmp_path):
    release = registry_snapshot.build({}, registry_snapshot.bundled_release(), generation=1).get(DIFFERS)
    registry = _registry()
    del registry["workflows"][SAME], registry["workflows"][RETIRED]
    registry["workflows"][DIFFERS][REPLACE].update(state=release.state, published=release.published)
    db = _store_with(tmp_path, registry)
    before = _raw(db)

    admin_store.init_db(db)

    assert _raw(db) == before
    with closing(sqlite3.connect(db)) as conn:
        audit = conn.execute("SELECT metadata_json FROM audit_events WHERE action=?",
                             ("operator.workflow_overrides.migrate",)).fetchall()
    assert [json.loads(row[0])["workflows"] for row in audit] == [{DIFFERS: "kept-differs"}]
