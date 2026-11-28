"""Ordered model edges are registry data, independent of readiness and brands."""
from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from gateway import admin_store, registry_snapshot as rs, service_registry as sr
from gateway.alias_policy import resolve_request_target
from gateway.model_routes import ModelRouteError, RouteEdge, compile_model_routes


def graph():
    def service(sid, rows):
        return {"id": sid, "enabled": True, "capabilities": ["openai"], "config": {
            "protocol": "openai-compatible", "base_url": "http://127.0.0.1:18081/v1", "models": rows}}
    return {"anchor": service("anchor", [
        {"id": "logical", "upstream_model": "wire-a", "routes": [
            {"id": "anchor/logical", "service_id": "anchor", "model_id": "logical"},
            {"id": "cloud/wire", "service_id": "cloud", "model_id": "other"},
            {"id": "broker/wire", "service_id": "broker", "model_id": "vendor/exact"}]},
        {"id": "route-fast", "base_model_id": "logical", "upstream_model": "wire-a",
         "policy_overlay": [{"kind": "default-params", "set": {"max_tokens": 9}}]}]),
        "cloud": service("cloud", [{"id": "other", "upstream_model": "wire-b", "execution_only": True}]),
        "broker": service("broker", [{"id": "vendor/exact", "upstream_model": "wire-c", "execution_only": True}])}


def runtime(services):
    return {"backends": sr.runtime_backends({"services": services})}


def snap(services, generation=1):
    return rs.build({"services": services}, rs.BundledRelease({}), generation=generation)


def test_missing_routes_preserves_singleton_and_legacy_resolution_bytes():
    services = graph()
    del services["anchor"]["config"]["models"][0]["routes"]
    target = resolve_request_target("route-fast", runtime_config=runtime(services))
    assert target.routes == (RouteEdge("anchor/logical", "anchor", "logical"),)
    # All pre-slice fields retain the exact JSON representation of the parent.
    assert json.dumps({key: getattr(target, key) for key in (
        "requested_model", "logical_service_id", "base_model_id", "base_transform_specs", "alias_overlay_specs")}, sort_keys=True) == (
        '{"alias_overlay_specs": [{"kind": "default-params", "set": {"max_tokens": 9}}], '
        '"base_model_id": "logical", "base_transform_specs": [], "logical_service_id": "anchor", "requested_model": "route-fast"}')


def test_three_routes_and_overlay_resolve_base_first():
    services = graph()
    compiled = snap(services).model_routes
    base = resolve_request_target("logical", runtime_config=runtime(services), route_index=compiled)
    alias = resolve_request_target("route-fast", runtime_config=runtime(services), route_index=compiled)
    assert [edge.target for edge in base.routes] == [("anchor", "logical"), ("cloud", "other"), ("broker", "vendor/exact")]
    assert alias.routes is base.routes
    assert alias.requested_model == "route-fast" and alias.base_model_id == "logical"
    assert alias.alias_overlay_specs == ({"kind": "default-params", "set": {"max_tokens": 9}},)
    with pytest.raises(FrozenInstanceError):
        base.routes[0].service_id = "changed"
    with pytest.raises(TypeError):
        compiled.pins["new"] = base.routes[0]


def test_execution_only_skips_unscoped_but_scoped_lookup_finds_exact_record():
    services = graph()
    assert resolve_request_target("other", runtime_config=runtime(services)) is None
    target = resolve_request_target("other", runtime_config=runtime(services), service_id="cloud")
    assert target.base_model_id == "other" and target.logical_service_id == "cloud"
    assert not snap(services).names.owners("other")
    assert "other" not in {item["id"] for cap in __import__("gateway.admin_catalog", fromlist=["access_plan_editor_contract"]).access_plan_editor_contract({"services": services})["capabilities"] for item in cap["items"]}


def test_each_pin_resolves_one_edge_and_retains_logical_owner():
    services = graph()
    for pin, edge in snap(services).model_routes.pins.items():
        resolved = resolve_request_target(pin, runtime_config=runtime(services))
        assert resolved.routes == (edge,)
        assert (resolved.logical_service_id, resolved.base_model_id) == ("anchor", "logical")
    assert resolve_request_target(" cloud/wire ", runtime_config=runtime(services)) is None


@pytest.mark.parametrize("change,code", [
    (lambda s, r: r["routes"].append(dict(r["routes"][0])), "duplicate_route_id"),
    (lambda s, r: r["routes"][1].update(service_id="absent"), "missing_route_service"),
    (lambda s, r: r["routes"][1].update(model_id="absent"), "missing_route_model"),
    (lambda s, r: r["routes"][1].update(model_id=" other "), "missing_route_model"),
    (lambda s, r: r["routes"][1].update(model_id="OTHER"), "missing_route_model"),
    (lambda s, r: r["routes"][1].update(service_id="anchor", model_id="route-fast"), "route_target_alias"),
    (lambda s, r: s["cloud"]["config"]["models"][0].update(routes=[{"id": "nest", "service_id": "cloud", "model_id": "other"}]), "nested_routes"),
    (lambda s, r: r["routes"].append({"id": "again", "service_id": "anchor", "model_id": "logical"}), "duplicate_route_target"),
    (lambda s, r: r["routes"].append({"id": "cloud/again", "service_id": "cloud", "model_id": "other"}), "duplicate_route_target"),
    (lambda s, r: r.update(routes=[]), "empty_routes"),
    (lambda s, r: r.update(routes=None), "invalid_routes"),
    (lambda s, r: r["routes"][0].update(unknown="no"), "unknown_route_field"),
    (lambda s, r: r["routes"][0].update(model_id=""), "invalid_route"),
    (lambda s, r: r.update(execution_only="true"), "invalid_execution_only"),
    (lambda s, r: s["anchor"]["config"]["models"][1].update(routes=r["routes"]), "route_on_alias"),
    (lambda s, r: r["routes"][1].update(id="route-fast"), "pin_public_name_collision"),
    (lambda s, r: s["cloud"]["config"]["models"].append({"id": "logical"}), "duplicate_public_name"),
])
def test_named_validation_errors(change, code):
    services = graph()
    change(services, services["anchor"]["config"]["models"][0])
    with pytest.raises(ModelRouteError) as error:
        compile_model_routes(services)
    assert error.value.code == code
    with pytest.raises(rs.SnapshotBuildError) as error:
        snap(services)
    assert error.value.problems[0].code == code


def test_pin_can_coincide_with_identical_target_publication_only():
    services = graph()
    target = services["cloud"]["config"]["models"][0]
    target.update(execution_only=False, model_group="cloud/wire")
    assert snap(services).model_routes.pins["cloud/wire"].target == ("cloud", "other")
    resolved = resolve_request_target("cloud/wire", runtime_config=runtime(services))
    assert resolved.routes == (RouteEdge("cloud/wire", "cloud", "other"),)
    assert resolved.logical_service_id == "anchor"
    target["model_group"] = "broker/wire"
    with pytest.raises(ModelRouteError, match="pin_public_name_collision"):
        compile_model_routes(services)


def test_disconnected_or_paused_logical_owner_keeps_all_edges():
    services = graph()
    original = snap(services).model_routes
    services["anchor"]["enabled"] = False
    services["anchor"]["config"].update(credential_source={"kind": "connection", "connection_id": "disconnected-owner"})
    updated = snap(services)
    assert updated.model_routes.by_model == original.by_model
    target = resolve_request_target("logical", runtime_config=runtime(services))
    assert len(target.routes) == 3 and updated.names.owners("logical")


def test_old_request_keeps_immutable_edges_across_swap_and_failed_build():
    services = graph()
    old = snap(services)
    provider = rs.Provider(old)
    request = SimpleNamespace(state=SimpleNamespace())
    assert rs.snapshot_of(request, provider) is old
    routes = services["anchor"]["config"]["models"][0]["routes"]
    routes[1], routes[2] = routes[2], routes[1]
    new = snap(services, 2)
    provider.swap(new)
    assert rs.snapshot_of(request, provider) is old
    assert old.model_routes.by_model[("anchor", "logical")][1].service_id == "cloud"
    assert new.model_routes.by_model[("anchor", "logical")][1].service_id == "broker"
    services["anchor"]["config"]["models"][0]["routes"] = []
    with pytest.raises(rs.SnapshotBuildError):
        provider.swap(snap(services, 3))
    assert provider.current() is new


def test_replacement_service_and_model_ids_need_no_provider_code():
    services = graph()
    renamed = {f"new-{sid}": {**copy.deepcopy(row), "id": f"new-{sid}"} for sid, row in services.items()}
    for row in renamed.values():
        for record in row["config"]["models"]:
            record["id"] = f"x/{record['id']}"
            if record.get("base_model_id"):
                record["base_model_id"] = f"x/{record['base_model_id']}"
            for edge in record.get("routes", []):
                edge.update(id=f"pin/{edge['id']}", service_id=f"new-{edge['service_id']}", model_id=f"x/{edge['model_id']}")
    resolved = resolve_request_target("x/route-fast", runtime_config=runtime(renamed))
    assert [edge.service_id for edge in resolved.routes] == ["new-anchor", "new-cloud", "new-broker"]


def test_registry_form_save_export_import_round_trip_and_delete_refusal(tmp_path):
    services = graph()
    for row in services.values():
        raw = sr.service_form_to_raw({**row["config"], "capabilities": row["capabilities"]})
        assert raw["models"] == row["config"]["models"] or all(
            all(raw["models"][i].get(k) == v for k, v in model.items()) for i, model in enumerate(row["config"]["models"]))
    document = sr.normalize_service_registry({"services": services})
    source, destination = tmp_path / "source.db", tmp_path / "destination.db"
    for path in (source, destination):
        admin_store.init_db(path)
    admin_store.write_service_registry(document, expected_generation=0, path=source)
    exported = json.loads(admin_store.get_service_registry_json_with_generation(path=source)["value_json"])
    imported = sr.merge_imported_records({}, services=exported["services"])
    admin_store.write_service_registry(imported, expected_generation=0, path=destination)
    restored = sr.load_service_registry(path=destination)
    assert compile_model_routes(restored["services"]).by_model == compile_model_routes(document["services"]).by_model
    removed = copy.deepcopy(restored)
    del removed["services"]["cloud"]
    with pytest.raises(ModelRouteError, match="service_referenced_by_route"):
        admin_store.write_service_registry(removed, expected_generation=1, path=destination)
    assert admin_store.get_service_registry_json_with_generation(path=destination)["operator_settings_generation"] == 1


def test_bad_route_import_refuses_unknown_fields_instead_of_dropping_them():
    services = graph()
    services["anchor"]["config"]["models"][0]["routes"][0]["typo"] = "bad"
    with pytest.raises(ModelRouteError, match="unknown_route_field"):
        sr.merge_imported_records({}, services=services)


def test_litellm_private_deployments_are_exact_and_public_projection_hides_targets():
    from gateway import litellm_substrate as ls
    services = graph()
    compiled = ls.compile_litellm_substrate_config(services)
    names = [row["model_name"] for row in compiled["model_list"]]
    assert RouteEdge("", "cloud", "other").deployment_id in names
    assert RouteEdge("", "broker", "vendor/exact").deployment_id in names
    assert len(names) == len(set(names))
    assert "other" not in ls.litellm_public_model_records({"services": services})
    assert "vendor/exact" not in ls.litellm_public_model_service_ids({"services": services})
    assert [row["litellm_params"]["model"] for row in compiled["model_list"]] == ["openai/wire-a", "openai/wire-a", "openai/wire-c", "openai/wire-b"]


def test_provider_form_keeps_both_fields_and_validates_closed_edges():
    from gateway.provider_configuration import model_records_from_body
    row = {"id": "paid/logical", "upstream_model": "wire", "execution_only": False, "routes": [
        {"id": "paid/logical", "service_id": "paid", "model_id": "paid/logical"}]}
    assert model_records_from_body({"models": [row]}, "paid")[0] == {**row, "model_group": row["id"]}
    row["routes"][0]["secret"] = "refused"
    with pytest.raises(ModelRouteError, match="unknown_route_field"):
        model_records_from_body({"models": [row]}, "paid")


def test_remote_catalog_hides_execution_only_but_scoped_resolver_sees_it():
    from gateway.provider_model_catalog import remote_provider_model_capabilities, remote_provider_model_metadata
    services = graph()
    service = services["cloud"]
    service["config"].update(remote_provider=True, provider_id="cloud", protocol="openai-compatible")
    assert remote_provider_model_capabilities("cloud", service) == []
    assert remote_provider_model_metadata("cloud", service) == {}
    assert resolve_request_target("other", runtime_config=runtime(services), service_id="cloud").base_model_id == "other"


def test_local_catalog_hides_execution_only_but_scoped_resolver_sees_it():
    from gateway.models_catalog import registered_model_capabilities, _service_model_metadata
    services = graph()
    services["cloud"]["capabilities"] = ["chat", "completions"]
    assert registered_model_capabilities({"cloud": services["cloud"]}) == []
    assert _service_model_metadata({"cloud": services["cloud"]}) == {}
    assert resolve_request_target("other", runtime_config=runtime(services), service_id="cloud").base_model_id == "other"


def test_owner_provider_response_carries_only_route_fields():
    from gateway.provider_admin import _provider_model_summaries
    services = graph()
    services["anchor"]["config"].update(remote_provider=True, provider_id="anchor", api_key_env="TEST_ROUTE_KEY", credential_origin="http://127.0.0.1:18081")
    model = services["anchor"]["config"]["models"][0]
    # A valid external reference is allowed in stored config, never in this summary.
    model["api_key_env"] = "TEST_UNUSED_REFERENCE"
    response = _provider_model_summaries({"services": services})["anchor"][0]
    assert response["routes"] == model["routes"]
    assert response["model"] == "logical"
    assert "api_key_env" not in response


def test_bad_stored_routes_cannot_be_hidden_by_tolerant_record_recovery(tmp_path):
    import sqlite3
    path = tmp_path / "synthetic.db"
    admin_store.init_db(path)
    document = {"services": graph()}
    admin_store.write_service_registry(document, expected_generation=0, path=path)
    old = rs.load_current(path)
    provider = rs.Provider(old)
    stored = json.loads(admin_store.get_service_registry_json_with_generation(path=path)["value_json"])
    stored["services"]["anchor"]["config"]["models"][0]["routes"] = []
    with sqlite3.connect(path) as db:
        db.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(stored),))
    with pytest.raises(rs.SnapshotBuildError, match="empty_routes"):
        provider.swap(rs.load_current(path))
    assert provider.current() is old


def test_duplicate_pin_id_for_different_scoped_singletons_is_named():
    services = {"a/b": {"config": {"models": [{"id": "c"}]}},
                "a": {"config": {"models": [{"id": "b/c"}]}}}
    with pytest.raises(ModelRouteError, match="duplicate_pin_id"):
        compile_model_routes(services)


def test_execution_only_public_alias_is_ignored_for_public_claim_validation():
    services = graph()
    services["cloud"]["config"]["models"].append({"id": "route-fast", "base_model_id": "other", "execution_only": True})
    normalized = sr.normalize_service_registry({"services": services})
    assert resolve_request_target("route-fast", runtime_config=runtime(normalized["services"])).logical_service_id == "anchor"
    assert resolve_request_target("route-fast", runtime_config=runtime(normalized["services"]), service_id="cloud").logical_service_id == "cloud"


def test_execution_only_does_not_publish_a_derived_speech_name():
    from gateway.derived_engines import derive_engines
    services = graph()
    services["cloud"]["capabilities"] = ["speech"]
    assert derive_engines(services, "speech") == {}


def test_route_projection_has_closed_public_edge_shape():
    from gateway.projection import project
    services = graph()
    row = services["anchor"]["config"]["models"][0]
    row["routes"][0]["token"] = "never-project"
    value = project("ServiceRecord", services["anchor"]).tree
    assert "never-project" not in json.dumps(value)
    assert value["config"]["models"][0]["routes"][0] == {"id": "anchor/logical", "service_id": "anchor", "model_id": "logical", "hidden_settings": 1}


def test_runtime_reload_failure_keeps_published_snapshot_and_config(tmp_path, monkeypatch):
    import sqlite3
    import proxy
    path = tmp_path / "reload.db"
    admin_store.init_db(path)
    document = {"services": graph()}
    admin_store.write_service_registry(document, expected_generation=0, path=path)
    stored = json.loads(admin_store.get_service_registry_json_with_generation(path=path)["value_json"])
    stored["services"]["anchor"]["config"]["models"][0]["routes"][1]["service_id"] = "missing"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(stored),))
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", path)
    before = proxy.GATEWAY_REGISTRY_PROVIDER.current()
    config_before = proxy.config
    # Exercise the real publication boundary; preparatory derivations stay value-only.
    monkeypatch.setattr(proxy, "_derive_backend_constants", lambda *a, **k: {"_GRAPH_BINDING": "unused"})
    monkeypatch.setattr(proxy, "_load_gateway_workflow_registry", lambda **k: None)
    monkeypatch.setattr(proxy, "_load_image_graph_roles", lambda *a: {})
    with pytest.raises(rs.SnapshotBuildError, match="missing_route_service"):
        proxy._publish_runtime_config({"backends": runtime(graph())["backends"]})
    assert proxy.GATEWAY_REGISTRY_PROVIDER.current() is before
    assert proxy.config is config_before


def test_dynamic_alias_and_workflow_names_cannot_be_claimed_by_a_pin(monkeypatch):
    services = graph()
    services["anchor"]["config"]["models"][0]["routes"][1]["id"] = "external-alias"
    aliases = {"chat": {"stable": {"external-alias": "logical"}}}
    with pytest.raises(rs.SnapshotBuildError, match="pin_public_name_collision"):
        rs.build({"services": services}, rs.BundledRelease({}), generation=1, model_aliases=aliases)
    monkeypatch.setattr(sr, "_dynamic_model_aliases", lambda: aliases)
    with pytest.raises(ModelRouteError, match="pin_public_name_collision"):
        sr.refuse_new_name_clashes({}, {"services": services})
    # The same compiler rejects any non-model public namespace reserved by its caller.
    with pytest.raises(ModelRouteError, match="pin_public_name_collision"):
        compile_model_routes(services, reserved_names=["external-alias"])


def test_exact_route_refs_also_resolve_against_frozen_snapshot_records():
    snapshot = snap(graph())
    config = {"backends": {sid: row["config"] for sid, row in snapshot.services.items()}}
    target = resolve_request_target("route-fast", runtime_config=config, route_index=snapshot.model_routes)
    assert target.routes is snapshot.model_routes.by_model[("anchor", "logical")]
    target.alias_overlay_specs[0]["set"]["max_tokens"] = 50
    assert snapshot.services["anchor"]["config"]["models"][1]["policy_overlay"][0]["set"]["max_tokens"] == 9


def test_same_model_id_on_three_services_has_one_public_owner():
    services = graph()
    for sid in ("cloud", "broker"):
        services[sid]["config"]["models"][0]["id"] = "logical"
    for edge in services["anchor"]["config"]["models"][0]["routes"]:
        edge["model_id"] = "logical"
    target = resolve_request_target("logical", runtime_config=runtime(services))
    assert len(target.routes) == 3 and target.logical_service_id == "anchor"
    assert resolve_request_target("logical", runtime_config=runtime(services), service_id="broker").logical_service_id == "broker"


def test_private_deployment_name_cannot_be_shadowed_by_a_public_record():
    from gateway.litellm_substrate import compile_litellm_substrate_config
    services = graph()
    services["cloud"]["config"]["models"].append({"id": RouteEdge("", "anchor", "logical").deployment_id, "upstream_model": "different-wire"})
    with pytest.raises(ModelRouteError, match="private_deployment_collision"):
        compile_litellm_substrate_config(services)


@pytest.mark.parametrize("order", [(1, 2, 0), (1, 0, 2)])
def test_the_self_edge_may_stand_anywhere_in_its_own_group(order):
    services = graph()
    record = services["anchor"]["config"]["models"][0]
    record["routes"] = [record["routes"][i] for i in order]
    compiled = snap(services).model_routes
    expected = [[("anchor", "logical"), ("cloud", "other"), ("broker", "vendor/exact")][i] for i in order]
    assert [edge.target for edge in compiled.by_model[("anchor", "logical")]] == expected
    assert [edge.target for edge in resolve_request_target("logical", runtime_config=runtime(services),
                                                           route_index=compiled).routes] == expected
