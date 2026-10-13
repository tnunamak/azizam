"""A restricted category (Adult, Experimental) hides its items from everyone but the owner.

The owner names the access plans that may see a category (``visible_to``). Items in
it are refused and left out of discovery for any other person or app, whether or not
the owner has listed that person or app in the category rules.
"""
from __future__ import annotations

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
ALLOW = PolicyDecision(decision=ALLOWED, sources=("plan",), reasons=("allowed",))
PLAN_FAMILY = plan_for("plan_family")
PLAN_OWNER = plan_for("plan_owner")


@dataclass
class Who:
    person_id: str | None
    app_id: str | None
    access: object = None


def owner_in_person():
    return Who(OWNER, "app_console", access(PLAN_OWNER))


def family_member():
    return Who("usr_family", "app_family", access(PLAN_FAMILY))


def owners_service_app():
    """An app the owner runs for the household: the owner's account, not the owner in person."""
    return Who(None, "app_service", access(PLAN_OWNER))


def _service(stem, category):
    fact = policy.Assertion(category, "included", "owner", True, "2026-01-01", "2026-01-01", OWNER, "owner-edit-1")
    return policy.Service(f"{stem}@r1", (policy.Subject(f"{stem}@r1", "workflow", (fact,)),))


def rules(experimental_visible_to=(), adult_visible_to=(), extra_services=()):
    def category(identifier, label, visible_to):
        return policy.Category(identifier, label, values=("included",), applies_to=("release", "workflow"),
                               visible_to=visible_to)
    services = [_service("exp-flow", "experimental"), _service("adult-flow", "adult"), *extra_services]
    return policy.PolicyVersion(
        "rules",
        {"experimental": category("experimental", "Experimental", tuple(experimental_visible_to)),
         "adult": category("adult", "Adult", None if adult_visible_to is None else tuple(adult_visible_to))},
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


def test_the_owner_in_person_sees_a_restricted_category(gate):
    activate(gate, rules())
    for stem in ("exp-flow", "adult-flow"):
        assert category_enforcement.apply(owner_in_person(), request(stem), ALLOW) is ALLOW
        assert category_enforcement.visible(owner_in_person(), request(stem))


def test_a_family_member_is_refused_and_never_sees_it_in_discovery(gate):
    activate(gate, rules())
    for stem in ("exp-flow", "adult-flow"):
        decision = category_enforcement.apply(family_member(), request(stem), ALLOW)
        assert decision.decision == DENIED
        assert decision.reasons[-1].startswith("category rules: CATEGORY_HIDDEN ")
        assert not category_enforcement.visible(family_member(), request(stem))


def test_an_unlisted_person_is_still_hidden_from_it(gate):
    """Hiding does not depend on the owner having listed the person in the rules."""
    activate(gate, rules())
    assert rules().people == {} and rules().apps == {}
    assert not category_enforcement.visible(family_member(), request("exp-flow"))


def test_items_outside_the_category_stay_visible_to_the_family(gate):
    activate(gate, rules(extra_services=[policy.Service("plain@r1", (policy.Subject("plain@r1", "workflow"),))]))
    assert category_enforcement.visible(family_member(), request("plain"))
    assert category_enforcement.visible(family_member(), request("not-in-any-rules"))


def test_the_owners_own_service_app_does_not_see_experimental(gate):
    activate(gate, rules())
    assert not category_enforcement.visible(owners_service_app(), request("exp-flow"))


def test_an_access_plan_can_be_allowed_into_a_category(gate):
    activate(gate, rules(adult_visible_to=["plan_family"]))
    assert category_enforcement.visible(family_member(), request("adult-flow"))
    assert not category_enforcement.visible(family_member(), request("exp-flow"))


def test_both_the_person_plan_and_the_app_plan_must_be_allowed(gate):
    activate(gate, rules(adult_visible_to=["plan_family"]))
    mixed = Who("usr_family", "app_other", access(PLAN_FAMILY, PLAN_OWNER))
    assert not category_enforcement.visible(mixed, request("adult-flow"))


def test_a_category_without_a_visibility_list_hides_nothing(gate):
    activate(gate, rules(adult_visible_to=None))
    assert category_enforcement.visible(family_member(), request("adult-flow"))


def test_a_request_without_plans_is_hidden(gate):
    activate(gate, rules(adult_visible_to=["plan_family"]))
    assert not category_enforcement.visible(Who("usr_family", "app_family", None), request("adult-flow"))


def test_an_unreadable_owner_record_never_grants_the_owner_view(tmp_path):
    def broken():
        raise RuntimeError("admin db unavailable")
    path = tmp_path / "categories.db"
    category_enforcement.configure(path, owner_id=broken)
    try:
        activate(path, rules())
        assert not category_enforcement.visible(owner_in_person(), request("exp-flow"))
    finally:
        category_enforcement.configure(None)


def test_without_an_owner_lookup_nobody_is_exempt(tmp_path):
    path = tmp_path / "categories.db"
    category_enforcement.configure(path)
    try:
        activate(path, rules())
        assert not category_enforcement.visible(owner_in_person(), request("exp-flow"))
    finally:
        category_enforcement.configure(None)


def test_a_model_name_in_a_category_is_hidden_too(gate):
    activate(gate, rules())
    model_request = PolicyRequest(protocol="openai.chat", intent="chat.completions", model="exp-flow")
    assert not category_enforcement.visible(family_member(), model_request)
    assert category_enforcement.visible(owner_in_person(), model_request)


def test_the_category_fields_survive_the_api_codec():
    from gateway.category_api import snapshot_from_json
    snapshot = rules(experimental_visible_to=["plan_a"], adult_visible_to=None)
    again = snapshot_from_json(snapshot_to_json(snapshot))
    assert again.categories["experimental"].visible_to == ("plan_a",)
    assert again.categories["adult"].visible_to is None


@pytest.mark.parametrize("bad", [("",), ("a", "a"), (1,)])
def test_visibility_must_be_unique_plan_ids(bad):
    with pytest.raises(policy.PolicyError):
        policy.Category("c", "C", values=("included",), visible_to=bad).validate()


# --- over HTTP: the lists other apps and the showcase read -------------------

def _workflow_entry():
    """A model-list entry that is a workflow, and the workflow ids that exist."""
    entries = [e for e in proxy._build_workflow_model_entries() if e.get("azizam_intent", "").startswith("image.")]
    assert len(entries) >= 2
    return entries[0], entries[1]


def _hide(path, workflow_id):
    category = policy.Category("experimental", "Experimental", values=("included",),
                               applies_to=("release", "workflow"), visible_to=())
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
    return authenticated(runtime_identity(plan_for("plan_everyone"), app_id="app_x", person_id=person))


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
            context = proxy.AuthContext(identity=runtime_identity(plan_for("plan_everyone"), app_id="app_x", person_id=person))
            return {c.workflow for c in proxy._gateway_capabilities_for_auth_context(context)}
        assert listed(None) == {other["azizam_workflow_id"]}
        assert listed(OWNER) == {hidden["azizam_workflow_id"], other["azizam_workflow_id"]}


def test_a_hidden_workflow_is_not_found_by_another_app(http_gate):
    hidden, _ = _workflow_entry()
    _hide(http_gate, hidden["azizam_workflow_id"])
    client = TestClient(proxy.app)
    with _as(None):
        assert client.get(f"/gateway/v1/workflows/{hidden['azizam_workflow_id']}").status_code == 404
    with _as(OWNER):
        assert client.get(f"/gateway/v1/workflows/{hidden['azizam_workflow_id']}").status_code == 200
