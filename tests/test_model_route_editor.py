"""Owner route edits promote discovery into the existing registry atomically."""
import copy
import pytest
from gateway import model_route_editor as editor, registry_snapshot as rs, admin_store, service_registry


def fixture():
    document = {"schema_version": 1, "services": {
        "plan": {"id": "plan", "enabled": True, "config": {
            "credential_source": {"kind": "connection", "connection_id": "fixture-owner"},
            "request_protocol": "openai-responses-plan", "billing_mode": "plan"}},
        "paid": {"id": "paid", "enabled": True, "config": {"remote_provider": True,
            "provider_id": "fixture-paid", "api_key_env": "AZIZAM_FIXTURE_PAID_KEY",
            "base_url": "https://example.test/v1", "models": [
            {"id": "wire", "upstream_model": "exact-wire", "execution_only": True}]}}}}
    snapshot = rs.build(document, rs.BundledRelease({}), generation=1,
                        connection_models={"fixture-owner": {"models": {"logical": ["low"]}}})
    return document, snapshot


def test_discovery_materialization_preserves_execution_and_pins(tmp_path):
    document, snapshot = fixture()
    pairs = [("paid", "wire"), ("plan", "logical")]
    candidate = editor.draft(document, snapshot.services, ("plan", "logical"), pairs)
    built = rs.build(candidate, rs.BundledRelease({}), generation=2)
    chain = built.model_routes.by_model[("plan", "logical")]
    assert [edge.target for edge in chain] == pairs
    assert chain[1].id == "plan/logical"
    assert candidate["services"]["plan"]["config"]["models"][0]["reasoning_efforts"] == ["low"]
    assert candidate["services"]["paid"] == document["services"]["paid"]
    path = tmp_path / "registry.db"
    admin_store.init_db(path)
    admin_store.write_service_registry(document, expected_generation=0, path=path)
    admin_store.write_service_registry(candidate, expected_generation=1, path=path)
    with pytest.raises(admin_store.OperatorSettingsGenerationConflict):
        admin_store.write_service_registry(document, expected_generation=1, path=path)
    restored = service_registry.load_service_registry(path=path)
    assert rs.build(restored, rs.BundledRelease({}), generation=2).model_routes.by_model[("plan", "logical")] == chain


def test_invalid_selection_leaves_document_unchanged():
    document, snapshot = fixture()
    before = copy.deepcopy(document)
    with pytest.raises(ValueError):
        editor.draft(document, snapshot.services, ("plan", "logical"), [("missing", "wire")])
    assert document == before


def test_owner_route_api_saves_once_and_refuses_stale_and_nonowner(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from gateway.admin_api import AdminContext, create_admin_router
    from gateway.admin_runtime import AdminSQLiteStore
    document, snapshot = fixture()
    from gateway import subscription_store
    monkeypatch.setattr(subscription_store, "cached_model_discovery", lambda path: {
        "fixture-owner": {"status": "connected", "models": {"logical": ["low"]}}})
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry(document, expected_generation=0, path=path)
    store = AdminSQLiteStore(path)
    monkeypatch.setattr(store, "write_litellm_config", lambda value: None)
    applied = []
    store.on_service_registry_changed = lambda: applied.append(True)
    app = FastAPI()
    @app.middleware("http")
    async def held(request, call_next):
        current, generation = store.get_service_registry_snapshot()
        request.state.registry_snapshot = rs.build(current, rs.BundledRelease({}), generation=generation,
            connection_models=subscription_store.cached_model_discovery(path))
        return await call_next(request)
    app.include_router(create_admin_router(store, auth=lambda request: AdminContext(
        subject="fixture-owner", roles=frozenset(["admin", "owner"] if request.headers.get("x-owner") == "yes" else ["admin"]))))
    with TestClient(app) as client:
        url = "/admin/api/operator/model-routes"
        assert client.get(url, params={"service_id": "plan", "model_id": "logical"}).status_code == 403
        read = client.get(url, params={"service_id": "plan", "model_id": "logical"}, headers={"x-owner": "yes"})
        assert read.status_code == 200, read.text
        response = client.put(url, headers={"x-owner": "yes", "If-Match": read.headers["etag"]}, json={
            "service_id": "plan", "model_id": "logical", "targets": [
                {"service_id": "paid", "model_id": "wire"}, {"service_id": "plan", "model_id": "logical"}]})
        assert response.status_code == 200, response.text
        assert [row["service_id"] for row in response.json()["route_editor"]["routes"]] == ["paid", "plan"]
        assert applied == [True]
        valid = {"service_id": "plan", "model_id": "logical", "targets": [
            {"service_id": "plan", "model_id": "logical"}, {"service_id": "paid", "model_id": "wire"}]}
        stale = client.put(url, headers={"x-owner": "yes", "If-Match": read.headers["etag"]}, json=valid)
        assert stale.status_code == 412
        assert stale.json()["error"]["code"] == "generation_mismatch"
        missing = client.put(url, headers={"x-owner": "yes"}, json=valid)
        assert missing.status_code == 412
        assert missing.json()["error"]["code"] == "generation_mismatch"
        assert applied == [True]


def test_two_discovered_connections_with_same_slug_freeze_exact_targets():
    document, _ = fixture()
    document["services"]["second"] = {"id": "second", "enabled": True, "config": {
        "credential_source": {"kind": "connection", "connection_id": "second-owner"}, "billing_mode": "plan"}}
    snapshot = rs.build(document, rs.BundledRelease({}), generation=1, connection_models={
        "fixture-owner": {"models": {"logical": ["low"]}}, "second-owner": {"models": {"logical": ["high"]}}})
    candidate = editor.draft(document, snapshot.services, ("plan", "logical"), [("second", "logical"), ("plan", "logical")])
    built = rs.build(candidate, rs.BundledRelease({}), generation=2)
    assert [edge.target for edge in built.model_routes.by_model[("plan", "logical")]] == [("second", "logical"), ("plan", "logical")]
    assert editor.records(candidate["services"])[("second", "logical")]["execution_only"] is True
    assert editor.records(candidate["services"])[("second", "logical")]["reasoning_efforts"] == ["high"]
    # Discovery removal preserves the explicit mapping and saved pin identities.
    again = editor.draft(candidate, built.services, ("plan", "logical"), [("plan", "logical"), ("second", "logical")])
    assert [edge["id"] for edge in editor.records(again["services"])[("plan", "logical")]["routes"]] == [
        edge.id for edge in reversed(built.model_routes.by_model[("plan", "logical")])]


def test_unavailable_discovery_is_not_newly_promoted_and_nested_owner_is_not_stripped():
    document, snapshot = fixture()
    services = editor._copy(snapshot.services)
    services["plan"]["enabled"] = False
    with pytest.raises(ValueError, match="unavailable"):
        editor.draft(document, services, ("plan", "logical"), [("plan", "logical")])
    document, snapshot = fixture()
    first = editor.draft(document, snapshot.services, ("plan", "logical"), [("paid", "wire"), ("plan", "logical")])
    services = rs.build(first, rs.BundledRelease({}), generation=2).services
    with pytest.raises(ValueError):
        editor.draft(first, services, ("paid", "wire"), [("plan", "logical")])


def test_materialized_discovery_removal_and_disconnect_are_visible():
    document, snapshot = fixture()
    candidate = editor.draft(document, snapshot.services, ("plan", "logical"), [("plan", "logical")])
    services = rs.build(candidate, rs.BundledRelease({}), generation=2).services
    for discovery in ({}, {"fixture-owner": {"status": "disconnected", "models": {"logical": ["low"]}}},
                      {"fixture-owner": {"status": "connected", "models": {"logical": ["high"]}}}):
        assert editor.view(services, ("plan", "logical"), 2, discoveries=discovery)["routes"][0]["available"] is False
    assert editor.view(services, ("plan", "logical"), 2, discoveries={
        "fixture-owner": {"status": "connected", "models": {"logical": ["low"]}}})["routes"][0]["available"] is True


def test_disabled_stored_route_is_refused_before_reuse():
    document, snapshot = fixture()
    document["services"]["paid"]["enabled"] = False
    services = editor._copy(snapshot.services)
    services["paid"]["enabled"] = False
    with pytest.raises(ValueError, match="unavailable"):
        editor.draft(document, services, ("plan", "logical"), [("paid", "wire"), ("plan", "logical")])
