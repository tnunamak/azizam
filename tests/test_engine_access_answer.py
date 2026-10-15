"""Who can use an engine's models: the answer Add engine shows before Create.

It replaces the old "Use existing access rules for these models" checkbox. The
answer comes from the evaluator every request meets (gateway.policy.evaluate),
run once per access plan, so it cannot drift from what the gateway enforces.
Deny by default: a plan that does not grant the work, or lists other models,
does not include them.
"""

from __future__ import annotations

import pytest

from gateway import admin_store
from gateway.access_plans import AccessPlan, CapabilityGrant, plan_to_document
from gateway.engine_access import PLANS_LINK, who_can_use
from tests.support.fake_upstream import fake_upstreams  # noqa: F401 - fixture
from tests.test_engine_admin_routes import TOKEN, _create, _silent  # noqa: F401 - fixed_baseline below
from tests.test_engine_admin_routes import fixed_baseline  # noqa: F401 - fixture
from tests.support.fake_upstream import load_scenario
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

FAMILY = AccessPlan(id="plan_family", name="Family", can_use={"chat": CapabilityGrant(all_items=True)})
KIDS = AccessPlan(id="plan_kids", name="Kids", can_use={"chat": CapabilityGrant(models=frozenset({"qwen3-32b"}))})
ART = AccessPlan(id="plan_art", name="Art", can_use={"image": CapabilityGrant(all_items=True)})


def _holders(**counts):
    return {plan_id: {"people": [f"person {n}" for n in range(people)], "apps": [], "invites": 0}
            for plan_id, people in counts.items()}


def test_a_plan_that_allows_a_whole_kind_of_work_includes_every_new_model_of_that_kind():
    answer = who_can_use(["qwen3-32b", "llama-3-8b"], ["chat"], [FAMILY, ART], holders=_holders(plan_family=3),
                         viewer_plan_id=None)
    assert [row["id"] for row in answer["plans"]] == ["plan_family"]
    family = answer["plans"][0]
    assert family["models"] == ["qwen3-32b", "llama-3-8b"]
    assert family["why"] == "allows all chat models"
    assert family["people"] == 3
    assert answer["not_included"] == []
    assert answer["sentence"] == "Usable by: the Family plan (allows all chat models). Your own plan doesn’t include them."


def test_a_plan_that_lists_models_includes_only_the_ones_it_lists():
    answer = who_can_use(["qwen3-32b", "llama-3-8b"], ["chat"], [KIDS], holders={}, viewer_plan_id="plan_kids")
    kids = answer["plans"][0]
    assert kids["models"] == ["qwen3-32b"] and kids["why"] == "lists qwen3-32b"
    assert answer["you"] is True
    assert answer["not_included"] == ["llama-3-8b"]
    assert answer["sentence"] == "Usable by: you (Kids plan: lists qwen3-32b). Not in any plan yet: llama-3-8b."


def test_with_no_plans_nobody_can_use_them():
    answer = who_can_use(["qwen3-32b"], ["chat"], [], holders={}, viewer_plan_id=None)
    assert answer["plans"] == [] and answer["you"] is False
    assert answer["not_included"] == ["qwen3-32b"]
    assert answer["sentence"] == ("No access plan includes these yet, so nobody can use them, you included. "
                                  "Add them to a plan in Access plans.")
    assert answer["link"] == PLANS_LINK


def test_a_plan_for_other_work_does_not_include_them():
    """Images for everyone says nothing about chat: deny by default."""
    answer = who_can_use(["qwen3-32b"], ["chat"], [ART], holders={}, viewer_plan_id="plan_art")
    assert answer["plans"] == [] and answer["you"] is False


def test_work_no_plan_can_grant_is_never_included():
    everything = AccessPlan(id="plan_all", name="Everything",
                            can_use={cap: CapabilityGrant(all_items=True) for cap in ("chat", "image", "speech")})
    answer = who_can_use(["voice-live"], ["realtime"], [everything], holders={}, viewer_plan_id="plan_all")
    assert answer["plans"] == []


def test_you_and_other_plans_read_as_one_sentence():
    answer = who_can_use(["qwen3-32b"], ["chat", "embeddings"], [FAMILY, KIDS, ART], holders={},
                         viewer_plan_id="plan_family")
    assert answer["you"] is True
    assert answer["sentence"] == "Usable by: you, and the Kids plan (lists qwen3-32b)."
    assert [row["link"] for row in answer["plans"]] == ["/people/?plan=plan_family", "/people/?plan=plan_kids"]


def test_a_model_known_to_do_other_work_is_not_included_by_a_plan_for_chat():
    """An embedding model under a plan that allows only chat would be refused at the gateway."""
    answer = who_can_use(["llama-chat", "nomic-embed"], ["chat", "embeddings"], [FAMILY], holders={},
                         viewer_plan_id=None, model_works={"nomic-embed": ["embeddings"]})
    assert answer["plans"][0]["models"] == ["llama-chat"]
    assert answer["not_included"] == ["nomic-embed"]


def test_a_plan_must_also_list_the_paid_provider_the_models_run_on():
    paid = AccessPlan(id="plan_paid", name="Paid", can_use={"chat": CapabilityGrant(all_items=True)},
                      paid_providers=frozenset({"openrouter"}))
    answer = who_can_use(["openrouter/gpt"], ["chat"], [FAMILY, paid], holders={}, viewer_plan_id=None,
                         remote_provider="openrouter")
    assert [row["id"] for row in answer["plans"]] == ["plan_paid"]


def test_disabled_apps_are_not_counted_as_holders():
    holders = {"plan_family": {"people": ["Sam"], "apps": ["Kitchen", "Old tablet (disabled)"]}}
    answer = who_can_use(["qwen3-32b"], ["chat"], [FAMILY], holders=holders, viewer_plan_id=None)
    assert (answer["plans"][0]["people"], answer["plans"][0]["apps"]) == (1, 1)


@pytest.mark.parametrize("state, says", [
    ("governs", "People and apps under category rules can’t use them until you add them to a category."),
    ("unreadable", "The category rules can’t be read right now, so the gateway refuses every request until they can."),
])
def test_category_rules_are_part_of_the_answer(state, says):
    answer = who_can_use(["qwen3-32b"], ["chat"], [FAMILY], holders={}, viewer_plan_id="plan_family",
                         category_rules=state == "governs", category_rules_unreadable=state == "unreadable")
    assert answer["sentence"].endswith(says)


def test_an_engine_with_no_models_yet_says_so():
    answer = who_can_use([], ["chat"], [FAMILY], holders={}, viewer_plan_id="plan_family")
    assert answer["plans"] == []
    assert answer["sentence"] == "No models yet. When it has some, this shows who can use them."


# ---------------------------------------------------------------------------
# Through the routes, from the plans the gateway stores
# ---------------------------------------------------------------------------


def _serve_family_b(fakes):
    """An embedding server whose model list marks its one model as an embedding model."""
    return fakes.asgi(load_scenario("engine-probe/family-c-embedding-model", token=TOKEN), host="embedder.fk.test")


def _store_plan(gateway, plan: AccessPlan) -> str:
    return admin_store.create_access_plan(plan_to_document(plan), path=gateway.path)["id"]


def _ask(client, body):
    return client.post("/admin/api/engines/access", headers=csrf_for(client), json=body)


def test_the_access_route_answers_from_the_stored_plans(gateway):
    owner, _, owner_plan, _ = owner_and_admin(gateway)
    family = _store_plan(gateway, FAMILY)
    kids = _store_plan(gateway, KIDS)
    _store_plan(gateway, ART)
    answer = _ask(owner, {"capabilities": ["chat"], "models": ["qwen3-32b", "llama-3-8b"]})
    assert answer.status_code == 200, answer.text
    access = answer.json()["access"]
    by_id = {row["id"]: row for row in access["plans"]}
    # The owner's starter plan allows everything local, so "you" comes first.
    assert set(by_id) == {owner_plan, family, kids}
    assert access["you"] is True
    assert by_id[family]["why"] == "allows all chat models"
    assert by_id[kids]["models"] == ["qwen3-32b"]
    # The owner holds the starter plan; nobody holds the others yet.
    assert (by_id[owner_plan]["people"], by_id[owner_plan]["apps"]) == (1, 0)
    assert (by_id[family]["people"], by_id[family]["apps"]) == (0, 0)
    assert access["sentence"].startswith("Usable by: you, and the Family plan (allows all chat models) and the Kids plan")


def test_the_access_route_says_nobody_when_no_plan_includes_them(gateway):
    owner, _, owner_plan, _ = owner_and_admin(gateway)
    # The owner's only plan now allows images and nothing else.
    admin_store.update_access_plan(owner_plan, plan_to_document(
        AccessPlan(id=owner_plan, name="Local only", can_use={"image": CapabilityGrant(all_items=True)})), path=gateway.path)
    access = _ask(owner, {"capabilities": ["chat"], "models": ["qwen3-32b"]}).json()["access"]
    assert access["plans"] == [] and access["you"] is False
    assert access["sentence"].startswith("No access plan includes these yet")


def test_an_administrator_without_a_plan_gets_the_same_answer_without_you(gateway):
    _, _, _, admin = owner_and_admin(gateway)
    _store_plan(gateway, FAMILY)
    access = _ask(admin, {"capabilities": ["chat"], "models": ["qwen3-32b"]}).json()["access"]
    assert access["you"] is False
    assert "the Family plan (allows all chat models)" in access["sentence"]


def test_the_access_route_takes_the_work_each_model_is_known_to_do(gateway):
    owner, *_ = owner_and_admin(gateway)
    family = _store_plan(gateway, FAMILY)
    access = _ask(owner, {"capabilities": ["chat", "embeddings"],
                          "models": ["llama-chat", {"name": "nomic-embed", "capabilities": ["embeddings"]}]}).json()["access"]
    assert {row["id"]: row["models"] for row in access["plans"]}[family] == ["llama-chat"]


def test_the_engine_page_asks_about_each_model_for_the_work_the_check_found(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    fake = _serve_family_b(fake_upstreams)
    family = _store_plan(gateway, FAMILY)
    created = _create(owner, {"definition": "openai-compatible", "name": "embedder", "acknowledge_warnings": True,
                              "values": {"address": f"{fake.base_url}/v1"}, "capabilities": ["chat", "embeddings"]})
    assert created.status_code == 201, created.text
    engine = owner.get("/admin/api/engines/embedder").json()["engine"]
    assert engine["models"][0]["use"] == "Embeddings"
    assert family not in {row["id"] for row in engine["access"]["plans"]}, "Family allows chat only"


@pytest.mark.parametrize("body, field", [
    ({"capabilities": ["no-such-work"], "models": ["m"]}, "capabilities"),
    ({"capabilities": ["chat"], "models": "m"}, "models"),
    ({"capabilities": ["chat"], "models": [""]}, "models"),
])
def test_the_access_route_refuses_a_malformed_question(gateway, body, field):
    owner, *_ = owner_and_admin(gateway)
    refused = _ask(owner, body)
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["field"] == field


def test_the_engine_page_carries_the_answer_for_its_saved_models(gateway, fake_upstreams):
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "answered.fk.test")
    _store_plan(gateway, KIDS)
    assert _create(owner, {"definition": "openai-compatible", "name": "answered", "disabled": True,
                           "values": {"address": "http://answered.fk.test/v1"}, "capabilities": ["chat"],
                           "models": [{"id": "qwen3-32b-q4", "public_name": "qwen3-32b"}]}).status_code == 201
    engine = owner.get("/admin/api/engines/answered").json()["engine"]
    by_name = {row["name"]: row for row in engine["access"]["plans"]}
    assert by_name["Kids"]["models"] == ["qwen3-32b"]
    assert engine["access"]["you"] is True
