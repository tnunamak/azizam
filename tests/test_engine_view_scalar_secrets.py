"""The engine view hides the secrets a scalar can hold, and a page save keeps what it hid.

An administrator reads the engine page. A header row that carries a credential, an address with
a user name, a password or a ``?token=`` query, a probe path with a query token, and a list-form
start command must not show their secret. A save that sends the marker back keeps the stored value.
"""

from __future__ import annotations

import json

import pytest

from gateway import engine_admin, engine_redaction as redaction
from gateway.engine_definitions import load_engine_catalog
from tests.test_engine_redaction import MARKER, SECRET, _command_record, _stored, _write
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

HEADERS = [
    {"name": "Authorization", "value": "Bearer hdr-syn-77"},
    {"name": "X-Api-Key", "value": "key-syn-88"},
    {"name": "Cookie", "value": "sid=cookie-syn-99"},
    {"name": "X-Org", "value": "org-public"},
]


def _view(record):
    from pathlib import Path
    return engine_admin.engine_view(load_engine_catalog(), record, db_path=Path("/nonexistent/admin.db"))


def _patch(client, view, values):
    etag = client.get("/admin/api/operator/service-registry").headers["etag"]
    return client.patch("/admin/api/operator/services/engine-a", headers={**csrf_for(client), "If-Match": etag},
                        json={"definition": view["definition"], "values": values})


def test_the_engine_view_hides_credential_header_values_and_keeps_the_rest() -> None:
    record = _command_record()
    record["config"]["headers"] = [dict(row) for row in HEADERS]
    text = json.dumps(_view(record))
    for secret in ("hdr-syn-77", "key-syn-88", "cookie-syn-99"):
        assert secret not in text
    view = _view(record)
    assert [row["name"] for row in view["values"]["headers"]] == [row["name"] for row in HEADERS]
    assert [row["value"] for row in view["values"]["headers"]] == [MARKER, MARKER, MARKER, "org-public"]
    assert [row["value"] for row in view["record"]["config"]["headers"]] == [MARKER, MARKER, MARKER, "org-public"]


def test_an_administrator_reads_no_header_secret_from_the_engine_route(gateway) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    record = _command_record()
    record["config"]["headers"] = [dict(row) for row in HEADERS]
    _write(gateway, record)
    reply = admin.get("/admin/api/engines/engine-a")
    assert reply.status_code == 200, reply.text
    for secret in ("hdr-syn-77", "key-syn-88", "cookie-syn-99"):
        assert secret not in reply.text


def test_an_address_with_a_user_name_a_password_or_a_query_token_is_shown_without_them() -> None:
    record = _command_record()
    record["config"]["address"] = "http://user:addr-syn-5@engine.invalid:8080/v1?token=qry-syn-6&key=k-syn-7"
    view = _view(record)
    text = json.dumps(view)
    for secret in ("addr-syn-5", "qry-syn-6", "k-syn-7", "user:"):
        assert secret not in text
    assert view["values"]["address"] == "http://engine.invalid:8080/v1"
    assert view["calls"].startswith("GET http://engine.invalid:8080/v1")
    assert view["v2"]["connection"]["address"] == "http://engine.invalid:8080/v1"


def test_a_probe_path_with_a_query_token_is_shown_without_it(gateway) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    record = _command_record()
    record["observations"] = {"process_ready": {"http": {"path": "/health?token=obs-syn-9", "expect": 200}}}
    _write(gateway, record)
    reply = admin.get("/admin/api/engines/engine-a")
    assert reply.status_code == 200, reply.text
    assert "obs-syn-9" not in reply.text
    assert reply.json()["engine"]["v2"]["observations"]["process_ready"]["http"]["path"] == "/health"


def test_the_stored_observations_survive_the_view_and_the_v2_migration(gateway) -> None:
    from gateway import service_registry
    record = _command_record()
    record["observations"] = {"process_ready": {"http": {"path": "/health?token=obs-syn-9", "expect": 200}}}
    registry = {"schema_version": 1, "services": {"engine-a": record}}
    service_registry.migrate_engine_records_v2(registry)
    assert registry["services"]["engine-a"]["observations"]["process_ready"]["http"]["path"] == "/health?token=obs-syn-9"


def test_the_settings_view_and_the_engine_view_hide_the_same_url_credentials() -> None:
    url = "http://user:pw-syn@host.invalid:81/p?token=t-syn#frag"
    assert redaction.public_value({"u": url})["u"] == "http://host.invalid:81/p"
    from gateway import settings_api
    assert settings_api.public_value({"u": url}) == redaction.public_value({"u": url})


def test_a_page_save_keeps_a_hidden_header_value_by_its_name(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    record = _command_record()
    record["config"]["headers"] = [{"name": "X-Org", "value": "sk-synthetic-hdr-1234"},
                                   {"name": "X-Team", "value": "team-public"}]
    _write(gateway, record)
    view = owner.get("/admin/api/engines/engine-a").json()["engine"]
    assert view["values"]["headers"][0] == {"name": "X-Org", "value": MARKER}
    kept = _patch(owner, view, {"reply_timeout_s": 300})
    assert kept.status_code == 200, kept.text
    assert _stored(gateway)["config"]["headers"] == record["config"]["headers"]


def test_a_header_row_renamed_with_the_marker_in_it_is_refused_not_given_the_hidden_value() -> None:
    stored = [{"name": "X-Org", "value": "sk-synthetic-hdr-1234"}, {"name": "X-Team", "value": "team-public"}]
    renamed = [{"name": "X-Renamed", "value": MARKER}, {"name": "X-Org", "value": MARKER}, {"name": "X-Team", "value": "team-public"}]
    with pytest.raises(ValueError, match="X-Renamed"):
        redaction.restore_headers(renamed, stored)
    with pytest.raises(ValueError, match="no hidden stored value"):
        redaction.restore_headers([{"name": "X-Org", "value": MARKER}], None)
    kept = [{"name": "X-Org", "value": MARKER}, {"name": "X-Team", "value": "team-public"}]
    assert redaction.restore_headers(kept, stored) == stored


def test_a_page_save_that_changes_only_the_ready_timeout_leaves_a_list_start_byte_identical(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _write(gateway, _command_record())
    view = owner.get("/admin/api/engines/engine-a").json()["engine"]
    assert view["values"]["lifecycle_start"] == ["runner", "--token", MARKER]
    saved = _patch(owner, view, {"lifecycle_ready_s": 200})
    assert saved.status_code == 200, saved.text
    config = _stored(gateway)["process"]["legacy_lifecycle"]["driver_config"]
    assert config["start"] == ["runner", "--token", SECRET]
    assert json.dumps(config["start"]) == json.dumps(["runner", "--token", SECRET])
    assert config["health_timeout_s"] == 200
    assert config["status"] == ["status"]


def test_a_page_save_that_sends_the_whole_list_back_keeps_the_list(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _write(gateway, _command_record())
    view = owner.get("/admin/api/engines/engine-a").json()["engine"]
    values = {"lifecycle_start": view["values"]["lifecycle_start"], "lifecycle_stop": "", "lifecycle_ready_s": 90}
    assert _patch(owner, view, values).status_code == 200
    assert _stored(gateway)["process"]["legacy_lifecycle"]["driver_config"]["start"] == ["runner", "--token", SECRET]


def test_a_page_save_does_not_store_the_hidden_address_form(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    record = _command_record()
    record["config"]["address"] = "http://user:addr-syn-5@engine.invalid:8080/v1"
    _write(gateway, record)
    view = owner.get("/admin/api/engines/engine-a").json()["engine"]
    assert "addr-syn-5" not in json.dumps(view)
    _patch(owner, view, {"reply_timeout_s": 300})
    assert _stored(gateway)["config"]["address"] == "http://user:addr-syn-5@engine.invalid:8080/v1"
