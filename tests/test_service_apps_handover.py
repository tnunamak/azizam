"""The owner hands a service app to another person: its keys and trusted addresses stop, the new person makes new ones."""
from __future__ import annotations

import json
import sqlite3

import pytest

from gateway import accounts, admin_store, app_binding_preview
from gateway.admin_runtime import runtime_identity_for_app_credential_hash
from gateway.runtime_auth import hash_api_key
from tests.support.access import store_plan
from tests.support.app_keys import issue_app_key
from tests.test_service_apps_create import _create, _people
from tests.test_signin_who_can_enter import OWNER, csrf_for, gateway, signed_in  # noqa: F401  (gateway is a fixture)


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


def _live_bindings(w):
    return w.g.rows("SELECT id, app_id, created_by FROM credentials WHERE app_id=? AND revoked_at IS NULL",
                    (w.service_id,))


def _address(w):
    admin_store.add_trusted_address(w.service_id, "203.0.113.7", created_by=w.member_id, max_entries=10, path=w.g.path)


def test_after_a_handover_old_keys_stop_and_a_key_the_new_person_makes_runs_under_their_plan(world):
    w = world
    old_key = issue_app_key(w.g.path, w.service_id)
    _address(w)
    before = _identity(w.g, old_key)
    assert before is not None and not before.denial
    assert before.access.app_plan.id == w.chat_only["id"]

    moved = _handover(w.owner, w.service_id, w.owner_id)
    assert moved.status_code == 200, moved.text
    assert admin_store.get_app(w.service_id, path=w.g.path)["owner_user_id"] == w.owner_id

    refused = _identity(w.g, old_key)
    assert refused is None or refused.denial
    assert w.g.rows("SELECT 1 FROM app_trusted_addresses WHERE app_id=?", (w.service_id,)) == []

    new_key = issue_app_key(w.g.path, w.service_id)  # issued by the app's new person, as the product does
    after = _identity(w.g, new_key)
    assert after is not None and not after.denial
    assert after.auth_kind == "service_key" and after.person_id is None
    owner_plan = admin_store.person_plan_id(w.owner_id, path=w.g.path)
    assert owner_plan and owner_plan != w.chat_only["id"]
    assert after.access.app_plan.id == owner_plan


def test_the_cutover_and_its_preview_find_nothing_to_revoke_or_flag_on_a_moved_app(world):
    w = world
    issue_app_key(w.g.path, w.service_id)
    assert _handover(w.owner, w.service_id, w.owner_id).status_code == 200
    issue_app_key(w.g.path, w.service_id)
    assert admin_store.cutover_for_bindings(w.owner_id, keys=_live_bindings(w), path=w.g.path) == {}
    conn = app_binding_preview.connect_read_only(w.g.path)
    try:
        body = app_binding_preview.build(conn, as_of=2_000_000_000)
    finally:
        conn.close()
    [entry] = [app for app in body["apps"] if app["app_id"] == w.service_id]
    assert entry["keys"] and all(key["action"] == "keep" and key["proven"] for key in entry["keys"])
    assert entry["blocks_apply"] == []


def test_a_handover_that_cannot_write_its_audit_row_changes_nothing(world, monkeypatch):
    w = world
    key = issue_app_key(w.g.path, w.service_id)
    _address(w)

    def broken(*_args, **_kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(admin_store, "_audit_in_write", broken)
    with pytest.raises(RuntimeError):
        admin_store.reassign_service_app(
            w.service_id, w.owner_id, audit={"action": "app.handover", "actor_kind": "user", "actor_id": w.owner_id},
            path=w.g.path)
    assert admin_store.get_app(w.service_id, path=w.g.path)["owner_user_id"] == w.member_id
    assert len(_live_bindings(w)) == 1
    assert len(w.g.rows("SELECT 1 FROM app_trusted_addresses WHERE app_id=?", (w.service_id,))) == 1
    still = _identity(w.g, key)
    assert still is not None and not still.denial


def test_removing_the_old_person_afterwards_leaves_the_moved_app_working_and_stops_their_personal_one(world):
    w = world
    personal_key = issue_app_key(w.g.path, w.personal_id)
    assert _handover(w.owner, w.service_id, w.owner_id).status_code == 200
    service_key = issue_app_key(w.g.path, w.service_id)

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


def test_a_handover_needs_a_service_app_and_naming_the_same_person_changes_nothing(world):
    w = world
    key = issue_app_key(w.g.path, w.service_id)
    assert _handover(w.owner, w.personal_id, w.owner_id).status_code == 400
    assert _handover(w.owner, w.service_id, w.member_id).status_code == 200
    assert admin_store.get_app(w.service_id, path=w.g.path)["owner_user_id"] == w.member_id
    still = _identity(w.g, key)
    assert still is not None and not still.denial


def test_a_person_who_cannot_answer_for_an_app_is_refused_with_a_409(world):
    w = world
    key = issue_app_key(w.g.path, w.service_id)

    def person(email):
        signed_in(w.g, sub=email, email=email)
        helper = w.g.user(email)["id"]
        accounts.change_person(helper, w.owner_id, plan_id=w.chat_only["id"], path=w.g.path)
        return helper

    suspended, removed, refused = person("a@example.test"), person("b@example.test"), person("c@example.test")
    accounts.change_person(suspended, w.owner_id, disabled=True, path=w.g.path)
    accounts.change_person(removed, w.owner_id, remove=True, path=w.g.path)
    accounts.record_signin_refusal(refused, path=w.g.path)
    with sqlite3.connect(w.g.path) as conn:  # a user row that never got an access row
        conn.execute("INSERT INTO users(id, issuer, subject, email, display_name, created_at) "
                     "VALUES ('usr_noaccess', 'https://x.invalid', 'noaccess', NULL, 'No access', 0)")
    for target in (suspended, removed, refused, "usr_noaccess"):
        response = _handover(w.owner, w.service_id, target)
        assert response.status_code == 409, (target, response.text)
    assert admin_store.get_app(w.service_id, path=w.g.path)["owner_user_id"] == w.member_id
    still = _identity(w.g, key)
    assert still is not None and not still.denial


def test_one_audit_row_names_the_old_and_the_new_person_and_the_counts(world):
    w = world
    issue_app_key(w.g.path, w.service_id)
    _address(w)
    assert _handover(w.owner, w.service_id, w.owner_id).status_code == 200
    [row] = w.g.rows("SELECT metadata_json FROM audit_events WHERE action='app.handover' AND target_id=?",
                     (w.service_id,))
    metadata = json.loads(row["metadata_json"])
    assert (metadata["from_user_id"], metadata["to_user_id"]) == (w.member_id, w.owner_id)
    assert metadata["owner_user_id"] == w.owner_id
    assert (metadata["revoked_keys"], metadata["removed_addresses"]) == (1, 1)


def test_the_original_guard_is_back_a_bound_app_cannot_change_person_by_a_raw_update(world):
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
