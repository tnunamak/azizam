"""The owner hands a service app to another person; the app and its keys keep working when the old person leaves."""
from __future__ import annotations

import json
import sqlite3

import pytest

from gateway import accounts, admin_store
from gateway.admin_runtime import runtime_identity_for_app_credential_hash
from gateway.runtime_auth import hash_api_key
from tests.support.access import store_plan
from tests.support.app_keys import issue_app_key
from tests.test_service_apps_create import _create, _people
from tests.test_signin_who_can_enter import OWNER, csrf_for, gateway  # noqa: F401  (gateway is a fixture)


def _identity(g, key):
    return runtime_identity_for_app_credential_hash(hash_api_key(key), path=g.path)


def _handover(client, app_id, person_id):
    return client.patch(f"/admin/api/apps/{app_id}", headers=csrf_for(client), json={"owner_user_id": person_id})


@pytest.fixture
def world(gateway):
    owner, admin, member = _people(gateway)
    owner_id = gateway.user(OWNER)["id"]
    member_id = gateway.user("member@example.test")["id"]
    chat_only = store_plan("Chat only", path=gateway.path, caps=("chat",))
    accounts.change_person(member_id, owner_id, plan_id=chat_only["id"], path=gateway.path)
    created = _create(owner, name="Door camera", kind="service", access_plan="person", owner_user_id=member_id)
    assert created.status_code == 201, created.text
    service_id = created.json()["app"]["id"]
    personal_id = _create(member, name="Laptop").json()["app"]["id"]
    return type("World", (), dict(g=gateway, owner=owner, admin=admin, member=member, owner_id=owner_id,
                                  member_id=member_id, chat_only=chat_only, service_id=service_id,
                                  personal_id=personal_id))


def test_a_key_made_before_the_handover_works_after_it_under_the_new_persons_plan(world):
    w = world
    key = issue_app_key(w.g.path, w.service_id)
    before = _identity(w.g, key)
    assert before is not None and not before.denial
    assert before.access.app_plan.id == w.chat_only["id"]

    moved = _handover(w.owner, w.service_id, w.owner_id)
    assert moved.status_code == 200, moved.text
    assert admin_store.get_app(w.service_id, path=w.g.path)["owner_user_id"] == w.owner_id

    after = _identity(w.g, key)
    assert after is not None and not after.denial
    assert after.auth_kind == "service_key"
    owner_plan = admin_store.person_plan_id(w.owner_id, path=w.g.path)
    assert owner_plan and owner_plan != w.chat_only["id"]
    assert after.access.app_plan.id == owner_plan


def test_removing_the_old_person_afterwards_keeps_the_moved_app_and_stops_their_personal_one(world):
    w = world
    service_key = issue_app_key(w.g.path, w.service_id)
    personal_key = issue_app_key(w.g.path, w.personal_id)
    assert _handover(w.owner, w.service_id, w.owner_id).status_code == 200

    accounts.change_person(w.member_id, w.owner_id, remove=True, path=w.g.path)

    kept = _identity(w.g, service_key)
    assert kept is not None and not kept.denial
    gone = _identity(w.g, personal_key)
    assert gone is None or gone.denial


def test_without_a_handover_removing_the_person_still_stops_their_service_app(world):
    w = world
    key = issue_app_key(w.g.path, w.service_id)
    accounts.change_person(w.member_id, w.owner_id, remove=True, path=w.g.path)
    identity = _identity(w.g, key)
    assert identity is None or identity.denial


def test_only_the_owner_can_hand_an_app_over(world):
    w = world
    for client in (w.admin, w.member):
        refused = _handover(client, w.service_id, w.owner_id)
        assert refused.status_code == 403, refused.text
    assert admin_store.get_app(w.service_id, path=w.g.path)["owner_user_id"] == w.member_id


def test_a_handover_needs_a_service_app_and_an_active_person(world):
    w = world
    assert _handover(w.owner, w.personal_id, w.owner_id).status_code == 400
    assert _handover(w.owner, w.service_id, "usr_missing").status_code == 400
    accounts.change_person(w.member_id, w.owner_id, disabled=True, path=w.g.path)
    helper_id = w.g.user("helper@example.test")["id"]
    accounts.change_person(helper_id, w.owner_id, plan_id=w.chat_only["id"], path=w.g.path)
    # A suspended person cannot be handed an app.
    moved_to_owner = _handover(w.owner, w.service_id, w.owner_id)
    assert moved_to_owner.status_code == 200
    again = _handover(w.owner, w.service_id, w.member_id)
    assert again.status_code == 400, again.text


def test_one_audit_row_names_the_old_and_the_new_person(world):
    w = world
    assert _handover(w.owner, w.service_id, w.owner_id).status_code == 200
    [row] = w.g.rows("SELECT metadata_json FROM audit_events WHERE action='app.handover' AND target_id=?",
                     (w.service_id,))
    metadata = json.loads(row["metadata_json"])
    assert (metadata["from_user_id"], metadata["to_user_id"]) == (w.member_id, w.owner_id)


def test_the_database_still_refuses_a_person_change_that_is_not_a_handover(world):
    w = world
    issue_app_key(w.g.path, w.service_id)
    with sqlite3.connect(w.g.path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE apps SET owner_user_id=? WHERE id=?", (w.owner_id, w.service_id))


def test_the_new_person_adds_keys_and_the_old_person_no_longer_can(world):
    w = world
    assert _handover(w.owner, w.service_id, w.owner_id).status_code == 200
    minted = w.owner.post(f"/me/api/apps/{w.service_id}/keys", headers={**csrf_for(w.owner), "x-azizam-user-action": "1"},
                          json={"name": "after"})
    assert minted.status_code in (200, 201), minted.text
    refused = w.member.post(f"/me/api/apps/{w.service_id}/keys", headers={**csrf_for(w.member), "x-azizam-user-action": "1"},
                            json={"name": "after"})
    assert refused.status_code >= 400
