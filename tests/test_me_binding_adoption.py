"""Owner visibility step 2: keeping an unproven binding, and replacing a key the person still uses.

Before the cut-over, some App keys and trusted addresses have no recorded creator, or name someone other
than the App's person. Keeping one (adopt) makes the App's person its creator, so the cut-over leaves it
alone. That claim is only true when nobody else could have made the binding: the owner has held the owner
role and no one else ever has, and the audit log names no other creator. Only the owner can keep. Anyone
else replaces a key instead (a new key on the same App, the old one revoked), and adds an address again.
A control-plane key or an API key cannot do either. Keeping and replacing ask for a recent sign-in.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from gateway import admin_store, app_binding_preview, step_up
from gateway.receipt_context import person_label
from gateway.runtime_auth import hash_api_key
from tests.support.app_keys import before_the_cut_over, issue_app_key
from tests.test_signin_who_can_enter import OWNER, csrf_for, gateway, owner_and_admin, signed_in  # noqa: F401  (gateway is a fixture)

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


def _unproven_key(g, app_id, created_by=None, display_name=None):
    """A key written the way an existing database holds it before the cut-over: no creator, or another person."""
    return admin_store.create_credential_hash(
        subject_kind="app", subject_id=app_id, app_id=app_id, hash=f"sha256:{uuid.uuid4().hex}",
        prefix="opk_legacy", display_name=display_name, created_by=created_by, path=g.path,
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


def _credential(g, key_id):
    return g.rows("SELECT * FROM credentials WHERE id = ?", (key_id,))[0]


def _creator_of_key(g, key_id):
    return _credential(g, key_id)["created_by"]


def _creator_of_address(g, entry_id):
    return g.rows("SELECT created_by FROM app_trusted_addresses WHERE id = ?", (entry_id,))[0]["created_by"]


def _audit(g, action):
    return g.rows("SELECT actor_kind, actor_id, target_kind, target_id, metadata_json FROM audit_events WHERE action = ?", (action,))


def _live_keys_on(g, app_id):
    return [row["id"] for row in g.rows(
        "SELECT id FROM credentials WHERE subject_kind = 'app' AND app_id = ? AND revoked_at IS NULL ORDER BY id", (app_id,))]


def _owner_and_app(g):
    owner, owner_id, _plan, _admin = owner_and_admin(g)
    return owner, owner_id, _own_app(owner)


# The owner fixture's helper is an administrator, so keeping a binding asks the owner to confirm (D55 amendment).
CONFIRM = {"confirm_admins_could_have_made_it": True}


def _admins(people=(), unnamed=0, keys=0):
    """What the gateway says the administrators on record are: named people, unnamed people, administrator keys."""
    return {"people": list(people), "unnamed_people": unnamed, "admin_keys": keys}


def _display_name(g, email, name):
    conn = sqlite3.connect(g.path)
    try:
        conn.execute("UPDATE users SET display_name = ? WHERE LOWER(email) = ?", (name, email))
        conn.commit()
    finally:
        conn.close()


# --- Keeping: only the owner, and only a binding nobody else could have made ----------------------------

def test_the_owner_keeps_an_unproven_key_on_their_own_app_as_their_own(gateway):
    owner, owner_id, app_id = _owner_and_app(gateway)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    kept = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=CONFIRM)

    assert kept.status_code == 200, kept.text
    assert kept.json()["credential"]["id"] == key_id
    assert _creator_of_key(gateway, key_id) == owner_id


def test_keeping_a_key_writes_an_audit_event_that_names_the_person(gateway):
    owner, owner_id, app_id = _owner_and_app(gateway)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=CONFIRM)

    events = _audit(gateway, "credential.adopt")
    assert len(events) == 1
    assert (events[0]["actor_kind"], events[0]["actor_id"]) == ("user", owner_id)
    assert (events[0]["target_kind"], events[0]["target_id"]) == ("credential", key_id)


def test_the_owner_keeps_an_unproven_address_on_their_own_app_as_their_own(gateway):
    owner, owner_id, app_id = _owner_and_app(gateway)
    before_the_cut_over(gateway.path)
    entry_id = _unproven_address(gateway, app_id, "10.0.0.7", created_by=None)

    kept = owner.post(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt", headers=_headers(owner), json=CONFIRM)

    assert kept.status_code == 200, kept.text
    assert kept.json()["trusted_address"]["id"] == entry_id
    assert _creator_of_address(gateway, entry_id) == owner_id
    events = _audit(gateway, "app.trusted_address.adopt")
    assert len(events) == 1 and events[0]["target_id"] == app_id


def test_a_kept_binding_counts_as_proven_in_the_cut_over_preview(gateway):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    entry_id = _unproven_address(gateway, app_id, "10.0.0.8", created_by=None)
    owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=CONFIRM)
    owner.post(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt", headers=_headers(owner), json=CONFIRM)

    conn = app_binding_preview.connect_read_only(gateway.path)
    try:
        body = app_binding_preview.build(conn, as_of=1_800_000_000)
    finally:
        conn.close()
    listed = next(app for app in body["apps"] if app["app_id"] == app_id)
    assert [(key["id"], key["action"]) for key in listed["keys"]] == [(key_id, "keep")]
    assert [(entry["id"], entry["action"]) for entry in listed["trusted_addresses"]] == [(entry_id, "keep")]


def test_a_member_who_is_not_the_owner_cannot_keep_a_key_even_on_their_own_app(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    refused = desk.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(desk))

    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "keep_not_provable"
    assert f"POST /me/api/credentials/{key_id}/replace" in error["message"]
    assert _creator_of_key(gateway, key_id) is None
    assert _audit(gateway, "credential.adopt") == []


def test_a_member_who_is_not_the_owner_cannot_keep_an_address_and_is_told_to_add_it_again(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    entry_id = _unproven_address(gateway, app_id, "10.0.0.10", created_by=None)

    refused = desk.post(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt", headers=_headers(desk))

    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "keep_not_provable"
    assert f"POST /me/api/apps/{app_id}/trusted-addresses/{entry_id}/readd" in error["message"]
    assert "DELETE" not in error["message"]  # one step: the re-add replaces the entry itself, so no removal comes first
    assert _creator_of_address(gateway, entry_id) is None
    assert _audit(gateway, "app.trusted_address.adopt") == []


def test_the_owner_cannot_keep_a_key_the_audit_log_names_another_person_as_its_maker(gateway):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    _member(gateway, "desk")
    member_id = gateway.user("desk@example.test")["id"]
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    admin_store.record_audit("credential.create", actor_kind="user", actor_id=member_id, target_kind="credential",
                             target_id=key_id, metadata={"app_id": app_id}, path=gateway.path)

    refused = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "keep_not_provable"
    assert _creator_of_key(gateway, key_id) is None


def test_a_person_who_held_the_owner_role_before_does_not_block_keeping_but_is_named_for_confirmation(gateway):
    owner, owner_id, app_id = _owner_and_app(gateway)
    _member(gateway, "desk")
    _display_name(gateway, "desk@example.test", "Desk Person")
    _display_name(gateway, "helper@example.test", "Helper Person")
    member_id = gateway.user("desk@example.test")["id"]
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=member_id, path=gateway.path)
    grant = gateway.rows("SELECT id FROM admin_role_assignments WHERE role = 'owner' AND subject_id = ?", (member_id,))[0]
    admin_store.revoke_admin_role(grant["id"], path=gateway.path)  # no longer an owner: named, not blocking
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    asked = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

    assert asked.status_code == 409, asked.text
    assert asked.json()["error"]["code"] == "keep_needs_confirmation"
    assert asked.json()["error"]["administrators"] == _admins(["Desk Person", "Helper Person"])
    assert _creator_of_key(gateway, key_id) is None

    kept = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=CONFIRM)

    assert kept.status_code == 200, kept.text
    assert _creator_of_key(gateway, key_id) == owner_id


def test_a_second_person_holding_the_owner_role_now_blocks_keeping(gateway):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    _member(gateway, "desk")
    member_id = gateway.user("desk@example.test")["id"]
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=member_id, path=gateway.path)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    refused = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=CONFIRM)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "keep_not_provable"
    assert _creator_of_key(gateway, key_id) is None


def test_a_person_cannot_keep_a_key_on_someone_elses_app(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    other = _member(gateway, "other")

    refused = other.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(other))

    # The same answer as a key that does not exist, so the refusal does not show that this key does (owner visibility §3.5).
    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "credential_not_found"
    assert _creator_of_key(gateway, key_id) is None
    assert _audit(gateway, "credential.adopt") == []


def _record_sql(monkeypatch) -> list[str]:
    """Every SQL statement the store runs from now on, as text. The values are not recorded."""
    statements: list[str] = []
    connect = admin_store.connect

    def recording_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(admin_store, "connect", recording_connect)
    return statements


@pytest.mark.parametrize("action", ["adopt", "replace"])
def test_a_missing_key_and_another_persons_key_take_the_same_lookup_and_get_the_same_answer(gateway, monkeypatch, action):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    other = _member(gateway, "other")
    statements = _record_sql(monkeypatch)

    foreign = other.post(f"/me/api/credentials/{key_id}/{action}", headers=_headers(other))
    foreign_lookup = list(statements)
    statements.clear()
    missing = other.post(f"/me/api/credentials/cred_does_not_exist/{action}", headers=_headers(other))
    missing_lookup = list(statements)

    assert foreign.status_code == missing.status_code == 404, foreign.text
    assert foreign.json() == missing.json()
    # One ownership-scoped lookup each, the same statements, so the work done does not show whether the key exists.
    assert foreign_lookup == missing_lookup
    assert _live_keys_on(gateway, app_id) == [key_id]


def test_the_owner_cannot_keep_a_key_on_a_member_app(gateway):
    owner, _owner_id, _plan, _admin = owner_and_admin(gateway)
    member = _member(gateway, "desk")
    app_id = _own_app(member)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    refused = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=CONFIRM)

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "credential_not_found"
    assert _creator_of_key(gateway, key_id) is None


def test_a_person_cannot_keep_an_address_on_someone_elses_app(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    entry_id = _unproven_address(gateway, app_id, "10.0.0.9", created_by=None)
    other = _member(gateway, "other")

    refused = other.post(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt", headers=_headers(other))

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "app_not_found"
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
    owner, _owner_id, app_id = _owner_and_app(gateway)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    admin_store.disable_app(app_id, path=gateway.path)

    refused = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

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


# --- Replacing: a new key on the same App for the person, and the old one revoked ----------------------

def test_replacing_a_key_issues_a_new_key_on_the_same_app_and_revokes_the_old_one(gateway):
    desk = _member(gateway, "desk")
    person = gateway.user("desk@example.test")["id"]
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id, display_name="Laptop")

    replaced = desk.post(f"/me/api/credentials/{key_id}/replace", headers=_headers(desk))

    assert replaced.status_code == 201, replaced.text
    body = replaced.json()
    new = body["credential"]
    assert body["replaced_credential_id"] == key_id
    assert new["id"] != key_id
    assert new["app_id"] == app_id
    assert new["display_name"] == "Laptop"
    assert new["active"] is True
    assert new["api_key"].startswith("opk_")
    assert "hash" not in new
    stored = _credential(gateway, new["id"])
    assert stored["hash"] == hash_api_key(new["api_key"])
    assert stored["created_by"] == person
    assert stored["revoked_at"] is None
    assert _credential(gateway, key_id)["revoked_at"] is not None


def test_replacing_a_key_writes_one_audit_event_that_names_both_keys(gateway):
    desk = _member(gateway, "desk")
    person = gateway.user("desk@example.test")["id"]
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    replaced = desk.post(f"/me/api/credentials/{key_id}/replace", headers=_headers(desk))

    events = _audit(gateway, "credential.replace")
    assert len(events) == 1
    assert (events[0]["actor_kind"], events[0]["actor_id"]) == ("user", person)
    assert (events[0]["target_kind"], events[0]["target_id"]) == ("credential", key_id)
    assert f'"replaced_by": "{replaced.json()["credential"]["id"]}"' in events[0]["metadata_json"]


def test_replacing_a_key_is_one_transaction_so_a_failed_revoke_leaves_the_old_key_live(gateway, monkeypatch):
    desk = _member(gateway, "desk")
    person = gateway.user("desk@example.test")["id"]
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("simulated failure after the insert")

    monkeypatch.setattr(admin_store, "revoke_credential_in", fail)
    with pytest.raises(sqlite3.OperationalError):
        admin_store.replace_app_credential(key_id, app_id=app_id, person_id=person, hash="sha256:new",
                                           prefix="opk_new", display_name=None, path=gateway.path)

    assert _credential(gateway, key_id)["revoked_at"] is None
    assert gateway.rows("SELECT COUNT(*) AS n FROM credentials WHERE hash = 'sha256:new'")[0]["n"] == 0
    assert _audit(gateway, "credential.replace") == []


def test_replacing_someone_elses_key_is_refused_as_if_the_key_were_missing(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    other = _member(gateway, "other")

    refused = other.post(f"/me/api/credentials/{key_id}/replace", headers=_headers(other))
    missing = other.post("/me/api/credentials/cred_does_not_exist/replace", headers=_headers(other))

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "credential_not_found"
    assert (refused.status_code, refused.json()) == (missing.status_code, missing.json())
    assert _live_keys_on(gateway, app_id) == [key_id]


def test_replacing_a_key_on_a_disabled_app_is_refused(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    admin_store.disable_app(app_id, path=gateway.path)

    refused = desk.post(f"/me/api/credentials/{key_id}/replace", headers=_headers(desk))

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "app_disabled"
    assert _live_keys_on(gateway, app_id) == [key_id]


def test_replacing_a_revoked_key_is_not_found(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    admin_store.revoke_credential(key_id, path=gateway.path)

    refused = desk.post(f"/me/api/credentials/{key_id}/replace", headers=_headers(desk))

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "credential_not_found"


def test_an_api_key_cannot_replace_a_key(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    api_key = issue_app_key(gateway.path, app_id, label="desk automation")

    refused = gateway.client().post(f"/me/api/credentials/{key_id}/replace",
                                    headers={"authorization": f"Bearer {api_key}", **ACT})

    assert refused.status_code == 401, refused.text
    assert _credential(gateway, key_id)["revoked_at"] is None
    assert len(_live_keys_on(gateway, app_id)) == 2  # the legacy key and the automation key, and no replacement


def test_keeping_and_replacing_are_sensitive_actions_that_ask_for_a_recent_sign_in():
    assert step_up.is_sensitive("POST", "/me/api/credentials/cred_abc/adopt")
    assert step_up.is_sensitive("POST", "/me/api/apps/app_abc/trusted-addresses/tad_abc/adopt")
    assert step_up.is_sensitive("POST", "/me/api/credentials/cred_abc/replace")


# --- Removing: the person narrows their own App's addresses (the refusal's second step) ---------------

def test_a_person_removes_an_address_on_their_own_app_and_the_removal_is_audited(gateway):
    desk = _member(gateway, "desk")
    person = gateway.user("desk@example.test")["id"]
    app_id = _own_app(desk)
    entry_id = desk.post(f"/me/api/apps/{app_id}/trusted-addresses", headers=_headers(desk),
                         json={"address": "10.0.0.11"}).json()["trusted_address"]["id"]

    removed = desk.delete(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}", headers=_headers(desk))

    assert removed.status_code == 200, removed.text
    assert removed.json()["trusted_address"]["id"] == entry_id
    assert gateway.rows("SELECT COUNT(*) AS n FROM app_trusted_addresses WHERE id = ?", (entry_id,))[0]["n"] == 0
    events = _audit(gateway, "app.trusted_address.remove")
    assert len(events) == 1
    assert (events[0]["actor_kind"], events[0]["actor_id"]) == ("user", person)
    assert (events[0]["target_kind"], events[0]["target_id"]) == ("app", app_id)


def test_removing_an_address_and_its_audit_event_are_one_transaction(gateway, monkeypatch):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    added = desk.post(f"/me/api/apps/{app_id}/trusted-addresses", headers=_headers(desk), json={"address": "10.0.0.41"})
    assert added.status_code == 201, added.text
    entry_id = added.json()["trusted_address"]["id"]

    def refuse_the_audit_event(*args, **kwargs):
        raise sqlite3.OperationalError("audit write refused")

    monkeypatch.setattr(admin_store, "_record_audit_in_transaction", refuse_the_audit_event)
    with pytest.raises(sqlite3.OperationalError):
        desk.delete(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}", headers=_headers(desk))

    assert gateway.rows("SELECT id FROM app_trusted_addresses WHERE id = ?", (entry_id,))


def test_a_person_cannot_remove_an_address_on_someone_elses_app(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    entry_id = desk.post(f"/me/api/apps/{app_id}/trusted-addresses", headers=_headers(desk),
                         json={"address": "10.0.0.12"}).json()["trusted_address"]["id"]
    other = _member(gateway, "other")

    refused = other.delete(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}", headers=_headers(other))

    assert refused.status_code == 404, refused.text  # the same answer as adding an address to that App
    assert refused.json()["error"]["code"] == "app_not_found"
    assert gateway.rows("SELECT COUNT(*) AS n FROM app_trusted_addresses WHERE id = ?", (entry_id,))[0]["n"] == 1


def test_removing_an_address_the_app_does_not_hold_is_not_found(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)

    missing = desk.delete(f"/me/api/apps/{app_id}/trusted-addresses/tad_missing", headers=_headers(desk))

    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == "trusted_address_not_found"


def test_the_owner_cannot_keep_a_key_the_record_names_as_made_by_another_person_even_without_an_audit_row(gateway):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    _member(gateway, "desk")
    member_id = gateway.user("desk@example.test")["id"]
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id, created_by=member_id)

    refused = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "keep_not_provable"
    assert _creator_of_key(gateway, key_id) == member_id


def test_the_owner_cannot_keep_an_address_the_record_names_as_added_by_another_person_even_without_an_audit_row(gateway):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    _member(gateway, "desk")
    member_id = gateway.user("desk@example.test")["id"]
    before_the_cut_over(gateway.path)
    entry_id = _unproven_address(gateway, app_id, "10.0.0.13", created_by=member_id)

    refused = owner.post(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt", headers=_headers(owner))

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "keep_not_provable"
    assert _creator_of_address(gateway, entry_id) == member_id


# --- Amended D55: administrators on record may be confirmed; a member, a second owner, or a changed App never keeps ---

def test_keeping_a_key_asks_the_owner_to_confirm_the_administrators_on_record(gateway):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    _display_name(gateway, "helper@example.test", "Helper Person")
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    asked = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

    assert asked.status_code == 409, asked.text
    assert asked.json()["error"]["code"] == "keep_needs_confirmation"
    assert asked.json()["error"]["administrators"] == _admins(["Helper Person"])
    assert _creator_of_key(gateway, key_id) is None
    assert _audit(gateway, "credential.adopt") == []


def test_confirming_the_administrators_keeps_the_key_and_the_audit_event_names_them(gateway):
    owner, owner_id, app_id = _owner_and_app(gateway)
    _display_name(gateway, "helper@example.test", "Helper Person")
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    kept = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=CONFIRM)

    assert kept.status_code == 200, kept.text
    assert _creator_of_key(gateway, key_id) == owner_id
    (event,) = _audit(gateway, "credential.adopt")
    assert json.loads(event["metadata_json"])["confirmed_administrators"] == _admins(["Helper Person"])


@pytest.mark.parametrize("body", [{}, {"confirm_admins_could_have_made_it": "true"}, {"confirm_admins_could_have_made_it": False}])
def test_only_a_json_true_confirmation_keeps_a_key_an_administrator_could_have_made(gateway, body):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    refused = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=body)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "keep_needs_confirmation"
    assert _creator_of_key(gateway, key_id) is None


def test_keeping_needs_no_confirmation_when_no_administrator_is_on_record(gateway):
    owner, _ = signed_in(gateway, sub="owner", email=OWNER)
    owner_id = gateway.user(OWNER)["id"]
    app_id = _own_app(owner)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    kept = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

    assert kept.status_code == 200, kept.text
    assert _creator_of_key(gateway, key_id) == owner_id


def test_a_past_administrator_is_named_even_after_the_role_is_revoked(gateway):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    _display_name(gateway, "helper@example.test", "Helper Person")
    helper_id = gateway.user("helper@example.test")["id"]
    for grant in gateway.rows("SELECT id FROM admin_role_assignments WHERE subject_kind = 'user' AND subject_id = ? "
                              "AND role IN ('admin', 'superadmin')", (helper_id,)):
        admin_store.revoke_admin_role(grant["id"], path=gateway.path)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    asked = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

    assert asked.status_code == 409, asked.text
    assert asked.json()["error"]["code"] == "keep_needs_confirmation"
    assert asked.json()["error"]["administrators"] == _admins(["Helper Person"])


def test_an_administrator_service_account_is_counted_as_an_administrator_key_and_not_named(gateway):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    _display_name(gateway, "helper@example.test", "Helper Person")
    bot = admin_store.create_service_account("CI bot", path=gateway.path)
    admin_store.assign_admin_role(role="admin", subject_kind="service_account", subject_id=bot["id"], path=gateway.path)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    asked = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

    assert asked.status_code == 409, asked.text
    assert asked.json()["error"]["administrators"] == _admins(["Helper Person"], keys=1)
    assert "CI bot" not in asked.text
    assert _creator_of_key(gateway, key_id) is None


def test_administrator_keys_are_counted_and_the_message_the_owner_reads_says_how_many(gateway):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    _display_name(gateway, "helper@example.test", "Helper Person")
    for name in ("CI bot", "Nightly sync", "Backup"):
        bot = admin_store.create_service_account(name, path=gateway.path)
        admin_store.assign_admin_role(role="admin", subject_kind="service_account", subject_id=bot["id"], path=gateway.path)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    asked = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

    assert asked.status_code == 409, asked.text
    assert asked.json()["error"]["administrators"] == _admins(["Helper Person"], keys=3)
    assert asked.json()["error"]["message"].startswith(
        "Administrators at the time could also have made this key: Helper Person and 3 administrator keys. ")
    assert _creator_of_key(gateway, key_id) is None


@pytest.mark.parametrize(("administrators", "sentence"), [
    (_admins(["Simon Agent"], unnamed=1, keys=23), "Simon Agent, 1 person with no name, and 23 administrator keys"),
    (_admins(unnamed=2), "2 people with no name"),
    (_admins(keys=1), "1 administrator key"),
    (_admins(["Ann", "Bob"]), "Ann and Bob"),
    (_admins(["Ann"], unnamed=1), "Ann and 1 person with no name"),
    (_admins(["Ann", "Bob"], keys=2), "Ann, Bob, and 2 administrator keys"),
    (_admins(["Ann"], keys=1), "Ann and 1 administrator key"),
    (_admins(), ""),
])
def test_the_administrators_sentence_names_people_and_counts_the_unnamed_and_the_keys(administrators, sentence):
    assert admin_store.administrators_sentence(administrators) == sentence


def test_a_control_plane_key_that_held_the_owner_role_neither_blocks_nor_is_named(gateway):
    owner, owner_id, app_id = _owner_and_app(gateway)
    _display_name(gateway, "helper@example.test", "Helper Person")
    tools = admin_store.create_service_account("Owner tools", path=gateway.path)
    admin_store.assign_admin_role(role="owner", subject_kind="service_account", subject_id=tools["id"], path=gateway.path)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    asked = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

    # Not a keep_not_provable (it does not block), and not named: only the helper, who is an administrator, is.
    assert asked.status_code == 409, asked.text
    assert asked.json()["error"]["code"] == "keep_needs_confirmation"
    assert asked.json()["error"]["administrators"] == _admins(["Helper Person"])
    kept = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=CONFIRM)
    assert kept.status_code == 200, kept.text
    assert _creator_of_key(gateway, key_id) == owner_id


def test_an_app_whose_accountable_person_changed_on_record_cannot_be_kept(gateway):
    owner, owner_id, app_id = _owner_and_app(gateway)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    admin_store.record_audit("app.update", actor_kind="user", actor_id=owner_id, target_kind="app", target_id=app_id,
                             metadata={"owner_user_id": "usr_someone_else"}, path=gateway.path)

    refused = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=CONFIRM)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "keep_not_provable"
    assert _creator_of_key(gateway, key_id) is None


def test_the_schema_seven_migration_that_changed_an_apps_accountable_person_blocks_keeping(gateway):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    admin_store.record_audit("access_plans.migrate", actor_kind="system", actor_id="migration", target_kind="admin_db",
                             target_id="schema-7", metadata={"apps": [{"id": app_id, "name": "Desk", "accountable": "Helper Person"}]},
                             path=gateway.path)

    refused = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=CONFIRM)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "keep_not_provable"
    assert _creator_of_key(gateway, key_id) is None


def test_keeping_an_address_asks_the_owner_to_confirm_the_administrators_on_record(gateway):
    owner, owner_id, app_id = _owner_and_app(gateway)
    _display_name(gateway, "helper@example.test", "Helper Person")
    before_the_cut_over(gateway.path)
    entry_id = _unproven_address(gateway, app_id, "10.0.0.21", created_by=None)
    address_keep = f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt"

    asked = owner.post(address_keep, headers=_headers(owner))

    assert asked.status_code == 409, asked.text
    assert asked.json()["error"]["code"] == "keep_needs_confirmation"
    assert asked.json()["error"]["administrators"] == _admins(["Helper Person"])
    assert _creator_of_address(gateway, entry_id) is None

    kept = owner.post(address_keep, headers=_headers(owner), json=CONFIRM)

    assert kept.status_code == 200, kept.text
    assert _creator_of_address(gateway, entry_id) == owner_id


def test_a_key_on_an_app_that_became_a_session_app_is_not_found_for_keep_or_replace(gateway):
    owner, owner_id, _plan, _admin = owner_and_admin(gateway)
    app_id = _own_app(owner)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    # The App becomes the owner's signed-in App after its key exists: the key stays, and keep and replace must refuse it.
    conn = sqlite3.connect(gateway.path)
    try:
        conn.execute("INSERT INTO session_apps(user_id, app_id) VALUES (?, ?)", (owner_id, app_id))
        conn.commit()
    finally:
        conn.close()

    kept = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=CONFIRM)
    replaced = owner.post(f"/me/api/credentials/{key_id}/replace", headers=_headers(owner))

    assert (kept.status_code, kept.json()["error"]["code"]) == (404, "credential_not_found")
    assert (replaced.status_code, replaced.json()["error"]["code"]) == (404, "credential_not_found")
    assert _live_keys_on(gateway, app_id) == [key_id]


def _remedy(g, kind, binding_id, app_id, person_id):
    conn = sqlite3.connect(g.path)
    conn.row_factory = sqlite3.Row
    try:
        table = "credentials" if kind == "key" else "app_trusted_addresses"
        binding = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (binding_id,)).fetchone()
        return admin_store.binding_cutover_remedy(conn, kind=kind, binding=binding, app_id=app_id, person_id=person_id)
    finally:
        conn.close()


def test_the_cut_over_remedy_is_keep_where_provable_and_replace_or_readd_otherwise(gateway):
    owner, owner_id, app_id = _owner_and_app(gateway)
    _display_name(gateway, "helper@example.test", "Helper Person")
    desk = _member(gateway, "desk")
    member_id = gateway.user("desk@example.test")["id"]
    member_app = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)
    entry_id = _unproven_address(gateway, app_id, "10.0.0.31", created_by=None)
    member_key = _unproven_key(gateway, member_app)
    member_entry = _unproven_address(gateway, member_app, "10.0.0.32", created_by=None)

    assert _remedy(gateway, "key", key_id, app_id, owner_id) == {"remedy": "keep", "administrators": ["Helper Person"]}
    assert _remedy(gateway, "address", entry_id, app_id, owner_id) == {"remedy": "keep", "administrators": ["Helper Person"]}
    assert _remedy(gateway, "key", member_key, member_app, member_id) == {"remedy": "replace", "administrators": []}
    assert _remedy(gateway, "address", member_entry, member_app, member_id) == {"remedy": "readd", "administrators": []}
    assert _remedy(gateway, "key", key_id, app_id, member_id) == {"remedy": "replace", "administrators": []}

    assert owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner), json=CONFIRM).status_code == 200
    assert _remedy(gateway, "key", key_id, app_id, owner_id) == {"remedy": "keep", "administrators": []}


def test_the_cut_over_preview_gives_each_binding_its_remedy(gateway):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    _display_name(gateway, "helper@example.test", "Helper Person")
    before_the_cut_over(gateway.path)
    _unproven_key(gateway, app_id)

    conn = app_binding_preview.connect_read_only(gateway.path)
    try:
        body = app_binding_preview.build(conn, as_of=1_800_000_000)
    finally:
        conn.close()
    (desk,) = [app for app in body["apps"] if app["app_id"] == app_id]
    (key,) = desk["keys"]

    assert (key["action"], key["remedy"], key["administrators"]) == ("revoke", "keep", _admins(["Helper Person"]))

# --- What the snapshot and the address list tell the person: the remedy each binding needs --------------

def _snapshot_app(client, app_id):
    snapshot = client.get("/me/api/session").json()
    return next(app for app in snapshot["apps"] if app["id"] == app_id)


def _key_cutovers(client, app_id):
    """{key id: remedy} for each key the cut-over would remove. A key the cut-over keeps has no entry."""
    return {key["id"]: key["cutover"] for key in _snapshot_app(client, app_id)["credentials"] if "cutover" in key}


def _address_cutovers(client, app_id):
    """{entry id: remedy} for each address the cut-over would remove, from the owner's address list."""
    entries = client.get(f"/admin/api/apps/{app_id}/trusted-addresses").json()["trusted_addresses"]
    return {entry["id"]: entry["cutover"] for entry in entries if "cutover" in entry}


def test_the_owner_is_offered_keep_for_an_unproven_key_and_the_count_covers_their_addresses(gateway):
    owner, _owner_id, _plan, _admin = owner_and_admin(gateway)
    hall = _own_app(owner, name="Hall")
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, hall)
    entry_id = _unproven_address(gateway, hall, "10.0.0.7")
    assert _key_cutovers(owner, hall) == {key_id: "keep"}
    assert _snapshot_app(owner, hall)["addresses_to_check"] == 1

    kept = owner.post(f"/me/api/apps/{hall}/trusted-addresses/{entry_id}/adopt", headers=_headers(owner), json=CONFIRM)

    assert kept.status_code == 200, kept.text
    assert _snapshot_app(owner, hall)["addresses_to_check"] == 0


def test_a_member_is_offered_replace_for_an_unproven_key_on_their_own_app_because_the_owner_exists(gateway):
    owner_and_admin(gateway)
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id)

    assert _key_cutovers(desk, app_id) == {key_id: "replace"}
    assert "made_by_name" not in _snapshot_app(desk, app_id)["credentials"][0]  # nobody is recorded as its maker


def test_a_key_another_person_made_is_offered_replace_and_the_remedy_names_its_maker(gateway):
    owner, _owner_id, app_id = _owner_and_app(gateway)
    _member(gateway, "desk")
    desk_user = gateway.user("desk@example.test")
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, app_id, created_by=desk_user["id"])

    credential = _snapshot_app(owner, app_id)["credentials"][0]
    assert credential["id"] == key_id
    assert credential["cutover"] == "replace"
    assert credential["made_by_name"] == person_label(desk_user)


def test_the_marked_bindings_are_exactly_the_ones_the_cut_over_removes_and_each_remedy_matches_the_keep_route(gateway):
    owner, owner_id, app_id = _owner_and_app(gateway)
    _member(gateway, "desk")
    desk_user = gateway.user("desk@example.test")
    before_the_cut_over(gateway.path)
    own_key = _unproven_key(gateway, app_id, created_by=owner_id)  # the cut-over keeps it
    legacy_key = _unproven_key(gateway, app_id)  # keep: nobody else is recorded, and the owner is the only owner
    made_key = _unproven_key(gateway, app_id, created_by=desk_user["id"])  # replace: another person made it
    legacy_address = _unproven_address(gateway, app_id, "10.0.0.31")  # keep
    made_address = _unproven_address(gateway, app_id, "10.0.0.32", created_by=desk_user["id"])  # readd

    conn = app_binding_preview.connect_read_only(gateway.path)
    try:
        listed = next(app for app in app_binding_preview.build(conn, as_of=1_800_000_000)["apps"]
                      if app["app_id"] == app_id)
    finally:
        conn.close()
    removed = ({key["id"] for key in listed["keys"] if key["action"] == "revoke"}
               | {entry["id"] for entry in listed["trusted_addresses"] if entry["action"] == "remove"})
    assert removed == {legacy_key, made_key, legacy_address, made_address}
    assert own_key not in removed

    assert _key_cutovers(owner, app_id) == {legacy_key: "keep", made_key: "replace"}
    assert _address_cutovers(owner, app_id) == {legacy_address: "keep", made_address: "readd"}
    entries = {entry["id"]: entry for entry in owner.get(f"/admin/api/apps/{app_id}/trusted-addresses").json()["trusted_addresses"]}
    assert entries[made_address]["made_by_name"] == person_label(desk_user)
    assert "made_by_name" not in entries[legacy_address]

    assert owner.post(f"/me/api/credentials/{legacy_key}/adopt", headers=_headers(owner), json=CONFIRM).status_code == 200
    assert owner.post(f"/me/api/credentials/{made_key}/adopt", headers=_headers(owner), json=CONFIRM).status_code == 409
    assert owner.post(f"/me/api/apps/{app_id}/trusted-addresses/{legacy_address}/adopt",
                      headers=_headers(owner), json=CONFIRM).status_code == 200
    assert owner.post(f"/me/api/apps/{app_id}/trusted-addresses/{made_address}/adopt",
                      headers=_headers(owner)).status_code == 409
    assert _key_cutovers(owner, app_id) == {made_key: "replace"}
    assert _address_cutovers(owner, app_id) == {made_address: "readd"}


def test_a_member_is_not_sent_a_count_of_addresses_they_cannot_list(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    _unproven_address(gateway, app_id, "10.0.0.8")

    assert "addresses_to_check" not in _snapshot_app(desk, app_id)


def test_an_address_an_app_already_trusts_cannot_be_added_again_so_an_unkept_one_is_removed_first(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    first = desk.post(f"/me/api/apps/{app_id}/trusted-addresses", headers=_headers(desk), json={"address": "10.0.0.40"})
    assert first.status_code == 201, first.text

    again = desk.post(f"/me/api/apps/{app_id}/trusted-addresses", headers=_headers(desk), json={"address": "10.0.0.40"})

    assert again.status_code == 400, again.text
    assert again.json()["error"]["code"] == "invalid_trusted_address"


# --- Adding an address again: one step, so a failed step leaves the old entry in place -------------------

def test_the_owner_adds_an_unkept_address_again_in_one_step_and_is_its_creator(gateway):
    owner, owner_id, _plan, _admin = owner_and_admin(gateway)
    hall = _own_app(owner, name="Hall")
    before_the_cut_over(gateway.path)
    entry_id = _unproven_address(gateway, hall, "10.0.0.50")
    conn = sqlite3.connect(gateway.path)
    try:
        conn.execute("UPDATE app_trusted_addresses SET note = 'office' WHERE id = ?", (entry_id,))
        conn.commit()
    finally:
        conn.close()

    readded = owner.post(f"/me/api/apps/{hall}/trusted-addresses/{entry_id}/readd", headers=_headers(owner))

    assert readded.status_code == 201, readded.text
    entry = readded.json()["trusted_address"]
    assert (entry["address"], entry["note"], entry["created_by"]) == ("10.0.0.50", "office", owner_id)
    assert readded.json()["replaced_entry_id"] == entry_id
    assert [row["id"] for row in gateway.rows("SELECT id FROM app_trusted_addresses WHERE app_id = ?", (hall,))] == [entry["id"]]
    (audit,) = _audit(gateway, "app.trusted_address.readd")
    assert (audit["actor_kind"], audit["actor_id"]) == ("user", owner_id)
    assert json.loads(audit["metadata_json"]) == {"address": "10.0.0.50", "entry_id": entry["id"], "replaced_entry_id": entry_id}


def test_adding_an_address_again_is_one_transaction_so_a_failed_step_leaves_the_old_entry(gateway, monkeypatch):
    owner, _owner_id, _plan, _admin = owner_and_admin(gateway)
    hall = _own_app(owner, name="Hall")
    before_the_cut_over(gateway.path)
    entry_id = _unproven_address(gateway, hall, "10.0.0.51")

    def fail_after_the_delete_and_the_insert(*_args, **_kwargs):
        raise RuntimeError("the audit write failed")

    monkeypatch.setattr(admin_store, "_record_audit_in_transaction", fail_after_the_delete_and_the_insert)
    with pytest.raises(RuntimeError, match="the audit write failed"):
        owner.post(f"/me/api/apps/{hall}/trusted-addresses/{entry_id}/readd", headers=_headers(owner))

    assert [row["id"] for row in gateway.rows("SELECT id FROM app_trusted_addresses WHERE app_id = ?", (hall,))] == [entry_id]
    assert _creator_of_address(gateway, entry_id) is None


def test_adding_an_address_again_on_someone_elses_app_is_not_found(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    before_the_cut_over(gateway.path)
    entry_id = _unproven_address(gateway, app_id, "10.0.0.52")
    other = _member(gateway, "other")

    refused = other.post(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}/readd", headers=_headers(other))

    assert (refused.status_code, refused.json()["error"]["code"]) == (404, "app_not_found")
    assert [row["id"] for row in gateway.rows("SELECT id FROM app_trusted_addresses WHERE app_id = ?", (app_id,))] == [entry_id]
    assert _audit(gateway, "app.trusted_address.readd") == []


def test_adding_a_missing_address_again_is_not_found(gateway):
    owner, _owner_id, _plan, _admin = owner_and_admin(gateway)
    hall = _own_app(owner, name="Hall")

    missing = owner.post(f"/me/api/apps/{hall}/trusted-addresses/tad_does_not_exist/readd", headers=_headers(owner))

    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "trusted_address_not_found")


def test_adding_an_address_again_without_the_user_action_header_writes_nothing(gateway):
    owner, _owner_id, _plan, _admin = owner_and_admin(gateway)
    hall = _own_app(owner, name="Hall")
    before_the_cut_over(gateway.path)
    entry_id = _unproven_address(gateway, hall, "10.0.0.53")

    refused = owner.post(f"/me/api/apps/{hall}/trusted-addresses/{entry_id}/readd", headers=csrf_for(owner))

    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "user_action_header_required")
    assert _creator_of_address(gateway, entry_id) is None


def test_adding_an_address_again_on_a_session_app_is_not_found(gateway):
    owner, owner_id, _plan, _admin = owner_and_admin(gateway)
    app_id = _own_app(owner)
    before_the_cut_over(gateway.path)
    entry_id = _unproven_address(gateway, app_id, "10.0.0.54")
    conn = sqlite3.connect(gateway.path)
    try:
        conn.execute("INSERT INTO session_apps(user_id, app_id) VALUES (?, ?)", (owner_id, app_id))
        conn.commit()
    finally:
        conn.close()

    refused = owner.post(f"/me/api/apps/{app_id}/trusted-addresses/{entry_id}/readd", headers=_headers(owner))

    assert (refused.status_code, refused.json()["error"]["code"]) == (404, "app_not_found")
    assert _creator_of_address(gateway, entry_id) is None


def test_adding_an_address_again_is_a_sensitive_action_that_asks_for_a_recent_sign_in():
    assert step_up.is_sensitive("POST", "/me/api/apps/app_abc/trusted-addresses/tad_abc/readd")


def test_the_snapshot_and_the_owners_address_list_name_the_administrators_a_keep_must_confirm(gateway):
    owner, _owner_id, _plan, _admin = owner_and_admin(gateway)
    hall = _own_app(owner, name="Hall")
    before_the_cut_over(gateway.path)
    key_id = _unproven_key(gateway, hall)
    entry_id = _unproven_address(gateway, hall, "10.0.0.55")

    key = next(item for item in _snapshot_app(owner, hall)["credentials"] if item["id"] == key_id)
    entries = {entry["id"]: entry for entry in owner.get(f"/admin/api/apps/{hall}/trusted-addresses").json()["trusted_addresses"]}

    assert (key["cutover"], key["administrators"]) == ("keep", _admins(unnamed=1))
    assert (entries[entry_id]["cutover"], entries[entry_id]["administrators"]) == ("keep", _admins(unnamed=1))
