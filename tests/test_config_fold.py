"""Slice C6: instance, media admission and service availability as sections of the config layer.

The responses of the three routes are held by ``test_config_fold_responses.py``. This file holds what that journey
cannot show: the one-time import of the old stores, the sparse rows, the revision rules and the reload hook.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import accounts, admin_store, config_fold, config_layers as layers, config_runtime
from gateway.admin_api import AdminContext, create_admin_router
from gateway.admin_runtime import AdminSQLiteStore
from gateway.settings_api import create_settings_router

OWNER = SimpleNamespace(subject="owner", actor_kind="user", audit_actor_id="owner", actor_label="owner")
STARTUP = {
    "admin": {"brand": {"product": "Startup brand"}},
    "proxy": {"public_url": "https://startup.example.test"},
    "gateway": {"resource_admission": {"media_jobs": {"max_queue_depth": 4, "scan_limit": 50, "reserved_tokens": ["file-token"]}}},
}


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    return path


def rows(path: Path, section: str) -> dict[str, object]:
    return {
        row["pointer"]: (json.loads(row["value_json"]) if row["op"] == "set" else None)
        for row in admin_store.list_config_overrides(section, path=path)
    }


def legacy(path: Path, key: str, value: dict) -> None:
    admin_store.put_operator_setting(key, value, path=path)


# -- the sections ---------------------------------------------------------------------------------------------------

def test_the_release_ships_the_three_sections_and_they_load():
    home = config_runtime.ConfigHome()
    assert {"instance", "resource_admission", "service_availability"} <= set(home.sections)
    assert home.sections["instance"].defaults == {"name": "Azizam", "base_url": "", "theme_default": "system"}
    assert home.sections["resource_admission"].defaults == {"media_jobs": {}}
    assert home.sections["service_availability"].defaults == {}


def test_the_schemas_refuse_what_the_routes_refuse():
    home = config_runtime.ConfigHome()
    instance = home.sections["instance"]
    assert layers.validate_section(instance, {"name": "X", "base_url": "", "theme_default": "sepia"})
    assert layers.validate_section(instance, {"name": "", "base_url": "", "theme_default": "dark"})
    assert layers.validate_section(instance, {"name": "X", "base_url": "", "theme_default": "dark", "extra": 1})
    admission = home.sections["resource_admission"]
    assert layers.validate_section(admission, {"media_jobs": {"max_queue_depth": -1}})
    assert layers.validate_section(admission, {"media_jobs": {"max_app_concurrency": {"mine": -1}}})
    assert layers.validate_section(admission, {"media_jobs": {"bogus": 1}})
    assert not layers.validate_section(admission, {"media_jobs": {"max_app_concurrency": {"mine": 2}, "max_queue_depth": None}})
    availability = home.sections["service_availability"]
    assert layers.validate_section(availability, {"engine": {"state": "bogus"}})
    assert layers.validate_section(availability, {"engine": {"state": "disabled", "extra": 1}})
    assert not layers.validate_section(availability, {"engine": {"state": "draining", "reason": None, "retry_after_seconds": 3}})


def test_the_entries_are_the_units_of_a_change():
    home = config_runtime.ConfigHome()
    admission = home.sections["resource_admission"].index
    rule = admission.rule_for("/media_jobs/max_app_concurrency")
    assert rule is not None and rule.kind == "atomic"  # the per-app map is replaced whole
    assert admission.rule_for("/media_jobs/disabled_intents") is not None  # a list is one entry
    member = home.sections["service_availability"].index.rule_for("/engine-a")
    assert member is not None and member.kind == "member"
    assert home.sections["instance"].index.rule_for("/name") is not None


# -- sparse rows, revisions, audit ----------------------------------------------------------------------------------

def test_an_instance_field_equal_to_the_release_default_has_no_row(db):
    value, revision = config_fold.instance_with_revision(db)
    config_fold.put_instance(db, {"name": "Home", "base_url": "https://gw.example", "theme_default": "system"}, OWNER,
                             expected_revision=revision)
    assert rows(db, "instance") == {"/name": "Home", "/base_url": "https://gw.example"}
    assert config_fold.instance(db) == {"name": "Home", "base_url": "https://gw.example", "theme_default": "system"}
    revision = config_fold.instance_with_revision(db)[1]
    config_fold.put_instance(db, {"name": "Azizam", "base_url": "", "theme_default": "system"}, OWNER, expected_revision=revision)
    assert rows(db, "instance") == {}
    assert config_fold.instance(db) == value


def test_a_row_records_the_release_value_it_was_written_over(db):
    config_fold.put_instance(db, {"name": "Home", "base_url": "", "theme_default": "dark"}, OWNER, expected_revision="0")
    stored = {row["pointer"]: row for row in admin_store.list_config_overrides("instance", path=db)}
    assert json.loads(stored["/name"]["base_json"]) == "Azizam"
    assert stored["/name"]["origin"] == "api"
    assert stored["/name"]["updated_by"] == "owner"


@pytest.mark.parametrize("section,save", [
    ("instance", lambda path, rev: config_fold.put_instance(
        path, {"name": "Azizam", "base_url": "", "theme_default": "system"}, OWNER, expected_revision=rev)),
    ("resource_admission", lambda path, rev: config_fold.put_media_job_admission(path, {}, OWNER, expected_revision=rev)),
    ("service_availability", lambda path, rev: config_fold.put_service_availability(path, {}, OWNER, expected_revision=rev)),
])
def test_a_save_that_changes_no_row_still_advances_the_revision_and_writes_its_audit_row(db, section, save):
    first = admin_store.read_config_section(section, path=db)["revision"]
    save(db, first)
    second = admin_store.read_config_section(section, path=db)["revision"]
    save(db, second)
    third = admin_store.read_config_section(section, path=db)["revision"]
    assert [first, second, third] == ["0", "1", "2"]
    actions = [row["action"] for row in admin_store.list_audit(path=db)]
    assert actions == [config_fold.AUDIT_ACTIONS[section]] * 2
    # A save that changes no row has no change record, only the audit row and the new revision.
    assert admin_store.list_config_changes(path=db) == []


def test_a_save_keeps_the_legacy_audit_target_and_never_writes_a_config_change_audit_row(db):
    config_fold.put_media_job_admission(db, {"max_queue_depth": 3}, OWNER, expected_revision="0")
    (event,) = admin_store.list_audit(path=db)
    assert (event["action"], event["target_kind"], event["target_id"]) == (
        "operator.media_job_admission.update", "operator_setting", "media_job_admission_overrides")
    assert event["metadata"] == {"fields": ["max_queue_depth"]}


def test_a_stale_revision_refuses_the_write_and_writes_nothing(db):
    config_fold.put_media_job_admission(db, {"max_queue_depth": 3}, OWNER, expected_revision="0")
    with pytest.raises(admin_store.ConfigRevisionMismatch):
        config_fold.put_media_job_admission(db, {"max_queue_depth": 9}, OWNER, expected_revision="0")
    assert rows(db, "resource_admission")["/media_jobs/max_queue_depth"] == 3
    assert len(admin_store.list_audit(path=db)) == 1


def test_a_media_save_replaces_the_document_but_leaves_keys_the_route_cannot_set(db):
    config_fold.put_media_job_admission(db, {"max_queue_depth": 3, "reserved_tokens": ["a"]}, OWNER, expected_revision="0")
    # scan_limit has no field in the route; it can only come from the startup file or an import.
    admin_store.apply_config_change(
        [admin_store.ConfigItem("resource_admission", "/media_jobs/scan_limit", "set", "50")],
        if_match={"resource_admission": "1"}, validate=None, path=db)
    config_fold.put_media_job_admission(db, {"retry_after_seconds": 5}, OWNER, expected_revision="2")
    assert rows(db, "resource_admission") == {"/media_jobs/retry_after_seconds": 5, "/media_jobs/scan_limit": 50}
    document, _revision = config_fold.media_job_admission(db)
    assert document == {"retry_after_seconds": 5, "scan_limit": 50}


def test_an_availability_member_is_one_row_and_clearing_it_removes_the_row(db):
    saved = config_fold.put_service_availability(
        db, {"a/b~c": {"state": "degraded", "reason": None, "retry_after_seconds": None}}, OWNER)
    assert saved == {"a/b~c": {"state": "degraded", "reason": None, "retry_after_seconds": None}}
    assert list(rows(db, "service_availability")) == ["/a~1b~0c"]
    assert config_fold.service_availability(db) == saved
    assert config_fold.put_service_availability(db, {}, OWNER) == {}
    assert rows(db, "service_availability") == {}


def test_availability_needs_no_precondition_but_honours_one(db):
    record = {"state": "maintenance", "reason": "fixing", "retry_after_seconds": 30}
    config_fold.put_service_availability(db, {"engine-a": record}, OWNER)
    revision = admin_store.read_config_section("service_availability", path=db)["revision"]
    with pytest.raises(admin_store.ConfigRevisionMismatch):
        config_fold.put_service_availability(db, {}, OWNER, expected_revision="0")
    assert config_fold.put_service_availability(db, {}, OWNER, expected_revision=revision) == {}


def test_an_override_the_section_refuses_is_not_served(db):
    admin_store.apply_config_change(
        [admin_store.ConfigItem("instance", "/theme_default", "set", json.dumps("sepia"))],
        if_match={"instance": "0"}, validate=None, path=db)
    assert config_fold.instance(db)["theme_default"] == "system"  # the release value stands, and the layer reports it


# -- the one-time import --------------------------------------------------------------------------------------------

def test_the_import_turns_the_old_documents_and_startup_values_into_rows_once(db):
    legacy(db, "instance_presentation", {"name": "Saved", "base_url": "https://saved.example.test", "theme_default": "dark"})
    legacy(db, "media_job_admission_overrides", {"max_queue_depth": 2, "disabled_intents": ["a"]})
    legacy(db, "service_availability_overrides", {"engine-a": {"state": "maintenance", "reason": "x", "retry_after_seconds": None}})
    report = config_fold.migrate(db, STARTUP, environ={})
    assert {item["section"]: item["rows"] for item in report} == {
        "instance": 3, "resource_admission": 4, "service_availability": 1}
    assert config_fold.instance(db) == {"name": "Saved", "base_url": "https://saved.example.test", "theme_default": "dark"}
    # The saved document wins over the file for each key it holds; the file still gives the keys it does not.
    assert config_fold.media_job_admission(db)[0] == {
        "max_queue_depth": 2, "disabled_intents": ["a"], "scan_limit": 50, "reserved_tokens": ["file-token"]}
    assert config_fold.service_availability(db) == {"engine-a": {"state": "maintenance", "reason": "x", "retry_after_seconds": None}}
    stored = admin_store.list_config_overrides(path=db)
    assert {row["origin"] for row in stored} == {"legacy-import"}
    for name in config_fold.SECTIONS:
        assert admin_store.migration_step_done(config_fold.STEP_PREFIX + name, path=db)
    assert config_fold.migrate(db, STARTUP, environ={}) == []  # once


def test_the_import_reads_the_startup_values_when_nothing_was_saved(db):
    config_fold.migrate(db, STARTUP, environ={})
    assert config_fold.instance(db) == {"name": "Startup brand", "base_url": "https://startup.example.test", "theme_default": "system"}
    assert config_fold.media_job_admission(db)[0] == {"max_queue_depth": 4, "scan_limit": 50, "reserved_tokens": ["file-token"]}
    assert config_fold.service_availability(db) == {}


def test_the_environment_beats_the_startup_file_in_the_import_as_it_did_before(db):
    config_fold.migrate(db, STARTUP, environ={"AZIZAM_BRAND": "Env brand", "AZIZAM_PUBLIC_URL": "https://env.example.test"})
    assert config_fold.instance(db)["name"] == "Env brand"
    assert config_fold.instance(db)["base_url"] == "https://env.example.test"


def test_an_import_with_nothing_to_write_still_records_its_step(db):
    assert [item["rows"] for item in config_fold.migrate(db, {}, environ={})] == [0, 0, 0]
    assert rows(db, "instance") == {}
    legacy(db, "instance_presentation", {"name": "Late"})
    assert config_fold.migrate(db, {}, environ={}) == []
    assert config_fold.instance(db)["name"] == "Azizam"  # the old document is not read after the import


def test_the_import_leaves_the_old_keys_exactly_as_they_were(db):
    legacy(db, "instance_presentation", {"name": "Saved", "base_url": "", "theme_default": "dark"})
    legacy(db, "media_job_admission_overrides", {"max_queue_depth": 2})
    before = {row["key"]: row for row in admin_store.list_operator_settings(path=db)}
    config_fold.migrate(db, STARTUP, environ={})
    config_fold.put_instance(db, {"name": "Changed", "base_url": "", "theme_default": "light"}, OWNER,
                             expected_revision=admin_store.read_config_section("instance", path=db)["revision"])
    after = {row["key"]: row for row in admin_store.list_operator_settings(path=db)}
    assert after == before  # a rollback to the previous commit finds its keys as they were


def test_a_value_a_section_refuses_is_left_out_and_named_and_the_rest_is_kept(db):
    legacy(db, "instance_presentation", {"name": "Fine", "theme_default": "sepia", "unknown": "x"})
    report = config_fold.migrate(db, {}, environ={})
    instance = next(item for item in report if item["section"] == "instance")
    assert instance["dropped"] == ["/theme_default"]
    assert config_fold.instance(db) == {"name": "Fine", "base_url": "", "theme_default": "system"}


def test_a_section_that_already_has_rows_is_not_overwritten_by_the_import(db):
    admin_store.apply_config_change(
        [admin_store.ConfigItem("instance", "/name", "set", json.dumps("From the API"))],
        if_match={"instance": "0"}, validate=None, path=db)
    legacy(db, "instance_presentation", {"name": "Stale"})
    config_fold.migrate(db, {}, environ={})
    assert config_fold.instance(db)["name"] == "From the API"


# -- reload hook and the owner API ----------------------------------------------------------------------------------

def test_a_folded_save_through_the_store_tells_the_host_to_reload(db):
    store = AdminSQLiteStore(db)
    calls: list[str] = []
    store.on_config_changed = lambda: calls.append("reload")
    actor = AdminContext(subject="owner", roles=frozenset({"owner"}), auth_method="bearer")
    store.update_media_job_admission_overrides(data={"max_queue_depth": 1}, actor=actor, expected_revision="0")
    store.update_service_availability_overrides(data={}, actor=actor)
    assert calls == ["reload", "reload"]

    def failing() -> None:
        raise RuntimeError("reload failed")

    store.on_config_changed = failing
    store.update_service_availability_overrides(data={}, actor=actor)  # the change is stored; the failure is only logged


def test_the_instance_route_tells_the_host_to_reload_after_a_save_and_survives_a_failed_reload(db):
    accounts.initialize(db)
    actor = AdminContext(subject="owner", roles=frozenset({"owner"}), auth_method="bearer")
    calls: list[str] = []

    def reload() -> None:
        calls.append("reload")
        raise RuntimeError("reload failed")

    app = FastAPI()
    app.include_router(create_settings_router(auth=lambda _request: actor, admin_path=db, config_provider=lambda: {},
                                              on_config_changed=reload))
    client = TestClient(app)
    body = {"name": "Home", "base_url": "", "theme_default": "dark"}
    saved = client.put("/admin/api/settings/instance", json=body, headers={"If-Match": "0"})
    assert saved.status_code == 200 and saved.json()["instance"] == body
    assert calls == ["reload"]
    stale = client.put("/admin/api/settings/instance", json=body, headers={"If-Match": "0"})
    assert stale.status_code == 412 and calls == ["reload"]  # a refused write does not reload


def _app(db: Path) -> TestClient:
    accounts.initialize(db)
    actor = AdminContext(subject="owner", roles=frozenset({"owner"}), auth_method="bearer")
    app = FastAPI()
    app.include_router(create_admin_router(AdminSQLiteStore(db), auth=lambda _request: actor))
    return TestClient(app)


def test_the_availability_route_answers_412_for_a_stale_precondition_and_accepts_none(db):
    client = _app(db)
    url = "/admin/api/operator/service-availability"
    assert client.put(url, json={"engine-a": {"state": "maintenance"}}).status_code == 200
    stale = client.put(url, json={"engine-b": {"state": "draining"}}, headers={"If-Match": '"0"'})
    assert stale.status_code == 412
    assert stale.json() == {"error": {"code": "settings_conflict", "message": "Settings changed elsewhere; reload to see the latest"}}
    current = admin_store.read_config_section("service_availability", path=db)["revision"]
    assert client.put(url, json={"engine-b": {"state": "draining"}}, headers={"If-Match": f'"{current}"'}).status_code == 200
    assert sorted(config_fold.service_availability(db)) == ["engine-a", "engine-b"]


def test_a_folded_save_is_a_config_change_the_owner_api_can_list_and_the_layer_serves(db):
    client = _app(db)
    client.put("/admin/api/operator/media-job-admission", json={"max_queue_depth": 3}, headers={"If-Match": "0"})
    (change,) = admin_store.list_config_changes(path=db)
    assert change["kind"] == "edit" and change["items"][0]["pointer"] == "/media_jobs/max_queue_depth"
    home = config_runtime.ConfigHome()
    layer = home.layer({}, path=db)
    assert layer.config["resource_admission"]["media_jobs"]["max_queue_depth"] == 3
    assert dict(layer.revisions)["resource_admission"] == "1"
