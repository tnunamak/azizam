"""The engine record v2 view is a public projection: no secret in a start command or an environment.

The reviewer's input is a command engine whose start argv carries ``--token synthetic-secret`` and
whose environment holds ``API_KEY``. Every place that returns the record or its process block must
show the shape and hide the value; a save that sends the marker back keeps the stored value.
"""

from __future__ import annotations

import copy
import json

import pytest

from gateway import admin_store, engine_admin, service_registry
from gateway.engine_definitions import load_engine_catalog
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

SECRET = "synthetic-secret"
MARKER = "[redacted]"


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


def test_the_v2_view_hides_a_promoted_start_argument_and_env_value() -> None:
    view = service_registry.service_record_v2(_command_record())
    assert SECRET not in json.dumps(view)
    assert view["process"]["start"] == ["runner", "--token", MARKER]
    assert view["process"]["env"] == {"API_KEY": MARKER, "MODE": MARKER}
    assert view["process"]["status"] == ["status"]


def test_the_v2_view_hides_a_native_process_env_and_argument() -> None:
    view = service_registry.service_record_v2(_command_record(native=True))
    assert SECRET not in json.dumps(view)
    assert view["process"]["start"] == ["runner", "--token", MARKER]
    assert view["process"]["env"] == {"API_KEY": MARKER, "MODE": MARKER}
    assert view["process"]["port"] == {"fixed": 8080}


@pytest.mark.parametrize("native", [False, True])
def test_the_engine_view_shows_no_secret_anywhere(native: bool) -> None:
    assert SECRET not in json.dumps(_view(_command_record(native=native)))


def _put(client, registry):
    etag = client.get("/admin/api/operator/service-registry").headers["etag"]
    return client.put("/admin/api/operator/service-registry", headers={**csrf_for(client), "If-Match": etag}, json=registry)


def _stored(gateway) -> dict:
    return admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["engine-a"]


def _write(gateway, record: dict, *, migrate: bool = True) -> None:
    registry = {"schema_version": 1 if migrate else 2, "services": {"engine-a": record}}
    if migrate:
        service_registry.migrate_engine_records_v2(registry)
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry(registry), expected_generation=0, updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)


def _seed(gateway, owner, *, native: bool = False) -> None:
    _write(gateway, _command_record(native=native), migrate=not native)


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_the_registry_routes_return_no_secret_to_the_owner_or_an_administrator(gateway, who) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _seed(gateway, owner)
    client = owner if who == "owner" else admin
    assert SECRET not in client.get("/admin/api/operator/service-registry").text
    assert SECRET not in client.get("/admin/api/engines/engine-a").text


def test_a_save_that_returns_the_marker_keeps_the_stored_secret(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner)
    body = owner.get("/admin/api/operator/service-registry").json()["service_registry"]
    body["services"]["engine-a"]["name"] = "Renamed"
    saved = _put(owner, body)
    assert saved.status_code == 200, saved.text
    assert SECRET not in saved.text
    stored = _stored(gateway)
    assert stored["name"] == "Renamed"
    assert stored["process"]["legacy_lifecycle"]["driver_config"]["start"] == ["runner", "--token", SECRET]
    assert stored["process"]["legacy_lifecycle"]["driver_config"]["env"] == {"API_KEY": SECRET, "MODE": "prod"}


def test_editing_a_command_around_the_marker_keeps_the_secret_and_takes_the_new_argument(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner, native=True)
    body = owner.get("/admin/api/operator/service-registry").json()["service_registry"]
    process = body["services"]["engine-a"]["process"]
    assert process["start"] == ["runner", "--token", MARKER]
    process["start"] = ["runner", "--verbose", "--token", MARKER]
    process["env"]["EXTRA"] = "plain"
    assert _put(owner, body).status_code == 200
    stored = _stored(gateway)["process"]
    assert stored["start"] == ["runner", "--verbose", "--token", SECRET]
    assert stored["env"] == {"API_KEY": SECRET, "MODE": "prod", "EXTRA": "plain"}


def test_a_marker_with_no_stored_value_is_refused_not_stored(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner, native=True)
    body = owner.get("/admin/api/operator/service-registry").json()["service_registry"]
    body["services"]["engine-a"]["process"]["start"] = ["runner", "--token", MARKER, "--key", MARKER]
    refused = _put(owner, body)
    assert refused.status_code == 400, refused.text
    assert _stored(gateway)["process"]["start"] == ["runner", "--token", SECRET]


def test_an_engine_page_save_that_changes_the_command_keeps_the_secret_behind_the_marker(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    record = _command_record()
    record["lifecycle"]["driver_config"]["start"] = f"runner --token {SECRET}"
    record["config"]["lifecycle"] = copy.deepcopy(record["lifecycle"])
    _write(gateway, record)
    view = owner.get("/admin/api/engines/engine-a").json()["engine"]
    assert SECRET not in json.dumps(view)
    start = view["values"]["lifecycle_start"]
    assert MARKER in start
    etag = owner.get("/admin/api/operator/service-registry").headers["etag"]
    edited = owner.patch("/admin/api/operator/services/engine-a", headers={**csrf_for(owner), "If-Match": etag},
                         json={"definition": view["definition"], "values": {"lifecycle_start": start + " --verbose"}})
    assert edited.status_code == 200, edited.text
    assert SECRET not in edited.text
    stored = _stored(gateway)["process"]["legacy_lifecycle"]["driver_config"]["start"]
    assert stored == f"runner --token {SECRET} --verbose"
