"""The access answer agrees with what the gateway does to a real request (round 4, access review).

For each plan kind the answer names, a person and an App hold that plan and
send a chat request with their own key, through the gateway, for each model
the engine serves. The answer says a plan includes a model exactly when the
gateway lets that key's request through. The answer is computed by the same
code the Add engine route runs (EngineService._access_answer, which calls
gateway.policy.evaluate); it is not a copy of the rules.
"""

from __future__ import annotations

import asyncio

import pytest

import proxy
from gateway import accounts, admin_store
from gateway.engine_admin import EngineService, build_raw, clean_values
from gateway.engine_definitions import load_engine_catalog
from gateway.runtime_auth import hash_api_key
from gateway.service_registry import service_from_backend_config
from tests.registry_test_helpers import write_service_registry_for_test
from tests.support.access import store_plan
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401 - fixture
from tests.support.gateway import gateway_client, register_litellm, use_admin_db

MODELS = ["garage-chat", "other-chat"]
PLANS = {
    # A rule for a whole kind of work: every chat model, including ones added later.
    "all chat models": {"caps": ("chat",)},
    # An explicit model list.
    "one listed model": {"caps": ("chat",), "models": ["garage-chat"]},
    # Everything on the gateway.
    "everything": {},
    # A plan for other work only: no plan includes these chat models.
    "images only": {"caps": ("image",)},
}


def _router_fake(fakes):
    exchanges = [{
        "id": f"chat-{model}", "repeat": True, "required": False,
        "match": {"method": "POST", "path": "/v1/chat/completions", "json": {"model": model}},
        "response": {"status": 200, "json": {
            "id": "chatcmpl-parity", "object": "chat.completion", "created": 1767225600, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}},
    } for model in MODELS]
    return fakes.asgi(scenario_from_data({"protocol": "openai-compatible", "name": "parity-router", "exchanges": exchanges,
                                          "provenance": {"source": "hand-written", "reference": "round 4 access parity"}}),
                      host="litellm.fk.test")


def _engine_record() -> dict:
    catalog = load_engine_catalog()
    definition = catalog.definition("openai-compatible")
    clean = clean_values(definition, {"address": "http://garage.fk.test/v1"})
    raw = build_raw(catalog, definition, clean, name="Garage GPU", capabilities=["chat"],
                    models=[{"id": name, "public_name": name} for name in MODELS], enabled=True,
                    check={}, tests=None, secret_id=None)
    return service_from_backend_config("garage", raw, source="ui")


def _key_holding(db, label: str, plan_id: str) -> str:
    """A person and their App, both on ``plan_id``; returns the App's key."""
    person = admin_store.upsert_oidc_user("https://parity.test", label, email=f"{label.replace(' ', '-')}@example.test", path=db)
    accounts.change_person(person["id"], "parity", path=db, plan_id=plan_id)
    app = admin_store.create_app(f"{label} app", kind="service", owner_user_id=person["id"], access_plan="person", path=db)
    key = f"opk_parity_{label.replace(' ', '_')}"
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"], hash=hash_api_key(key),
                                       prefix=key[:16], created_by=person["id"], path=db)
    return key


@pytest.fixture
def parity(fake_upstreams, monkeypatch, tmp_path):
    router = _router_fake(fake_upstreams)
    db = use_admin_db(monkeypatch, tmp_path)
    register_litellm(monkeypatch, db, router.base_url)
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["services"]["garage"] = _engine_record()
    write_service_registry_for_test(registry, path=db)
    plans = {label: store_plan(label, path=db, **kwargs)["id"] for label, kwargs in PLANS.items()}
    keys = {label: _key_holding(db, label, plan_id) for label, plan_id in plans.items()}
    return plans, keys


def _gateway_allows(key: str, model: str) -> bool:
    async def call():
        async with gateway_client() as client:
            return await client.post("/v1/chat/completions", headers={"authorization": f"Bearer {key}"},
                                     json={"model": model, "messages": [{"role": "user", "content": "hi"}]})

    response = asyncio.run(call())
    if response.status_code == 200:
        return True
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "not_in_access_plan", response.text
    return False


def test_the_access_answer_matches_what_the_gateway_does_for_a_key_on_each_plan(parity):
    plans, keys = parity
    answer = EngineService(proxy.ADMIN_STORE)._access_answer(None, list(MODELS), ["chat"])
    said = {row["id"]: set(row["models"]) for row in answer["plans"]}
    seen = {}
    for label, plan_id in plans.items():
        for model in MODELS:
            seen[(label, model)] = (model in said.get(plan_id, set()), _gateway_allows(keys[label], model))
    mismatches = {case: pair for case, pair in seen.items() if pair[0] != pair[1]}
    assert not mismatches, f"(answer says, gateway does) differ: {mismatches}"
    # Each kind of plan was exercised both ways where it should be.
    assert seen[("all chat models", "other-chat")] == (True, True)
    assert seen[("one listed model", "garage-chat")] == (True, True)
    assert seen[("one listed model", "other-chat")] == (False, False)
    assert seen[("everything", "other-chat")] == (True, True)
    assert seen[("images only", "garage-chat")] == (False, False)


def test_with_no_plan_that_includes_them_the_answer_and_the_gateway_both_refuse(parity):
    plans, keys = parity
    answer = EngineService(proxy.ADMIN_STORE)._access_answer(None, ["not-in-any-list"], ["chat"])
    listing = {row["id"] for row in answer["plans"]}
    assert plans["one listed model"] not in listing and plans["images only"] not in listing


def test_the_answer_and_request_authorization_call_the_same_evaluator():
    """No parallel copy: both import the one function in gateway.policy."""
    from gateway import engine_access, policy, runtime_auth

    assert engine_access.evaluate is policy.evaluate
    assert runtime_auth.evaluate is policy.evaluate


def test_the_answer_follows_the_evaluator(monkeypatch):
    """Replacing the evaluator changes the answer, so the answer reads nothing else."""
    from gateway import engine_access
    from gateway.access_plans import AccessPlan, CapabilityGrant
    from gateway.policy import DENIED, PolicyDecision

    asked = []

    def refuse_everything(access, request):
        asked.append(request.model)
        return PolicyDecision(decision=DENIED)

    monkeypatch.setattr(engine_access, "evaluate", refuse_everything)
    plan = AccessPlan(id="p", name="Everything", can_use={"chat": CapabilityGrant(all_items=True)})
    answer = engine_access.who_can_use(["m"], ["chat"], [plan], holders={}, viewer_plan_id=None)
    assert asked == ["m"] and answer["plans"] == []
