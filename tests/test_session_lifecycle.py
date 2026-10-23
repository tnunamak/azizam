"""Browser sign-ins over HTTP: renewal, ending, sign-out everywhere, and step-up.

Real routes and a real database, with the identity provider replaced (see
tests/test_signin_who_can_enter.py). Sessions of any age are forged with the
gateway's own signing key, then sent like a browser would.
"""
import re
import threading
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from gateway import accounts, admin_store, session_policy, step_up
from gateway.admin_runtime import create_admin_service_account_key
from gateway.admin_oidc import (
    AUTH_TIME_UNKNOWN, AdminSession, CookieConfig, csrf_token_for_session, decode_session_cookie, encode_session_cookie,
)
from gateway.session_policy import DAY, MINUTE
from tests.support.access import store_plan
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    OWNER, SIGNING_KEY, gateway, signed_in,
)

COOKIE = "opx_admin_session"
HELPER = "helper@example.test"
PASSWORD = "a sufficiently long password"


@pytest.fixture
def sg(gateway, monkeypatch):
    monkeypatch.setattr(accounts, "_SCRYPT_N", 2 ** 10)       # a real password hash, at a test's cost
    return gateway


def csrf(client):
    return {"x-azizam-csrf": client.get("/admin/api/session").json()["actor"]["csrf_token"]}


def owner_client(g):
    client, response = signed_in(g, sub="owner", email=OWNER)
    assert response.status_code == 302, response.text
    return client


def helper_client(g):
    client, response = signed_in(g, sub="helper", email=HELPER, groups=["/Trusted"])
    assert response.status_code == 302, response.text
    return client


def put_cookie(client, value):
    """Replace the client's session cookie (the jar keys cookies by domain, so say which one)."""
    if COOKIE in client.cookies:
        client.cookies.delete(COOKIE, domain="ai.example", path="/")
    client.cookies.set(COOKIE, value, domain="ai.example", path="/")


def person_id(g, email):
    return g.user(email)["id"]


def forge(g, client, email, *, issued_ago=0.0, renewed_ago=None, authenticated_ago=None,
          expires_in=None, absolute_in=None, legacy=False, sid="forged_session_0001", register=True):
    """Put a session of any age in the client's cookie jar (and the server's record of it)."""
    now = time.time()
    if expires_in is None:
        expires_in = 30 * DAY - (issued_ago if renewed_ago is None else renewed_ago)    # idle window from the last renewal
    session = AdminSession(
        subject=person_id(g, email), email=email, role="owner", issued_at=now - issued_ago,
        expires_at=now + expires_in, session_id=sid,
        absolute_expires_at=None if legacy else now + (absolute_in if absolute_in is not None else 90 * DAY - issued_ago),
        renewed_at=None if legacy else now - (issued_ago if renewed_ago is None else renewed_ago),
        authenticated_at=None if legacy else now - (issued_ago if authenticated_ago is None else authenticated_ago),
    )
    if register:
        accounts.register_session(session.subject, sid, int(session.expires_at), path=g.path)
    put_cookie(client, encode_session_cookie(session, signing_key=SIGNING_KEY))
    return session


def signed_in_state(client) -> bool:
    return client.get("/me/api/session").json()["authenticated"] is True


def session_cookies(response) -> list[str]:
    return [value for value in response.headers.get_list("set-cookie") if value.startswith(f"{COOKIE}=")]


def cookie_session(client) -> AdminSession:
    return decode_session_cookie(client.cookies.get(COOKIE), signing_key=SIGNING_KEY)


def registry(g, sid):
    return g.rows("SELECT * FROM account_sessions WHERE session_id=?", (sid,))


def set_policy(g, **change):
    admin_store.put_operator_setting(
        session_policy.SESSION_POLICY_SETTING_KEY, {**session_policy.DEFAULT_SETTING, **change}, path=g.path)


def set_password(g, email):
    admin_store.set_local_password_verifier(person_id(g, email), accounts.hash_password(PASSWORD), path=g.path)


# ---------------------------------------------------------------------------
# Cookie attributes
# ---------------------------------------------------------------------------

def test_a_sign_in_cookie_is_http_only_lax_site_wide_and_lives_for_the_idle_window(sg):
    _, response = signed_in(sg, sub="owner", email=OWNER)

    header = session_cookies(response)[0]
    assert "HttpOnly" in header and "SameSite=lax" in header and "Path=/" in header
    assert abs(int(re.search(r"Max-Age=(\d+)", header).group(1)) - 30 * DAY) <= 5
    assert "Secure" not in header                                  # this fixture runs the documented plain-HTTP setting


def test_the_cookie_is_secure_unless_the_documented_plain_http_setting_is_on(sg):
    sg.set_config(replace(sg.config, cookie=CookieConfig(secure=True)))

    _, response = signed_in(sg, sub="owner", email=OWNER)

    header = session_cookies(response)[0]
    assert "Secure" in header and "HttpOnly" in header and "SameSite=lax" in header and "Path=/" in header


def test_a_renewed_cookie_keeps_the_same_safe_attributes(sg):
    sg.set_config(replace(sg.config, cookie=CookieConfig(secure=True)))
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=10 * DAY, renewed_ago=8 * DAY)

    response = client.get("/me/api/session")

    header = session_cookies(response)[0]
    assert all(attribute in header for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"))


# ---------------------------------------------------------------------------
# Sliding renewal over HTTP
# ---------------------------------------------------------------------------

def test_a_session_unused_for_over_a_quarter_of_the_idle_window_is_renewed_by_the_next_request(sg):
    client = owner_client(sg)
    old = forge(sg, client, OWNER, issued_ago=10 * DAY, renewed_ago=8 * DAY, authenticated_ago=10 * DAY)

    response = client.get("/me/api/session")

    assert response.json()["authenticated"] is True
    assert len(session_cookies(response)) == 1
    new = cookie_session(client)
    assert new.session_id != old.session_id and new.family_id == old.session_id        # renewing replaces the id (D35)
    assert new.expires_at > old.expires_at + 7 * DAY
    assert abs(new.expires_at - (time.time() + 30 * DAY)) < 10
    assert abs(new.last_renewed - time.time()) < 10
    assert int(new.last_authenticated) == int(old.authenticated_at)          # renewing is not a sign-in
    assert registry(sg, new.session_id)[0]["expires_at"] == int(new.expires_at)


def test_a_recently_renewed_session_gets_no_new_cookie(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=10 * DAY, renewed_ago=2 * DAY)

    response = client.get("/me/api/session")

    assert response.json()["authenticated"] is True
    assert session_cookies(response) == []


def test_an_api_response_renews_too_and_the_csrf_token_is_stable_across_it(sg):
    client = owner_client(sg)
    old = forge(sg, client, OWNER, issued_ago=10 * DAY, renewed_ago=8 * DAY)

    response = client.get("/admin/api/apps")

    assert response.status_code == 200 and len(session_cookies(response)) == 1
    assert csrf(client) == {"x-azizam-csrf": csrf_token_for_session(old, signing_key=SIGNING_KEY)}


def test_static_assets_do_not_renew(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=10 * DAY, renewed_ago=8 * DAY)

    response = client.get("/favicon.ico")

    assert session_cookies(response) == []


def test_a_session_idle_longer_than_the_window_is_signed_out_whatever_its_cookie_says(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=40 * DAY, renewed_ago=31 * DAY, expires_in=10 * DAY, absolute_in=50 * DAY)

    response = client.get("/me/api/session")

    assert response.json()["authenticated"] is False
    assert any("Max-Age=0" in header for header in session_cookies(response))      # the cookie is cleared


def test_a_session_past_its_absolute_lifetime_is_signed_out_even_if_busy(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=91 * DAY, renewed_ago=1 * DAY, expires_in=29 * DAY, absolute_in=-1 * DAY)

    assert client.get("/me/api/session").json()["authenticated"] is False


def test_a_busy_session_just_inside_its_absolute_lifetime_is_still_in_and_not_extended_past_it(sg):
    client = owner_client(sg)
    old = forge(sg, client, OWNER, issued_ago=89 * DAY, renewed_ago=8 * DAY, expires_in=2 * DAY, absolute_in=1 * DAY)

    response = client.get("/me/api/session")

    assert response.json()["authenticated"] is True
    assert session_cookies(response) == []                           # already as long as it may be
    assert int(cookie_session(client).expires_at) == int(old.expires_at)


def test_shortening_the_idle_timeout_signs_out_sessions_already_idle_for_longer(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=10 * DAY, renewed_ago=2 * DAY)
    assert signed_in_state(client)

    set_policy(sg, idle_timeout_days=1)

    assert not signed_in_state(client)


# -- the eight-hour cookies already in people's phones ----------------------

def test_an_old_eight_hour_cookie_still_works_and_is_upgraded_in_place_by_its_first_request(sg):
    client = owner_client(sg)
    old = forge(sg, client, OWNER, issued_ago=3600, expires_in=7 * 3600, legacy=True, sid="legacy_sid_0000001")

    response = client.get("/me/api/session")

    assert response.json()["authenticated"] is True
    upgraded = cookie_session(client)
    assert upgraded.session_id != old.session_id and upgraded.family_id == old.session_id
    assert upgraded.absolute_expires_at is not None and upgraded.renewed_at is not None
    assert abs(upgraded.expires_at - (time.time() + 30 * DAY)) < 10
    assert abs(upgraded.absolute_expires_at - (old.issued_at + 90 * DAY)) < 10      # from the original sign-in
    assert int(upgraded.last_authenticated) == int(old.issued_at)
    assert registry(sg, upgraded.session_id)[0]["expires_at"] == int(upgraded.expires_at)


def test_an_old_eight_hour_cookie_that_has_run_out_is_refused(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=9 * 3600, expires_in=-3600, legacy=True, sid="legacy_sid_0000002")

    assert not signed_in_state(client)


def test_an_old_cookie_without_a_session_id_is_still_valid_until_it_expires_and_keeps_its_identity(sg):
    client = owner_client(sg)
    old = forge(sg, client, OWNER, issued_ago=3600, expires_in=7 * 3600, legacy=True)
    payload = old.to_dict()
    payload.pop("sid")
    put_cookie(client, encode_session_cookie(AdminSession.from_dict(payload), signing_key=SIGNING_KEY))

    assert signed_in_state(client)
    assert cookie_session(client).family_id.startswith("legacy_")


# ---------------------------------------------------------------------------
# Ending sessions
# ---------------------------------------------------------------------------

def test_signing_out_ends_that_session_and_a_copy_of_its_cookie_stops_working(sg):
    client = owner_client(sg)
    copy = client.cookies.get(COOKIE)

    out = client.post("/logout", headers={**csrf(client), "accept": "application/json"})

    assert out.status_code == 200
    replay = sg.client()
    put_cookie(replay, copy)
    assert not signed_in_state(replay)


def test_signing_out_after_a_renewal_still_kills_the_older_copy_of_the_cookie(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=10 * DAY, renewed_ago=8 * DAY)
    older_copy = client.cookies.get(COOKIE)
    client.get("/me/api/session")                                    # renews
    assert client.cookies.get(COOKIE) != older_copy

    client.post("/logout", headers={**csrf(client), "accept": "application/json"})

    replay = sg.client()
    put_cookie(replay, older_copy)
    assert not signed_in_state(replay)


def test_sign_out_everywhere_ends_every_session_of_the_person_and_no_one_elses(sg):
    phone, laptop, other = owner_client(sg), owner_client(sg), helper_client(sg)
    phone_cookie = phone.cookies.get(COOKIE)
    assert signed_in_state(phone) and signed_in_state(laptop) and signed_in_state(other)

    response = phone.post("/auth/api/sign-out-everywhere", headers=csrf(phone))

    assert response.status_code == 200 and response.json() == {"next": "/sign-in/"}
    assert any("Max-Age=0" in header for header in session_cookies(response))
    assert not signed_in_state(phone) and not signed_in_state(laptop)
    stale = sg.client()
    put_cookie(stale, phone_cookie)
    assert not signed_in_state(stale)
    assert signed_in_state(other)
    assert sg.rows("SELECT * FROM account_sessions WHERE user_id=?", (person_id(sg, OWNER),)) == []
    assert len(sg.rows("SELECT 1 FROM audit_events WHERE action='account.sessions.revoke_all'")) == 1


def test_sign_out_everywhere_needs_the_csrf_token_and_a_session(sg):
    phone, laptop = owner_client(sg), owner_client(sg)

    refused = phone.post("/auth/api/sign-out-everywhere")
    anonymous = sg.client().post("/auth/api/sign-out-everywhere")

    assert refused.status_code == 403 and refused.json()["error"]["code"] == "csrf_required"
    assert anonymous.status_code == 401
    assert signed_in_state(phone) and signed_in_state(laptop)


def test_signing_in_again_after_signing_out_everywhere_works(sg):
    client = owner_client(sg)
    client.post("/auth/api/sign-out-everywhere", headers=csrf(client))

    again = owner_client(sg)

    assert signed_in_state(again)


def test_setting_a_password_ends_the_persons_other_sessions_and_keeps_the_one_making_the_change(sg):
    phone, laptop = owner_client(sg), owner_client(sg)

    response = phone.post("/admin/api/signin/owner-password", json={"password": PASSWORD}, headers=csrf(phone))

    assert response.status_code == 200, response.text
    assert signed_in_state(phone)
    assert not signed_in_state(laptop)


def test_a_password_reset_ends_the_persons_sessions(sg):
    owner = owner_client(sg)
    plan = store_plan("Member plan", path=sg.path, models=["test-model"])
    invite = owner.post("/admin/api/invites", json={"access_plan": plan["id"]}, headers=csrf(owner))
    member = sg.client()
    accepted = member.post("/auth/api/invite/accept", json={
        "token": invite.json()["url"].split("#token=")[1], "email": "member@example.test",
        "name": "Member", "password": PASSWORD})
    assert accepted.status_code == 200, accepted.text
    assert signed_in_state(member)
    link = owner.post(f"/admin/api/people/{person_id(sg, 'member@example.test')}/reset",
                      json={"expires_in": 3600}, headers=csrf(owner))

    reset = sg.client().post("/auth/api/password-reset", json={
        "token": link.json()["url"].split("#reset=")[1], "password": "a different long password"})

    assert reset.status_code == 200, reset.text
    assert not signed_in_state(member)


def test_removing_a_person_ends_their_sessions(sg):
    owner = owner_client(sg)
    plan = store_plan("Member plan", path=sg.path, models=["test-model"])
    invite = owner.post("/admin/api/invites", json={"access_plan": plan["id"]}, headers=csrf(owner))
    member = sg.client()
    member.post("/auth/api/invite/accept", json={
        "token": invite.json()["url"].split("#token=")[1], "email": "member@example.test",
        "name": "Member", "password": PASSWORD})
    assert signed_in_state(member)

    removed = owner.delete(f"/admin/api/people/{person_id(sg, 'member@example.test')}", headers=csrf(owner))

    assert removed.status_code == 200, removed.text
    assert not signed_in_state(member)


# ---------------------------------------------------------------------------
# API keys are not browser sessions
# ---------------------------------------------------------------------------

def test_keys_neither_renew_nor_step_up_and_never_get_a_session_cookie(sg):
    owner = owner_client(sg)
    admin_key = owner.post("/admin/api/admin-keys", json={"name": "automation", "role": "owner"}, headers=csrf(owner))
    assert admin_key.status_code == 201, admin_key.text
    oak = admin_key.json()["credential"]["api_key"]
    plan = store_plan("Member plan", path=sg.path, models=["test-model"])
    app = owner.post("/admin/api/apps", json={"name": "Phone app", "kind": "personal", "access_plan": plan["id"],
                                              "owner_user_id": person_id(sg, OWNER)}, headers=csrf(owner)).json()["app"]
    opk = owner.post(f"/admin/api/apps/{app['id']}/keys", json={"label": "k"}, headers=csrf(owner)).json()["credential"]["api_key"]

    bare = sg.client()
    gated = bare.get("/admin/api/settings/effective/export", headers={"authorization": f"Bearer {oak}"})
    gated_write = bare.put("/admin/api/operator/host-commands", json={"enabled": False},
                           headers={"authorization": f"Bearer {oak}", "if-match": '"0"'})
    models = bare.get("/openai/v1/models", headers={"authorization": f"Bearer {opk}"})

    assert gated.status_code == 200, gated.text
    assert gated_write.json().get("error", {}).get("code") != "reauth_required"
    assert models.status_code == 200, models.text
    for response in (gated, gated_write, models):
        assert session_cookies(response) == []
        assert "set-cookie" not in response.headers


def test_a_stale_session_cookie_beside_a_key_does_not_stop_the_key(sg):
    owner = owner_client(sg)
    admin_key = owner.post("/admin/api/admin-keys", json={"name": "automation", "role": "owner"}, headers=csrf(owner))
    oak = admin_key.json()["credential"]["api_key"]
    stale = sg.client()
    forge(sg, stale, OWNER, issued_ago=3 * DAY, authenticated_ago=3 * DAY, sid="stale_beside_key_01")
    accounts.revoke_session("stale_beside_key_01", int(time.time() + 30 * DAY), path=sg.path)      # revoked: no session

    response = stale.get("/admin/api/settings/effective/export", headers={"authorization": f"Bearer {oak}"})

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Step-up
# ---------------------------------------------------------------------------

def concrete(path):
    return path.replace("*", "someid")


def stale_owner(g, *, authenticated_ago=20 * MINUTE, sid="stale_owner_session_1"):
    client = owner_client(g)
    forge(g, client, OWNER, issued_ago=authenticated_ago, renewed_ago=0, authenticated_ago=authenticated_ago, sid=sid)
    return client


def unknown_proof_owner(g, sid="unknown_proof_session_1"):
    """An owner whose session proves no sign-in time. An ordinary sign-in can no longer make one (A2), so this is
    planted the way a session from before A2 would be."""
    client = owner_client(g)
    session = forge(g, client, OWNER, renewed_ago=0, sid=sid)
    unknown = replace(session, authenticated_at=AUTH_TIME_UNKNOWN)
    put_cookie(client, encode_session_cookie(unknown, signing_key=SIGNING_KEY))
    return client


def asks_again(response) -> bool:
    if response.status_code != 401:
        return False
    if response.headers.get("content-type", "").startswith("text/html"):
        return "Sign in again" in response.text           # a form post on the setup page: asked in words
    try:
        return response.json()["error"]["code"] == "reauth_required"
    except (ValueError, KeyError, TypeError):
        return False


@pytest.mark.parametrize("method, route", step_up.SENSITIVE_ROUTES)
def test_every_risky_route_asks_a_stale_session_to_confirm_and_leaves_a_fresh_one_alone(sg, method, route, monkeypatch):
    async def status(_config):                       # the setup page renders the gateway's status
        return {"generated_at": 0, "banner": "ok", "errors": {}}

    monkeypatch.setattr(sg.proxy.status_endpoint, "get_cached_status", status)
    path = concrete(route)
    stale, fresh = stale_owner(sg), owner_client(sg)

    denied = stale.request(method, path, headers=csrf(stale))
    allowed = fresh.request(method, path, headers=csrf(fresh))

    assert asks_again(denied), (denied.status_code, denied.text[:200])
    assert not asks_again(allowed)
    assert denied.headers["cache-control"] == "no-store"


def test_the_risky_route_list_names_routes_that_exist(sg):
    routes = {(method, route.path) for route in sg.proxy.app.routes for method in getattr(route, "methods", ()) or ()}
    templates = {(method, re.sub(r"\{[^}]+\}", "*", path).rstrip("/")) for method, path in routes}

    missing = [(method, path) for method, path in step_up.SENSITIVE_ROUTES if (method, path) not in templates]

    assert missing == []


def test_other_owner_routes_are_not_asked_to_confirm(sg):
    stale = stale_owner(sg)

    for path in ("/admin/api/apps", "/admin/api/people", "/admin/api/settings/instance", "/admin/api/session"):
        assert not asks_again(stale.get(path)), path


def test_the_401_says_how_this_person_can_confirm_and_for_how_long(sg):
    stale = stale_owner(sg)
    set_password(sg, OWNER)

    body = stale.get("/admin/api/settings/effective/export").json()["error"]

    assert body["code"] == "reauth_required"
    assert body["reauth"] == {"methods": ["password", "oidc"], "window_seconds": 15 * MINUTE, "oidc_url": "/login?reauth=1"}


def test_a_person_with_only_the_provider_is_offered_the_provider(sg):
    stale = stale_owner(sg)

    assert stale.get("/admin/api/settings/effective/export").json()["error"]["reauth"]["methods"] == ["oidc"]


def test_confirming_with_the_right_password_makes_the_session_fresh_and_the_retry_goes_through(sg):
    stale = stale_owner(sg)
    set_password(sg, OWNER)
    assert asks_again(stale.get("/admin/api/settings/effective/export"))
    before = cookie_session(stale)

    response = stale.post("/auth/api/reauth", json={"password": PASSWORD}, headers=csrf(stale))

    assert response.status_code == 200 and response.json()["ok"] is True
    after = cookie_session(stale)
    assert after.session_id != before.session_id and after.family_id == before.family_id
    assert abs(after.last_authenticated - time.time()) < 10
    retry = stale.get("/admin/api/settings/effective/export")
    assert retry.status_code == 200, retry.text
    assert registry(sg, after.session_id)[0]["expires_at"] == int(after.expires_at)


def test_a_wrong_password_does_not_make_the_session_fresh(sg):
    stale = stale_owner(sg)
    set_password(sg, OWNER)

    response = stale.post("/auth/api/reauth", json={"password": "not the password at all"}, headers=csrf(stale))

    assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_password"
    assert session_cookies(response) == []
    assert asks_again(stale.get("/admin/api/settings/effective/export"))


def test_confirming_needs_the_csrf_token_and_a_session(sg):
    stale = stale_owner(sg)
    set_password(sg, OWNER)

    no_token = stale.post("/auth/api/reauth", json={"password": PASSWORD})
    anonymous = sg.client().post("/auth/api/reauth", json={"password": PASSWORD})

    assert no_token.status_code == 403 and no_token.json()["error"]["code"] == "csrf_required"
    assert anonymous.status_code == 401


def test_a_person_with_no_password_cannot_confirm_with_one(sg):
    stale = stale_owner(sg)

    response = stale.post("/auth/api/reauth", json={"password": PASSWORD}, headers=csrf(stale))

    assert response.status_code == 409 and response.json()["error"]["code"] == "password_not_set"


def test_confirming_shares_the_sign_in_throttle(sg):
    stale = stale_owner(sg)
    set_password(sg, OWNER)

    codes = {stale.post("/auth/api/reauth", json={"password": "wrong password here"}, headers=csrf(stale)).status_code
             for _ in range(14)}

    assert codes == {401, 429}


def test_the_step_up_window_is_the_owners_setting(sg):
    set_policy(sg, step_up_minutes=60)
    client = stale_owner(sg, authenticated_ago=40 * MINUTE)
    assert not asks_again(client.get("/admin/api/settings/effective/export"))

    set_policy(sg, step_up_minutes=30)

    assert asks_again(client.get("/admin/api/settings/effective/export"))


def test_a_fresh_sign_in_is_good_for_exactly_the_window(sg):
    inside = stale_owner(sg, authenticated_ago=14 * MINUTE, sid="inside_window_0001")
    outside = stale_owner(sg, authenticated_ago=16 * MINUTE, sid="outside_window_001")

    assert not asks_again(inside.get("/admin/api/settings/effective/export"))
    assert asks_again(outside.get("/admin/api/settings/effective/export"))


def test_renewal_does_not_count_as_confirming(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=10 * DAY, renewed_ago=8 * DAY, authenticated_ago=10 * DAY)

    client.get("/me/api/session")                                       # renews the cookie

    assert asks_again(client.get("/admin/api/settings/effective/export"))


def test_a_non_admin_is_refused_by_the_route_not_asked_to_confirm(sg):
    member = sg.client()
    plan = store_plan("Member plan", path=sg.path, models=["test-model"])
    owner = owner_client(sg)
    invite = owner.post("/admin/api/invites", json={"access_plan": plan["id"]}, headers=csrf(owner))
    member.post("/auth/api/invite/accept", json={
        "token": invite.json()["url"].split("#token=")[1], "email": "member@example.test", "name": "M", "password": PASSWORD})
    old = time.time() - DAY
    session = AdminSession(subject=person_id(sg, "member@example.test"), email="member@example.test", role="none",
                           issued_at=old, expires_at=old + 30 * DAY, session_id="member_stale_000001",
                           absolute_expires_at=old + 90 * DAY, renewed_at=time.time(), authenticated_at=old)
    accounts.register_session(session.subject, session.session_id, int(session.expires_at), path=sg.path)
    put_cookie(member, encode_session_cookie(session, signing_key=SIGNING_KEY))

    response = member.get("/admin/api/settings/effective/export")

    assert response.status_code == 403 and not asks_again(response)


# -- confirming with the identity provider -----------------------------------

def through_provider(g, client, *, reauth=True, sub="owner", email=OWNER, auth_time: int | float | str | None = "now",
                     iat: int | None = None):
    """Run the provider sign-in the browser would: /login (with reauth=1 for step-up), then the callback.

    The provider says the person signed in just now, as Keycloak does. ``auth_time=None`` is a provider whose
    ID token carries no ``auth_time``. ``iat`` is the token's own issue time (default: the fixed fixture time)."""
    if auth_time == "now":
        auth_time = int(time.time())
    params = {"return_to": "/settings/", **({"reauth": "1"} if reauth else {})}
    login = client.get("/login", params=params, follow_redirects=False)
    if "redirect_uri=" not in login.headers["location"]:
        login = client.get(login.headers["location"], follow_redirects=False)
    state, record = list(g.proxy.ADMIN_OIDC_STATES.items())[-1]

    async def exchange(code, *, config, pkce):
        return {"id_token": "fake.id.token"}

    async def verify(token, *, config):
        claims = {"iss": config.issuer, "aud": config.client_id, "sub": sub, "email": email, "email_verified": True,
                  "nonce": record.nonce, "exp": 4_000_000_000, "iat": int(time.time()) if iat is None else iat}
        if auth_time is not None:
            claims["auth_time"] = auth_time
        return claims

    g.monkeypatch.setattr(g.proxy, "_oidc_exchange_code", exchange)
    g.monkeypatch.setattr(g.proxy, "_oidc_verify_id_token", verify)
    callback = client.get("/admin/oidc/callback", params={"state": state, "code": "c"},
                          headers={"accept": "text/html"}, follow_redirects=False)
    return login, callback


reauth_through_provider = through_provider


def test_signing_in_through_the_provider_asks_it_to_log_the_person_in_again(sg):
    stale = stale_owner(sg)

    login = stale.get("/login", params={"reauth": "1", "return_to": "/"}, follow_redirects=False)
    plain = owner_client(sg).get("/login", params={"return_to": "/"}, follow_redirects=False)

    assert "prompt=login" in login.headers["location"] and "max_age=0" in login.headers["location"]
    # A2: a sign-in from the sign-in page asks too, so "Continue with {provider}" cannot resume the last person.
    assert "prompt=login" in plain.headers["location"] and "max_age=0" in plain.headers["location"]


# -- a fresh sign-in on every sign-in from the sign-in page (A2) --------------

@pytest.mark.parametrize("auth_time_ago", [None, 3600, 120], ids=["no-auth-time", "an-hour-old", "two-minutes-old"])
def test_fresh_sso_rejects_missing_or_old_auth_time(sg, auth_time_ago):
    """Give or take the 60-second allowance, the provider must say the person signed in after the flow began.
    Otherwise it may have reused its own login, so nobody is signed in."""
    client = sg.client()
    auth_time = None if auth_time_ago is None else int(time.time()) - auth_time_ago

    _, callback = through_provider(sg, client, reauth=False, auth_time=auth_time)

    assert callback.status_code == 303 and callback.headers["location"].startswith("/sign-in/?error=not_fresh")
    assert session_cookies(callback) == [] and not signed_in_state(client)


def test_fresh_sso_accepts_any_subject(sg):
    """A fresh sign-in is not a step-up, so the person may be someone else (a switch). Only a step-up (reauth=1)
    has to be the same person: see test_confirming_through_the_provider_as_someone_else_is_refused."""
    client = stale_owner(sg)

    callback = sg.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"])

    assert callback.status_code == 302 and callback.headers["location"] == "/"
    assert cookie_session(client).subject == person_id(sg, HELPER)


@pytest.mark.parametrize("ahead_seconds, accepted", [
    (60, True), (61, False), (10 * 365 * DAY, False),
], ids=["on-the-allowance", "one-second-past-it", "ten-years-ahead"])
def test_fresh_sso_refuses_an_auth_time_later_than_the_tokens_own_issue_time(sg, ahead_seconds, accepted):
    """The provider's clock on both sides: auth_time may be at most the clock-skew allowance after the token's own
    iat. A provider (or a replayed token) that dates the sign-in in the future proves nothing."""
    client = sg.client()
    now_s = int(time.time())

    _, callback = through_provider(sg, client, reauth=False, auth_time=now_s + ahead_seconds, iat=now_s)

    if accepted:
        assert callback.status_code == 302 and signed_in_state(client)
    else:
        assert callback.status_code == 303 and callback.headers["location"].startswith("/sign-in/?error=not_fresh")
        assert not signed_in_state(client)


def test_a_refused_sign_in_links_to_the_sign_out_when_the_provider_publishes_one(sg, monkeypatch):
    async def discovery(issuer):
        return {"end_session_endpoint": "https://id.example/logout"}

    monkeypatch.setattr(sg.proxy, "_provider_discovery", discovery)
    client = sg.client()

    _, callback = through_provider(sg, client, reauth=False, auth_time=None)

    query = parse_qs(urlsplit(callback.headers["location"]).query)
    assert query["error"] == ["not_fresh"] and query["provider_sign_out"] == ["1"]


def test_a_refused_sign_in_offers_no_link_when_the_provider_publishes_no_sign_out_address(sg, monkeypatch):
    async def discovery(issuer):
        return {"issuer": issuer}

    monkeypatch.setattr(sg.proxy, "_provider_discovery", discovery)
    client = sg.client()

    _, callback = through_provider(sg, client, reauth=False, auth_time=None)

    query = parse_qs(urlsplit(callback.headers["location"]).query)
    assert query["error"] == ["not_fresh"] and "provider_sign_out" not in query


def test_confirming_through_the_provider_rotates_the_session_id_inside_the_same_sign_in(sg):
    """A provider confirmation rotates the session id inside the same sign-in, as a password confirmation does
    (browser_sessions.confirm). The id it replaced works only inside the grace window, because its response may
    have been lost; afterwards a copy of it ends the sign-in once the new id has been used (decision D35)."""
    stale = stale_owner(sg)
    old_cookie = stale.cookies.get(COOKIE)
    before = cookie_session(stale)

    _, callback = reauth_through_provider(sg, stale, auth_time=int(time.time()))

    assert callback.status_code == 302 and callback.headers["location"] == "/settings/"
    assert not asks_again(stale.get("/admin/api/settings/effective/export"))
    after = cookie_session(stale)
    assert after.family_id == before.family_id and after.session_id != before.session_id
    replay = sg.client()
    put_cookie(replay, old_cookie)
    assert signed_in_state(replay)


def test_a_provider_that_reuses_its_own_old_login_does_not_confirm_anyone(sg):
    stale = stale_owner(sg)

    _, callback = reauth_through_provider(sg, stale, auth_time=int(time.time()) - 3600)

    assert callback.status_code == 303 and callback.headers["location"].startswith("/sign-in/?error=provider")
    assert asks_again(stale.get("/admin/api/settings/effective/export"))


def test_an_ordinary_provider_sign_in_with_a_recent_login_is_a_recent_sign_in(sg):
    client = sg.client()

    through_provider(sg, client, reauth=False, auth_time=int(time.time()) - 30)

    assert not asks_again(client.get("/admin/api/settings/effective/export"))


def test_a_provider_that_reports_no_auth_time_does_not_sign_the_person_in(sg):
    """A2: an ordinary sign-in must prove a sign-in from just now. A provider that reports no time cannot prove
    that, so nobody is signed in. (D40's "signed in, but not recently" came from this path; it can no longer.)"""
    client = sg.client()

    _, callback = through_provider(sg, client, reauth=False, auth_time=None)

    assert callback.status_code == 303 and callback.headers["location"].startswith("/sign-in/?error=not_fresh")
    assert not signed_in_state(client)


def test_a_session_that_proves_no_sign_in_time_stays_not_recent_when_it_renews(sg):
    """Sessions from before A2 may still carry an unknown sign-in time. Renewing one must not make it recent."""
    client = unknown_proof_owner(sg)
    session = cookie_session(client)
    renewed = session_policy.renewal(session, session_policy.load_policy(sg.path), session.issued_at + 10 * DAY)

    assert renewed is not None and renewed.authenticated_at == AUTH_TIME_UNKNOWN
    assert not session_policy.is_recently_authenticated(renewed, session_policy.load_policy(sg.path), renewed.renewed_at)


def test_a_person_with_no_recent_time_confirms_with_a_password_and_is_recent_after(sg):
    client = unknown_proof_owner(sg)
    set_password(sg, OWNER)
    assert asks_again(client.get("/admin/api/settings/effective/export"))

    confirmed = client.post("/auth/api/reauth", json={"password": PASSWORD}, headers=csrf(client))

    assert confirmed.status_code == 200, confirmed.text
    assert not asks_again(client.get("/admin/api/settings/effective/export"))


def test_a_person_with_no_recent_time_confirms_through_a_provider_that_reports_the_time(sg):
    client = unknown_proof_owner(sg)

    _, callback = through_provider(sg, client, reauth=True, auth_time=int(time.time()))

    assert callback.status_code == 302
    assert not asks_again(client.get("/admin/api/settings/effective/export"))


def test_a_provider_that_never_reports_the_time_cannot_confirm_and_says_why_in_plain_words(sg):
    client = unknown_proof_owner(sg)

    _, callback = through_provider(sg, client, reauth=True, auth_time=None)

    assert callback.status_code == 303 and callback.headers["location"].startswith("/sign-in/?error=no_sign_in_time")
    assert asks_again(client.get("/admin/api/settings/effective/export"))


def test_the_same_refusal_in_json_names_the_provider_and_the_other_way_to_confirm(sg):
    client = unknown_proof_owner(sg)
    login = client.get("/login", params={"return_to": "/settings/", "reauth": "1"}, follow_redirects=False)
    if "redirect_uri=" not in login.headers["location"]:
        login = client.get(login.headers["location"], follow_redirects=False)
    state, record = list(sg.proxy.ADMIN_OIDC_STATES.items())[-1]

    async def verify(token, *, config):
        return {"iss": config.issuer, "aud": config.client_id, "sub": "owner", "email": OWNER, "email_verified": True,
                "nonce": record.nonce, "exp": 4_000_000_000, "iat": int(time.time())}

    sg.monkeypatch.setattr(sg.proxy, "_oidc_verify_id_token", verify)
    callback = client.get("/admin/oidc/callback", params={"state": state, "code": "c"}, follow_redirects=False)

    assert callback.status_code == 400
    error = callback.json()["error"]
    assert error["code"] == "provider_no_sign_in_time"
    assert "does not tell Azizam when you signed in" in error["message"] and "password" in error["message"]


def test_an_auth_time_that_is_not_a_number_is_no_auth_time(sg):
    client = sg.client()

    _, callback = through_provider(sg, client, reauth=False, auth_time=float("nan"))

    assert callback.status_code == 303 and callback.headers["location"].startswith("/sign-in/?error=not_fresh")
    assert not signed_in_state(client)


def test_a_reauth_whose_auth_time_is_just_inside_the_clock_skew_allowance_confirms(sg):
    """Decision D40: the provider's clock may run up to 60 seconds behind ours."""
    stale = stale_owner(sg)

    _, callback = through_provider(sg, stale, reauth=True, auth_time=int(time.time()) - 45)

    assert callback.status_code == 302
    assert not asks_again(stale.get("/admin/api/settings/effective/export"))


def test_the_clock_skew_allowance_is_sixty_seconds_and_written_down(sg):
    from pathlib import Path

    from gateway.admin_oidc import AUTH_TIME_CLOCK_SKEW_SECONDS

    doc = (Path(__file__).resolve().parents[1] / "docs" / "ops" / "SIGN_IN_SESSIONS.md").read_text()

    assert AUTH_TIME_CLOCK_SKEW_SECONDS == 60
    assert "AUTH_TIME_CLOCK_SKEW_SECONDS" in doc and "60 seconds" in doc


def test_a_reauth_whose_auth_time_is_past_the_clock_skew_allowance_does_not_confirm(sg):
    stale = stale_owner(sg)

    _, callback = through_provider(sg, stale, reauth=True, auth_time=int(time.time()) - 120)

    assert callback.status_code == 303 and callback.headers["location"].startswith("/sign-in/?error=provider")
    assert asks_again(stale.get("/admin/api/settings/effective/export"))


def test_confirming_through_the_provider_as_someone_else_is_refused(sg):
    stale = stale_owner(sg)
    helper = helper_client(sg)                                      # a second person exists
    assert signed_in_state(helper)

    _, callback = through_provider(sg, stale, sub="helper", email=HELPER, auth_time=int(time.time()))

    assert callback.status_code == 303 and callback.headers["location"].startswith("/sign-in/?error=provider")
    assert asks_again(stale.get("/admin/api/settings/effective/export"))


def test_the_reauth_request_survives_the_hop_to_the_callback_host(sg):
    elsewhere = sg.client()
    elsewhere.base_url = "https://other.example"

    response = elsewhere.get("/login", params={"reauth": "1", "return_to": "/settings/"}, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://ai.example/login?")
    assert "reauth=1" in response.headers["location"]


# -- review findings ---------------------------------------------------------

def test_a_host_header_that_reshapes_the_url_does_not_skip_the_confirmation(sg):
    stale = stale_owner(sg)

    for host in ("ai.example#", "ai.example?x=1", "ai.example/x"):
        response = stale.get("/admin/api/settings/effective/export", headers={"host": host})
        assert asks_again(response), (host, response.status_code)


def test_signing_out_an_old_cookie_also_stops_a_copy_that_was_renewed_before(sg):
    client = owner_client(sg)
    old = forge(sg, client, OWNER, issued_ago=3600, expires_in=7 * 3600, legacy=True, sid="legacy_signout_0001")
    thief = sg.client()
    put_cookie(thief, client.cookies.get(COOKIE))
    thief.get("/me/api/session")                                  # the copy renews: it now lasts for weeks
    renewed_copy = thief.cookies.get(COOKIE)
    assert cookie_session(thief).absolute_expires_at is not None

    still_old = {"x-azizam-csrf": csrf_token_for_session(old, signing_key=SIGNING_KEY)}   # no call that would renew it
    out = client.post("/logout", headers={**still_old, "accept": "application/json"})
    assert out.status_code == 200, out.text

    replay = sg.client()
    put_cookie(replay, renewed_copy)
    assert not signed_in_state(replay)
    revoked_until = sg.rows("SELECT expires_at FROM account_session_revocations WHERE session_id=?", (old.session_id,))[0]["expires_at"]
    assert revoked_until >= old.issued_at + 365 * DAY - 5               # past the longest absolute lifetime an owner may set


def test_a_session_claimed_with_an_admin_key_never_renews_and_ends_at_its_fixed_life(sg):
    from gateway import browser_sessions
    client = owner_client(sg)
    claimed = browser_sessions.issue({"sub": person_id(sg, OWNER), "email": OWNER}, role="owner",
                                     cookie=sg.config.cookie, path=sg.path, register=False, renewable=False,
                                     now=time.time() - 7 * 3600)
    put_cookie(client, encode_session_cookie(claimed, signing_key=SIGNING_KEY))

    response = client.get("/me/api/session")

    assert response.json()["authenticated"] is True
    assert session_cookies(response) == []                           # not renewed, however old
    assert claimed.expires_at - claimed.issued_at == 8 * 3600
    assert claimed.renewable is False
    assert decode_session_cookie(encode_session_cookie(claimed, signing_key=SIGNING_KEY), signing_key=SIGNING_KEY,
                                 now=time.time()).renewable is False


# ---------------------------------------------------------------------------
# Where a sign-in writes its session rows
# ---------------------------------------------------------------------------

SETUP_EMAIL = "local.owner@example.test"


class _LoopWatch:
    """The event loop thread of each request, and the thread that ran each session write."""

    def __init__(self):
        self.loop_threads: list[int] = []                  # one ident per request, in order
        self.writes: list[tuple[str, int, int]] = []       # (call, thread, request index)

    def clear(self):
        self.loop_threads.clear()
        self.writes.clear()

    def client(self, g) -> TestClient:
        async def serve(scope, receive, send):
            if scope["type"] == "http":
                self.loop_threads.append(threading.get_ident())
            await g.proxy.app(scope, receive, send)
        return TestClient(serve, base_url="https://ai.example", client=("127.0.0.1", 50000))


def _claim_with_an_admin_key(g, client, watch):
    admin_key, _, _ = create_admin_service_account_key("real-owner", path=g.path)
    watch.clear()
    return client.post("/setup/claim", data={"admin_key": admin_key}, follow_redirects=False)


def _sign_in_as_a_local_owner(g, client, watch):
    created = client.post("/setup/local-owner", data={
        "email": SETUP_EMAIL, "password": PASSWORD, "password_confirm": PASSWORD,
    }, follow_redirects=False)
    assert created.status_code == 303, created.text
    client.cookies.clear()
    watch.clear()
    return client.post("/setup/local-login", data={"email": SETUP_EMAIL, "password": PASSWORD},
                       follow_redirects=False)


def _sign_in_through_the_provider(g, client, watch):
    watch.clear()
    return g.sign_in(client, sub="trusted", email="trusted@example.test", groups=["/Trusted"])


@pytest.mark.parametrize("sign_in", [
    _claim_with_an_admin_key, _sign_in_as_a_local_owner, _sign_in_through_the_provider,
], ids=["setup claim", "local login", "provider callback"])
def test_a_sign_in_writes_the_session_database_off_the_event_loop(sg, monkeypatch, sign_in):
    """end_incoming and issue open and write SQLite. Both must leave the event loop.

    An fsync on the loop stalls every request behind it, so the two writes run in a thread, in
    this order: end the incoming session, then issue the new one.
    """
    from gateway import browser_sessions
    real = {name: getattr(browser_sessions, name) for name in ("end_incoming", "issue")}
    watch = _LoopWatch()

    def recording(name):
        def call(*args, **kwargs):
            # The request being served right now, so a reused thread ident cannot confuse the check.
            watch.writes.append((name, threading.get_ident(), len(watch.loop_threads) - 1))
            return real[name](*args, **kwargs)
        return call

    for name in real:
        monkeypatch.setattr(browser_sessions, name, recording(name))
    client = watch.client(sg)
    try:
        response = sign_in(sg, client, watch)
    finally:
        client.close()

    assert response.status_code in (302, 303), response.text
    assert [name for name, _, _ in watch.writes] == ["end_incoming", "issue"], (
        "a sign-in must end the incoming session before it issues the new one")
    assert all(index >= 0 for _, _, index in watch.writes), "a session write ran outside a request"
    on_loop = [name for name, thread, index in watch.writes if thread == watch.loop_threads[index]]
    assert not on_loop, f"{on_loop} wrote the session database on the event loop thread"


def test_reauth_keeps_the_session_view_id_whichever_way_the_person_confirms(sg):
    """The view id is the public face of a sign-in. A step-up keeps it: a password confirm and a provider confirm
    both keep the sign-in's family, as the browser's other tabs rely on (web/src/lib/session-end.ts)."""
    client = stale_owner(sg)
    set_password(sg, OWNER)
    view = client.get("/me/api/session").json()["session_view_id"]

    confirmed = client.post("/auth/api/reauth", json={"password": PASSWORD}, headers=csrf(client))
    assert confirmed.status_code == 200
    assert client.get("/me/api/session").json()["session_view_id"] == view

    _, callback = reauth_through_provider(sg, client, auth_time=int(time.time()))
    assert callback.status_code == 302
    assert client.get("/me/api/session").json()["session_view_id"] == view
