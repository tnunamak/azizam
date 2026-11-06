"""Cases from the independent review of the W1 round 3 projection (6c5235de), checked against decisions D26 to D29.

The review found seven problems in a projection that recognised secrets by name and shape. Each test below carries
one finding forward to the design that replaced it: a secret is an entry in the store, a record holds a reference,
a read shows "configured", and a write changes a secret only by command. The test names say which finding they carry.

- F1 (opaque header values, host-move guard): through every write route, then every read route and the host move.
- F2 (secret inside a container): a header row's extra fields are dropped; a credential-named config key is not a
  declared carrier (``test_f2_f3_undeclared_config_extras``).
- F3 (neutral strings): the same values in the declared carriers, and query tokens in the declared URL fields.
- F4 (restore bound by position, ids that disagree with keys): there is no restore; the replacement rules are tested.
- F5 (error envelopes and stored errors): submitted text and a failure that quotes a stored secret.
- F6 (the sweep did not prove success paths): the unclassified-route test and the discovery cases.
- F7 (the AST guard missed shapes): the gate refuses by response class; each shape the review named.
- The upstream error echo candidate: a stored URL credential and header secret echoed by an engine.
"""

from __future__ import annotations

import json
import re
import sqlite3

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse as JR  # an alias, as in the review's example
from fastapi.responses import Response
from fastapi.testclient import TestClient

from gateway import admin_store, egress, record_carriers, runtime_secrets, secret_refs, service_registry
from tests.support import canary
from tests.support.route_discovery import can_answer_with_a_record, discover
from tests.test_admin_secret_sweep import host_isolated, sweep_get_routes  # noqa: F401 - fixture
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401 - fixtures

M = {**canary.MARKERS, **canary.REVIEW_MARKERS}
REGISTRY = "/admin/api/operator/service-registry"
OPAQUE_VALUES = ("review-opaque", "review-basic", "review-jwt")


# --- helpers ---------------------------------------------------------------------------------------------


def _headers(client) -> dict:
    return {**csrf_for(client), "If-Match": client.get(REGISTRY).headers["etag"]}


def _empty_registry(gateway) -> None:
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry({"schema_version": 2, "services": {}}), expected_generation=0, updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)


def _service(service_id: str = "r3-engine", **config) -> dict:
    return {"id": service_id, "name": service_id, "kind": "engine", "enabled": True, "capabilities": ["chat"],
            "config": {"host": "r3.invalid", "port": 8080, "protocol": "openai-compatible", "address": "http://r3.invalid:8080/v1",
                       "models": [{"id": "m", "upstream_model": "m"}], **config}}


def _stored(gateway) -> dict:
    return admin_store.get_service_registry_with_generation(path=gateway.path)["value"]


def _secret_count(gateway) -> int:
    with sqlite3.connect(gateway.path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='provider_secrets'").fetchone():
            return 0
        return conn.execute("SELECT count(*) FROM provider_secrets WHERE id LIKE 'sref_%'").fetchone()[0]


def _plant_header(owner, gateway, route: str, value: str) -> str:
    """Write one header with ``value`` through ``route``; returns the id of the service that holds it."""
    _empty_registry(gateway)
    if route == "registry-put-row":
        registry = owner.get(REGISTRY).json()["service_registry"]
        registry["services"]["r3-engine"] = _service(headers=[{"name": "X-Vendor", "value": value}])
        done = owner.put(REGISTRY, headers=_headers(owner), json=registry)
    elif route == "registry-put-secret-flag":
        registry = owner.get(REGISTRY).json()["service_registry"]
        registry["services"]["r3-engine"] = _service(headers=[{"name": "X-Region", "value": value, "secret": True}])
        done = owner.put(REGISTRY, headers=_headers(owner), json=registry)
    elif route == "engine-form":
        done = owner.post("/admin/api/operator/services", headers=_headers(owner), json={
            "definition": "openai-compatible", "name": "r3 engine", "disabled": True, "values": {
                "address": "http://192.168.1.99:8080", "headers": [{"name": "X-Vendor", "value": value}]}})
    else:
        assert route == "command-add"
        made = owner.post("/admin/api/operator/services", headers=_headers(owner), json={"name": "r3 engine", "base_url": "http://r3.invalid:8080/v1"})
        assert made.status_code == 201, made.text
        done = owner.post("/admin/api/operator/services/r3-engine/secret-entries", headers=_headers(owner), json={"commands": [
            {"carrier": "config.headers", "operation": "add", "name": "X-Vendor", "value": value}]})
    assert done.status_code in (200, 201), (route, done.text[:300])
    return "r3-engine"


def _every_read_is_clean(gateway, clients, service_id: str) -> None:
    statuses, leaks, errored = sweep_get_routes(gateway, clients)
    assert not leaks, "\n".join(leaks)
    assert not errored, "\n".join(errored)
    for who, client in clients.items():
        view = client.get(f"/admin/api/engines/{service_id}")
        assert not canary.leaks_in(view.text), (who, canary.leaks_in(view.text))


# --- F1: an opaque header value under a neutral name -------------------------------------------------------


@pytest.mark.parametrize("marker", OPAQUE_VALUES)
@pytest.mark.parametrize("route", ["registry-put-row", "registry-put-secret-flag", "engine-form", "command-add"])
def test_f1_an_opaque_header_value_under_a_neutral_name_goes_to_the_store_and_is_never_shown(gateway, host_isolated, route, marker) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    value = M[marker]

    service_id = _plant_header(owner, gateway, route, value)

    assert value not in json.dumps(_stored(gateway)), "the record holds the value: it must hold a reference"
    assert value in canary.stored_values(gateway.path), "positive control: the value reached the store"
    record = _stored(gateway)["services"][service_id]
    rows = [row for row in record["config"]["headers"] if isinstance(row, dict)]
    assert rows and all(secret_refs.is_secret_entry(row) for row in rows)
    _every_read_is_clean(gateway, {"admin": admin, "owner": owner, "anonymous": gateway.client()}, service_id)
    shown = owner.get(f"/admin/api/engines/{service_id}").json()["engine"]
    assert [row["configured"] for row in shown["values"]["headers"]] == [True]


def test_f1_a_name_to_value_mapping_is_a_place_a_value_can_be_written_and_it_moves_to_the_store(gateway, host_isolated) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _empty_registry(gateway)
    registry = owner.get(REGISTRY).json()["service_registry"]
    registry["services"]["r3-engine"] = _service(headers={"X-Vendor": M["review-map"]})

    written = owner.put(REGISTRY, headers=_headers(owner), json=registry)

    assert written.status_code == 200, written.text
    assert M["review-map"] not in json.dumps(_stored(gateway))
    assert M["review-map"] in canary.stored_values(gateway.path)
    for client in (owner, admin, gateway.client()):
        assert M["review-map"] not in client.get(REGISTRY).text


def test_f1_a_public_header_is_a_row_the_writer_declared_public_and_a_value_is_never_public_by_its_name(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _empty_registry(gateway)
    registry = owner.get(REGISTRY).json()["service_registry"]
    registry["services"]["r3-engine"] = _service(headers=[
        {"name": "X-Region", "value": "eu", "public": True}, {"name": "X-Region-Two", "value": M["review-opaque"]}])

    assert owner.put(REGISTRY, headers=_headers(owner), json=registry).status_code == 200

    read = owner.get(REGISTRY).json()["service_registry"]["services"]["r3-engine"]["config"]["headers"]
    assert read[0] == {"name": "X-Region", "value": "eu", "public": True}
    assert "value" not in read[1] and read[1]["configured"] is True


def _moved_host(registry: dict) -> dict:
    config = registry["services"]["r3-engine"]["config"]
    for key in ("address", "base_url", "url"):
        if key in config:
            config[key] = "http://192.168.1.98:9/v1"
    config["host"] = "192.168.1.98"
    return registry


def test_f1_an_administrator_cannot_move_the_host_of_an_engine_whose_only_credential_is_a_neutral_header(gateway, host_isolated) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _plant_header(owner, gateway, "engine-form", M["review-opaque"])
    before = _stored(gateway)
    echo = _moved_host(admin.get(REGISTRY).json()["service_registry"])

    refused = admin.put(REGISTRY, headers=_headers(admin), json=echo)

    assert refused.status_code == 403 and refused.json()["error"]["code"] == "owner_required", refused.text[:300]
    assert _stored(gateway) == before
    patched = admin.patch("/admin/api/operator/services/r3-engine", headers=_headers(admin),
                          json={"definition": "openai-compatible", "disabled": True, "values": {"address": "http://192.168.1.98:9"}})
    assert patched.status_code == 403, patched.text[:300]
    assert _stored(gateway) == before
    assert owner.put(REGISTRY, headers=_headers(owner), json=_moved_host(owner.get(REGISTRY).json()["service_registry"])).status_code == 200


def test_f1_the_host_move_guard_counts_a_header_entry_and_not_a_header_declared_public() -> None:
    entry = secret_refs.entry(secret_refs.new_ref(), name="X-Vendor")
    held = _service(headers=[entry])
    moved = _service(headers=[entry], address="http://moved.invalid:9/v1")
    assert "host" in service_registry.credential_binding_changes({"services": {"r3-engine": held}}, {"services": {"r3-engine": moved}})["r3-engine"]

    public = _service(headers=[{"name": "X-Region", "value": "eu", "public": True}])
    public_moved = _service(headers=[{"name": "X-Region", "value": "eu", "public": True}], address="http://moved.invalid:9/v1")
    assert service_registry.credential_binding_changes({"services": {"r3-engine": public}}, {"services": {"r3-engine": public_moved}}) == {}


def test_an_administrator_adds_no_secret_by_inline_input_and_no_secret_is_even_stored_for_the_refusal(gateway, monkeypatch) -> None:
    """A refused write must not put secret material in the store, even for a moment (the owner-only rule runs first)."""
    owner, _, _, admin = owner_and_admin(gateway)
    _empty_registry(gateway)
    owner.put(REGISTRY, headers=_headers(owner), json={**owner.get(REGISTRY).json()["service_registry"],
                                                       "services": {"r3-engine": _service()}})
    stores: list[str] = []
    real = secret_refs.store
    monkeypatch.setattr(secret_refs, "store", lambda *args, **kwargs: (stores.append("stored"), real(*args, **kwargs))[1])
    echo = admin.get(REGISTRY).json()["service_registry"]
    echo["services"]["r3-engine"]["config"]["headers"] = [{"name": "X-Vendor", "value": M["review-opaque"]}]

    refused = admin.put(REGISTRY, headers=_headers(admin), json=echo)

    assert refused.status_code == 403 and refused.json()["error"]["code"] == "owner_required"
    assert stores == [] and _secret_count(gateway) == 0


@pytest.mark.parametrize("operation", ["clear", "rename", "replace"])
def test_an_administrator_cannot_clear_rename_or_replace_an_entry_the_owner_set(gateway, operation) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _empty_registry(gateway)
    owner.put(REGISTRY, headers=_headers(owner), json={**owner.get(REGISTRY).json()["service_registry"], "services": {"r3-engine": _service()}})
    added = owner.post("/admin/api/operator/services/r3-engine/secret-entries", headers=_headers(owner), json={"commands": [
        {"carrier": "config.headers", "operation": "add", "name": "X-Vendor", "value": M["review-opaque"]}]})
    assert added.status_code == 200, added.text
    entry_id = owner.get(REGISTRY).json()["service_registry"]["services"]["r3-engine"]["config"]["headers"][0]["id"]
    command = {"clear": {"operation": "clear"}, "rename": {"operation": "rename", "name": "X-Other"},
               "replace": {"operation": "replace", "value": M["review-basic"]}}[operation]
    before, count = _stored(gateway), _secret_count(gateway)

    refused = admin.post("/admin/api/operator/services/r3-engine/secret-entries", headers=_headers(admin), json={"commands": [
        {"carrier": "config.headers", "entry_id": entry_id, **command}]})

    assert refused.status_code == 403 and refused.json()["error"]["code"] == "owner_required", refused.text[:300]
    assert _stored(gateway) == before and _secret_count(gateway) == count
    assert M["review-opaque"] in canary.stored_values(gateway.path)


# --- F2 and F3: containers and neutral strings ----------------------------------------------------------------


def test_f2_the_extra_fields_of_a_header_row_are_dropped_not_stored_and_not_shown(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _empty_registry(gateway)
    registry = owner.get(REGISTRY).json()["service_registry"]
    registry["services"]["r3-engine"] = _service(headers=[
        {"name": "X-Team", "value": "blue", "public": True, "metadata": {"password": M["review-flag"]}}])

    assert owner.put(REGISTRY, headers=_headers(owner), json=registry).status_code == 200

    assert M["review-flag"] not in json.dumps(_stored(gateway)) and M["review-flag"] not in owner.get(REGISTRY).text


@pytest.mark.parametrize("carrier, record", [
    ("env value", lambda v: {"process": {"driver": "process", "start": ["run"], "env": {"VENDOR_BLOB": v}}}),
    ("argv after a flag", lambda v: {"process": {"driver": "process", "start": ["run", "--vendor-token", v]}}),
    ("url user info", lambda v: {"config": {"address": f"http://svc:{v}@r3.invalid:8080/v1"}}),
])
@pytest.mark.parametrize("marker", ["review-basic", "review-jwt", "review-opaque"])
def test_f3_a_neutral_looking_value_in_a_declared_carrier_is_moved_whatever_it_looks_like(gateway, carrier, record, marker) -> None:
    value = M[marker]
    moved, paths = record_carriers.externalize_service(
        {**_service(), **record(value)} if "config" not in record(value) else _service(**record(value)["config"]), secret_refs.putter(gateway.path))

    assert paths, f"{carrier}: nothing was moved"
    assert value not in json.dumps(moved)


@pytest.mark.parametrize("field, value", [
    ("process.health_endpoint", lambda v: f"/ready?signature={v}"),
    ("process.health_endpoint", lambda v: f"ready?X-Amz-Signature={v}"),
    ("observations", lambda v: f"/ready?signature={v}"),
    ("context_window_probe", lambda v: f"http://r3.invalid:8080/props?X-Amz-Signature={v}"),
])
def test_f3_a_query_token_in_a_declared_path_or_url_field_moves_to_the_store(gateway, field, value) -> None:
    secret = M["review-signature"]
    record = _service()
    if field == "process.health_endpoint":
        record["process"] = {"driver": "process", "start": ["run"], "health_endpoint": value(secret)}
    elif field == "observations":
        record["observations"] = {"ready": {"http": {"path": value(secret), "expect": 200}}}
    else:
        record["config"]["context_window_probe"] = {"endpoint": value(secret)}

    moved, paths = record_carriers.externalize_service(record, secret_refs.putter(gateway.path))

    assert paths and secret not in json.dumps(moved), (field, moved)


def test_f2_f3_undeclared_config_extras_are_kept_and_never_shown_and_the_contract_says_so(gateway) -> None:
    """The review's F2 and F3 (a key the record type does not declare as a carrier), settled by decision D34.

    Round 1 classified a ``config`` key outside the declared carriers as public JSON; the round 2 review showed that a
    secret written there (a token under a neutral name, a mapping under ``password``) was then shown. D34: a key no type
    classifies is stored unchanged and omitted from every output, with only a count; a write built from a read keeps it.
    """
    owner, *_ = owner_and_admin(gateway)
    _empty_registry(gateway)
    registry = owner.get(REGISTRY).json()["service_registry"]
    registry["services"]["r3-engine"] = _service(vendor_blob="Basic dTpw", credentials=["listed"], password={"primary": "mapped"})

    assert owner.put(REGISTRY, headers=_headers(owner), json=registry).status_code == 200

    read = owner.get(REGISTRY)
    shown = read.json()["service_registry"]["services"]["r3-engine"]["config"]
    assert not {"vendor_blob", "credentials", "password"} & set(shown) and shown["hidden_settings"] == 3
    assert "dTpw" not in read.text and "listed" not in read.text and "mapped" not in read.text
    again = owner.put(REGISTRY, headers={**_headers(owner), "If-Match": read.headers["etag"]}, json=read.json()["service_registry"])
    assert again.status_code == 200, again.text
    stored = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["r3-engine"]["config"]
    assert stored["vendor_blob"] == "Basic dTpw" and stored["credentials"] == ["listed"] and stored["password"] == {"primary": "mapped"}
    contract = (canary_root() / "docs" / "design" / "ENGINE_CONTRACT.md").read_text()
    assert re.search(r"config extras.{0,600}never shown", contract, re.S | re.I), "ENGINE_CONTRACT.md must say config extras are never shown"


def canary_root():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


# --- F4: restore by position, ids that disagree with keys --------------------------------------------------------
# There is no restore: a "[redacted]" marker, an empty string, null or a position never keeps a secret (D27).


def test_f4_a_position_never_keeps_a_secret_so_a_reorder_cannot_cross_two_credentials(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _empty_registry(gateway)
    owner.put(REGISTRY, headers=_headers(owner), json={**owner.get(REGISTRY).json()["service_registry"], "services": {"r3-engine": _service()}})
    owner.post("/admin/api/operator/services/r3-engine/secret-entries", headers=_headers(owner), json={"commands": [
        {"carrier": "config.headers", "operation": "add", "name": "X-East", "value": "synthetic-east-r3"},
        {"carrier": "config.headers", "operation": "add", "name": "X-West", "value": "synthetic-west-r3"}]})
    read = owner.get(REGISTRY).json()["service_registry"]
    rows = read["services"]["r3-engine"]["config"]["headers"]
    stored_before = _stored(gateway)
    read["services"]["r3-engine"]["config"]["headers"] = [rows[1], rows[0]]  # the two entries, swapped

    reordered = owner.put(REGISTRY, headers=_headers(owner), json=read)

    assert reordered.status_code == 400 and "holds secret entries" in reordered.json()["error"]["message"], reordered.text[:300]
    assert _stored(gateway) == stored_before
    runtime_secrets.configure(gateway.path)
    try:
        pairs = runtime_secrets.header_pairs(_stored(gateway)["services"]["r3-engine"]["config"]["headers"], origin="http://r3.invalid:8080")
    finally:
        runtime_secrets.configure(None)
    assert pairs == {"X-East": "synthetic-east-r3", "X-West": "synthetic-west-r3"}


def test_f4_a_secret_follows_its_entry_id_when_a_command_reorders_a_list(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _empty_registry(gateway)
    owner.put(REGISTRY, headers=_headers(owner), json={**owner.get(REGISTRY).json()["service_registry"], "services": {"r3-engine": _service(**{})}})
    registry = owner.get(REGISTRY).json()["service_registry"]
    registry["services"]["r3-engine"]["process"] = {
        "driver": "process", "port": {"fixed": 8080},
        "start": ["runner", "--token", "synthetic-east-r3", "--api-key=synthetic-west-r3"]}
    assert owner.put(REGISTRY, headers=_headers(owner), json=registry).status_code == 200
    start = owner.get(REGISTRY).json()["service_registry"]["services"]["r3-engine"]["process"]["start"]
    east, west = [item for item in start if isinstance(item, dict)]

    swapped = owner.post("/admin/api/operator/services/r3-engine/secret-entries", headers=_headers(owner), json={"commands": [
        {"carrier": "process.start", "operation": "set", "items": [
            {"literal": "runner"}, {"retain": west["id"]}, {"literal": "--token"}, {"retain": east["id"]}]}]})

    assert swapped.status_code == 200, swapped.text
    runtime_secrets.configure(gateway.path)
    try:
        argv = runtime_secrets.argv(_stored(gateway)["services"]["r3-engine"]["process"]["start"])
    finally:
        runtime_secrets.configure(None)
    assert argv == ["runner", "--api-key=synthetic-west-r3", "--token", "synthetic-east-r3"]


def test_f4_a_registry_whose_record_ids_disagree_with_their_keys_cannot_move_a_secret_between_services(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _empty_registry(gateway)
    owner.put(REGISTRY, headers=_headers(owner), json={**owner.get(REGISTRY).json()["service_registry"],
                                                       "services": {"svc-a": _service("svc-a"), "svc-b": _service("svc-b")}})
    for service, secret in (("svc-a", "synthetic-a-r3"), ("svc-b", "synthetic-b-r3")):
        done = owner.post(f"/admin/api/operator/services/{service}/secret-entries", headers=_headers(owner), json={"commands": [
            {"carrier": "config.headers", "operation": "add", "name": "X-Vendor", "value": secret}]})
        assert done.status_code == 200, done.text
    before = _stored(gateway)
    swapped = owner.get(REGISTRY).json()["service_registry"]
    swapped["services"]["svc-a"]["id"], swapped["services"]["svc-b"]["id"] = "svc-b", "svc-a"

    answer = owner.put(REGISTRY, headers=_headers(owner), json=swapped)

    after = _stored(gateway)
    runtime_secrets.configure(gateway.path)
    try:
        held = {sid: runtime_secrets.header_pairs(record["config"]["headers"], origin="http://r3.invalid:8080")
                for sid, record in after["services"].items() if sid in ("svc-a", "svc-b")}
    finally:
        runtime_secrets.configure(None)
    assert held == {"svc-a": {"X-Vendor": "synthetic-a-r3"}, "svc-b": {"X-Vendor": "synthetic-b-r3"}}, (answer.status_code, answer.text[:200])
    assert answer.status_code >= 400 or after == before


def test_f4_two_records_that_end_up_with_one_id_are_refused_not_merged(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _empty_registry(gateway)
    registry = owner.get(REGISTRY).json()["service_registry"]
    registry["services"] = {"svc-a": _service("svc-a"), "svc-b": _service("svc-a")}  # the second record claims the first's id
    before = _stored(gateway)

    answer = owner.put(REGISTRY, headers=_headers(owner), json=registry)

    assert answer.status_code == 400, answer.text[:300]
    assert _stored(gateway) == before


# --- F5: error envelopes -----------------------------------------------------------------------------------------


def test_f5_a_validation_error_never_repeats_the_text_that_was_submitted(gateway) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _empty_registry(gateway)
    for client in (owner, admin):
        answer = client.post("/admin/api/engines/access", headers=csrf_for(client), json={"capabilities": [M["review-submitted"]], "models": []})
        assert answer.status_code >= 400, answer.text[:200]
        assert M["review-submitted"] not in answer.text + str(dict(answer.headers))
        again = client.put(REGISTRY, headers=_headers(client), json={"services": {"x": {"id": "x", "config": M["review-submitted"]}}})
        assert again.status_code >= 400 and M["review-submitted"] not in again.text


def test_f5_a_reload_failure_that_quotes_a_stored_secret_is_reported_by_its_class_only(gateway, monkeypatch) -> None:
    owner, *_ = owner_and_admin(gateway)
    _plant_header(owner, gateway, "command-add", M["review-opaque"])
    registry = owner.get(REGISTRY).json()["service_registry"]

    def broken(*args, **kwargs):
        raise RuntimeError(f"cannot configure header {M['review-opaque']} for http://u:{M['review-stored-short']}@r3.invalid")

    monkeypatch.setattr(service_registry, "ensure_service_registry", broken)
    answer = owner.put(REGISTRY, headers=_headers(owner), json=registry)

    assert answer.status_code == 500, answer.text[:300]
    assert answer.json()["error"]["code"] == "registry_saved_not_applied" and "RuntimeError" in answer.json()["error"]["message"]
    assert not canary.leaks_in(answer.text), canary.leaks_in(answer.text)
    assert not canary.leaks_in(json.dumps(gateway.rows("SELECT * FROM audit_events"), default=str))


# --- F6: the sweep did not prove success paths --------------------------------------------------------------------


def test_f6_every_mutating_route_that_can_answer_with_a_record_has_a_success_fixture() -> None:
    import proxy
    from tests import test_admin_secret_sweep_writes as writes

    routes = [item for item in discover(proxy.app) if item.path.startswith(("/admin/api", "/me/api"))]
    answering = {(method, item.path) for item in routes if can_answer_with_a_record(item) for method in item.methods if method in writes.MUTATING}
    succeeding = {key for key, calls in writes.BUILT.items() if any(must_succeed for _, must_succeed, _ in calls)}

    assert len(answering) >= 14, "the scan found no record routes: it no longer reads the handlers"
    assert not sorted(answering - succeeding), f"a route that can answer with a record has no call the owner must succeed at: {sorted(answering - succeeding)}"


def test_f6_the_scan_finds_a_new_route_that_answers_with_a_record_through_a_helper() -> None:
    app = FastAPI()
    router = APIRouter(prefix="/admin/api", route_class=egress.ManagementRoute)

    def helper():
        return egress.projected_response({"service_registry": {"schema_version": 2, "services": {}}})

    @router.post("/example")
    def example(body: dict):
        return helper()

    @router.post("/plain")
    def plain(body: dict):
        return egress.plain_json_response({"ok": True}, response_class="acknowledgement")

    router.add_api_route("/head-only", lambda: egress.plain_json_response({"ok": True}, response_class="acknowledgement"), methods=["HEAD"])
    app.include_router(router)

    found = {item.path: item for item in discover(app)}

    assert can_answer_with_a_record(found["/admin/api/example"]) and not can_answer_with_a_record(found["/admin/api/plain"])
    assert found["/admin/api/head-only"].methods == ("HEAD",)


def test_f6_every_management_route_uses_a_method_the_sweeps_select(gateway) -> None:
    unswept = [f"{item.methods} {item.path}" for item in discover(gateway.proxy.app)
               if item.management and not set(item.methods) & {"GET", "POST", "PUT", "PATCH", "DELETE"}]
    assert not unswept, f"routes no sweep selects (a HEAD-only or OPTIONS-only route): {unswept}"


# --- F7: the structural boundary, with each shape the review named -------------------------------------------------

SEED = "SEED-r3-gate-canary-7743"
RECORD = {"id": "engine-a", "config": {"headers": [{"name": "X-Vendor", "value": SEED}]}}


def _bypass_app() -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/admin/api", route_class=egress.ManagementRoute)

    @router.get("/variable-payload")
    def variable_payload():
        payload = {"engine": RECORD}
        return JR(payload)

    @router.get("/nested-result")
    def nested_result():
        return JR({"result": {"engine": RECORD}})

    @router.get("/neutral-key")
    def neutral_key():
        return JR({"item": RECORD})

    @router.get("/plain-dict")
    def plain_dict():
        return {"engine": RECORD}

    @router.get("/response-with-json")
    def response_with_json():
        return Response(content=json.dumps({"engine": RECORD}), media_type="application/json")

    @router.get("/plain-constructor-with-a-record-key")
    def plain_with_record():
        return egress.plain_json_response({"engine": RECORD, "service_registry": RECORD}, response_class="acknowledgement")

    app.include_router(router)
    app.add_middleware(egress.EgressGate)
    return app


@pytest.mark.parametrize("path", ["variable-payload", "nested-result", "neutral-key", "plain-dict", "response-with-json",
                                  "plain-constructor-with-a-record-key"])
def test_f7_a_record_in_any_shape_the_review_named_is_refused_by_the_response_class_not_by_its_syntax(path) -> None:
    response = TestClient(_bypass_app(), raise_server_exceptions=False).get(f"/admin/api/{path}")

    assert response.status_code == 500 and SEED not in response.text
    assert response.json()["error"]["code"] in {"egress_blocked", "internal_error"}


def test_f7_a_management_router_in_a_nested_package_without_the_route_class_stops_startup() -> None:
    app = FastAPI()
    nested = APIRouter(prefix="/admin/api/nested/deeper")  # a plain router: the review's "nested gateway package" case

    @nested.get("/record")
    def record():
        return JR({"engine": RECORD})

    app.include_router(nested)

    with pytest.raises(Exception, match="/admin/api/nested/deeper/record"):
        egress.verify_and_freeze(app)


def test_f5_the_model_detection_route_names_an_unexpected_failure_by_its_class_only() -> None:
    from tests.test_admin_api import _auth, _make_client

    async def probe(host, port, path):
        raise RuntimeError(f"cannot reach http://u:{M['review-stored-short']}@{host} with X-Vendor {M['review-opaque']}")

    client, _ = _make_client(service_probe=probe)
    answer = client.post("/admin/api/operator/services/detect-models", headers=_auth("admin"), json={"base_url": "http://engine.invalid:1/v1"})

    assert answer.status_code == 200, answer.text
    assert "RuntimeError" in answer.json()["detected"]["error"] and not canary.leaks_in(answer.text), answer.text
