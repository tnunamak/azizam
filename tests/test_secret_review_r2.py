"""Round 2 of the secret handling (decisions D26 to D34): every way of writing a secret into a service record.

One planted marker per shape, written through the real registry route; the marker must not come back in any
output of any GET route (as owner or admin) nor in the write's own response, and it must not sit inline in the
store unless it is in a key no type classifies (decision D34: stored unchanged, never shown). A read written back
unchanged keeps those hidden settings. The shapes come from the independent review of Phase 1 (its probes are in
``Documents/azizam/p1-rv-probes``).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from gateway import admin_store, service_registry
from tests.test_admin_secret_sweep import _fill, _get_routes, host_isolated  # noqa: F401
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

REGISTRY = "/admin/api/operator/service-registry"
BASE = {"id": "probe-a", "name": "Probe A", "kind": "engine", "enabled": True, "capabilities": ["chat"]}
MARKERS: dict[str, list[str]] = {}
SHAPES: dict = {}
_planted: list[str] = []
# a number written as the secret has no generated marker: it is what the shape itself plants
NUMERIC_MARKERS = {"argv_int_secret": "424242424", "hdr_map_num": "1234567"}


def _etag(client):
    return {**csrf_for(client), "If-Match": client.get(REGISTRY).headers["etag"]}


def mk(name):
    value = f"ZQ{name}-{sum(map(len, MARKERS.values())) + len(_planted):02d}x9"
    _planted.append(value)
    return value


def shape(fn):
    SHAPES[fn.__name__] = fn
    return fn


def rec(**kw):
    base = {**BASE, "config": {"host": "10.0.0.5", "port": 8000, "protocol": "openai-compatible", "models": [{"id": "m", "upstream_model": "m"}]}}
    cfg = kw.pop("config", {})
    base["config"].update(cfg)
    base.update(kw)
    return base


@shape
def hdr_public_truthy():
    v = mk("hdr_public_truthy")
    return rec(config={"headers": [{"name": "X-A", "value": v, "public": "yes"}]})
@shape
def hdr_list_value_nested():
    v = mk("hdr_list_value")
    return rec(config={"headers": [{"name": "X-A", "value": [v]}]})
@shape
def hdr_map():
    v = mk("hdr_map")
    return rec(config={"headers": {"X-A": v}})
@shape
def hdr_map_num():
    return rec(config={"headers": {"X-A": 1234567}})
@shape
def extra_headers():
    v = mk("extra_headers")
    return rec(config={"extra_headers": {"X-A": v}})
@shape
def request_headers():
    v = mk("request_headers")
    return rec(config={"request_headers": [{"name": "Authorization", "value": "Bearer " + v}]})
@shape
def default_headers():
    v = mk("default_headers")
    return rec(config={"default_headers": {"Authorization": "Bearer " + v}})
@shape
def auth_block():
    v = mk("auth_block")
    return rec(config={"auth": {"type": "bearer", "token": v}})
@shape
def api_key_inline():
    v = mk("api_key_inline")
    return rec(config={"api_key": v})
@shape
def token_inline():
    v = mk("token_inline")
    return rec(config={"token": v})
@shape
def conn_string():
    v = mk("conn_string")
    return rec(config={"connection_string": f"Server=db;User=x;Password={v};"})
@shape
def dsn():
    v = mk("dsn")
    return rec(config={"dsn": f"host=db user=x password={v}"})
@shape
def load_url_query():
    v = mk("load_url_query")
    return rec(lifecycle={"driver": "http-load-unload", "type": "http-load-unload", "managed": True, "service_name": "",
                          "driver_config": {"load_url": f"http://10.0.0.5:8000/load?token={v}"}})
@shape
def load_url_userinfo():
    v = mk("load_url_userinfo")
    return rec(lifecycle={"driver": "http-load-unload", "type": "http-load-unload", "managed": True, "service_name": "",
                          "driver_config": {"load_url": f"http://u:{v}@10.0.0.5:8000/load"}})
@shape
def env_dict_value():
    v = mk("env_dict_value")
    return rec(process={"driver": "process", "port": {"fixed": 8000}, "start": ["runner"], "env": {"A": {"value": v}}})
@shape
def env_list_value():
    v = mk("env_list_value")
    return rec(process={"driver": "process", "port": {"fixed": 8000}, "start": ["runner"], "env": {"A": [v]}})
@shape
def argv_unknown_flag_sep():
    v = mk("argv_unknown_flag_sep")
    return rec(process={"driver": "process", "port": {"fixed": 8000}, "start": ["runner", "--foo", v]})
@shape
def argv_unknown_flag_eq():
    v = mk("argv_unknown_flag_eq")
    return rec(process={"driver": "process", "port": {"fixed": 8000}, "start": ["runner", f"--foo={v}"]})
@shape
def argv_positional():
    v = mk("argv_positional")
    return rec(process={"driver": "process", "port": {"fixed": 8000}, "start": ["runner", v]})
@shape
def argv_dash_secret():
    v = mk("argv_dash_secret")
    return rec(process={"driver": "process", "port": {"fixed": 8000}, "start": ["runner", "--token", "-" + v]})
@shape
def argv_int_secret():
    return rec(process={"driver": "process", "port": {"fixed": 8000}, "start": ["runner", "--pin", 424242424]})
@shape
def argv_str_unbalanced():
    v = mk("argv_str_unbalanced")
    return rec(process={"driver": "process", "port": {"fixed": 8000}, "start": f"runner --password '{v}"})
@shape
def argv_str_unknown():
    v = mk("argv_str_unknown")
    return rec(process={"driver": "process", "port": {"fixed": 8000}, "start": f"runner --foo {v}"})
@shape
def argv_connstring():
    v = mk("argv_connstring")
    return rec(process={"driver": "process", "port": {"fixed": 8000}, "start": ["runner", "--conn", f"Server=x;Password={v}"]})
@shape
def argv_other_key_command():
    v = mk("argv_other_key_command")
    return rec(process={"driver": "process", "port": {"fixed": 8000}, "start": ["runner"], "command": ["x", "--token", v]})
@shape
def argv_args_key():
    v = mk("argv_args_key")
    return rec(process={"driver": "process", "port": {"fixed": 8000}, "start": ["runner"], "args": ["--token", v]})
@shape
def argv_in_config_start():
    v = mk("argv_in_config_start")
    return rec(config={"start": ["runner", "--token", v]})
@shape
def url_ipv6():
    v = mk("url_ipv6")
    return rec(config={"base_url": f"http://u:{v}@[::1]:8000/v1"})
@shape
def url_pct():
    v = mk("url_pct")
    return rec(config={"base_url": f"http://u%40x:{v}%3A@10.0.0.5:8000/v1"})
@shape
def url_at_in_path():
    v = mk("url_at_in_path")
    return rec(config={"base_url": f"http://10.0.0.5:8000/{v}@x/v1"})
@shape
def url_path_secret():
    v = mk("url_path_secret")
    return rec(config={"base_url": f"https://hooks.example/services/T0/B0/{v}"})
@shape
def url_backslash():
    v = mk("url_backslash")
    return rec(config={"base_url": f"http://10.0.0.5\\@{v}:x@evil/v1"})
@shape
def url_semicolon_params():
    v = mk("url_semicolon_params")
    return rec(config={"base_url": f"http://10.0.0.5:8000/v1;token={v}"})
@shape
def url_query_noeq():
    v = mk("url_query_noeq")
    return rec(config={"base_url": f"http://10.0.0.5:8000/v1?{v}"})
@shape
def url_other_key_endpoint():
    v = mk("url_other_key_endpoint")
    return rec(config={"endpoint": f"http://10.0.0.5:8000/x?token={v}", "upstream": f"http://u:{v}2@10.0.0.5/"})
@shape
def url_models_entry():
    v = mk("url_models_entry")
    return rec(config={"models": [{"id": "m", "upstream_model": "m", "url": f"http://u:{v}@10.0.0.5/"}]})
@shape
def url_host_userinfo():
    v = mk("url_host_userinfo")
    return rec(config={"host": f"u:{v}@10.0.0.5"})
@shape
def nested_extras():
    v = mk("nested_extras")
    return rec(config={"transforms": ["x"], "custom": {"nested": {"secret": v}}})
@shape
def provenance_json():
    v = mk("provenance_json")
    return rec(provenance={"token": v})
@shape
def residency_json():
    v = mk("residency_json")
    return rec(resources={"note": f"password={v}"})
@shape
def secret_refs_map():
    v = mk("secret_refs_map")
    return rec(secret_refs={"api_key": v})
@shape
def top_unknown_field():
    v = mk("top_unknown_field")
    return rec(zzz_extra=v)
@shape
def observation_headers():
    v = mk("observation_headers")
    return rec(observations={"ready": {"http": {"path": "/ready", "headers": {"Authorization": "Bearer " + v}}}})


# Shapes whose marker sits in a key no projection type classifies: the store keeps it unchanged (D34) and no output shows it.
HIDDEN_AND_STORED = {
    "api_key_inline", "token_inline", "conn_string", "dsn", "extra_headers", "request_headers", "default_headers", "auth_block",
    "nested_extras", "provenance_json", "residency_json", "argv_other_key_command", "argv_args_key", "argv_in_config_start",
    "observation_headers", "secret_refs_map",
}
# A command argument that no flag name and no structure marks as a credential is a public literal, as the writer sent it
# (decision D29): there is nothing to tell ``--foo value`` from ``--port 8000``.
PUBLIC_LITERALS = {"argv_unknown_flag_sep", "argv_unknown_flag_eq", "argv_positional", "argv_str_unknown"}
# A secret written into the path of an address, in a path parameter, or after an ``@`` in the path has no structure to
# tell it from the path (decision D29 separates user info, the query and the fragment): the path stays public text.
PATH_TEXT = {"url_at_in_path", "url_path_secret", "url_semicolon_params"}


def _store_dump(path):
    with sqlite3.connect(path) as c:
        return "\n".join(str(row) for row in c.execute("SELECT * FROM operator_settings"))


def _plant_all(gateway, owner):
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry({"schema_version": 2, "services": {}}), expected_generation=0, updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)
    answers = {}
    for name, build in SHAPES.items():
        _planted.clear()
        record = build()
        MARKERS[name] = [*_planted, *([NUMERIC_MARKERS[name]] if name in NUMERIC_MARKERS else [])]
        reg = owner.get(REGISTRY).json()["service_registry"]
        reg["services"][f"s-{name}".replace("_", "-")] = {**record, "id": f"s-{name}".replace("_", "-")}
        answers[name] = owner.put(REGISTRY, headers=_etag(owner), json=reg)
    return answers


def test_no_shape_of_input_comes_back_in_any_output_and_only_unclassified_keys_stay_inline(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    answers = _plant_all(gateway, owner)
    problems: list[str] = []
    for name, response in answers.items():
        if name not in PATH_TEXT | PUBLIC_LITERALS and any(marker in response.text for marker in MARKERS[name]):
            problems.append(f"{name}: the write's own response shows the marker (status {response.status_code})")
    dump = _store_dump(gateway.path)
    for name, markers in MARKERS.items():
        inline = any(marker in dump for marker in markers)
        if inline and name not in HIDDEN_AND_STORED and name not in PATH_TEXT and name not in PUBLIC_LITERALS:
            problems.append(f"{name}: the marker is stored inline")
        if not inline and name in HIDDEN_AND_STORED and answers[name].status_code == 200:
            problems.append(f"{name}: an unclassified key was not kept unchanged")
    seen: set[tuple[str, str, str]] = set()
    for path in _get_routes(gateway.proxy.app):
        if path in ("/mcp", "/{path:path}", "/me/api/prompt-memory") or ("events" in path and "media" in path):
            continue
        for who, client in (("owner", owner), ("admin", admin)):
            try:
                reply = client.get(_fill(path))
            except BaseException:  # noqa: BLE001 - a route that raises answered nothing
                continue
            hay = reply.text + str(dict(reply.headers))
            seen.update((name, path, who) for name, markers in MARKERS.items() if any(marker in hay for marker in markers))
    problems.extend(f"{name}: shown by GET {path} to {who}" for name, path, who in sorted(seen) if name not in PATH_TEXT | PUBLIC_LITERALS)
    assert not problems, "\n".join(problems)


def test_a_registry_read_and_written_back_unchanged_keeps_every_hidden_setting(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    answers = _plant_all(gateway, owner)
    kept = {name: MARKERS[name][0] for name in HIDDEN_AND_STORED if answers[name].status_code == 200}
    before = _store_dump(gateway.path)
    assert [name for name, marker in kept.items() if marker not in before] == []
    read = owner.get(REGISTRY)
    assert not [name for name, marker in kept.items() if marker in read.text]
    again = owner.put(REGISTRY, headers={**csrf_for(owner), "If-Match": read.headers["etag"]}, json=read.json()["service_registry"])
    assert again.status_code == 200, again.text
    after = _store_dump(gateway.path)
    assert [name for name, marker in kept.items() if marker not in after] == []


# --- M1: the effective configuration (the YAML the operator keeps) ------------------------------------------------

def test_the_effective_configuration_shows_only_classified_keys_of_each_yaml_section(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    config = gateway.proxy.config
    planted = {}
    for section, key, value in (
        ("llm", "api_key", "ZYAML-llm-1"), ("llm", "master_key", "ZYAML-llm-2"), ("proxy", "api_key", "ZYAML-proxy-3"),
        ("admin", "session_secret", "ZYAML-admin-4"), ("auth", "bootstrap_password", "ZYAML-auth-5"), ("audio", "api_key", "ZYAML-audio-6"),
        ("identity", "token", "ZYAML-ident-7"), ("videos", "secret", "ZYAML-vid-8"), ("images", "other_secret", "ZYAML-img-10"),
        ("images", "signed_url_secret", "ZYAML-img-11"),
    ):
        config.setdefault(section, {})[key] = value
        planted[f"{section}.{key}"] = value
    for route in ("/admin/api/settings/effective", "/admin/api/settings/effective/export"):
        reply = owner.get(route)
        assert reply.status_code == 200
        shown = [name for name, value in planted.items() if value in reply.text]
        assert shown == [], (route, shown)
        body = reply.json()["config"]
        assert body["llm"]["hidden_settings"] == 2 and body["images"]["signed_url_secret"] == {"configured": True}
        assert body["images"]["hidden_settings"] == 1 and "cache_dir" in body["images"]  # a classified key is still shown


# --- M2: the package index and the source of a package -------------------------------------------------------------

SOURCE_SECRETS = {"token": "ZPKGtoken-1", "password": "ZPKGpass-2", "auth": "ZPKGauth-3", "headers": {"Authorization": "Bearer ZPKGhdr-4"},
                  "username": "ZPKGuser-5", "ssh_key": "ZPKGssh-7"}
PACKAGE_YAML = "id: pkgsvc2\nname: Plain\nconfig:\n  protocol: openai-compatible\n  host: 192.168.1.80\n  port: 8000\n  models:\n    - {id: m, upstream_model: m}\n"


def _enable_with_secret_source(gateway, owner, monkeypatch, tmp_path):
    from tests.test_admin_owner_holes_round4 import _git_package

    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry({"schema_version": 2, "services": {}}), expected_generation=0, updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)
    source = {**_git_package(monkeypatch, tmp_path, PACKAGE_YAML, name="pkgextra"), "url": "https://u:ZPKGidx-6@git.invalid/pkgextra.git",
              "ref": "main", **SOURCE_SECRETS}
    return owner.post("/admin/api/extensions/enable", headers=_etag(owner), json={"source": source})


def test_a_package_source_keeps_no_credential_field_inline_and_no_output_shows_one_or_a_reference_id(gateway, host_isolated, monkeypatch, tmp_path):
    owner, _, _, admin = owner_and_admin(gateway)
    enabled = _enable_with_secret_source(gateway, owner, monkeypatch, tmp_path)
    assert enabled.status_code == 200, enabled.text
    values = ["ZPKGtoken-1", "ZPKGpass-2", "ZPKGauth-3", "ZPKGhdr-4", "ZPKGuser-5", "ZPKGidx-6", "ZPKGssh-7"]
    dump = _store_dump(gateway.path)
    assert [value for value in values if value in dump] == []
    outputs = [enabled, *(client.get(path) for path in ("/admin/api/extensions", "/admin/api/operator/overrides", "/admin/api/settings/effective",
                                                        "/admin/api/operator/service-registry", "/admin/api/config/export")
                          for client in (owner, admin) if not (client is admin and path in ("/admin/api/settings/effective", "/admin/api/config/export")))]
    for reply in outputs:
        assert [value for value in values if value in reply.text] == []
        assert "sref_" not in reply.text, "a reference id of the secret store is shown"
    source = owner.get("/admin/api/extensions").json()["extension_packages"]["packages"]["pkgextra"]["source"]
    assert sorted(source["extra_entries"]) == sorted(SOURCE_SECRETS) and all(item["configured"] for item in source["extra_entries"].values())


def test_the_overrides_route_answers_through_the_package_index_type(gateway, host_isolated, monkeypatch, tmp_path):
    owner, _, _, admin = owner_and_admin(gateway)
    assert _enable_with_secret_source(gateway, owner, monkeypatch, tmp_path).status_code == 200
    admin_store.put_operator_setting("some_future_setting", {"api_token": "ZFUTURE-TOKEN-1"}, path=gateway.path)
    index = json.loads(_store_dump_value(gateway.path, "extension_package_index"))
    index["packages"]["pkgextra"]["surprise"] = "ZSURPRISE-1"
    index["sources"][0]["surprise_field"] = "ZSURPRISE-2"
    admin_store.put_operator_setting("extension_package_index", index, path=gateway.path)
    for client in (owner, admin):
        reply = client.get("/admin/api/operator/overrides")
        assert reply.status_code == 200
        assert "ZSURPRISE" not in reply.text and "ZFUTURE" not in reply.text and "sref_" not in reply.text
        assert "pkgextra" in reply.json()["overrides"]["extension_package_index"]["packages"]


def _store_dump_value(path, key):
    with sqlite3.connect(path) as c:
        return c.execute("SELECT value_json FROM operator_settings WHERE key = ?", (key,)).fetchone()[0]


# --- M5: a registry read and written back unchanged, with a provider that holds a stored key -----------------------

def _clean_registry(gateway):
    current = admin_store.get_service_registry_with_generation(path=gateway.path)
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry({"schema_version": 2, "services": {}}),
        expected_generation=int(current["operator_settings_generation"]) if current else 0, updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_a_registry_read_and_written_back_unchanged_is_accepted_when_a_provider_holds_a_stored_key(gateway, host_isolated, who):
    from tests.support import canary

    owner, _, _, admin = owner_and_admin(gateway)
    _clean_registry(gateway)
    assert canary.plant_provider(owner).status_code == 201
    client = {"owner": owner, "admin": admin}[who]
    read = client.get(REGISTRY)
    provider = read.json()["service_registry"]["services"]["provider-a"]
    assert provider["config"]["api_key_secret_id"] == {"configured": True}, "the read shows only that a key is stored"
    stored_before = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["provider-a"]
    again = client.put(REGISTRY, headers={**csrf_for(client), "If-Match": read.headers["etag"]}, json=read.json()["service_registry"])
    assert again.status_code == 200, again.text
    stored_after = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["provider-a"]
    assert stored_after["config"]["api_key_secret_id"] == stored_before["config"]["api_key_secret_id"]
    assert stored_after["secret_refs"] == stored_before["secret_refs"]
    assert client.get(REGISTRY).json()["service_registry"]["services"]["provider-a"]["config"]["api_key_secret_id"] == {"configured": True}


def test_a_marker_that_is_not_what_the_read_showed_is_refused_not_taken_as_retain(gateway, host_isolated):
    from tests.support import canary

    owner, *_ = owner_and_admin(gateway)
    _clean_registry(gateway)
    assert canary.plant_provider(owner).status_code == 201
    read = owner.get(REGISTRY)
    stored_id = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["provider-a"]["config"]["api_key_secret_id"]
    for bad in ({"configured": False}, {"configured": True, "extra": "x"}, {}, "[redacted]", "<redacted>", ["configured"]):
        document = read.json()["service_registry"]
        document["services"]["provider-a"]["config"]["api_key_secret_id"] = bad
        answer = owner.put(REGISTRY, headers={**csrf_for(owner), "If-Match": read.headers["etag"]}, json=document)
        assert answer.status_code == 400, (bad, answer.text)
        assert "[redacted]" not in answer.text or bad == "[redacted]"
    after = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["provider-a"]["config"]["api_key_secret_id"]
    assert after == stored_id, "a refused write leaves the stored key reference as it was"


# --- M8: errors and reports that echoed what a caller typed or what a record held -----------------------------------

TYPED_ADDRESS = "http://uZ:ZPW-PROBE-1@h.invalid:9/v1?token=ZTOK-PROBE-2#ZFRAG-3"
PROBE_MARKERS = ("ZPW-PROBE-1", "ZTOK-PROBE-2", "ZFRAG-3", "ZSIG-PROBE-4")


@pytest.mark.parametrize("route", ["/admin/api/operator/services/test", "/admin/api/operator/services/detect-models"])
def test_a_connection_test_reports_the_address_without_the_credentials_it_was_given(gateway, host_isolated, monkeypatch, route):
    owner, _, _, admin = owner_and_admin(gateway)

    async def fails_quoting_the_address(host, port, path, **kwargs):
        # a transport error quotes the address it failed on, and the report carries that address too
        url = f"http://{host}:{port}{path}"
        return {"ok": False, "status": None, "error": f"cannot connect to {url}", "url": url, "body": None}

    monkeypatch.setattr(gateway.proxy, "probe_backend", fails_quoting_the_address)
    for client in (owner, admin):
        for body in ({"base_url": TYPED_ADDRESS, "health_endpoint": "/h?sig=ZSIG-PROBE-4"}, {"host": "uZ:ZPW-PROBE-1@h.invalid", "port": 9, "health_endpoint": "/h?sig=ZSIG-PROBE-4"}):
            reply = client.post(route, headers=csrf_for(client), json=body)
            assert reply.status_code == 200, reply.text
            assert [marker for marker in PROBE_MARKERS if marker in reply.text] == [], reply.text


def test_the_report_of_a_real_probe_shows_the_address_in_its_public_form(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    reply = owner.post("/admin/api/operator/services/test", headers=csrf_for(owner),
                       json={"base_url": TYPED_ADDRESS, "health_endpoint": "/h?sig=ZSIG-PROBE-4"})
    assert reply.status_code == 200
    probe = reply.json()["probe"]
    assert [marker for marker in PROBE_MARKERS if marker in reply.text] == []
    assert "?" not in probe["url"] and "@" not in probe["url"] and probe["url"].startswith("http://")


def test_a_refused_service_form_does_not_repeat_the_credentials_it_held(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    _clean_registry(gateway)
    forms = (
        {"name": "n1", "base_url": "http://uZ:ZPW-ADD-5@h.invalid:99999999/v1"},
        {"name": "n2", "base_url": "http://h.invalid:9/v1", "headers": [{"name": "X-A", "value": "ZHDR-ADD-6"}], "protocol": "not a plain name ZHDR-ADD-6"},
        {"name": "n3", "base_url": "http://uZ:ZPW-ADD-7@h.invalid:9/v1", "timeout": "ZPW-ADD-7 seconds"},
        {"name": "n4", "base_url": "http://h.invalid:9/v1?token=ZTOK-ADD-8", "priority": "ZTOK-ADD-8"},
    )
    for form in forms:
        reply = owner.post("/admin/api/operator/services", headers=_etag(owner), json=form)
        assert reply.status_code in (400, 409, 422, 403), (form, reply.status_code, reply.text)
        assert [m for m in ("ZPW-ADD-5", "ZHDR-ADD-6", "ZPW-ADD-7", "ZTOK-ADD-8") if m in reply.text] == [], reply.text


def test_a_failed_reload_answers_with_fixed_text_and_a_reload_answer_shows_hosts_without_credentials(gateway, host_isolated, monkeypatch):
    owner, *_ = owner_and_admin(gateway)
    proxy = gateway.proxy

    def fails(*args, **kwargs):
        raise RuntimeError("cannot build route for http://uZ:ZPW-RELOAD-1@h.invalid/x with token ZTOK-RELOAD-2")

    monkeypatch.setattr(proxy, "reload_runtime_config", fails)
    reply = owner.post("/admin/api/config/reload", headers=csrf_for(owner))
    assert reply.status_code == 500 and reply.json()["error"]["code"] == "reload_failed"
    assert "ZPW-RELOAD-1" not in reply.text and "ZTOK-RELOAD-2" not in reply.text and "h.invalid" not in reply.text

    hosts = {"LLM_BACKEND_HOSTS": {"a": "http://uZ:ZPW-RELOAD-3@10.0.0.5:8000/v1?key=ZTOK-RELOAD-4"}, "KOBOLD_HOST": "http://uZ:ZPW-RELOAD-3@10.0.0.6:5001",
             "TTS_HOST": None, "STT_HOST": "", "EMBEDDINGS_HOST": "http://10.0.0.7:1", "COMFY_HOST": "http://10.0.0.8:2"}
    monkeypatch.setattr(proxy, "reload_runtime_config", lambda: hosts)
    ok = owner.post("/admin/api/config/reload", headers=csrf_for(owner))
    assert ok.status_code == 200
    assert "ZPW-RELOAD-3" not in ok.text and "ZTOK-RELOAD-4" not in ok.text
    assert ok.json()["backends"]["embeddings"] == "http://10.0.0.7:1"


def test_a_reload_that_fails_reports_the_stale_problem_without_a_value_a_record_holds(gateway, host_isolated, monkeypatch):
    proxy = gateway.proxy
    registry = {"schema_version": 2, "services": {"held": {
        "id": "held", "name": "Held", "kind": "engine", "enabled": True, "capabilities": ["chat"],
        "config": {"host": "10.0.0.5", "port": 8000, "protocol": "openai-compatible", "headers": [{"name": "X-A", "value": "ZINLINE-HELD-9"}],
                   "models": "not a list ZINLINE-HELD-9"}}}}
    admin_store.write_service_registry_with_audit(
        registry, expected_generation=0, updated_by="test", audit_action="test", audit_actor_kind=None, audit_actor_id=None,
        audit_actor_label=None, audit_target_kind=None, audit_target_id=None, path=gateway.path)

    def fails(*args, **kwargs):
        raise ValueError("cannot apply: 'ZINLINE-HELD-9' at http://uZ:ZPW-STALE-1@h.invalid/x")

    monkeypatch.setattr(proxy, "_runtime_config_for_reload", fails)
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", None)
    with pytest.raises(ValueError):
        proxy.reload_runtime_config()
    problem = json.dumps(proxy.REGISTRY_APPLY_PROBLEM)
    assert proxy.REGISTRY_APPLY_PROBLEM["mode"] == "stale"
    assert "ZINLINE-HELD-9" not in problem and "ZPW-STALE-1" not in problem


# --- LOW findings of the review -----------------------------------------------------------------------------------------

def test_the_framework_422_of_an_auth_route_does_not_quote_the_body_it_could_not_read(gateway):
    """``POST /auth/api/invite`` answered 422 with the request body in ``detail.input``: the gate now gives it the fixed text."""
    owner, _, _, admin = owner_and_admin(gateway)
    anonymous = gateway.client()
    for client in (owner, admin, anonymous):
        for body in ({"carrier": "ZBODY-AUTH-1", "operation": "ZBODY-AUTH-1", "extra": "ZBODY-AUTH-1"}, ["ZBODY-AUTH-1"], "ZBODY-AUTH-1"):
            for route in ("/auth/api/invite", "/auth/api/invite/accept"):
                reply = client.post(route, headers=csrf_for(client) if client is not anonymous else {}, json=body)
                assert "ZBODY-AUTH-1" not in reply.text and "ZBODY-AUTH-1" not in str(dict(reply.headers)), (route, reply.status_code, reply.text)
                if reply.status_code == 422:
                    assert reply.json()["error"]["code"] == "invalid_request"


def test_a_header_named_like_a_credential_cannot_be_made_public_by_marking_it_so(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    _clean_registry(gateway)
    record = {**BASE, "id": "probe-a", "config": {"host": "10.0.0.5", "port": 8000, "protocol": "openai-compatible", "models": [{"id": "m", "upstream_model": "m"}],
                                                  "headers": [{"name": "Authorization", "value": "Bearer ZPUBLIC-AUTH-1", "public": True},
                                                              {"name": "X-Region", "value": "eu", "public": True}]}}
    reg = admin.get(REGISTRY).json()["service_registry"]
    reg["services"]["probe-a"] = record
    refused = admin.put(REGISTRY, headers=_etag(admin), json=reg)
    assert refused.status_code == 403 and "ZPUBLIC-AUTH-1" not in refused.text  # a credential is the owner's to add
    reg = owner.get(REGISTRY).json()["service_registry"]
    reg["services"]["probe-a"] = record
    accepted = owner.put(REGISTRY, headers=_etag(owner), json=reg)
    assert accepted.status_code == 200 and "ZPUBLIC-AUTH-1" not in accepted.text
    rows = {row["name"]: row for row in admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["probe-a"]["config"]["headers"]}
    assert "secret_ref" in rows["Authorization"] and "value" not in rows["Authorization"] and "public" not in rows["Authorization"]
    assert rows["X-Region"]["public"] is True and rows["X-Region"]["value"] == "eu"
    assert "ZPUBLIC-AUTH-1" not in _store_dump(gateway.path)
    command = owner.post("/admin/api/operator/services/probe-a/secret-entries", headers=_etag(owner), json={"commands": [
        {"carrier": "config.headers", "operation": "add", "name": "X-Api-Key", "value": "ZPUBLIC-AUTH-2", "public": True}]})
    assert command.status_code == 400 and command.json()["error"]["code"] == "credential_header_public" and "ZPUBLIC-AUTH-2" not in command.text


def test_an_argv_secret_replaced_by_the_command_keeps_its_entry_id(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    _clean_registry(gateway)
    record = {**BASE, "id": "probe-a", "config": {"host": "10.0.0.5", "port": 8000, "protocol": "openai-compatible", "models": [{"id": "m", "upstream_model": "m"}]},
              "process": {"driver": "process", "port": {"fixed": 8000}, "start": ["runner", "--model", "m"]}}
    reg = owner.get(REGISTRY).json()["service_registry"]
    reg["services"]["probe-a"] = record
    assert owner.put(REGISTRY, headers=_etag(owner), json=reg).status_code == 200
    first = owner.post("/admin/api/operator/services/probe-a/secret-entries", headers=_etag(owner), json={"commands": [
        {"carrier": "process.start", "operation": "set", "items": [{"literal": "runner"}, {"literal": "--model"}, {"literal": "m"}, {"secret": "ZARGV-OLD-1", "prefix": "--api-key="}], "clear": []}]})
    assert first.status_code == 200, first.text
    entry = owner.get(REGISTRY).json()["service_registry"]["services"]["probe-a"]["process"]["start"][-1]
    assert entry["configured"] and entry["prefix"] == "--api-key="
    replaced = owner.post("/admin/api/operator/services/probe-a/secret-entries", headers=_etag(owner), json={"commands": [
        {"carrier": "process.start", "operation": "set", "items": [{"literal": "runner"}, {"literal": "--model"}, {"literal": "m"}, {"replace": entry["id"], "secret": "ZARGV-NEW-2"}], "clear": []}]})
    assert replaced.status_code == 200, replaced.text
    after = owner.get(REGISTRY).json()["service_registry"]["services"]["probe-a"]["process"]["start"][-1]
    assert after["id"] == entry["id"] and after["prefix"] == "--api-key=" and after["configured"]
    dump = _store_dump(gateway.path)
    assert "ZARGV-OLD-1" not in dump and "ZARGV-NEW-2" not in dump and "ZARGV" not in replaced.text
    for bad in ({"replace": entry["id"]}, {"replace": "ent_" + "0" * 16, "secret": "x-new-secret"}, {"replace": entry["id"], "secret": "x-new", "literal": "y"}):
        refused = owner.post("/admin/api/operator/services/probe-a/secret-entries", headers=_etag(owner), json={"commands": [
            {"carrier": "process.start", "operation": "set", "items": [{"literal": "runner"}, bad], "clear": []}]})
        assert refused.status_code == 400, (bad, refused.text)


def test_the_engine_page_is_answered_through_the_projection_whatever_the_view_holds(gateway, host_isolated, monkeypatch):
    """Survivor R18 of the review's mutation run: replacing ``projected_response`` with a bare approved response in the engine
    route changed no test, because the view the route builds was already clean. A field the view builds that no type
    classifies must still not reach the answer."""
    from gateway import engine_admin

    owner, *_ = owner_and_admin(gateway)
    _clean_registry(gateway)
    reg = owner.get(REGISTRY).json()["service_registry"]
    reg["services"]["probe-a"] = rec(id="probe-a")
    assert owner.put(REGISTRY, headers=_etag(owner), json=reg).status_code == 200
    original = engine_admin.engine_view

    def view_with_a_surprise(*args, **kwargs):
        view = original(*args, **kwargs)
        view["unclassified_surprise"] = "ZR18-SURPRISE"
        return view

    monkeypatch.setattr(engine_admin, "engine_view", view_with_a_surprise)
    reply = owner.get("/admin/api/engines/probe-a")
    assert reply.status_code == 200 and "engine" in reply.json()
    assert "ZR18-SURPRISE" not in reply.text
