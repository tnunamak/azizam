"""Decision D27: a secret changes only through an explicit command that names a stable entry id.

Retain, replace, rename, clear, add and set are separate verbs. A ``[redacted]`` marker, an empty string, ``null`` or a
position never means "keep". Every command is checked before anything is stored; a refused call stores nothing and a
stale revision stores nothing. The commands are the owner's; the answer shows an entry as configured, never a value.
"""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from gateway import admin_store, record_carriers, runtime_secrets, secret_commands, secret_refs, service_registry
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

SEEDS = {name: f"SEED-cmd-{name}" for name in ("a", "b", "c", "d", "e", "f", "g", "h")}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    runtime_secrets.configure(path)
    yield path
    runtime_secrets.configure(None)


def _record(**extra):
    return {
        "id": "engine-a", "name": "Engine A", "kind": "engine", "enabled": True, "capabilities": ["chat"],
        "config": {"host": "engine.invalid", "port": 8080, "protocol": "openai-compatible", "address": "http://engine.invalid:8080/v1",
                   "models": [{"id": "m", "upstream_model": "m"}]},
        "process": {"driver": "process", "start": ["runner", "--port", "8080"], "port": {"fixed": 8080}}, **extra,
    }


def _apply(db, record, *commands):
    put = secret_refs.putter(db)
    return secret_commands.apply_commands(record, secret_commands.parse_commands({"commands": list(commands)}), put)


def _first_header(record):
    return record["config"]["headers"][0]


def _added(db):
    record, _ = _apply(db, _record(), {"carrier": "config.headers", "operation": "add", "name": "X-Vendor", "value": SEEDS["a"]})
    return record


def test_add_stores_the_value_apart_and_the_record_holds_only_an_entry(db):
    record = _added(db)
    row = _first_header(record)
    assert set(row) == {"id", "name", "secret_ref"} and secret_refs.is_entry_id(row["id"])
    assert SEEDS["a"] not in json.dumps(record)
    assert runtime_secrets.header_pairs(record["config"]["headers"], origin="http://engine.invalid:8080") == {"X-Vendor": SEEDS["a"]}


def test_replace_keeps_the_entry_id_and_retires_the_old_secret(db):
    record = _added(db)
    old_ref, entry_id = _first_header(record)["secret_ref"], _first_header(record)["id"]

    new, outcome = _apply(db, record, {"carrier": "config.headers", "operation": "replace", "entry_id": entry_id, "value": SEEDS["b"]})

    row = _first_header(new)
    assert row["id"] == entry_id and row["secret_ref"] != old_ref and outcome.retired == [old_ref]
    assert runtime_secrets.header_pairs(new["config"]["headers"], origin="http://engine.invalid:8080") == {"X-Vendor": SEEDS["b"]}


def test_retain_changes_nothing_and_needs_an_existing_id(db):
    record = _added(db)
    entry_id = _first_header(record)["id"]

    same, outcome = _apply(db, record, {"carrier": "config.headers", "operation": "retain", "entry_id": entry_id})

    assert same == record and not outcome.changed and outcome.created == []
    with pytest.raises(secret_commands.CommandError) as refused:
        _apply(db, record, {"carrier": "config.headers", "operation": "retain", "entry_id": "ent_" + "0" * 16})
    assert refused.value.code == "unknown_entry"


@pytest.mark.parametrize("value", ["[redacted]", "<redacted>", "<configured>", "", None, 5, "x" * 9000, "a\x00b"])
def test_a_marker_an_empty_value_null_or_anything_but_real_text_is_never_a_secret_value(db, value):
    record = _added(db)
    entry_id = _first_header(record)["id"]
    with pytest.raises(secret_commands.CommandError):
        _apply(db, record, {"carrier": "config.headers", "operation": "replace", "entry_id": entry_id, "value": value})
    with pytest.raises(secret_commands.CommandError):
        _apply(db, record, {"carrier": "config.headers", "operation": "add", "name": "X-B", "value": value})


def test_clear_removes_the_entry_and_retires_its_secret_and_rename_changes_only_the_name(db):
    record = _added(db)
    row = _first_header(record)

    renamed, outcome = _apply(db, record, {"carrier": "config.headers", "operation": "rename", "entry_id": row["id"], "name": "X-New"})
    assert _first_header(renamed)["name"] == "X-New" and _first_header(renamed)["secret_ref"] == row["secret_ref"] and outcome.retired == []

    cleared, outcome = _apply(db, record, {"carrier": "config.headers", "operation": "clear", "entry_id": row["id"]})
    assert cleared["config"]["headers"] == [] and outcome.retired == [row["secret_ref"]]


def test_reordering_headers_never_moves_a_secret_to_another_entry(db):
    record, _ = _apply(db, _record(),
                       {"carrier": "config.headers", "operation": "add", "name": "X-A", "value": SEEDS["a"]},
                       {"carrier": "config.headers", "operation": "add", "name": "X-B", "value": SEEDS["b"]})
    record["config"]["headers"].reverse()  # a stored reorder, as any editor of the public part could make
    assert runtime_secrets.header_pairs(record["config"]["headers"], origin="http://engine.invalid:8080") == {"X-A": SEEDS["a"], "X-B": SEEDS["b"]}


def test_a_public_header_is_explicit_and_keeps_its_value_in_the_record(db):
    record, _ = _apply(db, _record(), {"carrier": "config.headers", "operation": "add", "name": "X-Region", "value": "eu", "public": True})
    assert _first_header(record)["value"] == "eu" and _first_header(record)["public"] is True
    assert runtime_secrets.header_pairs(record["config"]["headers"], origin=None) == {"X-Region": "eu"}


def test_the_environment_commands_follow_the_same_verbs(db):
    record, _ = _apply(db, _record(), {"carrier": "process.env", "operation": "add", "name": "API_TOKEN", "value": SEEDS["c"]})
    entry = record["process"]["env"]["API_TOKEN"]
    assert secret_refs.is_secret_entry(entry) and SEEDS["c"] not in json.dumps(record)
    assert runtime_secrets.env_mapping(record["process"]["env"]) == {"API_TOKEN": SEEDS["c"]}
    with pytest.raises(secret_commands.CommandError) as duplicate:
        _apply(db, record, {"carrier": "process.env", "operation": "add", "name": "API_TOKEN", "value": SEEDS["d"]})
    assert duplicate.value.code == "duplicate_name"
    replaced, _ = _apply(db, record, {"carrier": "process.env", "operation": "replace", "entry_id": entry["id"], "value": SEEDS["d"]})
    assert runtime_secrets.env_mapping(replaced["process"]["env"]) == {"API_TOKEN": SEEDS["d"]}
    cleared, outcome = _apply(db, record, {"carrier": "process.env", "operation": "clear", "entry_id": entry["id"]})
    assert cleared["process"]["env"] == {} and outcome.retired == [entry["secret_ref"]]
    with pytest.raises(secret_commands.CommandError):
        _apply(db, record, {"carrier": "process.env", "operation": "add", "name": "bad name", "value": SEEDS["e"]})


def test_a_command_list_is_set_whole_with_every_item_saying_what_it_is(db):
    record, _ = _apply(db, _record(), {"carrier": "process.start", "operation": "set", "items": [
        {"literal": "runner"}, {"literal": "--api-key"}, {"secret": SEEDS["a"]}, {"secret": SEEDS["b"], "prefix": "--token="}]})
    start = record["process"]["start"]
    assert start[:2] == ["runner", "--api-key"] and all(secret_refs.is_secret_entry(item) for item in start[2:])
    assert runtime_secrets.argv(start) == ["runner", "--api-key", SEEDS["a"], f"--token={SEEDS['b']}"]

    # Reordered and one dropped: every stored entry is retained by id or named in clear; identity follows the id.
    first, second = start[2]["id"], start[3]["id"]
    new, outcome = _apply(db, record, {"carrier": "process.start", "operation": "set", "items": [
        {"literal": "runner"}, {"retain": second}, {"literal": "--api-key"}, {"secret": SEEDS["c"]}], "clear": [first]})
    assert runtime_secrets.argv(new["process"]["start"]) == ["runner", f"--token={SEEDS['b']}", "--api-key", SEEDS["c"]]
    assert outcome.retired == [start[2]["secret_ref"]]


def test_an_entry_the_new_list_leaves_out_must_be_named_in_clear(db):
    record, _ = _apply(db, _record(), {"carrier": "process.start", "operation": "set", "items": [{"literal": "r"}, {"literal": "--token"}, {"secret": SEEDS["a"]}]})
    with pytest.raises(secret_commands.CommandError) as refused:
        _apply(db, record, {"carrier": "process.start", "operation": "set", "items": [{"literal": "r"}]})
    assert refused.value.code == "unmentioned_entry"


def test_a_structured_command_is_not_guessed_at_a_literal_is_public_as_declared(db):
    """Decision D29: where the input says what each item is, no flag name is read to decide otherwise.

    The writer who means a secret sends it as a ``secret`` item; one who sends a ``literal`` after ``--api-key`` has said it
    is public, and it is stored and shown as text. (A plain line or list written before D26 is read by the rule instead.)
    """
    record, outcome = _apply(db, _record(), {"carrier": "process.start", "operation": "set",
                                             "items": [{"literal": "r"}, {"literal": "--api-key"}, {"literal": SEEDS["a"]}]})
    assert record["process"]["start"] == ["r", "--api-key", SEEDS["a"]] and outcome.created == []
    stored, outcome = _apply(db, _record(), {"carrier": "process.start", "operation": "set",
                                             "items": [{"literal": "r"}, {"literal": "--api-key"}, {"secret": SEEDS["a"]}]})
    assert SEEDS["a"] not in json.dumps(stored) and len(outcome.created) == 1


def test_url_credentials_are_set_and_cleared_as_entries(db):
    record, _ = _apply(db, _record(), {"carrier": "config.address", "operation": "set_credentials",
                                       "url": f"http://u:{SEEDS['a']}@engine.invalid:8080/v1?token={SEEDS['b']}"})
    assert record["config"]["address"] == "http://engine.invalid:8080/v1" and SEEDS["a"] not in json.dumps(record)
    assert runtime_secrets.url(record["config"], "address") == f"http://u:{SEEDS['a']}@engine.invalid:8080/v1?token={SEEDS['b']}"
    cleared, outcome = _apply(db, record, {"carrier": "config.address", "operation": "clear_credentials"})
    assert "address_entries" not in cleared["config"] and len(outcome.retired) == 2


@pytest.mark.parametrize("command", [
    {"carrier": "config.nonsense", "operation": "add", "name": "X", "value": "v"},
    {"carrier": "config.headers", "operation": "frobnicate"},
    {"carrier": "config.headers", "operation": "add", "name": "X", "value": "v", "entry_id": "ent_" + "0" * 16},
    {"carrier": "config.headers", "operation": "replace", "value": "v"},
    {"carrier": "config.headers", "operation": "replace", "entry_id": "0", "value": "v"},
    {"carrier": "config.headers", "operation": "clear", "entry_id": "ent_" + "0" * 16, "value": "v"},
])
def test_a_command_with_an_unknown_carrier_operation_or_field_is_refused_whole(command):
    with pytest.raises(secret_commands.CommandError):
        secret_commands.parse_commands({"commands": [command]})


def test_two_commands_naming_one_entry_are_refused():
    entry = {"carrier": "config.headers", "operation": "clear", "entry_id": "ent_" + "0" * 16}
    with pytest.raises(secret_commands.CommandError) as refused:
        secret_commands.parse_commands({"commands": [entry, dict(entry, operation="retain")]})
    assert refused.value.code == "duplicate_entry"


# --- through the app ----------------------------------------------------------------------------


def _write(gateway, record=None):
    registry = service_registry.normalize_service_registry({"schema_version": 2, "services": {"engine-a": record or _record()}})
    current = admin_store.get_service_registry_with_generation(path=gateway.path)
    admin_store.write_service_registry_with_audit(
        registry, expected_generation=int(current["operator_settings_generation"]) if current else 0, updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)


def _etag(client):
    return client.get("/admin/api/operator/service-registry").headers["etag"]


def _post(client, commands, *, etag=None, service="engine-a"):
    return client.post(f"/admin/api/operator/services/{service}/secret-entries",
                       headers={**csrf_for(client), "If-Match": etag or _etag(client)}, json={"commands": commands})


def _secret_count(gateway):
    with sqlite3.connect(gateway.path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='provider_secrets'").fetchone():
            return 0
        return conn.execute("SELECT count(*) FROM provider_secrets WHERE id LIKE 'sref_%'").fetchone()[0]


ADD = {"carrier": "config.headers", "operation": "add", "name": "X-Vendor", "value": SEEDS["f"]}


def test_the_owner_adds_an_entry_and_the_answer_shows_it_configured_never_as_a_value(gateway):
    owner, *_ = owner_and_admin(gateway)
    _write(gateway)

    response = _post(owner, [ADD])

    assert response.status_code == 200, response.text
    assert SEEDS["f"] not in response.text and SEEDS["f"] not in owner.get("/admin/api/operator/service-registry").text
    assert _secret_count(gateway) == 1
    runtime_secrets.configure(gateway.path)
    try:
        stored = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["engine-a"]
        assert runtime_secrets.header_pairs(stored["config"]["headers"], origin="http://engine.invalid:8080") == {"X-Vendor": SEEDS["f"]}
    finally:
        runtime_secrets.configure(None)


def test_an_administrator_cannot_change_a_secret(gateway):
    _, _, _, admin = owner_and_admin(gateway)
    _write(gateway)

    response = _post(admin, [ADD])

    assert response.status_code == 403 and response.json()["error"]["code"] == "owner_required"
    assert _secret_count(gateway) == 0


def test_a_stale_revision_stores_nothing(gateway):
    owner, *_ = owner_and_admin(gateway)
    _write(gateway)
    etag = _etag(owner)
    assert _post(owner, [ADD], etag=etag).status_code == 200

    stale = _post(owner, [dict(ADD, name="X-Other", value=SEEDS["g"])], etag=etag)

    assert stale.status_code == 412
    assert _secret_count(gateway) == 1, "the refused call left a secret behind"


def test_a_refused_command_stores_nothing_even_when_an_earlier_command_in_the_call_would_have(gateway):
    owner, *_ = owner_and_admin(gateway)
    _write(gateway)

    response = _post(owner, [ADD, {"carrier": "config.headers", "operation": "replace", "entry_id": "ent_" + "0" * 16, "value": SEEDS["g"]}])

    assert response.status_code == 400 and response.json()["error"]["code"] == "unknown_entry"
    assert _secret_count(gateway) == 0
    stored = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["engine-a"]
    assert "headers" not in stored["config"]


def test_an_unknown_service_is_not_found(gateway):
    owner, *_ = owner_and_admin(gateway)
    _write(gateway)
    assert _post(owner, [ADD], service="nothing").status_code == 404


def test_the_answer_to_a_write_is_the_answer_to_a_read(gateway):
    owner, *_ = owner_and_admin(gateway)
    _write(gateway)
    written = _post(owner, [ADD])
    read = owner.get("/admin/api/operator/service-registry")
    assert written.json()["service_registry"]["services"]["engine-a"] == read.json()["service_registry"]["services"]["engine-a"]
    assert written.json()["excluded"] == read.json()["excluded"]


def test_protocol_provider_key_command_and_registry_echo_hide_private_reference(gateway):
    owner, *_ = owner_and_admin(gateway)
    saved = _record()
    saved['config'].update(host='127.0.0.1', address='http://127.0.0.1:8080', connection={
        'dispatch': 'protocol', 'protocol': 'openai-compatible', 'address': 'http://127.0.0.1:8080', 'transport': 'http',
    })
    _write(gateway, saved)
    response = _post(owner, [{'carrier': 'config.api_key', 'operation': 'add', 'value': SEEDS['h']}])
    assert response.status_code == 200, response.text
    raw = admin_store.get_service_registry_with_generation(path=gateway.path)['value']['services']['engine-a']
    private_ref = raw['config']['connection']['credentials']['inference']['secret_ref']
    read = owner.get('/admin/api/operator/service-registry')
    for answer in (response, read):
        assert private_ref not in answer.text and SEEDS['h'] not in answer.text
        shown = answer.json()['service_registry']['services']['engine-a']['config']['connection']
        assert shown['credentials']['inference']['secret_ref'] == {'configured': True}
    echoed = owner.put('/admin/api/operator/service-registry',
                       headers={**csrf_for(owner), 'If-Match': read.headers['etag']}, json=read.json()['service_registry'])
    assert echoed.status_code == 200, echoed.text
    retained = admin_store.get_service_registry_with_generation(path=gateway.path)['value']['services']['engine-a']
    assert retained['config']['connection']['credentials']['inference']['secret_ref'] == private_ref
