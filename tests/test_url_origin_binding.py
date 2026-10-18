"""Decision D39: the credentials stored with a URL go only to the origin they were stored for.

The review of phase1-r3 (053d16b7) reproduced this: an owner stores a health endpoint ``/ready?token=CANARY``; an
administrator changes only the public endpoint to ``//attacker.invalid/collect``; the owner-only rule saw no change to a
credential binding and the resolver put the token back, so the catalog probe called ``http://attacker.invalid/collect?token=CANARY``.

Four layers are tested here, each on its own so that one failing does not hide another:

* ``url_binding``: what origin a field is bound to (case, default port, IPv6, percent-encoded host, user-info tricks);
* the owner-only rule: an administrator's change of such a field is refused (403) for PUT and PATCH, field by field;
* the runtime: a record written past that rule (by hand) is not put back for an address with another origin, in every reader;
* the schema 13 migration, the log scrub and the two smaller findings (host user info, structured environment values).
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

import proxy
from gateway import (
    admin_store, decision_routing, engine_observations, lifecycle_drivers, record_carriers, runtime_secrets, secret_refs,
    secret_store, service_registry, url_binding,
)
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401
from tests.test_admin_secret_sweep import host_isolated  # noqa: F401
from tests.test_secret_canary_matrix import REGISTRY, _etag
from tests.test_secret_review_r3 import (  # noqa: F401
    change, empty_registry, error_code, secret_rows, stored, write_raw,
)
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

CANARY = "D39-CANARY-VALUE"
GOOD = "http://good.invalid:9"
SERVICE = "/admin/api/operator/services/d39"


@pytest.fixture
def store(tmp_path):
    """A real secret store in a temporary database, which the runtime resolves from."""
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    runtime_secrets.configure(path)
    yield path
    runtime_secrets.configure(None)


@pytest.fixture
def wire(monkeypatch):
    """Every request the gateway's HTTP client sends, recorded and answered 200 (no request reaches a network)."""
    sent: list[httpx.Request] = []

    async def handle(self, request):
        sent.append(request)
        return httpx.Response(200, json={"status": "finished"}, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return sent


def _leaks(sent) -> list[str]:
    """The recorded requests that carry a canary, or go to a host other than the good one."""
    return [str(request.url) for request in sent if CANARY in str(request.url) or request.url.host != "good.invalid"]


# ---------------------------------------------------------------------------
# url_binding: which origin a field is bound to
# ---------------------------------------------------------------------------

ORIGINS = [
    ("http://good.invalid:9/x", "http://good.invalid:9"),
    ("HTTP://GOOD.INVALID:9/x", "http://good.invalid:9"),
    ("http://good.invalid/x", "http://good.invalid:80"),
    ("http://good.invalid:80/x", "http://good.invalid:80"),
    ("https://good.invalid/x", "https://good.invalid:443"),
    ("https://good.invalid:443/x?a=b#f", "https://good.invalid:443"),
    ("http://u:p@good.invalid:9/x", "http://good.invalid:9"),
    ("http://[::1]:9/x", "http://[::1]:9"),
    ("http://[::FFFF:1.2.3.4]:9/x", "http://[::ffff:1.2.3.4]:9"),
    ("http://good.invalid.:9/x", "http://good.invalid.:9"),
    # what a client could read as another host
    ("http://good.invalid:9@attacker.invalid/x", "http://attacker.invalid:80"),
    ("http://attacker.invalid\\@good.invalid:9/x", None),
    ("http://good%2einvalid:9/x", None),
    ("http://good.invalid%00.attacker.invalid/x", None),
    ("http://good.invalid:99999/x", None),
    ("http://good.invalid:notaport/x", None),
    ("http://[::1/x", None),
    ("http://[zz::1]:9/x", None),
    ("http:///x", None),
    ("http://good .invalid/x", None),
    ("http://good.invalid\t/x", None),
    ("http://gooɡ.invalid/x", None),
    ("ftp://good.invalid/x", None),
    ("unix:///tmp/s.sock", None),
    ("file:///etc/passwd", None),
    ("//good.invalid/x", None),
    ("/x", None),
    ("", None),
]


@pytest.mark.parametrize("url,expected", ORIGINS, ids=[repr(url)[:50] for url, _ in ORIGINS])
def test_the_origin_of_an_absolute_url_is_the_one_a_client_would_dial(url, expected):
    assert url_binding.strict_origin(url) == expected


FIELD_ORIGINS = [
    ("/ready", GOOD, "http://good.invalid:9"),
    ("/ready", "HTTP://GOOD.INVALID:9/v1", "http://good.invalid:9"),
    ("/ready", "http://good.invalid/v1", "http://good.invalid:80"),
    ("/%2f/attacker.invalid", GOOD, "http://good.invalid:9"),
    ("http://other.invalid/ready", GOOD, "http://other.invalid:80"),
    ("HTTP://OTHER.INVALID:80/ready", GOOD, "http://other.invalid:80"),
    # a field that can hold no credentials
    ("//attacker.invalid/collect", GOOD, None),
    ("\\\\attacker.invalid\\collect", GOOD, None),
    ("@attacker.invalid/collect", GOOD, None),
    ("ready", GOOD, None),
    ("?token=a", GOOD, None),
    ("/ready", "", None),
    ("/ready", "not a url", None),
    ("/ready", "//good.invalid/v1", None),
    ("ftp://good.invalid/ready", GOOD, None),
    ("http://good%2einvalid:9/ready", GOOD, None),
]


@pytest.mark.parametrize("public,base,expected", FIELD_ORIGINS, ids=[f"{p!r} on {b!r}"[:60] for p, b, _ in FIELD_ORIGINS])
def test_a_field_is_bound_to_its_own_origin_or_to_the_base_it_is_joined_to(public, base, expected):
    assert url_binding.field_origin(public, base) == expected


# ---------------------------------------------------------------------------
# The records: one field of every kind that can hold credentials
# ---------------------------------------------------------------------------

def _record(**config):
    return {
        "id": "d39", "name": "d39", "kind": "engine", "enabled": True, "capabilities": ["chat"],
        "config": {
            "host": "good.invalid", "port": 9, "protocol": "openai-compatible", "models": [{"id": "m", "upstream_model": "m"}],
            "health_endpoint": f"/ready?token={CANARY}-health",
            "context_window_probe": {"endpoint": f"/props?token={CANARY}-context"},
            **config},
        "lifecycle": {"type": "http", "driver": "http-load-unload",
                      "driver_config": {"load_url": f"/v1/model/load?token={CANARY}-load",
                                        "unload_url": f"/v1/model/unload?token={CANARY}-unload",
                                        "verify_endpoint": f"/v1/model?sig={CANARY}-verify",
                                        "health_endpoint": f"/health?token={CANARY}-driver-health"}},
    }


# (label, path into the record). Every field a record can hold credentials in and an administrator can reach.
FIELDS = {
    "health_endpoint": ("config", "health_endpoint"),
    "context_window_probe.endpoint": ("config", "context_window_probe", "endpoint"),
    # a record keeps its lifecycle driver in ``process.legacy_lifecycle`` (the form path also writes ``config.lifecycle``)
    "load_url": ("process", "legacy_lifecycle", "driver_config", "load_url"),
    "unload_url": ("process", "legacy_lifecycle", "driver_config", "unload_url"),
    "verify_endpoint": ("process", "legacy_lifecycle", "driver_config", "verify_endpoint"),
    "driver health_endpoint": ("process", "legacy_lifecycle", "driver_config", "health_endpoint"),
    "probe path": ("observations", "process_ready", "http", "path"),
}
BASE_FIELDS = {
    "base_url": ("config", "base_url"),
    "api_base": ("config", "api_base"),
    "default_base_url": ("config", "default_base_url"),
}

# What an administrator might write in place of a field that holds credentials, and why it must not be allowed.
ATTACKS = {
    "protocol-relative": "//attacker.invalid/collect",
    "absolute": "http://attacker.invalid/collect",
    "absolute, upper case": "HTTP://ATTACKER.INVALID/collect",
    "absolute, https": "https://attacker.invalid/collect",
    "absolute, same host and port 80": "http://good.invalid:80/collect",
    "absolute, same host and default port": "http://good.invalid/collect",
    "absolute, same host and https": "https://good.invalid:9/collect",
    "absolute, user info trick": "http://good.invalid:9@attacker.invalid/collect",
    "absolute, ipv6": "http://[::1]:9/collect",
    "absolute, percent-encoded host": "http://good%2einvalid:9/collect",
    "absolute, backslash": "http://attacker.invalid\\@good.invalid:9/collect",
    "no leading slash": "@attacker.invalid/collect",
    "bare host": "attacker.invalid/collect",
    "empty": "",
}


# Written like these, the attack carries credentials of its own (user info) or no readable address at all: the write is
# refused as invalid (400) before the owner-only rule is asked. Every other attack is refused as owner-only (403).
UNWRITABLE = {"absolute, backslash", "no leading slash", "bare host", "absolute, user info trick"}


def _refused(response, attack) -> bool:
    if response.status_code == 403 and error_code(response) == "owner_required":
        return True
    return attack in UNWRITABLE and response.status_code == 400


def _get(node, path):
    for part in path:
        node = node[part]
    return node


def _set(node, path, value):
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def _owner_plants(owner, record):
    read = owner.get(REGISTRY)
    document = read.json()["service_registry"]
    document["services"]["d39"] = record
    return owner.put(REGISTRY, headers={**csrf_for(owner), "If-Match": read.headers["etag"]}, json=document)


def _plant(gateway, owner, **config):
    empty_registry(gateway)
    planted = _owner_plants(owner, _record(**config))
    assert planted.status_code == 200, planted.text
    held = stored(gateway)["d39"]
    assert CANARY not in json.dumps(held)
    return held


def _put_field(client, path, value):
    return change(client, lambda services: _set(services["d39"], path, value))


def _patch(client, body):
    return client.patch(SERVICE, headers=_etag(client), json=body)


# ---------------------------------------------------------------------------
# The owner-only rule: PUT, field by field
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("attack", sorted(ATTACKS))
@pytest.mark.parametrize("field", sorted(FIELDS))
def test_an_administrator_cannot_re_aim_a_field_that_holds_credentials_by_a_registry_put(gateway, host_isolated, field, attack):
    """The reviewer's attack, for every field that can hold credentials: only the public address changes."""
    owner, _, _, admin = owner_and_admin(gateway)
    before = copy.deepcopy(_plant(gateway, owner))
    refused = _put_field(admin, FIELDS[field], ATTACKS[attack])
    assert _refused(refused, attack), (refused.status_code, refused.text[:300])
    assert stored(gateway)["d39"] == before


@pytest.mark.parametrize("attack", sorted(ATTACKS))
@pytest.mark.parametrize("field", sorted(BASE_FIELDS))
def test_an_administrator_cannot_re_aim_a_base_address_that_holds_credentials(gateway, host_isolated, field, attack):
    owner, _, _, admin = owner_and_admin(gateway)
    held = _plant(gateway, owner, **{BASE_FIELDS[field][1]: f"{GOOD}/v1?key={CANARY}-base"})
    before = copy.deepcopy(held)
    assert held["config"][BASE_FIELDS[field][1] + "_entries"]
    refused = _put_field(admin, BASE_FIELDS[field], ATTACKS[attack].replace("/collect", "/v1") if ATTACKS[attack] else "")
    assert _refused(refused, attack), (refused.status_code, refused.text[:300])
    assert stored(gateway)["d39"] == before


@pytest.mark.parametrize("move", ["host", "port", "scheme", "base_url", "api_base", "default_base_url"])
def test_an_administrator_cannot_move_the_base_a_path_with_credentials_is_joined_to(gateway, host_isolated, move):
    owner, _, _, admin = owner_and_admin(gateway)
    before = copy.deepcopy(_plant(gateway, owner))
    moves = {"host": "attacker.invalid", "port": 4444, "scheme": "https", "base_url": "http://attacker.invalid:4444/v1",
             "api_base": "http://attacker.invalid:4444/v1", "default_base_url": "http://attacker.invalid:4444/v1"}
    refused = change(admin, lambda services: services["d39"]["config"].update({move: moves[move]}))
    assert refused.status_code == 403 and error_code(refused) == "owner_required", (refused.status_code, refused.text[:300])
    assert stored(gateway)["d39"] == before


@pytest.mark.parametrize("field", ["health_endpoint", "context_window_probe.endpoint"])
def test_an_administrator_can_still_edit_the_path_of_a_field_that_holds_credentials_on_the_same_origin(gateway, host_isolated, field):
    owner, _, _, admin = owner_and_admin(gateway)
    _plant(gateway, owner)
    done = _put_field(admin, FIELDS[field], "/elsewhere")
    assert done.status_code == 200, done.text
    assert _get(stored(gateway)["d39"], FIELDS[field]) == "/elsewhere"


def test_an_administrator_can_still_edit_a_record_that_holds_credentials_without_touching_them(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    _plant(gateway, owner)
    done = change(admin, lambda services: services["d39"]["config"].update(note="a note"))
    assert done.status_code == 200, done.text


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_the_owner_can_move_a_field_that_holds_credentials(gateway, host_isolated, field):
    """The owner may. The credentials stay bound to the address they were stored for, so the moved field sends none (the owner sets them again)."""
    owner, _, _, _ = owner_and_admin(gateway)
    _plant(gateway, owner)
    done = _put_field(owner, FIELDS[field], "http://elsewhere.invalid:9/ready")
    assert done.status_code == 200, done.text


# ---------------------------------------------------------------------------
# The owner-only rule: PATCH
# ---------------------------------------------------------------------------

PATCH_BODIES = {
    "health_endpoint": lambda value: {"health_endpoint": value},
    "context_window_probe.endpoint": lambda value: {"context_window_probe": {"endpoint": value}},
    "load_url": lambda value: {"lifecycle": {"type": "http", "driver": "http-load-unload", "driver_config": {"load_url": value}}},
    "unload_url": lambda value: {"lifecycle": {"type": "http", "driver": "http-load-unload", "driver_config": {"unload_url": value}}},
    "verify_endpoint": lambda value: {"lifecycle": {"type": "http", "driver": "http-load-unload", "driver_config": {"verify_endpoint": value}}},
    "driver health_endpoint": lambda value: {"lifecycle": {"type": "http", "driver": "http-load-unload", "driver_config": {"health_endpoint": value}}},
}


@pytest.mark.parametrize("attack", sorted(ATTACKS))
@pytest.mark.parametrize("field", sorted(PATCH_BODIES))
def test_an_administrator_cannot_re_aim_a_field_that_holds_credentials_by_a_service_patch(gateway, host_isolated, field, attack):
    owner, _, _, admin = owner_and_admin(gateway)
    before = copy.deepcopy(_plant(gateway, owner))
    refused = _patch(admin, PATCH_BODIES[field](ATTACKS[attack]))
    assert refused.status_code in (400, 403), (refused.status_code, refused.text[:300])
    assert stored(gateway)["d39"] == before, "a refused patch must leave the record as it was"


def test_the_reviewer_s_attack_by_patch_is_refused_as_owner_only(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    before = copy.deepcopy(_plant(gateway, owner))
    refused = _patch(admin, {"health_endpoint": "//attacker.invalid/collect"})
    assert refused.status_code == 403 and error_code(refused) == "owner_required", refused.text
    assert stored(gateway)["d39"] == before


@pytest.mark.parametrize("move", ["host", "port", "base_url"])
def test_an_administrator_cannot_move_the_base_by_a_service_patch(gateway, host_isolated, move):
    owner, _, _, admin = owner_and_admin(gateway)
    before = copy.deepcopy(_plant(gateway, owner))
    body = {"host": "attacker.invalid"} if move == "host" else {"port": 4444} if move == "port" else {"base_url": "http://attacker.invalid:4444/v1"}
    sent = _patch(admin, body)
    after = stored(gateway)["d39"]
    if sent.status_code == 200:
        # a local engine's form ignores ``base_url``: nothing that decides where a credential goes may have moved
        for key in ("host", "port", "base_url", "api_base", "default_base_url", "scheme"):
            assert after["config"].get(key) == before["config"].get(key), key
    else:
        assert sent.status_code == 403 and error_code(sent) == "owner_required", (sent.status_code, sent.text[:300])
        assert after == before


def test_an_administrator_can_still_rename_a_record_that_holds_credentials_by_a_service_patch(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    before = copy.deepcopy(_plant(gateway, owner))
    done = _patch(admin, {"name": "A new public name"})
    assert done.status_code == 200, done.text
    after = stored(gateway)["d39"]
    assert after["name"] == "A new public name"
    for key in ("health_endpoint", "health_endpoint_entries", "context_window_probe"):
        assert after["config"][key] == before["config"][key], key


def test_an_administrator_can_patch_the_path_of_the_health_endpoint_on_the_same_origin(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    _plant(gateway, owner)
    done = _patch(admin, {"health_endpoint": "/status"})
    assert done.status_code == 200, done.text
    assert stored(gateway)["d39"]["config"]["health_endpoint"] == "/status"


def test_a_write_that_puts_credentials_in_a_field_with_no_origin_is_refused_with_a_clear_message(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    empty_registry(gateway)
    for value in ("//attacker.invalid/collect?token=" + CANARY, "@attacker.invalid/x?token=" + CANARY, "ready?token=" + CANARY):
        refused = _owner_plants(owner, _record(health_endpoint=value))
        assert refused.status_code == 400, (value, refused.text[:200])
        assert CANARY not in refused.text and "config.health_endpoint" in refused.text
    assert not secret_rows(gateway)


def test_the_commands_that_set_url_credentials_refuse_a_field_with_no_origin(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    _plant(gateway, owner)
    sent = owner.post(f"{SERVICE}/secret-entries", headers=_etag(owner), json={"commands": [
        {"carrier": "config.health_endpoint", "operation": "set_credentials", "url": "//attacker.invalid/x?token=" + CANARY}]})
    assert sent.status_code == 400 and CANARY not in sent.text, sent.text
    assert "config.health_endpoint" in sent.text
    ok = owner.post(f"{SERVICE}/secret-entries", headers=_etag(owner), json={"commands": [
        {"carrier": "config.health_endpoint", "operation": "set_credentials", "url": "/ready?token=" + CANARY + "-again"}]})
    assert ok.status_code == 200, ok.text


def test_a_stored_secret_is_bound_to_the_origin_of_its_field(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    held = _plant(gateway, owner)
    refs = record_carriers.refs_in(held["config"]["health_endpoint_entries"])
    bound = {secret_rows(gateway)[ref] for ref in refs}
    assert bound == {GOOD}, bound


# ---------------------------------------------------------------------------
# The fingerprint, without the routes
# ---------------------------------------------------------------------------

def _entries(public, base_config, db, path="config.health_endpoint"):
    """A service record with ``public`` stored the way a write stores it (credentials in the store)."""
    key = path.rpartition(".")[2]
    record = {"id": "e", "name": "e", "config": {**base_config, key: f"{public}?token={CANARY}"}}
    moved, _ = record_carriers.externalize_service(record, secret_refs.putter(db))
    return moved


def test_the_fingerprint_marks_move_with_the_origin_of_a_field_that_holds_credentials(store):
    base = {"host": "good.invalid", "port": 9}
    before = {"services": {"e": _entries("/ready", base, store)}}
    for public in ("/other", "/ready/deeper"):
        after = copy.deepcopy(before)
        after["services"]["e"]["config"]["health_endpoint"] = public
        assert service_registry.credential_binding_changes(before, after) == {}, public
    for attack in ATTACKS.values():
        after = copy.deepcopy(before)
        after["services"]["e"]["config"]["health_endpoint"] = attack
        assert "config.health_endpoint" in service_registry.credential_binding_changes(before, after).get("e", []), attack


def test_a_field_without_credentials_is_not_in_the_fingerprint(store):
    before = {"services": {"e": {"id": "e", "name": "e", "config": {"host": "good.invalid", "port": 9, "health_endpoint": "/ready"}}}}
    after = copy.deepcopy(before)
    after["services"]["e"]["config"]["health_endpoint"] = "//attacker.invalid/collect"
    assert service_registry.credential_binding_changes(before, after) == {}


# ---------------------------------------------------------------------------
# The runtime: a record written past the rule above still sends nothing to another origin
# ---------------------------------------------------------------------------

def _moved(record, db):
    moved, paths = record_carriers.externalize_service(record, secret_refs.putter(db))
    assert paths
    return moved


def _record_for_runtime(db):
    return _moved(_record(), db)


RUNTIME_ATTACKS = {name: value for name, value in ATTACKS.items() if value}


def _serve(fakes, host, *exchanges):
    scenario = scenario_from_data({
        "protocol": "http-load-unload", "name": "d39",
        "provenance": {"source": "hand-written", "reference": "gateway/runtime_secrets.py url"},
        "exchanges": [{"required": False, **exchange} for exchange in exchanges]})
    return fakes.asgi(scenario, host=host)


def test_the_reviewer_s_catalog_probe_attack_sends_nothing(store):
    """``/ready?token=CANARY`` stored; the public endpoint then written as ``//attacker.invalid/collect``."""
    moved = _record_for_runtime(store)
    assert proxy._runtime_health_endpoint(moved) == f"{GOOD}/ready?token={CANARY}-health"
    moved["config"]["health_endpoint"] = "//attacker.invalid/collect"
    assert proxy._runtime_health_endpoint(moved) is None


@pytest.mark.parametrize("attack", sorted(RUNTIME_ATTACKS))
def test_the_catalog_probe_and_the_resource_probe_send_nothing_to_another_origin(store, attack):
    moved = _record_for_runtime(store)
    moved["config"]["health_endpoint"] = RUNTIME_ATTACKS[attack]
    assert proxy._runtime_health_endpoint(moved) is None
    assert proxy._service_resource_probe_url(moved["config"]) in ("", f"{GOOD}/v1/health", "http://127.0.0.1:9/health") or CANARY not in proxy._service_resource_probe_url(moved["config"])
    assert CANARY not in (proxy._service_resource_probe_url(moved["config"]) or "")


def test_the_catalog_probe_does_not_follow_the_base_to_another_origin(store):
    moved = _record_for_runtime(store)
    for move in ({"host": "attacker.invalid"}, {"port": 4444}, {"scheme": "https"}, {"base_url": "http://attacker.invalid:4444/v1"}):
        record = copy.deepcopy(moved)
        record["config"].update(move)
        assert proxy._runtime_health_endpoint(record) is None, move
        assert CANARY not in proxy._service_resource_probe_url(record["config"]), move


@pytest.mark.parametrize("attack", sorted(RUNTIME_ATTACKS))
def test_the_context_window_probe_sends_nothing_to_another_origin(store, monkeypatch, attack):
    moved = _record_for_runtime(store)
    monkeypatch.setitem(proxy.config, "backends", {"d39": copy.deepcopy(moved["config"])})
    assert proxy._context_window_probe_endpoint("d39") == f"/props?token={CANARY}-context"
    proxy.config["backends"]["d39"]["context_window_probe"]["endpoint"] = RUNTIME_ATTACKS[attack]
    assert proxy._context_window_probe_endpoint("d39") is None
    proxy.config["backends"]["d39"]["context_window_probe"]["endpoint"] = "/props"
    proxy.config["backends"]["d39"]["port"] = 4444
    assert proxy._context_window_probe_endpoint("d39") is None


@pytest.mark.parametrize("attack", sorted(RUNTIME_ATTACKS))
def test_the_placement_probe_sends_nothing_to_another_origin(store, monkeypatch, attack):
    moved = _record_for_runtime(store)
    moved["config"]["health_endpoint"] = RUNTIME_ATTACKS[attack]
    seen: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            seen.append(url)
            raise httpx.ConnectError("no engine runs in this test")

    monkeypatch.setattr(httpx, "Client", Client)
    runtime = decision_routing.DecisionRuntime(lambda: {"backends": {"d39": moved["config"]}}, lambda *a, **k: None, lambda *a, **k: None)
    runtime.observed_engine("d39", fresh=True)
    assert not [url for url in seen if CANARY in url], seen


@pytest.mark.parametrize("attack", sorted(RUNTIME_ATTACKS))
def test_the_readiness_poll_sends_nothing_to_another_origin(store, wire, attack):
    moved = _record_for_runtime(store)
    backend = service_registry.runtime_backend_from_service(moved)
    backend["lifecycle"]["driver_config"]["health_endpoint"] = RUNTIME_ATTACKS[attack]
    backend["lifecycle"]["driver_config"]["verify_endpoint"] = RUNTIME_ATTACKS[attack]
    asyncio.run(lifecycle_drivers.HttpLoadUnloadDriver().ensure_ready(backend, 0.2))
    assert wire, "the poll still asks the default path, with no credential"
    assert not _leaks(wire), _leaks(wire)


DRIVER_KEYS = ("load_url", "unload_url", "verify_endpoint")


@pytest.mark.parametrize("attack", sorted(RUNTIME_ATTACKS))
@pytest.mark.parametrize("key", DRIVER_KEYS)
def test_the_driver_sends_nothing_when_a_stored_path_was_re_aimed(store, wire, key, attack, monkeypatch):
    monkeypatch.setenv("D39_ADMIN", "D39-ADMIN-KEY")
    monkeypatch.setenv("D39_API", "D39-API-KEY")
    moved = _record_for_runtime(store)
    moved["lifecycle"]["driver_config"].update(admin_key_env="D39_ADMIN", api_key_env="D39_API")
    moved["config"]["driver_credential_origin"] = GOOD
    moved["lifecycle"]["driver_config"][key] = RUNTIME_ATTACKS[attack]
    backend = service_registry.runtime_backend_from_service(moved)
    ctx = lifecycle_drivers.SwapContext(loaded_model="old", target_model="wanted")
    done = asyncio.run(lifecycle_drivers.HttpLoadUnloadDriver().swap(backend, {"model_name": "wanted"}, ctx=ctx))
    assert done is False
    assert wire == [], [str(request.url) for request in wire]


def test_the_driver_still_sends_to_the_origin_the_credentials_were_stored_for(store, fake_upstreams):
    moved = _record_for_runtime(store)
    fake = _serve(
        fake_upstreams, "good.invalid:9",
        {"id": "unload", "match": {"method": "POST", "path": "/v1/model/unload", "query": {"token": f"{CANARY}-unload"}}, "response": {"status": 204}},
        {"id": "load", "match": {"method": "POST", "path": "/v1/model/load", "query": {"token": f"{CANARY}-load"}},
         "response": {"body": [{"text": "{\"status\":\"finished\"}"}]}})
    backend = service_registry.runtime_backend_from_service(moved)
    ctx = lifecycle_drivers.SwapContext(loaded_model="old", target_model="wanted")
    assert asyncio.run(lifecycle_drivers.HttpLoadUnloadDriver().swap(backend, {"model_name": "wanted"}, ctx=ctx)) is True
    assert len(fake.requests) == 2


@pytest.mark.parametrize("attack", sorted(RUNTIME_ATTACKS))
def test_a_declared_probe_sends_nothing_to_another_origin(store, attack):
    moved = _moved({"id": "d39", "config": {"host": "good.invalid", "port": 9}, "connection": {"address": GOOD},
                    "observations": {"ready": {"http": {"path": f"/ready?token={CANARY}-obs", "expect": 200}}}}, store)
    http = moved["observations"]["ready"]["http"]
    http["path"] = RUNTIME_ATTACKS[attack]
    sent: list[str] = []

    class Factory:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, **kwargs):
            sent.append(url)
            raise httpx.ConnectError("no engine runs in this test")

    result = asyncio.run(engine_observations.ObservationReader(client_factory=Factory).read(moved, "ready"))
    assert result.known is False and not sent, sent


@pytest.mark.parametrize("field", ["base_url", "api_base", "default_base_url"])
def test_a_base_address_with_credentials_is_put_back_only_for_its_own_origin(store, field):
    moved = _moved({"id": "d39", "config": {field: f"http://u:{CANARY}@good.invalid:9/v1?key={CANARY}"}}, store)
    assert runtime_secrets.first_url(moved["config"]) == f"http://u:{CANARY}@good.invalid:9/v1?key={CANARY}"
    for attack in RUNTIME_ATTACKS.values():
        record = copy.deepcopy(moved)
        record["config"][field] = attack.replace("/collect", "/v1")
        with pytest.raises(runtime_secrets.SecretUnavailable) as refused:
            runtime_secrets.first_url(record["config"])
        assert CANARY not in str(refused.value)


def test_a_stored_secret_is_not_released_for_another_origin_even_if_the_resolver_were_asked(store):
    """The store itself holds the binding: asking for a secret with the wrong origin is refused whatever the caller computed."""
    moved = _record_for_runtime(store)
    ref = record_carriers.refs_in(moved["config"]["health_endpoint_entries"])[0]
    assert runtime_secrets.resolve(ref, origin=GOOD).startswith(CANARY)
    for other in ("http://attacker.invalid:80", "http://good.invalid:80", "https://good.invalid:9"):
        with pytest.raises(runtime_secrets.OriginRefused):
            runtime_secrets.resolve(ref, origin=other)


def test_a_url_kept_whole_is_not_sent_to_an_origin_other_than_the_one_it_was_stored_for(store):
    from gateway import secret_commands

    record = {"id": "d39", "config": {"host": "good.invalid", "port": 9, "health_endpoint": ""}}
    new, outcome = secret_commands.apply_commands(
        record, [{"carrier": "config.health_endpoint", "operation": "set_credentials", "url": f"/ready;k={CANARY}", "whole": True, "label": "health"}],
        secret_refs.putter(store))
    assert outcome.created
    join = lambda base, path: f"{base}{path}"  # noqa: E731
    assert runtime_secrets.url(new["config"], "health_endpoint", base=f"{GOOD}/v1", join=join) == f"{GOOD}/v1/ready;k={CANARY}"
    for base in ("http://attacker.invalid:9/v1", "http://good.invalid:4444", "https://good.invalid:9"):
        with pytest.raises(runtime_secrets.OriginRefused):
            runtime_secrets.url(new["config"], "health_endpoint", base=base, join=join)


@pytest.mark.parametrize("reader_join", [
    lambda base, path: f"{base}{path}",
    lambda base, path: f"{base.rstrip('/')}/{path.lstrip('/')}",
    lambda base, path: __import__("urllib.parse").parse.urljoin(base.rstrip("/") + "/", path),
], ids=["concatenation", "slash join", "urljoin"])
@pytest.mark.parametrize("attack", sorted(RUNTIME_ATTACKS))
def test_whatever_the_reader_s_join_the_dialed_origin_must_be_the_stored_one(store, reader_join, attack):
    moved = _record_for_runtime(store)
    moved["config"]["health_endpoint"] = RUNTIME_ATTACKS[attack]
    try:
        dialed = runtime_secrets.url(moved["config"], "health_endpoint", base=GOOD, join=reader_join)
    except runtime_secrets.SecretUnavailable:
        return
    assert CANARY not in dialed


def test_the_refusal_is_logged_without_a_value(store, capsys):
    moved = _record_for_runtime(store)
    moved["config"]["health_endpoint"] = "//attacker.invalid/collect"
    runtime_secrets._REFUSALS.clear()
    assert proxy._runtime_health_endpoint(moved) is None
    out = capsys.readouterr().out
    assert "were not sent" in out and "health_endpoint" in out
    assert CANARY not in out and "attacker.invalid" not in out


# ---------------------------------------------------------------------------
# Schema 13: bind what older versions stored
# ---------------------------------------------------------------------------

def _store_as_v12(path: Path, document: dict) -> None:
    """Leave ``path`` as a schema 12 database holding ``document`` as its service registry."""
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version = 12")
        conn.execute("DELETE FROM operator_settings WHERE key='service_registry'")
        conn.execute("INSERT INTO operator_settings(key, value_json, updated_at) VALUES ('service_registry', ?, 0)", (json.dumps(document),))
        conn.execute("INSERT OR REPLACE INTO operator_settings_generations(setting_key, operator_settings_generation) VALUES ('service_registry', 1)")


def _bound(path: Path) -> dict[str, str | None]:
    with sqlite3.connect(path) as conn:
        return {row[0]: row[1] for row in conn.execute("SELECT id, approved_origin FROM provider_secrets")}


def test_the_schema_13_migration_binds_each_stored_credential_to_the_origin_of_its_field_once(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    runtime_secrets.configure(path)
    try:
        record = _record(base_url=f"{GOOD}/v1?key={CANARY}-base")
        put = lambda value, origin=None: secret_refs.store(value, db_path=path, origin=None)  # noqa: E731 - version 12 bound a path to nothing
        moved, _ = record_carriers.externalize_service(record, put)
        document = service_registry.normalize_service_registry({"schema_version": 2, "services": {"d39": moved}})
        _store_as_v12(path, document)
        refs = record_carriers.refs_in(moved)
        assert refs and set(_bound(path).values()) == {None}
        values_before = {ref: secret_store.get_provider_secret(ref, db_path=path) for ref in refs}

        admin_store.init_db(path)

        assert admin_store.schema_version(path) == admin_store.SCHEMA_VERSION
        bound = _bound(path)
        assert set(bound) >= set(refs)
        assert {bound[ref] for ref in refs} == {"http://good.invalid:9"}
        assert {ref: secret_store.get_provider_secret(ref, db_path=path, expected_origin=GOOD) for ref in refs} == values_before
        with sqlite3.connect(path) as conn:
            audit = [json.loads(row[0]) for row in conn.execute(
                "SELECT metadata_json FROM audit_events WHERE action='operator.url_binding.migrate'")]
            generation = conn.execute("SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key='service_registry'").fetchone()[0]
            registry_after = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0]
        assert len(audit) == 1 and audit[0]["services"] == ["d39"] and audit[0]["rebound"] == len(refs)
        assert CANARY not in json.dumps(audit)
        assert json.loads(registry_after) == document, "the migration changes secrets' bindings, never the record"

        admin_store.init_db(path)  # a second start changes nothing
        with sqlite3.connect(path) as conn:
            again = conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='operator.url_binding.migrate'").fetchone()[0]
            assert again == 1
            assert conn.execute("SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key='service_registry'").fetchone()[0] == generation
        assert _bound(path) == bound
        # and the runtime now puts them back for that origin, and for no other
        from gateway import service_registry as registry_module

        reread = registry_module.load_service_registry(path=path)["services"]["d39"]
        assert proxy._runtime_health_endpoint(reread) == f"{GOOD}/ready?token={CANARY}-health"
        reread["config"]["health_endpoint"] = "//attacker.invalid/collect"
        assert proxy._runtime_health_endpoint(reread) is None
    finally:
        runtime_secrets.configure(None)


def test_the_schema_13_migration_leaves_a_field_with_no_origin_unbound_and_says_so(tmp_path, capsys):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    runtime_secrets.configure(path)
    try:
        record = _record()
        put = lambda value, origin=None: secret_refs.store(value, db_path=path, origin=None)  # noqa: E731
        moved, _ = record_carriers.externalize_service(record, put)
        moved["config"]["health_endpoint"] = "//attacker.invalid/collect"  # a record an administrator re-aimed under schema 12
        document = service_registry.normalize_service_registry({"schema_version": 2, "services": {"d39": moved}})
        _store_as_v12(path, document)
        health_refs = record_carriers.refs_in(moved["config"]["health_endpoint_entries"])
        admin_store.init_db(path)
        bound = _bound(path)
        assert {bound[ref] for ref in health_refs} == {None}, "a field with no origin binds nothing"
        out = capsys.readouterr().out
        assert "left unbound" in out and CANARY not in out
        reread = service_registry.load_service_registry(path=path)["services"]["d39"]
        assert proxy._runtime_health_endpoint(reread) is None
    finally:
        runtime_secrets.configure(None)


def test_the_schema_13_migration_writes_nothing_to_a_database_with_no_url_credentials(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    document = service_registry.normalize_service_registry({"schema_version": 2, "services": {"d39": {
        "id": "d39", "name": "d39", "kind": "engine", "enabled": True, "config": {"host": "good.invalid", "port": 9, "health_endpoint": "/ready"}}}})
    _store_as_v12(path, document)
    with sqlite3.connect(path) as conn:
        generation = conn.execute("SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key='service_registry'").fetchone()[0]
    admin_store.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='operator.url_binding.migrate'").fetchone()[0] == 0
        assert conn.execute("SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key='service_registry'").fetchone()[0] == generation
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == admin_store.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# The log scrub
# ---------------------------------------------------------------------------

def test_scrub_removes_every_resolved_value_in_every_form_and_cuts_the_line(store):
    runtime_secrets._RESOLVED.clear()  # other tests of this process resolved values too
    ref = secret_refs.store("tok en/&=é+1", db_path=store)
    runtime_secrets.resolve(ref)
    text = "a tok en/&=é+1 b tok%20en%2F%26%3D%C3%A9%2B1 c tok+en%2F%26%3D%C3%A9%2B1 d tok%20en/%26%3D%C3%A9%2B1 " + "x" * 2000
    cleaned = runtime_secrets.scrub(text, limit=300)
    for form in ("tok en/&=é+1", "tok%20en%2F%26%3D%C3%A9%2B1", "tok+en%2F%26%3D%C3%A9%2B1", "tok%20en/%26%3D%C3%A9%2B1"):
        assert form not in cleaned
    assert cleaned.count("[removed]") == 4 and len(cleaned) <= 300 + len("...[cut]")


def test_scrub_removes_a_short_value_only_where_it_stands_alone(store):
    runtime_secrets._RESOLVED.clear()
    runtime_secrets.resolve(secrets_store_short(store, "ab"))
    assert runtime_secrets.scrub("token=ab and ab, but table and cab stay") == "token=[removed] and [removed], but table and cab stay"


def secrets_store_short(db, value):
    return secret_refs.store(value, db_path=db)


def test_a_canary_reflected_by_the_upstream_never_reaches_a_log_line(store, fake_upstreams, capsys, monkeypatch):
    """The load and unload replies echo what they were sent; the driver logs the start of a reply (decision D39)."""
    from urllib.parse import quote

    moved = _record_for_runtime(store)
    reflected = f"{CANARY}-load {quote(CANARY + '-load', safe='')} {CANARY}-unload {CANARY}-verify"
    fake = _serve(
        fake_upstreams, "good.invalid:9",
        {"id": "unload", "match": {"method": "POST", "path": "/v1/model/unload"}, "response": {"status": 500, "body": [{"text": f"unload said {reflected}"}]}},
        {"id": "load", "match": {"method": "POST", "path": "/v1/model/load"}, "response": {"status": 200, "body": [{"text": f'{{"error": "{reflected}"}}'}]}},
        {"id": "verify", "match": {"method": "GET", "path": "/v1/model"}, "response": {"body": [{"text": reflected}]}})
    backend = service_registry.runtime_backend_from_service(moved)
    ctx = lifecycle_drivers.SwapContext(loaded_model="old", target_model="wanted")
    assert asyncio.run(lifecycle_drivers.HttpLoadUnloadDriver().swap(backend, {"model_name": "wanted"}, ctx=ctx)) is False
    assert len(fake.requests) >= 2
    out = capsys.readouterr().out
    assert "HttpLoadUnloadDriver.swap" in out
    assert CANARY not in out and quote(CANARY, safe="") not in out


def test_a_canary_inside_an_error_text_never_reaches_a_log_line(store, capsys, monkeypatch):
    moved = _record_for_runtime(store)
    runtime_secrets.url(moved["lifecycle"]["driver_config"], "load_url", base=GOOD, join=lambda base, path: f"{base}{path}")

    async def explode(self, request):
        raise httpx.ConnectError(f"could not connect to {request.url}", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", explode)
    backend = service_registry.runtime_backend_from_service(moved)
    ctx = lifecycle_drivers.SwapContext(loaded_model="old", target_model="wanted")
    assert asyncio.run(lifecycle_drivers.HttpLoadUnloadDriver().swap(backend, {"model_name": "wanted"}, ctx=ctx)) is False
    out = capsys.readouterr().out
    assert "unload error" in out and CANARY not in out


def test_every_print_of_the_lifecycle_drivers_goes_through_the_scrub():
    source = Path(lifecycle_drivers.__file__).read_text().splitlines()
    bare = [number for number, line in enumerate(source, 1) if line.strip().startswith("print(") and "runtime_secrets.scrub" not in line]
    assert not bare, f"lifecycle_drivers.py prints directly at lines {bare}; use _say"


# ---------------------------------------------------------------------------
# The smaller findings
# ---------------------------------------------------------------------------

def test_a_write_refuses_user_info_in_the_host_with_a_clear_message():
    put = lambda value, origin=None: pytest.fail("a refused host must not store anything")  # noqa: E731
    with pytest.raises(record_carriers.HostUserInfoRefused) as refused:
        record_carriers.ingest_service({"id": "e", "config": {"host": f"user:{CANARY}@good.invalid", "port": 9}}, None, put)
    assert CANARY not in str(refused.value) and "config.host" in str(refused.value)


def test_a_host_with_user_info_in_a_stored_record_is_not_sent_the_credentials(store):
    """A migration moved the user info out of ``config.host``; no reader restored it, so none is sent (and no write may add one)."""
    moved, paths = record_carriers.externalize_service({"id": "e", "config": {"host": f"user:{CANARY}@good.invalid", "port": 9}}, secret_refs.putter(store))
    assert "config.host" in paths and CANARY not in json.dumps(moved)
    assert "@" not in service_registry.service_base_url(moved["config"])


def test_a_structured_environment_value_a_migration_kept_is_marked_and_never_passed_to_a_process(store):
    record = {"id": "e", "config": {"host": "good.invalid", "port": 9},
              "process": {"driver": "command", "env": {"MODE": {"a": CANARY}, "NAME": "plain", "LIST": ["x"], "JSON": '{"a": 1}'}}}
    moved, _ = record_carriers.externalize_service(record, secret_refs.putter(store))
    env = moved["process"]["env"]
    assert env["MODE"]["structured"] is True and env["LIST"]["structured"] is True
    assert "structured" not in env["NAME"] and "structured" not in env["JSON"], "a value that was text stays text"
    assert CANARY not in json.dumps(moved)
    for name in ("MODE", "LIST"):
        with pytest.raises(runtime_secrets.StructuredEnvValue) as refused:
            runtime_secrets.env_mapping({name: env[name]})
        assert name in str(refused.value) and CANARY not in str(refused.value)
    assert runtime_secrets.env_mapping({"NAME": env["NAME"], "JSON": env["JSON"]}) == {"NAME": "plain", "JSON": '{"a": 1}'}


def test_a_process_with_a_structured_environment_value_is_not_started(store, capsys):
    from gateway import engine_process

    record = {"id": "e", "config": {"host": "good.invalid", "port": 9}, "process": {"driver": "command", "env": {"MODE": {"a": CANARY}}}}
    moved, _ = record_carriers.externalize_service(record, secret_refs.putter(store))
    with pytest.raises(ValueError) as refused:
        engine_process.ProcessDriver._environment(moved["process"])
    assert "MODE" in str(refused.value) and "not started" in str(refused.value) and CANARY not in str(refused.value)
    assert "MODE" in capsys.readouterr().out


def test_setting_a_structured_value_again_as_text_clears_the_mark(store):
    from gateway import secret_commands

    record = {"id": "e", "config": {"host": "good.invalid", "port": 9}, "process": {"driver": "command", "env": {"MODE": {"a": CANARY}}}}
    moved, _ = record_carriers.externalize_service(record, secret_refs.putter(store))
    new, _outcome = secret_commands.apply_commands(
        moved, [{"carrier": "process.env", "operation": "replace", "entry_id": moved["process"]["env"]["MODE"]["id"], "value": "fast"}],
        secret_refs.putter(store))
    assert "structured" not in new["process"]["env"]["MODE"]
    assert runtime_secrets.env_mapping(new["process"]["env"]) == {"MODE": "fast"}


# ---------------------------------------------------------------------------
# Every URL field a record can hold credentials in, not only the ones a reader puts back today
# ---------------------------------------------------------------------------

def _nested(path: str, value) -> dict:
    node: dict = {}
    parts = path.split(".")
    cursor = node
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value
    return node


def _merge(into: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value
    return into


URL_PATHS = [path for path in record_carriers.URL_FIELD_PATHS if path not in ("config.host", "config.gateway_lifecycle_base_url")]


@pytest.mark.parametrize("path", URL_PATHS)
def test_every_url_field_that_can_hold_credentials_is_bound_to_its_origin_and_is_in_the_fingerprint(store, path):
    absolute = path.rpartition(".")[2] in ("base_url", "api_base", "default_base_url", "url", "address", "endpoint", "upstream", "key_url", "docs_url")
    public = "http://good.invalid:9/x" if absolute else "/x"
    record = _merge({"id": "e", "name": "e", "config": {"host": "good.invalid", "port": 9}}, _nested(path, f"{public}?token={CANARY}"))
    moved, paths = record_carriers.externalize_service(record, secret_refs.putter(store))
    assert path in paths, paths
    bound = {secret_rows_of(store)[ref] for ref in record_carriers.refs_in(moved)}
    assert bound == {GOOD}, (path, bound)
    before = {"services": {"e": moved}}
    for attack in ("//attacker.invalid/x", "http://attacker.invalid/x", "HTTP://ATTACKER.INVALID/x", "http://good.invalid/x"):
        after = copy.deepcopy(before)
        parent_path, _, key = path.rpartition(".")
        node = after["services"]["e"]
        for part in parent_path.split("."):
            node = node[part]
        node[key] = attack
        assert url_binding.binding_marks(after["services"]["e"]) != url_binding.binding_marks(moved), (path, attack)


def secret_rows_of(db) -> dict:
    with sqlite3.connect(db) as conn:
        return {row[0]: row[1] for row in conn.execute("SELECT id, approved_origin FROM provider_secrets")}


def test_a_models_row_url_is_bound_too(store):
    record = {"id": "e", "name": "e", "config": {"host": "good.invalid", "port": 9, "models": [{"id": "m", "url": f"/m?token={CANARY}"}]}}
    moved, paths = record_carriers.externalize_service(record, secret_refs.putter(store))
    assert "config.models[0].url" in paths
    assert {secret_rows_of(store)[ref] for ref in record_carriers.refs_in(moved)} == {GOOD}
    after = copy.deepcopy({"services": {"e": moved}})
    after["services"]["e"]["config"]["models"][0]["url"] = "//attacker.invalid/m"
    assert service_registry.credential_binding_changes({"services": {"e": moved}}, after).get("e")
    assert url_binding.binding_marks(after["services"]["e"]) != url_binding.binding_marks(moved)
