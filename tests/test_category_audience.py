"""The owner's audience previews: which callers a category change reaches, as the runtime gate decides it.

Two previews. A candidate snapshot (the category review) reports each caller whose refusals change when the rules
change: a governed category that a caller's plans do not include is a loss, and removing it is a gain. A proposed
access plan (the plan editor) reports the callers that use that plan whose refusals change when its categories
change. The route cases read the preview the API returns. The parity cases activate the change and ask the runtime
gate the same question.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import accounts, admin_runtime, admin_store, category_audience, category_enforcement, category_policy as policy
from gateway.admin_api import AdminContext
from gateway.category_api import create_category_router, snapshot_to_json
from gateway.category_enforcement import is_owner_person, refusing_categories
from gateway.category_store import CategoryStore, NoActiveSnapshotError
from gateway.policy import PolicyRequest
from gateway.runtime_auth import hash_api_key
from tests.support.access import plan_document, plan_for, store_plan
from tests.support.app_keys import issue_app_key

ISSUER = "https://tests.example"
PREFIX = "/admin/api/categories"
REQUEST = PolicyRequest(protocol="openai.images", intent="image.generate", workflow="adult-flow")
UNREADABLE = "admin_store_unreadable"


def _owner(_request):
    return AdminContext(subject="owner-tester", roles=frozenset({"owner"}))


def rules(*, adult=True, empty=False):
    """The owner's rules. ``adult``: an Adult category holding ``adult-flow``. ``empty``: a category that holds nothing."""
    categories = {}
    if adult:
        categories["adult"] = policy.Category("adult", "Adult", values=("included",), applies_to=("release", "workflow"))
        fact = policy.Assertion("adult", "included", "owner", True, "2026-01-01", "2026-01-01", "usr_owner", "owner-edit-1")
        service = policy.Service("adult-flow@r1", (policy.Subject("adult-flow@r1", "workflow", (fact,)),))
    else:
        service = policy.Service("adult-flow@r1", (policy.Subject("adult-flow@r1", "workflow"),))
    if empty:
        categories["empty"] = policy.Category("empty", "Empty", values=("included",), applies_to=("release", "workflow"))
    return policy.PolicyVersion("rules", categories, {}, {}, {service.id: service})


def items_in_adult(*, first, second):
    """Two items, first-flow and second-flow. Each is in the adult category when its flag is set, and in none otherwise."""
    categories = {"adult": policy.Category("adult", "Adult", values=("included",), applies_to=("release", "workflow"))}
    services = {}
    for name, in_adult in (("first-flow", first), ("second-flow", second)):
        facts = ()
        if in_adult:
            facts = (policy.Assertion("adult", "included", "owner", True, "2026-01-01", "2026-01-01", "usr_owner",
                                      "owner-edit-1"),)
        services[f"{name}@r1"] = policy.Service(f"{name}@r1", (policy.Subject(f"{name}@r1", "workflow", facts),))
    return policy.PolicyVersion("rules", categories, {}, {}, services)


def active_revision(path):
    try:
        return CategoryStore(path).get_active().revision
    except NoActiveSnapshotError:
        return None


def activate(path, version):
    store = CategoryStore(path)
    active = active_revision(path)
    row = store.create_candidate(snapshot_to_json(version), active, "owner-tester", "test rules")
    store.activate(row.revision, active)


def activate_candidate(client, world, revision):
    response = client.post(PREFIX + f"/candidates/{revision}/activate",
                           json={"expected_active_revision": active_revision(world.categories)})
    assert response.status_code == 200, response.text


def preview_of(client, world, version):
    """Post a candidate on top of the active rules, as the owner does. Returns the response body."""
    response = client.post(PREFIX + "/candidates", json={
        "base_revision": active_revision(world.categories), "reason": "owner edit",
        "snapshot": snapshot_to_json(version)})
    assert response.status_code == 200, response.text
    return response.json()


def plan_preview(client, plan_id, categories):
    """Propose ``categories`` for a saved plan (or a new one when ``plan_id`` is None). Returns the response body."""
    response = client.post(PREFIX + "/plan-reach", json={
        "plan_id": plan_id, "plan": plan_document("Proposed", categories=categories)})
    assert response.status_code == 200, response.text
    return response.json()


@dataclass(frozen=True)
class World:
    db: Path
    categories: Path
    owner: str
    family: str
    family_plan: str
    kids_plan: str
    personal_app: str
    personal_key: str
    kiosk_app: str
    kiosk_key: str


@pytest.fixture
def world(tmp_path):
    """An owner, a family member, and two Apps whose person plan and app plan include no category.

    The personal App's key is the family member's own; the service App's key carries no person. Both are refused by
    the Adult category until the person plan and the app plan they use both list it.
    """
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    owner = admin_store.upsert_oidc_user(ISSUER, "owner-sub", display_name="Owner", path=db)["id"]
    assert admin_store.claim_first_owner(owner, granted_by=owner, path=db)
    family = admin_store.upsert_oidc_user(ISSUER, "family-sub", display_name="Family", path=db)["id"]
    family_plan = store_plan("Family", path=db, categories=())["id"]
    kids_plan = store_plan("Kids apps", path=db, categories=())["id"]
    accounts.change_person(family, "test", path=db, plan_id=family_plan)
    personal = admin_store.create_app("Family notes", kind="personal", owner_user_id=family,
                                      access_plan=kids_plan, path=db)["id"]
    kiosk = admin_store.create_app("Kiosk", kind="service", owner_user_id=family, access_plan=kids_plan, path=db)["id"]
    return World(db=db, categories=tmp_path / "categories.db", owner=owner, family=family,
                 family_plan=family_plan, kids_plan=kids_plan,
                 personal_app=personal, personal_key=issue_app_key(db, personal),
                 kiosk_app=kiosk, kiosk_key=issue_app_key(db, kiosk))


@pytest.fixture
def client(world):
    app = FastAPI()
    app.include_router(create_category_router(auth=_owner, store_path=world.categories, admin_path=world.db))
    return TestClient(app)


def _include(world, plan_id, category):
    """Have a saved plan list one more category, as the plan editor's Save does."""
    conn = sqlite3.connect(world.db)
    try:
        (raw,) = conn.execute("SELECT plan_json FROM access_plans WHERE id = ?", (plan_id,)).fetchone()
        document = json.loads(raw)
        document["categories"] = sorted({*document["categories"], category})
        conn.execute("UPDATE access_plans SET plan_json = ? WHERE id = ?",
                     (json.dumps(document, sort_keys=True), plan_id))
        conn.commit()
    finally:
        conn.close()


def adult_entry(world, app, name):
    """The audience entry for one of the family's Apps, for the adult-flow item in the adult category."""
    return item_entry(world, app, name, "adult-flow")


def both_apps(world):
    return [adult_entry(world, world.personal_app, "Family notes"), adult_entry(world, world.kiosk_app, "Kiosk")]


def item_entry(world, app, name, item, category="adult"):
    """The audience entry for one of the family's Apps, for one item in the adult category."""
    return {"item_id": item, "category_id": category, "category_label": "Adult", "app_id": app, "app_name": name,
            "person_id": world.family, "person_label": "Family", "person_plan_id": world.family_plan,
            "app_plan_id": world.kids_plan}


def both_apps_for(world, item):
    return by_app([item_entry(world, world.personal_app, "Family notes", item),
                   item_entry(world, world.kiosk_app, "Kiosk", item)])


def by_app(entries):
    return sorted(entries, key=lambda entry: entry["app_id"])


def hidden(world, identity):
    """What the runtime gate answers: True when the adult category hides the item from this identity."""
    gate = category_enforcement.CategoryGate(world.categories, owner_id=lambda: world.owner)
    decision = gate.check(identity, REQUEST)
    return decision is not None and category_enforcement.is_hidden(decision.reasons)


# A candidate that changes which callers a governed category refuses.

def test_adding_a_category_that_each_callers_plans_lack_is_a_loss_for_each_of_them(world, client):
    activate(world.categories, rules(adult=False))
    body = preview_of(client, world, rules())
    assert body["preview"]["audience_reason"] is None
    assert body["preview"]["audience"] == {"gains": [], "losses": by_app(both_apps(world)), "cannot_tell": []}


def test_removing_a_category_that_refused_each_caller_is_a_gain_for_each_of_them(world, client):
    activate(world.categories, rules())
    audience = preview_of(client, world, rules(adult=False))["preview"]["audience"]
    assert audience == {"gains": by_app(both_apps(world)), "losses": [], "cannot_tell": []}


def test_moving_one_item_out_of_a_category_is_a_gain_for_that_item_only(world, client):
    """Adult refuses first-flow and second-flow for every caller. Moving first-flow out leaves Adult refusing second-flow,
    so the category's refusal is unchanged, but first-flow becomes usable: a gain for first-flow alone."""
    activate(world.categories, items_in_adult(first=True, second=True))
    audience = preview_of(client, world, items_in_adult(first=False, second=True))["preview"]["audience"]
    assert audience == {"gains": both_apps_for(world, "first-flow"), "losses": [], "cannot_tell": []}


def test_moving_one_item_into_a_category_is_a_loss_for_that_item_only(world, client):
    """first-flow is in Adult already. Moving second-flow in changes only second-flow's answer: a loss for second-flow alone."""
    activate(world.categories, items_in_adult(first=True, second=False))
    audience = preview_of(client, world, items_in_adult(first=True, second=True))["preview"]["audience"]
    assert audience == {"gains": [], "losses": both_apps_for(world, "second-flow"), "cannot_tell": []}


def test_adding_a_category_that_every_caller_plan_already_lists_changes_no_caller(world, client):
    _include(world, world.family_plan, "adult")
    _include(world, world.kids_plan, "adult")
    activate(world.categories, rules(adult=False))
    assert preview_of(client, world, rules())["preview"]["audience"] == {"gains": [], "losses": [], "cannot_tell": []}


@pytest.mark.parametrize("empty", [False, True])
def test_a_candidate_that_changes_no_caller_reports_nothing(world, client, empty):
    """An unchanged rule set, and an added category that holds nothing, both leave every audience as it was."""
    activate(world.categories, rules())
    audience = preview_of(client, world, rules(empty=empty))["preview"]["audience"]
    assert audience == {"gains": [], "losses": [], "cannot_tell": []}


def test_restoring_an_earlier_revision_reports_the_callers_it_would_refuse(world, client):
    activate(world.categories, rules())
    earlier = active_revision(world.categories)
    body = preview_of(client, world, rules(adult=False))
    activate_candidate(client, world, body["revision"])
    response = client.post(PREFIX + f"/revisions/{earlier}/restore",
                           json={"base_revision": active_revision(world.categories), "reason": "undo"})
    assert response.status_code == 200, response.text
    assert response.json()["preview"]["audience"] == {"gains": [], "losses": by_app(both_apps(world)), "cannot_tell": []}


# A proposed access plan: the callers that use it.

def test_a_plan_that_gains_a_category_is_a_gain_for_each_caller_that_uses_it(world, client):
    _include(world, world.kids_plan, "adult")
    activate(world.categories, rules())
    body = plan_preview(client, world.family_plan, ["adult"])
    assert body["audience_reason"] is None
    assert body["audience"] == {"gains": by_app(both_apps(world)), "losses": [], "cannot_tell": []}


def test_a_plan_gains_nothing_while_the_callers_other_plan_still_lacks_the_category(world, client):
    activate(world.categories, rules())
    body = plan_preview(client, world.kids_plan, ["adult"])
    assert body["audience"] == {"gains": [], "losses": [], "cannot_tell": []}


def test_a_plan_that_loses_a_category_is_a_loss_for_each_caller_that_uses_it(world, client):
    _include(world, world.family_plan, "adult")
    _include(world, world.kids_plan, "adult")
    activate(world.categories, rules())
    body = plan_preview(client, world.family_plan, [])
    assert body["audience"] == {"gains": [], "losses": by_app(both_apps(world)), "cannot_tell": []}


def test_a_plan_not_saved_yet_reaches_no_caller(world, client):
    activate(world.categories, rules())
    assert plan_preview(client, None, ["adult"])["audience"] == {"gains": [], "losses": [], "cannot_tell": []}


def test_a_plan_preview_with_an_unknown_field_is_refused_as_an_invalid_request(world, client):
    activate(world.categories, rules())
    response = client.post(PREFIX + "/plan-reach", json={
        "plan_id": world.kids_plan, "plan": {**plan_document("Proposed"), "audience": ["adult"]}})
    assert response.status_code == 400


# Callers that gain nothing, and callers the preview cannot decide.

def test_a_disabled_app_gains_nothing_so_it_is_not_reported(world, client):
    admin_store.disable_app(world.personal_app, path=world.db)
    activate(world.categories, rules())
    audience = preview_of(client, world, rules(adult=False))["preview"]["audience"]
    assert audience == {"gains": [adult_entry(world, world.kiosk_app, "Kiosk")], "losses": [], "cannot_tell": []}


@pytest.mark.parametrize("plan, field, reason", [
    ("family_plan", "person_plan_id", "person plan missing or unreadable"),
    ("kids_plan", "app_plan_id", "app plan missing or unreadable"),
])
def test_a_caller_whose_plan_cannot_be_read_is_listed_as_undecided_not_compared(world, client, plan, field, reason):
    activate(world.categories, rules(adult=False))
    conn = sqlite3.connect(world.db)
    conn.execute("UPDATE access_plans SET plan_json = 'not json' WHERE id = ?", (getattr(world, plan),))
    conn.commit()
    conn.close()
    audience = preview_of(client, world, rules())["preview"]["audience"]
    assert audience["gains"] == [] and audience["losses"] == []
    assert [(entry["app_id"], entry[field], entry["reason"]) for entry in audience["cannot_tell"]] == sorted(
        (app, None, reason) for app in (world.personal_app, world.kiosk_app))


def test_the_owners_own_key_and_session_are_exempt_and_a_trusted_address_on_their_app_is_not(world, client):
    owner_plan = store_plan("Owner", path=world.db, categories=())["id"]
    accounts.change_person(world.owner, "test", path=world.db, plan_id=owner_plan)
    console = admin_store.create_app("Owner console", kind="personal", owner_user_id=world.owner,
                                     access_plan="person", path=world.db)["id"]
    issue_app_key(world.db, console)  # The owner's own key: the owner in person, so exempt.
    owner_session = admin_store.ensure_session_app_for_user(world.owner, name="Owner session", path=world.db)["id"]
    family_session = admin_store.ensure_session_app_for_user(world.family, name="Family session", path=world.db)["id"]
    admin_store.add_trusted_address(console, "192.0.2.7", created_by=world.owner, max_entries=8, path=world.db)
    activate(world.categories, rules())
    audience = preview_of(client, world, rules(adult=False))["preview"]["audience"]
    gains = {entry["app_id"]: entry for entry in audience["gains"]}
    assert set(gains) == {world.personal_app, world.kiosk_app, family_session, console}
    assert owner_session not in gains
    assert gains[console] == {"item_id": "adult-flow", "category_id": "adult", "category_label": "Adult",
                              "app_id": console, "app_name": "Owner console", "person_id": world.owner,
                              "person_label": "Owner", "person_plan_id": owner_plan, "app_plan_id": owner_plan}
    assert audience["losses"] == [] and audience["cannot_tell"] == []


def test_an_admin_store_that_is_not_configured_says_so():
    assert category_audience.candidate_audience(None, rules(), admin_path=None) == (None, "admin_store_not_configured")


def test_a_missing_admin_store_is_unreadable_and_is_not_created(tmp_path):
    path = tmp_path / "admin.db"
    assert category_audience.candidate_audience(None, rules(), admin_path=path) == (None, UNREADABLE)
    assert not path.exists()


def test_a_damaged_admin_store_is_unreadable_not_empty(tmp_path):
    path = tmp_path / "admin.db"
    path.write_bytes(b"not a database" * 64)
    assert category_audience.candidate_audience(None, rules(), admin_path=path) == (None, UNREADABLE)


def test_the_candidate_route_leaves_the_audience_empty_when_the_admin_store_is_damaged(tmp_path):
    damaged = tmp_path / "admin.db"
    damaged.write_bytes(b"not a database" * 64)
    categories = tmp_path / "categories.db"
    activate(categories, rules(adult=False))
    app = FastAPI()
    app.include_router(create_category_router(auth=_owner, store_path=categories, admin_path=damaged))
    response = TestClient(app).post(PREFIX + "/candidates", json={
        "base_revision": active_revision(categories), "reason": "owner edit",
        "snapshot": snapshot_to_json(rules())})
    assert response.status_code == 200, response.text
    assert response.json()["preview"]["audience"] is None
    assert response.json()["preview"]["audience_reason"] == UNREADABLE


# The preview and the runtime agree.

@pytest.mark.parametrize("gained", [True, False])
def test_the_preview_says_what_the_runtime_gate_does_once_the_change_is_active(world, client, gained):
    before, after = (rules(), rules(adult=False)) if gained else (rules(adult=False), rules())
    activate(world.categories, before)
    identity = admin_runtime.runtime_identity_for_app_credential_hash(hash_api_key(world.personal_key), path=world.db)
    assert hidden(world, identity) is gained
    body = preview_of(client, world, after)
    heading = body["preview"]["audience"]["gains" if gained else "losses"]
    assert world.personal_app in {entry["app_id"] for entry in heading}
    activate_candidate(client, world, body["revision"])
    assert hidden(world, identity) is (not gained)


@pytest.mark.parametrize("key, app", [("personal_key", "personal_app"), ("kiosk_key", "kiosk_app")])
def test_each_caller_is_built_the_way_the_runtime_builds_the_identity_for_its_key(world, key, app):
    callers = {caller.identity.app_id: caller.identity for caller in admin_runtime.audience_callers(path=world.db)}
    runtime = admin_runtime.runtime_identity_for_app_credential_hash(hash_api_key(getattr(world, key)), path=world.db)
    assert runtime is not None
    mirrored = callers[getattr(world, app)]
    assert (mirrored.person_id, mirrored.accountable_person_id, mirrored.access, mirrored.denial) == (
        runtime.person_id, runtime.accountable_person_id, runtime.access, runtime.denial)


# The shared decisions, as tables.

@pytest.mark.parametrize("person_categories, app_categories, refused", [
    ("all", "all", False),
    (["adult"], ["adult"], False),
    (["adult"], (), True),    # the app plan does not include it
    ((), ["adult"], True),    # the person plan does not include it
    (None, ["adult"], True),  # a missing plan includes nothing
    (["adult"], None, True),
])
def test_an_item_is_refused_unless_both_plans_include_its_category(person_categories, app_categories, refused):
    person = None if person_categories is None else plan_for("plan_a", categories=person_categories)
    app = None if app_categories is None else plan_for("plan_b", categories=app_categories)
    assert bool(refusing_categories(("adult",), person, app)) is refused


def test_an_item_in_no_category_is_never_refused():
    assert refusing_categories((), None, None) == ()


@pytest.mark.parametrize("person, owner, exempt", [
    ("usr_owner", "usr_owner", True),
    ("usr_family", "usr_owner", False),
    (None, "usr_owner", False),  # a trusted address carries no person
    ("usr_owner", None, False),  # no owner recorded: nobody is exempt
])
def test_only_the_instance_owner_is_exempt(person, owner, exempt):
    assert is_owner_person(person, owner) is exempt
