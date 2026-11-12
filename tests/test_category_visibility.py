"""A category hides its items from every person or app whose access plan does not include it.

A category is a label. An access plan lists the categories it includes (``categories``), or every category
(``"all"``). An item in a category is usable only when the request's person plan AND app plan both include
every one of its categories. The owner using their own identity is the only exemption. Items in no category
are not affected. Hidden items are left out of discovery for everyone else.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import category_enforcement, category_policy as policy
from gateway.category_api import snapshot_to_json
from gateway.category_store import CategoryStore
from gateway.policy import ALLOWED, DENIED, PolicyDecision, PolicyRequest
from tests.support.access import access, authenticated, identity as runtime_identity, plan_for

OWNER = "usr_owner"
MESH_WORKFLOW = "mesh.generate.trellis-2"
ALLOW = PolicyDecision(decision=ALLOWED, sources=("plan",), reasons=("allowed",))
LABELS = {"experimental": "Experimental", "adult": "Adult"}
PLAN_FAMILY = plan_for("plan_family", categories=())
PLAN_OWNER = plan_for("plan_owner")
EVERYONE = plan_for("plan_everyone", categories=())


@dataclass
class Who:
    person_id: str | None
    app_id: str | None
    access: object = None


def owner_in_person():
    return Who(OWNER, "app_console", access(PLAN_OWNER))


def owner_on_a_plan_without_categories():
    """The owner in person on a plan that lists no category: only the owner exemption can let them see a category item."""
    return Who(OWNER, "app_console", access(plan_for("plan_owner_bare", categories=())))


def family_member(categories=()):
    return Who("usr_family", "app_family", access(plan_for("plan_family", categories=categories)))


def owners_service_app():
    """An app the owner runs for the household: not the owner in person, so its own plan decides."""
    return Who(None, "app_service", access(plan_for("plan_owner_service", categories=())))


def _service(stem, category):
    fact = policy.Assertion(category, "included", "owner", True, "2026-01-01", "2026-01-01", OWNER, "owner-edit-1")
    return policy.Service(f"{stem}@r1", (policy.Subject(f"{stem}@r1", "workflow", (fact,)),))


def rules(extra_services=()):
    services = [_service("exp-flow", "experimental"), _service("adult-flow", "adult"), *extra_services]
    return policy.PolicyVersion(
        "rules",
        {key: policy.Category(key, label, values=("included",), applies_to=("release", "workflow"))
         for key, label in LABELS.items()},
        {}, {}, {service.id: service for service in services})


def activate(path: Path, snapshot) -> None:
    store = CategoryStore(path)
    active = None
    try:
        active = store.get_active().revision
    except Exception:  # noqa: BLE001 - nothing active yet
        pass
    row = store.create_candidate(snapshot_to_json(snapshot), active, OWNER, "test rules")
    store.activate(row.revision, active)


@pytest.fixture
def gate(tmp_path):
    path = tmp_path / "categories.db"
    category_enforcement.configure(path, owner_id=lambda: OWNER)
    yield path
    category_enforcement.configure(None)


def request(stem, intent="image.generate"):
    return PolicyRequest(protocol="openai.images", intent=intent, workflow=stem)


def test_the_owner_in_person_sees_every_category(gate):
    activate(gate, rules())
    for stem in ("exp-flow", "adult-flow"):
        assert category_enforcement.apply(owner_in_person(), request(stem), ALLOW) is ALLOW
        assert category_enforcement.visible(owner_in_person(), request(stem))


def test_a_family_member_whose_plan_lists_no_category_is_refused_and_never_sees_it_in_discovery(gate):
    activate(gate, rules())
    for stem in ("exp-flow", "adult-flow"):
        decision = category_enforcement.apply(family_member(), request(stem), ALLOW)
        assert decision.decision == DENIED
        assert decision.reasons[-1].startswith("category rules: CATEGORY_HIDDEN ")
        assert not category_enforcement.visible(family_member(), request(stem))


def test_a_person_the_rules_never_name_is_still_hidden_from_it(gate):
    """Hiding does not depend on the owner having listed the person in the rules."""
    activate(gate, rules())
    assert rules().people == {} and rules().apps == {}
    assert not category_enforcement.visible(family_member(), request("exp-flow"))


def test_items_in_no_category_stay_open_to_a_plan_with_no_categories(gate):
    activate(gate, rules(extra_services=[policy.Service("plain@r1", (policy.Subject("plain@r1", "workflow"),))]))
    assert category_enforcement.visible(family_member(), request("plain"))
    assert category_enforcement.visible(family_member(), request("not-in-any-rules"))


def test_an_app_the_owner_runs_sees_only_what_its_own_plan_includes(gate):
    activate(gate, rules())
    assert not category_enforcement.visible(owners_service_app(), request("exp-flow"))
    everything = Who(None, "app_service", access(plan_for("plan_owner_service", categories="all")))
    assert category_enforcement.visible(everything, request("exp-flow"))


def test_an_access_plan_can_list_a_category_and_then_sees_it(gate):
    activate(gate, rules())
    assert category_enforcement.visible(family_member(categories=["adult"]), request("adult-flow"))
    assert not category_enforcement.visible(family_member(categories=["adult"]), request("exp-flow"))


def test_both_the_person_plan_and_the_app_plan_must_include_the_category(gate):
    activate(gate, rules())
    mixed = Who("usr_family", "app_other", access(plan_for("plan_family", categories=["adult"]),
                                                  plan_for("plan_app_other", categories=())))
    assert not category_enforcement.visible(mixed, request("adult-flow"))


def test_a_plan_that_includes_every_category_sees_each_one(gate):
    activate(gate, rules())
    for stem in ("exp-flow", "adult-flow"):
        assert category_enforcement.visible(family_member(categories="all"), request(stem))


def test_a_request_without_plans_is_hidden(gate):
    activate(gate, rules())
    assert not category_enforcement.visible(Who("usr_family", "app_family", None), request("adult-flow"))


def test_an_unreadable_owner_record_never_grants_the_owner_view(tmp_path):
    def broken():
        raise RuntimeError("admin db unavailable")
    path = tmp_path / "categories.db"
    category_enforcement.configure(path, owner_id=broken)
    try:
        activate(path, rules())
        assert not category_enforcement.visible(owner_on_a_plan_without_categories(), request("exp-flow"))
    finally:
        category_enforcement.configure(None)


def test_without_an_owner_lookup_nobody_is_exempt(tmp_path):
    path = tmp_path / "categories.db"
    category_enforcement.configure(path)
    try:
        activate(path, rules())
        assert not category_enforcement.visible(owner_on_a_plan_without_categories(), request("exp-flow"))
    finally:
        category_enforcement.configure(None)


def test_the_owner_in_person_sees_a_category_their_own_plan_lacks(tmp_path):
    path = tmp_path / "categories.db"
    category_enforcement.configure(path, owner_id=lambda: OWNER)
    try:
        activate(path, rules())
        assert category_enforcement.visible(owner_on_a_plan_without_categories(), request("exp-flow"))
        assert not category_enforcement.visible(family_member(), request("exp-flow"))
    finally:
        category_enforcement.configure(None)


def test_a_model_name_in_a_category_is_hidden_too(gate):
    activate(gate, rules())
    model_request = PolicyRequest(protocol="openai.chat", intent="chat.completions", model="exp-flow")
    assert not category_enforcement.visible(family_member(), model_request)
    assert category_enforcement.visible(owner_in_person(), model_request)


def test_a_category_is_a_label_and_carries_no_audience():
    assert "visible_to" not in {field.name for field in dataclasses.fields(policy.Category)}


def test_a_stored_revision_that_still_has_visible_to_decodes_and_the_field_is_ignored():
    from gateway.category_api import snapshot_from_json
    body = snapshot_to_json(rules())
    body["categories"]["adult"]["visible_to"] = ["plan_a"]
    again = snapshot_from_json(body)
    assert again.categories["adult"].label == "Adult"
    assert not hasattr(again.categories["adult"], "visible_to")


# --- over HTTP: the lists other apps read -------------------

def _workflow_entry():
    """A model-list entry that is a workflow, and the workflow ids that exist."""
    entries = [e for e in proxy._build_workflow_model_entries() if e.get("azizam_intent", "").startswith("image.")]
    assert len(entries) >= 2
    return entries[0], entries[1]


def _hide(path, workflow_id):
    category = policy.Category("experimental", "Experimental", values=("included",), applies_to=("release", "workflow"))
    service = _service(workflow_id, "experimental")
    activate(path, policy.PolicyVersion("rules", {"experimental": category}, {}, {}, {service.id: service}))


def _ids(response, key):
    assert response.status_code == 200, response.text
    return {row.get("id") for row in response.json()[key]}


@pytest.fixture
def http_gate(tmp_path):
    path = tmp_path / "categories.db"
    category_enforcement.configure(path, owner_id=lambda: OWNER)
    yield path
    category_enforcement.configure(None)


def _as(person):
    return authenticated(runtime_identity(EVERYONE, app_id="app_x", person_id=person))


def test_workflow_discovery_hides_the_item_from_another_app_and_shows_it_to_the_owner(http_gate):
    hidden, other = _workflow_entry()
    _hide(http_gate, hidden["azizam_workflow_id"])
    client = TestClient(proxy.app)
    with _as(None):
        seen = _ids(client.get("/gateway/v1/workflows"), "workflows")
    assert other["azizam_workflow_id"] in seen and hidden["azizam_workflow_id"] not in seen
    with _as(OWNER):
        seen = _ids(client.get("/gateway/v1/workflows"), "workflows")
    assert {hidden["azizam_workflow_id"], other["azizam_workflow_id"]} <= seen


class _Models:
    status_code = 200
    headers = {"content-type": "application/json"}

    async def aread(self):
        return json.dumps({"object": "list", "data": [{"id": "plain-chat", "object": "model"}]}).encode()

    async def aclose(self):
        return None


class _Client:
    async def aclose(self):
        return None


def test_the_model_list_hides_the_item_from_another_app_and_shows_it_to_the_owner(http_gate):
    hidden, other = _workflow_entry()
    _hide(http_gate, hidden["azizam_workflow_id"])

    async def send(*_args, **_kwargs):
        return _Client(), _Models()

    registry = {"services": {"litellm": {
        "id": "litellm", "kind": "llm-substrate", "enabled": True, "capabilities": ["llm.execution"],
        "config": {"base_url": "http://127.0.0.1:4011"}}}}
    client = TestClient(proxy.app)
    with patch.object(proxy.ADMIN_STORE, "get_service_registry", lambda: registry), \
            patch.object(proxy, "send_llm_execution_request", send), \
            patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value=set())), \
            patch.object(proxy, "_build_workflow_model_entries", lambda: [hidden, other]):
        with _as(None):
            seen = _ids(client.get("/v1/models"), "data")
        assert hidden["id"] not in seen and {other["id"], "plain-chat"} <= seen
        with _as(OWNER):
            seen = _ids(client.get("/v1/models"), "data")
        assert {hidden["id"], other["id"]} <= seen


def test_the_capability_list_leaves_out_the_hidden_item(http_gate):
    from gateway.capabilities import Capability
    hidden, other = _workflow_entry()
    _hide(http_gate, hidden["azizam_workflow_id"])
    catalog = [Capability(intent="image.generate", backend="comfyui", workflow=entry["azizam_workflow_id"])
               for entry in (hidden, other)]
    with patch.object(proxy, "_gateway_catalog_base", lambda: ({}, [])), \
            patch.object(proxy, "_gateway_catalog_finish", lambda *_a: catalog), \
            patch.object(proxy, "_remote_decision_models", lambda *_a: []):
        def listed(person):
            context = proxy.AuthContext(identity=runtime_identity(EVERYONE, app_id="app_x", person_id=person))
            return {c.workflow for c in proxy._gateway_capabilities_for_auth_context(context)}
        assert listed(None) == {other["azizam_workflow_id"]}
        assert listed(OWNER) == {hidden["azizam_workflow_id"], other["azizam_workflow_id"]}


def test_a_hidden_category_hides_the_mesh_row_like_any_other_capability(http_gate):
    _hide(http_gate, MESH_WORKFLOW)
    catalog = proxy._mesh_facts({})
    with patch.object(proxy, "_gateway_catalog_base", lambda: ({}, [])), \
            patch.object(proxy, "_gateway_catalog_finish", lambda *_a: catalog), \
            patch.object(proxy, "_remote_decision_models", lambda *_a: []):
        def listed(person):
            context = proxy.AuthContext(identity=runtime_identity(EVERYONE, app_id="app_x", person_id=person))
            return {c.workflow for c in proxy._gateway_capabilities_for_auth_context(context)}
        assert MESH_WORKFLOW not in listed(None)
        assert MESH_WORKFLOW in listed(OWNER)


def test_a_hidden_workflow_is_not_found_by_another_app(http_gate):
    hidden, _ = _workflow_entry()
    _hide(http_gate, hidden["azizam_workflow_id"])
    client = TestClient(proxy.app)
    with _as(None):
        assert client.get(f"/gateway/v1/workflows/{hidden['azizam_workflow_id']}").status_code == 404
    with _as(OWNER):
        assert client.get(f"/gateway/v1/workflows/{hidden['azizam_workflow_id']}").status_code == 200


DENY = PolicyDecision(decision=DENIED, sources=("plan",), reasons=("not in your plan",))


def test_an_item_the_plan_denies_is_still_answered_as_hidden(gate):
    # A refusal must not say "not in your plan" for a hidden item: that would tell another person it exists.
    activate(gate, rules())
    shown = category_enforcement.apply(family_member(), request("exp-flow"), DENY)
    assert category_enforcement.is_hidden(shown.reasons) and not shown.allowed
    assert shown.sources == ("plan", category_enforcement.SOURCE)
    assert category_enforcement.apply(family_member(), request("plain-flow"), DENY) == DENY
    assert category_enforcement.apply(owner_in_person(), request("exp-flow"), DENY) == DENY
    # An allowed request gets the same hidden answer.
    assert category_enforcement.apply(family_member(), request("exp-flow"), ALLOW).reasons == shown.reasons


def test_a_remote_model_without_its_provider_is_still_hidden_from_the_capability_list(http_gate):
    from gateway.capabilities import Capability
    _hide(http_gate, "gone/hidden-model")
    catalog = [Capability(intent="chat.completions", backend="missing-provider", model=model, tags=("remote",))
               for model in ("gone/hidden-model", "gone/open-model")]
    with patch.object(proxy, "_gateway_catalog_base", lambda: ({}, [])), \
            patch.object(proxy, "_gateway_catalog_finish", lambda *_a: catalog), \
            patch.object(proxy, "_remote_decision_models", lambda *_a: []):
        def listed(person):
            context = proxy.AuthContext(identity=runtime_identity(EVERYONE, app_id="app_x", person_id=person))
            return {c.model: c for c in proxy._gateway_capabilities_for_auth_context(context)}
        others = listed(None)
        assert set(others) == {"gone/open-model"}
        # A missing provider still reads as unavailable, as before.
        assert others["gone/open-model"].policy_available is False
        assert others["gone/open-model"].policy_reasons == ("Provider configuration is unavailable.",)
        assert set(listed(OWNER)) == {"gone/hidden-model", "gone/open-model"}


def test_the_capability_list_hides_a_restricted_item_the_plan_denies(http_gate):
    from gateway.capabilities import Capability
    hidden, other = _workflow_entry()
    _hide(http_gate, hidden["azizam_workflow_id"])
    catalog = [Capability(intent="image.generate", backend="comfyui", workflow=entry["azizam_workflow_id"])
               for entry in (hidden, other)]
    with patch.object(proxy, "_gateway_catalog_base", lambda: ({}, [])), \
            patch.object(proxy, "_gateway_catalog_finish", lambda *_a: catalog), \
            patch.object(proxy, "_remote_decision_models", lambda *_a: []):
        context = proxy.AuthContext(identity=runtime_identity(plan_for("plan_nothing", caps=(), categories=()), app_id="app_x"))
        listed = {c.workflow: c for c in proxy._gateway_capabilities_for_auth_context(context)}
    # The plan grants nothing and lists no category, so the other item is listed as not allowed and the restricted one is not named.
    assert set(listed) == {other["azizam_workflow_id"]}
    assert listed[other["azizam_workflow_id"]].policy_available is False
