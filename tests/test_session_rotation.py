"""A renewal replaces the session id; a retired id shown late revokes the sign-in only if its successor
was used (decisions D35, D35b).

Same real routes and database as tests/test_session_lifecycle.py. Time passes by moving the
``retired_at`` stamp back, so no test waits and no clock is patched.
"""
import sqlite3
import threading
import time
from dataclasses import replace

from gateway import accounts, admin_store, browser_sessions
from gateway.admin_oidc import AdminSession, csrf_token_for_session, decode_session_cookie, encode_session_cookie
from gateway.session_policy import DAY
from tests.test_session_lifecycle import (  # noqa: F401  (sg is a fixture, and needs gateway)
    OWNER, SIGNING_KEY, COOKIE, cookie_session, csrf, forge, gateway, owner_client, person_id, put_cookie,
    registry, session_cookies, sg, signed_in_state,
)

SECOND_DEVICE = "second-device@example.test"


def lineage(g, sid=None):
    rows = g.rows("SELECT * FROM account_session_lineage ORDER BY rowid")
    return rows if sid is None else [row for row in rows if row["session_id"] == sid]


def current_ids(g, family):
    return [row["session_id"] for row in lineage(g) if row["family_id"] == family and row["retired_at"] is None]


def age_retirement(g, seconds):
    """Let ``seconds`` pass for every retired id."""
    with accounts.connection(g.path) as conn:
        conn.execute("UPDATE account_session_lineage SET retired_at=retired_at-? WHERE retired_at IS NOT NULL", (seconds,))


def decode_cookie(cookie):
    return decode_session_cookie(cookie, signing_key=SIGNING_KEY)


def copy_of(g, client):
    """Another browser holding the same cookie."""
    other = g.client()
    put_cookie(other, client.cookies.get(COOKIE))
    return other


def due_session(g, client, **kwargs):
    return forge(g, client, OWNER, issued_ago=10 * DAY, renewed_ago=8 * DAY, **kwargs)


def make_due(client):
    """Age the cookie's last renewal so the next request renews it (the server record is already there)."""
    put_cookie(client, encode_session_cookie(
        replace(cookie_session(client), renewed_at=time.time() - 8 * DAY), signing_key=SIGNING_KEY))


def renew(client):
    """Make the request that renews a due session; return the old and the new session."""
    old = cookie_session(client)
    assert len(session_cookies(client.get("/me/api/session"))) == 1
    return old, cookie_session(client)


# -- rotation ---------------------------------------------------------------

def test_a_renewal_issues_a_new_session_id_and_retires_the_old_one(sg):
    client = owner_client(sg)
    due_session(sg, client)

    old, new = renew(client)

    assert new.session_id != old.session_id
    assert new.family_id == old.family_id == old.session_id
    assert lineage(sg, old.session_id)[0]["retired_at"] is not None
    assert current_ids(sg, new.family_id) == [new.session_id]
    assert registry(sg, new.session_id)[0]["expires_at"] == int(new.expires_at)


def test_a_renewal_keeps_the_deadline_the_sign_in_time_and_the_csrf_token(sg):
    client = owner_client(sg)
    due_session(sg, client, authenticated_ago=9 * DAY)
    token = {"x-azizam-csrf": csrf_token_for_session(cookie_session(client), signing_key=SIGNING_KEY)}

    old, new = renew(client)

    assert int(new.absolute_expires_at) == int(old.absolute_expires_at)
    assert int(new.last_authenticated) == int(old.last_authenticated)
    assert csrf(client) == token                                 # an open page keeps working across a rotation


def test_each_renewal_makes_one_more_id_in_the_same_family(sg):
    client = owner_client(sg)
    due_session(sg, client)
    first_old, first = renew(client)
    make_due(client)                                              # a week later the new id is due too

    _, second = renew(client)

    assert second.family_id == first_old.family_id
    assert len({first_old.session_id, first.session_id, second.session_id}) == 3
    assert current_ids(sg, second.family_id) == [second.session_id]


def test_a_cookie_without_a_family_field_is_its_own_family():
    session = AdminSession.from_dict({"sub": "u", "email": "e", "role": "owner", "iat": 1, "exp": 2, "sid": "abc"})

    assert session.family_id == "abc" and "fid" not in session.to_dict()
    assert AdminSession.from_dict({**session.to_dict(), "fid": "root"}).family_id == "root"


# -- grace ------------------------------------------------------------------

def test_the_old_cookie_still_works_for_a_moment_but_never_renews(sg):
    client = owner_client(sg)
    due_session(sg, client)
    old_cookie = client.cookies.get(COOKIE)
    renew(client)
    in_flight = sg.client()
    put_cookie(in_flight, old_cookie)

    response = in_flight.get("/me/api/session")

    assert response.json()["authenticated"] is True
    assert session_cookies(response) == []                       # no second successor
    assert len(lineage(sg)) == 2


def test_two_requests_with_the_same_due_cookie_make_one_successor(sg):
    client = owner_client(sg)
    due = due_session(sg, client)
    twin = copy_of(sg, client)

    client.get("/me/api/session")
    second = twin.get("/me/api/session")

    assert second.json()["authenticated"] is True and session_cookies(second) == []
    assert len(current_ids(sg, due.session_id)) == 1


def test_the_old_id_stops_working_when_the_person_signs_out_during_the_grace_window(sg):
    client = owner_client(sg)
    due_session(sg, client)
    old_cookie = client.cookies.get(COOKIE)
    renew(client)

    client.post("/auth/api/sign-out-everywhere", headers=csrf(client))

    replay = sg.client()
    put_cookie(replay, old_cookie)
    assert not signed_in_state(replay)


# -- reuse ------------------------------------------------------------------

def test_a_retired_id_shown_after_the_grace_window_revokes_the_whole_sign_in(sg):
    client = owner_client(sg)
    due_session(sg, client)
    stolen = client.cookies.get(COOKIE)
    renew(client)
    assert signed_in_state(client)
    age_retirement(sg, 120)
    thief = sg.client()
    put_cookie(thief, stolen)

    response = thief.get("/me/api/session")

    assert response.json()["authenticated"] is False
    assert any("Max-Age=0" in header for header in session_cookies(response))
    assert not signed_in_state(client)                            # the person's own newer cookie is dead too
    audit = [row for row in sg.rows("SELECT * FROM audit_events WHERE action='account.session.reuse_detected'")]
    assert len(audit) == 1 and audit[0]["target_id"] == person_id(sg, OWNER)


def test_a_retired_id_inside_the_grace_window_is_not_theft(sg):
    client = owner_client(sg)
    due_session(sg, client)
    stolen = client.cookies.get(COOKIE)
    renew(client)
    age_retirement(sg, 50)
    other = sg.client()
    put_cookie(other, stolen)

    assert signed_in_state(other) and signed_in_state(client)
    assert sg.rows("SELECT * FROM audit_events WHERE action='account.session.reuse_detected'") == []


def test_when_the_thief_renews_first_the_person_is_signed_out_when_the_thiefs_id_is_next_shown(sg):
    client = owner_client(sg)
    due_session(sg, client)
    thief = copy_of(sg, client)
    thief.get("/me/api/session")                                  # the thief's request renews first; the thief has not used the new id
    age_retirement(sg, 120)

    assert signed_in_state(client)                                # the person shows the retired id: reissued, not theft (D35b)
    assert sg.rows("SELECT * FROM audit_events WHERE action='account.session.reuse_detected'") == []

    assert not signed_in_state(thief)                             # the thief's id was replaced unused: two holders
    assert not signed_in_state(client)                            # and the sign-in goes with it
    assert len(sg.rows("SELECT * FROM audit_events WHERE action='account.session.reuse_detected'")) == 1


def test_revoking_one_sign_in_leaves_the_persons_other_devices_alone(sg):
    client = owner_client(sg)
    due_session(sg, client)
    other_device = sg.client()
    forge(sg, other_device, OWNER, issued_ago=DAY, sid="other_device_session_1")
    stolen = client.cookies.get(COOKIE)
    renew(client)
    assert signed_in_state(client)                                # the person uses the new id
    age_retirement(sg, 120)
    thief = sg.client()
    put_cookie(thief, stolen)

    assert not signed_in_state(thief)

    assert signed_in_state(other_device)


def test_signing_out_ends_every_id_of_the_family(sg):
    client = owner_client(sg)
    due_session(sg, client)
    old_cookie = client.cookies.get(COOKIE)
    renew(client)

    out = client.post("/logout", headers={**csrf(client), "accept": "application/json"})

    assert out.status_code == 200, out.text
    replay = sg.client()
    put_cookie(replay, old_cookie)
    assert not signed_in_state(replay)
    assert not signed_in_state(client)


# -- keys bound to the family -----------------------------------------------

def mint_try_key(client):
    response = client.post("/me/api/try-key", json={}, headers={**csrf(client), "x-azizam-user-action": "1"})
    assert response.status_code == 201, response.text
    return response.json()["credential"]


def runtime_ok(client, key):
    return client.get("/v1/models", headers={"authorization": "Bearer " + key}).status_code == 200


def test_the_try_key_survives_a_renewal_and_is_found_again_by_the_new_id(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY, renewed_ago=0, authenticated_ago=0)
    key = mint_try_key(client)
    row = sg.rows("SELECT session_id FROM credentials WHERE id=?", (key["id"],))[0]
    assert row["session_id"] == cookie_session(client).family_id
    make_due(client)
    renew(client)

    again = client.post("/me/api/try-key", json={"reuse_id": key["id"]}, headers={**csrf(client), "x-azizam-user-action": "1"})

    assert again.status_code == 200 and again.json()["credential"]["reused"] is True
    assert runtime_ok(client, key["api_key"])


def test_theft_detected_on_a_retired_id_revokes_the_try_key_too(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY, renewed_ago=0, authenticated_ago=0)
    key = mint_try_key(client)
    stolen = client.cookies.get(COOKIE)
    make_due(client)
    renew(client)
    assert runtime_ok(client, key["api_key"])
    assert signed_in_state(client)                                # the new id is used: a fork, not a lost response
    age_retirement(sg, 120)
    put_cookie(client, stolen)

    assert not signed_in_state(client)

    assert not runtime_ok(client, key["api_key"])


def test_signing_out_everywhere_after_a_renewal_revokes_the_try_key(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY, renewed_ago=0, authenticated_ago=0)
    key = mint_try_key(client)
    make_due(client)
    renew(client)

    client.post("/auth/api/sign-out-everywhere", headers=csrf(client))

    assert not runtime_ok(client, key["api_key"])


# -- the race the renewal must lose ------------------------------------------

def test_a_renewal_that_loses_to_a_sign_out_does_not_register_a_session(sg):
    client = owner_client(sg)
    session = due_session(sg, client)
    accounts.revoke_person_sessions(session.subject, path=sg.path)

    rotated = accounts.rotate_session(session.subject, session.session_id, session.family_id, "new-id",
                                      old_expires_at=int(session.expires_at), expires_at=int(time.time()) + DAY, path=sg.path)

    assert rotated is False and registry(sg, "new-id") == [] and lineage(sg) == []


def test_only_one_of_two_renewals_of_the_same_id_wins(sg):
    client = owner_client(sg)
    session = due_session(sg, client)
    args = dict(old_expires_at=int(session.expires_at), expires_at=int(time.time()) + DAY, path=sg.path)

    first = accounts.rotate_session(session.subject, session.session_id, session.family_id, "winner", **args)
    second = accounts.rotate_session(session.subject, session.session_id, session.family_id, "loser", **args)

    assert (first, second) == (True, False)
    assert registry(sg, "winner") != [] and registry(sg, "loser") == []


# -- a lost renewal response is not theft (decision D35b) ---------------------

def lose_the_response(g, client):
    """Renew, and keep only what the phone still holds: the old cookie. Returns (old cookie, lost cookie)."""
    old_cookie = client.cookies.get(COOKIE)
    renew(client)
    return old_cookie, client.cookies.get(COOKIE)


def phone_with(g, cookie):
    phone = g.client()
    put_cookie(phone, cookie)
    return phone


def reuse_events(g):
    return g.rows("SELECT * FROM audit_events WHERE action='account.session.reuse_detected'")


def test_a_lost_renewal_response_leaves_the_person_signed_in_with_a_new_id(sg):
    client = owner_client(sg)
    due_session(sg, client)
    old_cookie, lost_cookie = lose_the_response(sg, client)
    old, lost = decode_cookie(old_cookie), decode_cookie(lost_cookie)
    age_retirement(sg, 120)
    phone = phone_with(sg, old_cookie)

    response = phone.get("/me/api/session")

    assert response.json()["authenticated"] is True
    assert len(session_cookies(response)) == 1 and "Max-Age=0" not in session_cookies(response)[0]
    again = cookie_session(phone)
    assert again.session_id not in {old.session_id, lost.session_id} and again.family_id == old.family_id
    assert current_ids(sg, again.family_id) == [again.session_id]
    assert registry(sg, again.session_id) != [] and registry(sg, lost.session_id) == []
    assert reuse_events(sg) == []
    assert signed_in_state(phone)                                 # the new cookie works, and it keeps working


def test_the_successor_that_never_arrived_is_refused_once_it_is_replaced(sg):
    client = owner_client(sg)
    due_session(sg, client)
    old_cookie, lost_cookie = lose_the_response(sg, client)
    age_retirement(sg, 120)
    phone = phone_with(sg, old_cookie)
    assert signed_in_state(phone)

    straggler = phone_with(sg, lost_cookie)                       # the response turns up after all

    assert not signed_in_state(straggler)
    assert len(reuse_events(sg)) == 1                             # two holders: the sign-in ends
    assert not signed_in_state(phone)


def test_a_reissue_keeps_the_csrf_token_and_the_deadline(sg):
    client = owner_client(sg)
    old = due_session(sg, client, authenticated_ago=9 * DAY)
    token = csrf_token_for_session(old, signing_key=SIGNING_KEY)
    old_cookie, _ = lose_the_response(sg, client)
    age_retirement(sg, 120)
    phone = phone_with(sg, old_cookie)

    phone.get("/me/api/session")

    again = cookie_session(phone)
    assert csrf_token_for_session(again, signing_key=SIGNING_KEY) == token
    assert int(again.absolute_expires_at) == int(old.absolute_expires_at)
    assert int(again.last_authenticated) == int(old.last_authenticated)


def test_the_try_key_survives_a_lost_renewal_response(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY, renewed_ago=0, authenticated_ago=0)
    key = mint_try_key(client)
    make_due(client)
    old_cookie, _ = lose_the_response(sg, client)
    age_retirement(sg, 120)
    phone = phone_with(sg, old_cookie)

    assert signed_in_state(phone)

    assert runtime_ok(phone, key["api_key"])
    again = phone.post("/me/api/try-key", json={"reuse_id": key["id"]}, headers={**csrf(phone), "x-azizam-user-action": "1"})
    assert again.status_code == 200 and again.json()["credential"]["reused"] is True


def test_the_try_key_ends_when_the_successor_was_used_and_the_old_id_comes_back(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY, renewed_ago=0, authenticated_ago=0)
    key = mint_try_key(client)
    stolen = client.cookies.get(COOKIE)
    make_due(client)
    renew(client)
    assert signed_in_state(client)                                # the successor is used
    age_retirement(sg, 120)
    thief = phone_with(sg, stolen)

    assert not signed_in_state(thief)

    assert not runtime_ok(client, key["api_key"]) and len(reuse_events(sg)) == 1


def test_the_first_use_of_an_id_is_recorded_once(sg):
    client = owner_client(sg)
    due_session(sg, client)
    _, new = renew(client)
    assert lineage(sg, new.session_id)[0]["used_at"] is None      # issued, not yet seen on a request
    assert signed_in_state(client)
    first = lineage(sg, new.session_id)[0]["used_at"]
    assert first is not None

    with accounts.connection(sg.path) as conn:
        conn.execute("UPDATE account_session_lineage SET used_at=used_at-1000 WHERE session_id=?", (new.session_id,))
    assert signed_in_state(client)
    assert lineage(sg, new.session_id)[0]["used_at"] == first - 1000     # later requests do not write it again


def test_a_second_lost_response_is_reissued_again(sg):
    client = owner_client(sg)
    due_session(sg, client)
    old_cookie, first_lost = lose_the_response(sg, client)
    age_retirement(sg, 120)
    phone = phone_with(sg, old_cookie)
    phone.get("/me/api/session")
    second_lost = phone.cookies.get(COOKIE)                       # this response is lost too
    age_retirement(sg, 120)
    phone = phone_with(sg, old_cookie)

    response = phone.get("/me/api/session")

    assert response.json()["authenticated"] is True and len(session_cookies(response)) == 1
    final = cookie_session(phone)
    family = decode_cookie(old_cookie).family_id
    assert current_ids(sg, family) == [final.session_id]
    assert len({decode_cookie(old_cookie).session_id, decode_cookie(first_lost).session_id,
                decode_cookie(second_lost).session_id, final.session_id}) == 4
    assert reuse_events(sg) == []


def test_after_a_reissue_the_old_id_is_a_fork_once_the_new_id_is_used(sg):
    client = owner_client(sg)
    due_session(sg, client)
    old_cookie, _ = lose_the_response(sg, client)
    age_retirement(sg, 120)
    phone = phone_with(sg, old_cookie)
    assert signed_in_state(phone) and signed_in_state(phone)      # reissued, then used
    age_retirement(sg, 120)
    copy = phone_with(sg, old_cookie)

    assert not signed_in_state(copy)

    assert not signed_in_state(phone) and len(reuse_events(sg)) == 1


def test_the_old_id_is_served_but_not_renewed_again_inside_the_new_grace_window(sg):
    client = owner_client(sg)
    due_session(sg, client)
    old_cookie, _ = lose_the_response(sg, client)
    age_retirement(sg, 120)
    first = phone_with(sg, old_cookie)
    first.get("/me/api/session")

    second = phone_with(sg, old_cookie).get("/me/api/session")    # the next request of the same burst

    assert second.json()["authenticated"] is True and session_cookies(second) == []
    assert len(current_ids(sg, decode_cookie(old_cookie).family_id)) == 1


def test_concurrent_requests_with_one_retired_id_make_one_successor(sg):
    client = owner_client(sg)
    due_session(sg, client)
    old = cookie_session(client)
    lose_the_response(sg, client)
    age_retirement(sg, 120)
    start, results, errors = threading.Barrier(6), [], []

    def request():
        try:
            start.wait()
            results.append(browser_sessions.advance(old, path=sg.path))
        except Exception as exc:  # noqa: BLE001  (a thread's failure must reach the assertion)
            errors.append(exc)

    threads = [threading.Thread(target=request) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    winners = [result for result in results if result is not None]
    assert len(winners) == 1 and len(results) == 6                # the others are served with no cookie
    assert current_ids(sg, old.family_id) == [winners[0].session_id]
    assert [row for row in lineage(sg) if row["superseded"]] != [] and reuse_events(sg) == []
    assert signed_in_state(phone_with(sg, encode_session_cookie(winners[0], signing_key=SIGNING_KEY)))


def test_a_reissue_that_loses_to_a_sign_out_registers_nothing(sg):
    client = owner_client(sg)
    due_session(sg, client)
    old = cookie_session(client)
    lose_the_response(sg, client)
    age_retirement(sg, 120)
    accounts.revoke_person_sessions(old.subject, path=sg.path)

    reissued = accounts.reissue_session(old.subject, old.session_id, old.family_id, "late-id",
                                        old_expires_at=int(old.expires_at), expires_at=int(time.time()) + DAY, path=sg.path)

    assert reissued is False and registry(sg, "late-id") == []


def test_confirming_with_a_retired_id_whose_successor_was_lost_succeeds(sg):
    client = owner_client(sg)
    due_session(sg, client)
    old = cookie_session(client)
    lose_the_response(sg, client)
    age_retirement(sg, 120)

    confirmed = browser_sessions.confirm(old, path=sg.path)

    assert confirmed.family_id == old.family_id and confirmed.session_id != old.session_id
    assert current_ids(sg, old.family_id) == [confirmed.session_id]


def test_lineage_tables_from_before_first_use_was_recorded_gain_the_columns_and_count_as_used(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE account_session_lineage")
        conn.execute("CREATE TABLE account_session_lineage (session_id TEXT PRIMARY KEY, family_id TEXT NOT NULL, "
                     "retired_at INTEGER, expires_at INTEGER NOT NULL)")
        conn.execute("INSERT INTO account_session_lineage VALUES('old','fam',100,9999999999),('current','fam',NULL,9999999999)")

    admin_store.init_db(db)

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = {row["session_id"]: row for row in conn.execute("SELECT * FROM account_session_lineage")}
    assert rows["old"]["used_at"] == 100 and rows["current"]["used_at"] is not None
    assert rows["old"]["superseded"] == rows["current"]["superseded"] == 0
