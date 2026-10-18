"""One bad stored record never takes the operator's pages or the request paths down with it.

Round 5 of W3. The re-review of round 4 found that boot served the stored services that validate, but the
operator pages (Overview, the service registry page, every add, edit and remove), the model alias lookup
that runs for any name with a slash, the bundled workflows and the capability catalog still read the
registry whole, so one bad record made them fail. These tests store a real invalid registry, boot the way
the gateway boots, and go through the app: no registry problem is faked.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import proxy
from gateway import admin_store, service_registry
from tests.registry_test_helpers import write_service_registry_for_test
from tests.support.gateway import gateway_client
from tests.test_frontend_mount_reload import live  # noqa: F401 - fixture
from tests.test_registry_apply_failure import (  # noqa: F401
    ACME,
    BAD,
    GOOD,
    OWNER,
    _boot,
    _owner_client,
    _service,
    _store_raw,
)

JUNK = "a hand edit that is not a service"


def _generation(db) -> int:
    return admin_store.get_service_registry_with_generation(path=db)["operator_settings_generation"]


def _stored(db) -> dict:
    return admin_store.get_service_registry_with_generation(path=db)["value"]


def _running_partial(live, monkeypatch, services: dict, **sections):
    """Store the registry as a hand edit left it, boot on it, and return an owner client of that gateway."""
    write_service_registry_for_test(
        {"schema_version": 1, "services": services, "protocol_frontends": sections.get("protocol_frontends", {}),
         "workflows": {}, "capabilities": {}},
        path=live,
    )
    config, problem = _boot(live)
    client = _owner_client(monkeypatch)  # clears the problem, so the boot's own is set after
    monkeypatch.setattr(proxy, "config", config)
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", problem, raising=False)
    return client


def _if_match(db) -> dict:
    return {"If-Match": f'"os-{_generation(db)}"'}


# --- the operator pages ---------------------------------------------------------------------------


def test_the_overview_answers_and_lists_the_excluded_record_on_a_partial_boot(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})

    response = client.get("/admin/api/overview")

    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert any(item["kind"] == "registry" and "bad-engine" in item["detail"] for item in body["attention"])
    [excluded] = body["registry_excluded"]
    assert excluded["id"] == "bad-engine" and excluded["section"] == "services" and excluded["error"]


def test_the_overview_lists_a_stored_record_left_out_even_when_the_boot_reported_nothing(live, monkeypatch):
    client = _owner_client(monkeypatch)
    write_service_registry_for_test(
        {"schema_version": 1, "services": {"good-engine": GOOD, "bad-engine": BAD}}, path=live)

    body = client.get("/admin/api/overview").json()

    assert [item["id"] for item in body["registry_excluded"]] == ["bad-engine"]
    assert any(item["kind"] == "registry" for item in body["attention"])


def test_the_service_registry_page_loads_and_shows_the_excluded_record_apart(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})

    response = client.get("/admin/api/operator/service-registry")

    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert set(body["service_registry"]["services"]) == {"good-engine"}
    [excluded] = body["excluded"]
    assert excluded["id"] == "bad-engine" and excluded["error"]
    assert excluded["record"]["config"]["models"] == BAD["config"]["models"]  # what the operator needs to fix it


def test_an_excluded_record_never_shows_a_credential_it_holds(live, monkeypatch):
    leaky = _service("leaky", models=["not a model record"], api_key="sk-live-should-never-be-shown")
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "leaky": leaky})

    response = client.get("/admin/api/operator/service-registry")

    assert response.status_code == 200
    assert "sk-live-should-never-be-shown" not in response.text
    assert [item["id"] for item in response.json()["excluded"]] == ["leaky"]


def test_a_full_save_with_a_new_engine_keeps_the_excluded_record_in_the_store(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})
    stored_before = _stored(live)["services"]["bad-engine"]
    registry = client.get("/admin/api/operator/service-registry").json()["service_registry"]
    registry["services"]["new-engine"] = _service("new-engine")

    response = client.put("/admin/api/operator/service-registry", json=registry, headers=_if_match(live))

    assert response.status_code == 200, response.text[:300]
    stored = _stored(live)["services"]
    assert stored["bad-engine"] == stored_before  # untouched, not rewritten and not dropped
    assert {"good-engine", "new-engine"} <= set(stored)
    assert [item["id"] for item in response.json()["excluded"]] == ["bad-engine"]
    assert "bad-engine" not in response.json()["service_registry"]["services"]


def test_adding_an_engine_keeps_the_excluded_record_and_applies_the_new_one_live(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})

    response = client.post("/admin/api/operator/services", json={"id": "new-engine", "name": "new-engine",
                                                                 "host": "10.0.0.6", "port": 8080}, headers=_if_match(live))

    assert response.status_code == 201, response.text[:300]
    assert "bad-engine" in _stored(live)["services"]
    assert {"good-engine", "new-engine"} <= set(proxy.config["backends"])  # the reload applied the servable part
    assert "bad-engine" not in proxy.config["backends"]
    assert proxy.REGISTRY_APPLY_PROBLEM["mode"] == "partial" and set(proxy.REGISTRY_APPLY_PROBLEM["left_out"]) == {"bad-engine"}


def test_editing_a_good_engine_keeps_the_excluded_record(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})
    stored_before = _stored(live)["services"]["bad-engine"]

    response = client.patch("/admin/api/operator/services/good-engine", json={"name": "Renamed"}, headers=_if_match(live))

    assert response.status_code == 200, response.text[:300]
    assert _stored(live)["services"]["bad-engine"] == stored_before
    assert _stored(live)["services"]["good-engine"]["name"] == "Renamed"


def test_the_operator_can_remove_the_excluded_service(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})

    response = client.delete("/admin/api/operator/services/bad-engine", headers=_if_match(live))

    assert response.status_code == 200, response.text[:300]
    assert set(_stored(live)["services"]) == {"good-engine"}
    assert response.json()["excluded"] == []
    assert proxy.REGISTRY_APPLY_PROBLEM is None  # the reload applied a registry with nothing left out


def test_removing_a_good_engine_does_not_remove_the_excluded_one(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})

    response = client.delete("/admin/api/operator/services/good-engine", headers=_if_match(live))

    assert response.status_code == 200, response.text[:300]
    assert set(_stored(live)["services"]) == {"bad-engine"}


def test_the_operator_can_fix_the_excluded_service_by_editing_it(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})
    models = [{"id": "m", "upstream_model": "m", "model_group": "m"}]

    response = client.patch("/admin/api/operator/services/bad-engine", json={"models": models}, headers=_if_match(live))

    assert response.status_code == 200, response.text[:300]
    assert response.json()["excluded"] == []
    assert "bad-engine" in response.json()["service_registry"]["services"]
    assert {"good-engine", "bad-engine"} <= set(proxy.config["backends"])
    assert proxy.REGISTRY_APPLY_PROBLEM is None


def test_an_edit_that_leaves_the_service_invalid_is_refused_and_the_store_is_unchanged(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})
    before = _stored(live)

    response = client.patch("/admin/api/operator/services/bad-engine", json={"models": "still not a list"},
                            headers=_if_match(live))

    assert response.status_code == 400, response.text[:300]
    assert _stored(live) == before


def test_adding_a_service_under_the_id_of_an_excluded_one_is_refused(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})
    before = _stored(live)

    response = client.post("/admin/api/operator/services", json={"id": "bad-engine", "name": "bad-engine",
                                                                 "host": "10.0.0.7", "port": 8080}, headers=_if_match(live))

    assert response.status_code == 409, response.text[:300]
    assert "left out" in response.json()["error"]["message"]
    assert _stored(live) == before  # nothing the operator did not mean to replace is replaced


def test_a_stored_save_never_writes_a_registry_without_a_record_it_was_not_asked_to_remove(live, monkeypatch):
    _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD, "junk-engine": JUNK})
    generation = _generation(live)
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["services"]["new-engine"] = _service("new-engine")

    proxy.ADMIN_STORE.update_service_registry(data=registry, actor=OWNER, expected_generation=generation)

    stored = _stored(live)["services"]
    assert stored["bad-engine"] == BAD and stored["junk-engine"] == JUNK


# --- a record that is not an object ---------------------------------------------------------------


def test_a_record_that_is_not_an_object_is_reported_as_left_out_at_boot_and_kept_by_the_next_save(live, monkeypatch, capsys):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "junk-engine": JUNK})

    assert proxy.REGISTRY_APPLY_PROBLEM["mode"] == "partial"
    assert set(proxy.REGISTRY_APPLY_PROBLEM["left_out"]) == {"junk-engine"}
    assert "junk-engine" in capsys.readouterr().out
    registry = client.get("/admin/api/operator/service-registry").json()
    assert [item["id"] for item in registry["excluded"]] == ["junk-engine"]

    registry["service_registry"]["services"]["new-engine"] = _service("new-engine")
    response = client.put("/admin/api/operator/service-registry", json=registry["service_registry"], headers=_if_match(live))

    assert response.status_code == 200, response.text[:300]
    assert _stored(live)["services"]["junk-engine"] == JUNK


def test_the_strict_read_refuses_a_record_that_is_not_an_object():
    with pytest.raises(ValueError, match="junk-engine"):
        service_registry.normalize_service_registry({"services": {"good-engine": GOOD, "junk-engine": JUNK}})


# --- the request paths ----------------------------------------------------------------------------


def _post(path: str, body: dict) -> tuple[int, dict]:
    async def call():
        async with gateway_client() as client:
            response = await client.post(path, json=body)
            try:
                return response.status_code, response.json()
            except ValueError:
                return response.status_code, {"text": response.text}

    return asyncio.run(call())


def test_a_model_name_with_a_slash_resolves_for_a_good_service_on_a_partial_boot(live):
    _store_raw(live, {"good-engine": GOOD, "bad-engine": BAD})
    _boot(live)

    for operation in ("speech", "transcriptions", "images", "videos", "chat"):
        try:
            proxy.resolve_model_for_operation(operation, "good-engine/some-model")
        except proxy.ModelCompatibilityError:
            pass  # a refusal in the gateway's own words is fine; a raw ValueError is the defect


@pytest.mark.parametrize("path, body", [
    ("/v1/audio/speech", {"input": "hi"}),
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/images/generations", {"prompt": "a cat"}),
    ("/v1/embeddings", {"input": "hi"}),
])
def test_a_request_for_a_good_service_is_not_a_server_error_on_a_partial_boot(live, monkeypatch, path, body):
    _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})

    status, payload = _post(path, {**body, "model": "good-engine/some-model"})

    assert status != 500, payload
    assert "ValueError" not in json.dumps(payload)


@pytest.mark.parametrize("path, body", [
    ("/v1/audio/speech", {"input": "hi"}),
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/images/generations", {"prompt": "a cat"}),
    ("/v1/embeddings", {"input": "hi"}),
])
def test_a_request_for_the_excluded_service_fails_with_a_typed_error_that_names_it(live, monkeypatch, path, body):
    _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})

    status, payload = _post(path, {**body, "model": "bad-engine/some-model"})

    assert status == 503, (status, payload)
    error = payload["error"]
    assert error["code"] == "model_not_deployed" and error["type"] == "service_unavailable"
    assert "bad-engine" in error["message"] and "left out" in error["message"]


# --- workflows and capabilities --------------------------------------------------------------------


def test_the_bundled_workflows_survive_one_bad_service(live):
    _store_raw(live, {"good-engine": GOOD})
    healthy = proxy._load_gateway_workflow_registry()
    _store_raw(live, {"good-engine": GOOD, "bad-engine": BAD})

    partial = proxy._load_gateway_workflow_registry()

    assert len(healthy._records) > 0
    assert len(partial._records) == len(healthy._records)


def test_the_bundled_workflows_survive_a_registry_that_cannot_be_read_at_all(live, monkeypatch):
    healthy = proxy._load_gateway_workflow_registry()

    def unreadable(**kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(service_registry, "load_service_registry", unreadable)

    assert len(proxy._load_gateway_workflow_registry()._records) == len(healthy._records) > 0


def test_a_bad_workflow_record_leaves_out_only_itself(live):
    write_service_registry_for_test(
        {"schema_version": 1, "services": {"good-engine": GOOD}, "workflows": {"junk-flow": "not an object"},
         "protocol_frontends": {}, "capabilities": {}}, path=live)
    registry, excluded = service_registry.read_registry(_stored(live))
    assert [(item.section, item.id) for item in excluded] == [("workflows", "junk-flow")]
    assert "good-engine" in registry["services"]  # the rest is read


def test_the_capability_catalog_survives_one_bad_service(live):
    _store_raw(live, {"good-engine": GOOD})
    healthy = proxy._gateway_capabilities_for_auth_context(proxy.NO_AUTH_CONTEXT)
    _store_raw(live, {"good-engine": GOOD, "bad-engine": BAD})

    partial = proxy._gateway_capabilities_for_auth_context(proxy.NO_AUTH_CONTEXT)

    assert len(healthy) > 0
    assert len(partial) == len(healthy)


# --- shapes that were read as empty and rewritten ---------------------------------------------------

INVALID_SHAPES = {
    "missing_host": {"id": "bad-engine", "kind": "engine", "enabled": True, "capabilities": ["chat"], "config": {"port": 8080}},
    "protocol_not_a_name": _service("bad-engine", protocol="not a name!"),
    "protocol_not_a_string": _service("bad-engine", protocol=["openai-compatible"]),
    "models_a_string": _service("bad-engine", models="one-model"),
    "lifecycle_not_an_object": {**_service("bad-engine"), "lifecycle": "start it"},
    "config_not_an_object": {"id": "bad-engine", "kind": "engine", "config": "host=10.0.0.9"},
    "not_an_object": "not a service",
}


@pytest.mark.parametrize("shape", sorted(INVALID_SHAPES))
def test_an_invalid_service_shape_is_left_out_with_an_error_and_never_rewritten(live, monkeypatch, shape):
    bad = INVALID_SHAPES[shape]
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": bad})

    assert proxy.REGISTRY_APPLY_PROBLEM["mode"] == "partial", shape
    assert set(proxy.REGISTRY_APPLY_PROBLEM["left_out"]) == {"bad-engine"}
    assert "good-engine" in proxy.config["backends"] and "bad-engine" not in proxy.config["backends"]
    registry = client.get("/admin/api/operator/service-registry").json()
    assert [item["id"] for item in registry["excluded"]] == ["bad-engine"]
    registry["service_registry"]["services"]["new-engine"] = _service("new-engine")
    assert client.put("/admin/api/operator/service-registry", json=registry["service_registry"],
                      headers=_if_match(live)).status_code == 200
    assert _stored(live)["services"]["bad-engine"] == bad  # the stored value is exactly what was there


def test_a_service_with_a_plain_protocol_name_and_an_address_still_validates():
    record = service_registry.normalize_service_record("ok", _service("ok", protocol="openai-compatible", models={"m": {}}))
    assert record["config"]["protocol"] == "openai-compatible"


# --- the log of what boot left out --------------------------------------------------------------------


def test_boot_logs_a_line_for_each_left_out_service_apart_from_the_cause(live, capsys):
    _store_raw(live, {"good-engine": GOOD, "bad-engine": BAD})

    _boot(live)

    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("[proxy] ERROR: service 'bad-engine' left out of the running config: ") for line in lines)


def test_a_stored_registry_that_cannot_be_read_stops_a_boot_that_reported_nothing(live, monkeypatch):
    def unreadable(**kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(service_registry, "load_service_registry", unreadable)
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", None, raising=False)

    with pytest.raises(RuntimeError, match="database is locked"):
        proxy._stored_registry_for_boot()


def test_a_stored_registry_that_cannot_be_read_mounts_only_the_bundled_routes_after_a_reported_boot(live, monkeypatch):
    def unreadable(**kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(service_registry, "load_service_registry", unreadable)
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", {"mode": "empty", "left_out": {}, "served": 0, "error": "x"}, raising=False)

    assert proxy._stored_registry_for_boot() is None
