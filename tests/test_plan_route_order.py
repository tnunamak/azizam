"""The owner sees, for each plan model, the ordered steps a plain-name request walks: the router's own chain."""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import model_routes, registry_snapshot, subscription_api
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.subscription_oauth import plain_name_chain
from tests.support.subscription_fakes import Upstream, connect, make_service
from tests.test_model_route_dispatch import graph


def build(services, **kwargs):
    return registry_snapshot.build({"services": services}, registry_snapshot.BundledRelease({}), generation=1, **kwargs)


def plan_only():
    return {"plan-source": {"id": "plan-source", "kind": "provider", "enabled": True, "config": {
        "credential_source": {"kind": "connection", "connection_id": "test-owner"},
        "request_protocol": "openai-responses-plan", "billing_mode": "plan", "models": []}}}


def shape(chain):
    return [(step["kind"], step["route_id"], step["paid_fallback"]) for step in chain["steps"]]


def test_plan_model_without_a_paid_route_has_one_plan_step():
    snapshot = build(plan_only(), connection_models={"test-owner": {"models": {"alpha": ["low"]}}})
    assert plain_name_chain(snapshot, "alpha") == {"slug": "alpha", "direct": False, "steps": [
        {"route_id": "plan-source/alpha", "kind": "plan", "service_id": "plan-source", "name": "plan-source",
         "paid_fallback": False, "connection_id": "test-owner"}]}


def test_plan_model_with_a_paid_route_lists_the_paid_step_as_a_fallback():
    chain = plain_name_chain(build(graph()), "logical-test")
    assert chain["direct"] is False
    assert shape(chain) == [("plan", "chatgpt-plan/gpt-test", False), ("paid", "paid/gpt-test", True)]
    assert chain["steps"][0]["connection_id"] == "chatgpt-owner" and "provider_id" not in chain["steps"][0]
    assert chain["steps"][1]["provider_id"] == "cloud" and "connection_id" not in chain["steps"][1]


def test_pinned_name_has_one_step_and_never_a_fallback():
    chain = plain_name_chain(build(graph()), "chatgpt-plan/gpt-test")
    assert shape(chain) == [("plan", "chatgpt-plan/gpt-test", False)]


def test_a_person_who_may_not_use_the_plan_reaches_the_paid_step_first_without_fallback():
    snapshot = build(graph())
    target = snapshot.model_routes.by_model[("plan-source", "logical-test")]
    everyone = model_routes.route_chain(snapshot.services, target, pinned=False)
    no_plan = model_routes.route_chain(snapshot.services, target, pinned=False, granted=lambda edge: edge.service_id != "plan-source")
    assert [(step.kind, step.paid_fallback) for step in everyone] == [("plan", False), ("paid", True)]
    assert [(step.kind, step.edge.id, step.paid_fallback) for step in no_plan] == [("paid", "paid/gpt-test", False)]


def test_a_plain_name_another_service_owns_is_served_directly_without_the_plan():
    services = {**graph(), **plan_only()}  # the plan service, and a paid service that publishes the same name
    services["cloud"]["config"]["models"] = [{"id": "alpha", "upstream_model": "alpha"}]
    chain = plain_name_chain(build(services, connection_models={"test-owner": {"models": {"alpha": []}}}), "alpha")
    assert chain["direct"] is True
    assert [(step["kind"], step["service_id"]) for step in chain["steps"]] == [("paid", "cloud")]


def test_an_unknown_name_has_no_steps():
    assert plain_name_chain(build(plan_only()), "nobody") == {"slug": "nobody", "direct": False, "steps": []}


def test_a_name_that_fails_to_resolve_has_no_steps_instead_of_failing_the_list(monkeypatch):
    def broken(snapshot, name):
        raise KeyError("malformed snapshot")

    monkeypatch.setattr("gateway.subscription_oauth.resolve_public_target", broken)
    assert plain_name_chain(build(plan_only()), "alpha") == {"slug": "alpha", "direct": False, "steps": []}


def _client(tmp_path, roles):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    asyncio.run(connect(service, jwks, upstream))
    snapshot = build(graph(), connection_models={"chatgpt-owner": {"models": {"gpt-test": ["low"]}}})
    service.snapshot = lambda: snapshot

    def auth(request):
        if request.headers.get("x-test-who") is None:
            raise AdminAuthError("no", status_code=401, code="unauthorized")
        return AdminContext(subject="p1", actor_id="p1", roles=frozenset(roles), auth_method="key")

    app = FastAPI()
    app.include_router(subscription_api.create_subscription_router(service=service, auth=auth))
    return TestClient(app)


def test_owner_list_carries_each_model_chain(tmp_path):
    response = _client(tmp_path, {"owner"}).get("/admin/api/subscriptions", headers={"x-test-who": "1"})
    assert response.status_code == 200, response.text
    shown = next(row for row in response.json()["subscriptions"] if row["kind"] == "chatgpt")
    chains = {chain["slug"]: shape(chain) for chain in shown["model_chains"]}
    assert chains == {"gpt-test": [("plan", "plan-source/gpt-test", False)],
                      "logical-test": [("plan", "chatgpt-plan/gpt-test", False), ("paid", "paid/gpt-test", True)]}


def test_member_gets_403_and_no_chain(tmp_path):
    response = _client(tmp_path, {"admin"}).get("/admin/api/subscriptions", headers={"x-test-who": "1"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "owner_required"
    assert "model_chains" not in response.text and "paid/gpt-test" not in response.text
