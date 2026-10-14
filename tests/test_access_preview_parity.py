"""Check access and enforcement give the same answer for every item and plan.

Check access is a list drawn from the decision code that enforcement runs
(``evaluate_and_apply``), not a second evaluator. This test keeps it so. It
runs both code paths over one fixture catalog and a set of plans: the
``POST /admin/api/access/preview`` route on one side, and a real request's
policy decision for each item on the other. Any difference in allow or deny,
or in the rule that decided it, fails the test.

A "cannot tell" row is a row Check access will not call allowed. Enforcement
refuses it too (the rules it depends on are not available), so the two agree
on the one thing a caller can act on: it is not allowed.
"""
from __future__ import annotations

import sqlite3

import pytest

from gateway import category_enforcement
from gateway.access_plans import capability_for_intent, plan_from_document
from gateway.capabilities import Capability
from gateway.capability_ids import CAPABILITY_LABELS
from gateway.policy import EffectiveAccess
from gateway.runtime_auth import evaluate_and_apply, make_policy_request
from tests.support.access import identity as runtime_identity, service_app, store_plan
from tests.test_access_preview import _cap, _post, _preview, fixture_catalog, owner  # noqa: F401  (owner is a fixture)
from tests.test_category_visibility import OWNER, _service, activate, rules

TEXT = {"input_modalities": ("text",), "output_modalities": ("text",)}


def _alias(name, target):
    return _cap("chat.completions", name, tags=("alias", f"resolves_to:{target}"), **TEXT)


CATALOG = [
    _cap("chat.completions", "chat-a", **TEXT),
    _cap("chat.completions", "chat-b", **TEXT),
    _cap("chat.completions", "chat-secret", **TEXT),  # in the restricted category below
    _alias("fast", "chat-a"),
    _alias("slow", "chat-b"),
    _cap("chat.completions", "multi", **TEXT),
    _cap("image.generate", "multi", input_modalities=("text",), output_modalities=("image",)),
    _cap("image.generate", "img-a", input_modalities=("text",), output_modalities=("image",)),
    _cap("video.generate", "vid-a", input_modalities=("text",), output_modalities=("video",)),
    _cap("audio.speech", "tts-a", input_modalities=("text",), output_modalities=("audio",)),
]
VIEWS = {"chat": {"stable": [{"alias": "fast", "resolves_to": "chat-a", "service": "svc"},
                             {"alias": "slow", "resolves_to": "chat-b", "service": "svc"}]}}

# What each plan lists. ``None`` means every capability; a list narrows what each listed capability grants.
PLANS = {
    "everything": {},
    "chat only": {"caps": ("chat",)},
    "kids (no video)": {"caps": ("chat", "image", "speech")},
    "listed models": {"models": ["chat-a", "img-a", "tts-a", "multi"]},
    "lists an alias": {"caps": ("chat",), "models": ["fast"]},
    "lists the model behind an alias": {"caps": ("chat",), "models": ["chat-b"]},
    "nothing": {"caps": ()},
}


def _label(intent):
    return CAPABILITY_LABELS[capability_for_intent(intent)]


def _request_for(capability):
    """The question a real request for this catalog item puts to the evaluator."""
    target = next((tag.removeprefix("resolves_to:") for tag in capability.tags if tag.startswith("resolves_to:")), None)
    return make_policy_request(
        protocol="openai.chat", intent=capability.intent,
        model=target or capability.model, requested_model=capability.model if target else None)


def _shown(entry, label):
    """What Check access says for one kind of work of one entry."""
    kinds = {kind["label"]: kind for kind in entry["kinds"]}
    if label in kinds:
        return kinds[label]
    return {"allowed": entry["state"] == "allowed", "rule": entry["rule"], "state": entry["state"]}


def _compare(preview, access_for_requests):
    who = runtime_identity(access_for_requests, app_id=None, person_id=None)
    entries = {entry["id"]: entry for entry in preview["entries"]}
    checked = 0
    for capability in CATALOG:
        entry = entries[capability.model]  # every item the gateway offers is listed
        decision = evaluate_and_apply(who, _request_for(capability))
        shown = _shown(entry, _label(capability.intent))
        assert shown["allowed"] is decision.allowed, (capability.model, capability.intent, shown, decision)
        assert shown["rule"]["code"] == decision.rule.code, (capability.model, capability.intent, shown, decision)
        checked += 1
    assert checked == len(CATALOG)


@pytest.fixture
def category_gate(tmp_path):
    path = tmp_path / "categories.db"
    category_enforcement.configure(path, owner_id=lambda: OWNER)
    yield path
    category_enforcement.configure(None)


def _plan_of(owner, record):
    return plan_from_document(record["plan"], plan_id=str(record["id"]))


@pytest.mark.parametrize("name", PLANS)
def test_check_access_gives_enforcements_answer_for_every_item(owner, category_gate, name):
    plan = store_plan(name, path=owner.db, **PLANS[name])
    seer = store_plan("Sees the secret", path=owner.db)
    activate(category_gate, rules(adult_visible_to=[seer["id"]], extra_services=[_service("chat-secret", "adult")]))
    preview = _preview(owner, plan, CATALOG, VIEWS)
    _compare(preview, EffectiveAccess(_plan_of(owner, plan), _plan_of(owner, plan)))


def test_a_plan_listed_for_the_restricted_category_is_allowed_into_it(owner, category_gate):
    seer = store_plan("Sees the secret", path=owner.db)
    other = store_plan("Does not", path=owner.db)
    activate(category_gate, rules(adult_visible_to=[seer["id"]], extra_services=[_service("chat-secret", "adult")]))
    seen = {e["id"]: e for e in _preview(owner, seer, CATALOG, VIEWS)["entries"]}
    unseen = {e["id"]: e for e in _preview(owner, other, CATALOG, VIEWS)["entries"]}
    assert seen["chat-secret"]["state"] == "allowed"
    assert unseen["chat-secret"]["state"] == "blocked"
    assert unseen["chat-secret"]["rule"]["code"] == "category.hidden"
    # A restricted category hides the model under every name; the alias is judged on the model it reaches.
    assert unseen["chat-secret"]["rule"]["name"] == "Adult"


def test_an_app_is_judged_by_both_of_its_plans_exactly_as_a_request_is(owner):
    from gateway import accounts, admin_store

    person_plan = store_plan("Person plan", path=owner.db)
    app_plan = store_plan("App plan", path=owner.db, caps=("chat",))
    person = admin_store.upsert_oidc_user("https://tests.example", "p1", display_name="P1", path=owner.db)
    accounts.change_person(person["id"], "test", path=owner.db, plan_id=person_plan["id"])
    app = service_app("Tablet", path=owner.db, owner_user_id=person["id"], plan_id=app_plan["id"])
    with fixture_catalog(CATALOG, VIEWS):
        response = _post(owner, {"app": app["id"]})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["plan_ids"] == [person_plan["id"], app_plan["id"]]
    _compare(preview, EffectiveAccess(_plan_of(owner, person_plan), _plan_of(owner, app_plan)))
    entries = {e["id"]: e for e in preview["entries"]}
    # The app's plan is the one that refuses video, and the row says so.
    assert entries["vid-a"]["rule"]["id"] == app_plan["id"]
    assert entries["vid-a"]["rule"]["name"] == "App plan"
    assert entries["vid-a"]["reason"].startswith("The app's App plan")


def test_when_the_rules_cannot_be_read_enforcement_refuses_and_check_access_says_it_cannot_tell(owner, category_gate):
    plan = store_plan("Everything", path=owner.db)
    activate(category_gate, rules(adult_visible_to=[]))
    with sqlite3.connect(category_gate) as conn:
        conn.execute("DROP TRIGGER IF EXISTS policy_snapshots_no_update")
        conn.execute("UPDATE policy_snapshots SET body_json = '{\"broken\": true}'")
    preview = _preview(owner, plan, CATALOG, VIEWS)
    who = runtime_identity(EffectiveAccess(_plan_of(owner, plan), _plan_of(owner, plan)), app_id=None, person_id=None)
    for entry in preview["entries"]:
        assert entry["state"] == "unknown", entry
    for capability in CATALOG:
        assert evaluate_and_apply(who, _request_for(capability)).denied
    assert preview["summary"]["usable"] == 0 and preview["summary"]["unknown"] == len(preview["entries"])


def test_a_provider_that_cannot_be_read_is_a_cannot_tell_and_never_allowed(owner):
    plan = store_plan("Everything", path=owner.db)
    remote = Capability(intent="chat.completions", backend="vanished-provider", model="remote-m",
                        tags=("remote",), **TEXT)
    body = _preview(owner, plan, [remote, *CATALOG[:1]], VIEWS)
    entries = {e["id"]: e for e in body["entries"]}
    assert entries["remote-m"]["state"] == "unknown"
    assert entries["remote-m"]["rule"]["code"] == "provider.config_unavailable"
    assert entries["chat-a"]["state"] == "allowed"
