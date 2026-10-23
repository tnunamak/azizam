"""Review probes for owner visibility step 2, round 3 (review of feat/owner-visibility-step2 at a21a12609).

Each strict xfail states the behaviour one review finding asks for; it fails on the reviewed branch.
The plain tests prove claims of the reviewed branch on paths its own tests do not cover.
"""

from __future__ import annotations

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


def _legacy_key(g, app_id, created_by=None):
    return admin_store.create_credential_hash(
        subject_kind="app", subject_id=app_id, app_id=app_id, hash=f"sha256:{uuid.uuid4().hex}",
        prefix="opk_legacy", display_name="legacy", created_by=created_by, path=g.path,
    )["id"]


def _live_keys_on(g, app_id):
    return [row["id"] for row in g.rows(
        "SELECT id FROM credentials WHERE subject_kind = 'app' AND app_id = ? AND revoked_at IS NULL ORDER BY id", (app_id,))]


# --- Finding: "only ever the owner" ignores administrators, who could key any App before 2026-10-13 ---------

@pytest.mark.xfail(strict=True, reason="r3 finding 1: _owner_only_ever counts only role='owner'; before b84eeb50e an "
                                       "admin or superadmin could mint a key on any App, so a key with no audit row "
                                       "is not provably the owner's while an administrator has ever existed")
def test_the_owner_cannot_keep_an_unaudited_key_while_an_administrator_has_ever_existed(gateway):
    owner, _owner_id, _plan, _admin = owner_and_admin(gateway)
    admin_id = gateway.user("helper@example.test")["id"]
    assert gateway.rows("SELECT 1 FROM admin_role_assignments WHERE role IN ('admin', 'superadmin') AND subject_id = ?",
                        (admin_id,)), "the fixture's helper holds an administrator role"
    app_id = _own_app(owner)
    before_the_cut_over(gateway.path)
    key_id = _legacy_key(gateway, app_id)  # made by the administrator in an older release; its audit row is gone

    kept = owner.post(f"/me/api/credentials/{key_id}/adopt", headers=_headers(owner))

    assert kept.status_code == 409, kept.text
    assert kept.json()["error"]["code"] == "keep_not_provable"


# --- Finding: the lifetime lock is taken only by `python proxy.py`, not by an ASGI launch ------------------

@pytest.mark.xfail(strict=True, reason="r3 finding 2: gateway_lifetime.hold runs only in proxy.py's __main__ block; "
                                       "a gateway served as proxy:app (uvicorn, scripts/ui/serve.py, test gateways) "
                                       "holds no lifetime lock, so apply's exclusive lock succeeds while it serves")
def test_a_gateway_serving_requests_through_its_asgi_app_holds_the_lifetime_lock(gateway):
    owner, _owner_id, _plan, _admin = owner_and_admin(gateway)
    assert _own_app(owner) in {row["id"] for row in gateway.rows("SELECT id FROM apps")}  # serving against this database

    with pytest.raises(app_binding_cutover.CutoverError):
        app_binding_cutover._hold_lifetime_lock_exclusively(gateway.path).close()


# --- Finding: replace and keep tell another person's key apart from a missing one --------------------------

@pytest.mark.xfail(strict=True, reason="r3 finding 3: replace answers 403 not_your_app for another person's key and "
                                       "404 for a missing one, against _credential_not_found's single public result")
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
