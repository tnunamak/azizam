"""Review probes for owner visibility step 2, round 3 (review of feat/owner-visibility-step2 at a21a12609).

Each finding's probe states the behaviour the review asks for. It failed on the reviewed branch as a strict
xfail; it is a plain assertion now. The plain tests prove claims of the reviewed branch on paths its own tests
do not cover.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid

import pytest

from gateway import admin_store, app_binding_cutover
from gateway.runtime_auth import hash_api_key
from tests.support.app_keys import before_the_cut_over
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin, signed_in  # noqa: F401  (gateway is a fixture)

ACT = {"x-azizam-user-action": "1"}


def _headers(client):
    return {**csrf_for(client), **ACT}


def _member(g, sub):
    g.set_rules([], fallback="open")
    client, response = signed_in(g, sub=sub, email=f"{sub}@example.test")
    assert response.status_code == 302, response.text
    return client


def _own_app(client, name="Desk"):
    created = client.post("/me/api/apps", headers=_headers(client), json={"name": name})
    assert created.status_code == 201, created.text
    return created.json()["app"]["id"]


def _display_name(g, email, name):
    conn = sqlite3.connect(g.path)
    try:
        conn.execute("UPDATE users SET display_name = ? WHERE LOWER(email) = ?", (name, email))
        conn.commit()
    finally:
        conn.close()


def _legacy_key(g, app_id, created_by=None):
    return admin_store.create_credential_hash(
        subject_kind="app", subject_id=app_id, app_id=app_id, hash=f"sha256:{uuid.uuid4().hex}",
        prefix="opk_legacy", display_name="legacy", created_by=created_by, path=g.path,
    )["id"]


def _live_keys_on(g, app_id):
    return [row["id"] for row in g.rows(
        "SELECT id FROM credentials WHERE subject_kind = 'app' AND app_id = ? AND revoked_at IS NULL ORDER BY id", (app_id,))]


# --- Finding: "only ever the owner" ignores administrators, who could key any App before 2026-10-13 ---------

def test_the_owner_keeps_an_unaudited_key_only_after_confirming_the_administrators_on_record(gateway):
    owner, owner_id, _plan, _admin = owner_and_admin(gateway)
    admin_id = gateway.user("helper@example.test")["id"]
    assert gateway.rows("SELECT 1 FROM admin_role_assignments WHERE role IN ('admin', 'superadmin') AND subject_id = ?",
                        (admin_id,)), "the fixture's helper holds an administrator role"
    _display_name(gateway, "helper@example.test", "Helper Person")
    app_id = _own_app(owner)
    before_the_cut_over(gateway.path)
    key_id = _legacy_key(gateway, app_id)  # made by the administrator in an older release; its audit row is gone

    asked = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

    assert asked.status_code == 409, asked.text
    assert asked.json()["error"]["code"] == "keep_needs_confirmation"
    assert asked.json()["error"]["administrators"] == ["Helper Person"]
    assert admin_store.get_credential(key_id, path=gateway.path)["created_by"] is None

    kept = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner),
                      json={"confirm_admins_could_have_made_it": True})

    assert kept.status_code == 200, kept.text
    assert admin_store.get_credential(key_id, path=gateway.path)["created_by"] == owner_id


# --- Finding: the lifetime lock is taken only by `python proxy.py`, not by an ASGI launch ------------------

def test_a_gateway_serving_requests_through_its_asgi_app_holds_the_lifetime_lock(gateway):
    owner, _owner_id, _plan, _admin = owner_and_admin(gateway)
    assert _own_app(owner) in {row["id"] for row in gateway.rows("SELECT id FROM apps")}  # serving against this database

    with pytest.raises(app_binding_cutover.CutoverError):
        app_binding_cutover._hold_lifetime_lock_exclusively(gateway.path).close()


# --- Finding: replace and keep tell another person's key apart from a missing one --------------------------

def test_replacing_another_persons_key_looks_the_same_as_replacing_a_missing_key(gateway):
    desk = _member(gateway, "desk")
    key_id = _legacy_key(gateway, _own_app(desk), created_by=gateway.user("desk@example.test")["id"])
    other = _member(gateway, "other")

    foreign = other.post(f"/me/api/credentials/{key_id}/replace", headers=_headers(other))
    missing = other.post("/me/api/credentials/cred_does_not_exist/replace", headers=_headers(other))

    assert (foreign.status_code, foreign.json()["error"]["code"]) == (missing.status_code, missing.json()["error"]["code"])


# --- Proofs of the reviewed branch's claims ----------------------------------------------------------------

def test_replaying_a_replace_mints_no_second_key(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    key_id = _legacy_key(gateway, app_id, created_by=gateway.user("desk@example.test")["id"])  # after the cut-over

    first = desk.post(f"/me/api/credentials/{key_id}/replace", headers=_headers(desk))
    again = desk.post(f"/me/api/credentials/{key_id}/replace", headers=_headers(desk))

    assert first.status_code == 201, first.text
    assert again.status_code == 404, again.text
    assert _live_keys_on(gateway, app_id) == [first.json()["credential"]["id"]]
    assert "api_key" not in desk.get("/me/api/apps").text  # the secret is shown by the replace response only


def test_concurrent_replaces_of_one_key_leave_exactly_one_new_key(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    person_id = gateway.user("desk@example.test")["id"]
    key_id = _legacy_key(gateway, app_id, created_by=person_id)
    results, barrier = [], threading.Barrier(4)

    def replace(n):
        barrier.wait()
        results.append(admin_store.replace_app_credential(
            key_id, app_id=app_id, person_id=person_id, hash=hash_api_key(f"opk_race_{n}_{uuid.uuid4().hex}"),
            prefix=f"opk_race_{n}", display_name="raced", path=gateway.path))

    threads = [threading.Thread(target=replace, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    made = [r for r in results if r is not None]
    assert len(results) == 4 and len(made) == 1
    assert _live_keys_on(gateway, app_id) == [made[0]["id"]]


def test_removing_another_persons_address_through_my_own_app_path_is_not_found(gateway):
    desk = _member(gateway, "desk")
    desk_app = _own_app(desk)
    other = _member(gateway, "other")
    other_app = _own_app(other, name="Other")
    added = other.post(f"/me/api/apps/{other_app}/trusted-addresses", headers=_headers(other), json={"address": "10.0.0.9"})
    assert added.status_code == 201, added.text
    entry_id = added.json()["trusted_address"]["id"]

    removed = desk.delete(f"/me/api/apps/{desk_app}/trusted-addresses/{entry_id}", headers=_headers(desk))

    assert removed.status_code == 404, removed.text
    assert gateway.rows("SELECT id FROM app_trusted_addresses WHERE id = ?", (entry_id,))
