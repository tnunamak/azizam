"""The engine record v2 view is a public projection: no secret in a start command or an environment.

The reviewer's input is a command engine whose start argv carries ``--token synthetic-secret`` and whose environment
holds ``API_KEY``. A record holds those as entries in the secret store (decision D26); every place that returns the
record or its process block shows each as a configured entry and never the value, and a record write that sends the
entries back as read keeps them. (The tests of the old marker, which a save sent back to keep a value, are gone with
the marker: a secret changes only by command, ``tests/test_secret_commands.py``.)
"""

from __future__ import annotations

import copy
import json

import pytest

from gateway import admin_store, engine_admin, record_carriers, runtime_secrets, secret_refs, service_registry
from gateway.engine_definitions import load_engine_catalog
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

SECRET = "synthetic-secret"


def _command_record(*, native: bool = False) -> dict:
    start = ["runner", "--token", SECRET]
    env = {"API_KEY": SECRET, "MODE": "prod"}
    record = {
        "id": "engine-a", "name": "Engine A", "kind": "provider", "source": "ui", "enabled": True,
        "capabilities": ["chat"], "editable": True,
        "config": {"address": "http://engine.invalid", "protocol": "openai-compatible",
                   "models": [{"id": "first", "upstream_model": "upstream-a"}]},
    }
    if native:
        record["process"] = {"driver": "process", "start": start, "env": env, "port": {"fixed": 8080}}
        return record
    lifecycle = {"driver": "command", "driver_config": {"start": start, "status": ["status"], "env": env}}
    record["lifecycle"] = {**copy.deepcopy(lifecycle), "type": "command", "managed": True, "service_name": ""}
    record["config"]["lifecycle"] = copy.deepcopy(lifecycle)
    return record


def _view(record):
    from pathlib import Path
    return engine_admin.engine_view(load_engine_catalog(), record, db_path=Path("/nonexistent/admin.db"))


def _migrated(record: dict, db) -> dict:
    """The record as the store holds it: its secrets moved out (what a write or the schema 11 migration leaves)."""
    registry = {"schema_version": 1, "services": {"engine-a": record}}
    if "process" not in record:
        service_registry.migrate_engine_records_v2(registry)
    moved, _ = record_carriers.externalize_service(registry["services"]["engine-a"], secret_refs.putter(db))
    return moved


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    runtime_secrets.configure(path)
    yield path
    runtime_secrets.configure(None)


def _entry_view(view_process: dict) -> tuple:
    start = view_process["start"]
    return start[:2], start[2], view_process["env"]


@pytest.mark.parametrize("native", [False, True])
def test_the_v2_view_shows_a_start_argument_and_an_env_value_as_configured_entries(db, native: bool) -> None:
    moved = _migrated(_command_record(native=native), db)
    view = service_registry.service_record_v2(moved)

    assert SECRET not in json.dumps(view)
    flags, token, env = _entry_view(view["process"])
    assert flags == ["runner", "--token"] and token["configured"] is True and secret_refs.is_entry_id(token["id"])
    assert env["API_KEY"]["configured"] is True and env["MODE"]["configured"] is True
    assert "secret_ref" not in json.dumps(view)


@pytest.mark.parametrize("native", [False, True])
def test_the_engine_view_shows_no_secret_anywhere(db, native: bool) -> None:
    assert SECRET not in json.dumps(_view(_migrated(_command_record(native=native), db)))


def _put(client, registry):
    etag = client.get("/admin/api/operator/service-registry").headers["etag"]
    return client.put("/admin/api/operator/service-registry", headers={**csrf_for(client), "If-Match": etag}, json=registry)


def _stored(gateway) -> dict:
    return admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["engine-a"]


def _write(gateway, record: dict) -> None:
    moved = _migrated(record, gateway.path)
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry({"schema_version": 2, "services": {"engine-a": moved}}), expected_generation=0,
        updated_by="test", audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)


def _seed(gateway, owner, *, native: bool = False) -> None:
    _write(gateway, _command_record(native=native))


def _resolved(gateway, block: dict) -> dict:
    runtime_secrets.configure(gateway.path)
    try:
        return {"start": runtime_secrets.argv(block["start"]), "env": runtime_secrets.env_mapping(block["env"])}
    finally:
        runtime_secrets.configure(None)


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_the_registry_routes_return_no_secret_to_the_owner_or_an_administrator(gateway, who) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _seed(gateway, owner)
    client = owner if who == "owner" else admin
    assert SECRET not in client.get("/admin/api/operator/service-registry").text
    assert SECRET not in client.get("/admin/api/engines/engine-a").text


def test_a_save_that_sends_the_entries_back_as_read_keeps_the_stored_secrets(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner, native=True)
    body = owner.get("/admin/api/operator/service-registry").json()["service_registry"]
    body["services"]["engine-a"]["name"] = "Renamed"

    saved = _put(owner, body)

    assert saved.status_code == 200, saved.text
    assert SECRET not in saved.text
    stored = _stored(gateway)
    assert stored["name"] == "Renamed"
    assert _resolved(gateway, stored["process"]) == {"start": ["runner", "--token", SECRET], "env": {"API_KEY": SECRET, "MODE": "prod"}}


def test_a_save_that_changes_a_command_around_its_entries_is_refused_and_stores_nothing(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner, native=True)
    body = owner.get("/admin/api/operator/service-registry").json()["service_registry"]
    before = _stored(gateway)
    body["services"]["engine-a"]["process"]["start"] = ["runner", "--verbose", *body["services"]["engine-a"]["process"]["start"][1:]]

    refused = _put(owner, body)

    assert refused.status_code == 400 and "holds secret entries" in refused.json()["error"]["message"], refused.text
    assert _stored(gateway) == before


@pytest.mark.parametrize("marker", ["[redacted]", "<redacted>", "<configured>"])
def test_a_marker_text_is_never_taken_for_a_value(gateway, marker) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner, native=True)
    body = owner.get("/admin/api/operator/service-registry").json()["service_registry"]
    body["services"]["engine-a"]["process"]["env"]["NEW_KEY"] = marker
    before = _stored(gateway)

    answer = _put(owner, body)

    assert answer.status_code == 400, answer.text
    assert _stored(gateway) == before


def test_an_engine_page_save_that_leaves_the_command_out_keeps_the_secret(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    record = _command_record()
    record["lifecycle"]["driver_config"]["start"] = f"runner --token {SECRET}"
    record["config"]["lifecycle"] = copy.deepcopy(record["lifecycle"])
    _write(gateway, record)
    view = owner.get("/admin/api/engines/engine-a").json()["engine"]
    assert SECRET not in json.dumps(view)
    etag = owner.get("/admin/api/operator/service-registry").headers["etag"]

    edited = owner.patch("/admin/api/operator/services/engine-a", headers={**csrf_for(owner), "If-Match": etag},
                         json={"definition": view["definition"], "name": "Renamed again", "values": {}})

    assert edited.status_code == 200, edited.text
    assert SECRET not in edited.text
    stored = _stored(gateway)["process"]["legacy_lifecycle"]["driver_config"]["start"]
    runtime_secrets.configure(gateway.path)
    try:
        assert runtime_secrets.argv(stored) == ["runner", "--token", SECRET]
    finally:
        runtime_secrets.configure(None)
