"""Owner visibility step 2: a person keeps an unproven key or address on their own App.

Before the cut-over, some App keys and trusted addresses have no recorded creator, or name someone other
than the App's person. The App's accountable person can keep one (adopt it): the binding then names them
as its creator and the cut-over leaves it alone. Nobody else can, the owner included, and a control-plane
key or an API key cannot at all. Keeping is a sensitive action, so it asks for a recent sign-in.
"""

from __future__ import annotations

import sqlite3
import uuid

from gateway import admin_store, app_binding_preview, step_up
from tests.support.app_keys import before_the_cut_over, issue_app_key
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin, signed_in  # noqa: F401  (gateway is a fixture)

ACT = {"x-azizam-user-action": "1"}


def _member(g, sub):
    g.set_rules([], fallback="open")
    client, response = signed_in(g, sub=sub, email=f"{sub}@example.test")
    assert response.status_code == 302, response.text
    return client


def _headers(client):
    return {**csrf_for(client), **ACT}


def _own_app(client, name="Desk"):
    created = client.post("/me/api/apps", headers=_headers(client), json={"name": name})
    assert created.status_code == 201, created.text
    return created.json()["app"]["id"]


def _unproven_key(g, app_id, created_by=None):
    """A key written the way an existing database holds it before the cut-over: no creator, or another person."""
    return admin_store.create_credential_hash(
        subject_kind="app", subject_id=app_id, app_id=app_id, hash=f"sha256:{uuid.uuid4().hex}",
        prefix="opk_legacy", created_by=created_by, path=g.path,
    )["id"]


def _unproven_address(g, app_id, address, created_by=None):
    entry_id = f"tad_{uuid.uuid4().hex[:10]}"
    conn = sqlite3.connect(g.path)
    try:
        conn.execute("INSERT INTO app_trusted_addresses(id, app_id, address, created_at, created_by) "
                     "VALUES (?, ?, ?, 1, ?)", (entry_id, app_id, address, created_by))
        conn.commit()
    finally:
        conn.close()
    return entry_id


def _creator_of_key(g, key_id):
    return g.rows("SELECT created_by FROM credentials WHERE id = ?", (key_id,))[0]["created_by"]


def _creator_of_address(g, entry_id):
    return g.rows("SELECT created_by FROM app_trusted_addresses WHERE id = ?", (entry_id,))[0]["created_by"]


def _audit(g, action):
    return g.rows("SELECT actor_kind, actor_id, target_kind, target_id, metadata FROM audit_events WHERE action = ?", (action,))


def test_a_person_keeps_an_unproven_key_on_their_own_app_as_their_own(gateway):
    desk = _member(gateway, "desk")
    person = gateway.user("desk@example.test")["id"]
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    kept = desk.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(desk))

    assert kept.status_code == 200, kept.text
    assert kept.json()["credential"]["id"] == key_id
    assert _creator_of_key(gateway, key_id) == person


def test_keeping_a_key_writes_an_audit_event_that_names_the_person(gateway):
    desk = _member(gateway, "desk")
    person = gateway.user("desk@example.test")["id"]
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    desk.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(desk))

    events = _audit(gateway, "credential.adopt")
    assert len(events) == 1
    assert (events[0]["actor_kind"], events[0]["actor_id"]) == ("user", person)
    assert (events[0]["target_kind"], events[0]["target_id"]) == ("credential", key_id)


def test_a_person_keeps_an_unproven_address_on_their_own_app_as_their_own(gateway):
    desk = _member(gateway, "desk")
    person = gateway.user("desk@example.test")["id"]
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    entry_id = _unproven_address(gateway, app_id, "10.0.0.7", created_by=None)

    kept = desk.post(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt", headers=_headers(desk))

    assert kept.status_code == 200, kept.text
    assert kept.json()["trusted_address"]["id"] == entry_id
    assert _creator_of_address(gateway, entry_id) == person
    events = _audit(gateway, "app.trusted_address.adopt")
    assert len(events) == 1 and events[0]["target_id"] == app_id


def test_a_kept_binding_counts_as_proven_in_the_cut_over_preview(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    entry_id = _unproven_address(gateway, app_id, "10.0.0.8", created_by=None)
    desk.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(desk))
    desk.post(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt", headers=_headers(desk))

    conn = app_binding_preview.connect_read_only(gateway.path)
    try:
        body = app_binding_preview.build(conn, as_of=1_800_000_000)
    finally:
        conn.close()
    listed = next(app for app in body["apps"] if app["app_id"] == app_id)
    assert [(key["id"], key["action"]) for key in listed["keys"]] == [(key_id, "keep")]
    assert [(entry["id"], entry["action"]) for entry in listed["trusted_addresses"]] == [(entry_id, "keep")]


def test_a_person_cannot_keep_a_key_on_someone_elses_app(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    other = _member(gateway, "other")

    refused = other.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(other))

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "not_your_app"
    assert _creator_of_key(gateway, key_id) is None
    assert _audit(gateway, "credential.adopt") == []


def test_the_owner_cannot_keep_a_key_on_a_member_app(gateway):
    owner, _owner_id, _plan, _admin = owner_and_admin(gateway)
    member = _member(gateway, "desk")
    app_id = _own_app(member)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    refused = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "not_your_app"
    assert _creator_of_key(gateway, key_id) is None


def test_a_person_cannot_keep_an_address_on_someone_elses_app(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    entry_id = _unproven_address(gateway, app_id, "10.0.0.9", created_by=None)
    other = _member(gateway, "other")

    refused = other.post(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt", headers=_headers(other))

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "not_your_app"
    assert _creator_of_address(gateway, entry_id) is None


def test_a_revoked_key_cannot_be_kept(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    admin_store.revoke_credential(key_id, path=gateway.path)

    refused = desk.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(desk))

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "credential_not_found"


def test_a_key_that_is_not_an_app_key_cannot_be_kept(gateway):
    desk = _member(gateway, "desk")
    _own_app(desk)
    control = admin_store.create_credential_hash(
        subject_kind="service_account", subject_id="svc_control", hash=f"sha256:{uuid.uuid4().hex}",
        prefix="oak_control", path=gateway.path,
    )["id"]

    refused = desk.post(f"/me/api/credentials/{control}/adopt", headers=_headers(desk))

    assert refused.status_code == 404, refused.text


def test_a_disabled_app_refuses_keeping_a_binding(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    admin_store.disable_app(app_id, path=gateway.path)

    refused = desk.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(desk))

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "app_disabled"
    assert _creator_of_key(gateway, key_id) is None


def test_keeping_a_binding_that_already_names_its_person_changes_nothing(gateway):
    desk = _member(gateway, "desk")
    person = gateway.user("desk@example.test")["id"]
    app_id = _own_app(desk)
    key_id = _unproven_key(gateway, app_id, created_by=person)  # already proven, as after the cut-over

    kept = desk.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(desk))

    assert kept.status_code == 200, kept.text
    assert _creator_of_key(gateway, key_id) == person
    assert _audit(gateway, "credential.adopt") == []


def test_an_api_key_cannot_keep_a_binding(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    api_key = issue_app_key(gateway.path, app_id, label="desk automation")  # a live key for the very App

    refused = gateway.client().post(f"/me/api/credentials/{key_id}/adopt",
                                    headers={"authorization": f"Bearer {api_key}", **ACT})

    assert refused.status_code == 401, refused.text
    assert _creator_of_key(gateway, key_id) is None


def test_keeping_is_a_sensitive_action_that_asks_for_a_recent_sign_in():
    assert step_up.is_sensitive("POST", "/me/api/credentials/cred_abc/adopt")
    assert step_up.is_sensitive("POST", "/me/api/apps/app_abc/trusted-addresses/tad_abc/adopt")
