"""The independent security review of the sliding-session branch (c248cc5d), kept as regressions.

Each test is a probe from that review, turned round: it passed there by showing the hole and passes here
by showing it closed. The review's input is named in each test.
"""
import copy
import time
from dataclasses import replace

import pytest

from gateway import accounts, admin_store, native_lane, step_up, user_runtime
from gateway.admin_api import AdminContext
from gateway.admin_oidc import AdminSession, encode_session_cookie, session_from_claims
from gateway.admin_runtime import AdminSQLiteStore
from gateway.runtime_auth import hash_api_key
from gateway.session_policy import DAY, MINUTE
from tests.test_credential_binding_fingerprint import ATTACKS, NEW_HOST, _attacked, _stored
from tests.test_session_lifecycle import (  # noqa: F401  (sg is a fixture, and needs gateway)
    COOKIE, HELPER, OWNER, PASSWORD, SIGNING_KEY, asks_again, cookie_session, csrf, forge, gateway, owner_client,
    person_id, put_cookie, registry, set_password, sg, signed_in_state, stale_owner, through_provider,
)
from tests.support.access import store_plan

ACT = {"x-azizam-user-action": "1"}


def credential(g, key):
    return admin_store.find_credential_by_hash(hash_api_key(key), path=g.path)


def runtime_status(client, key):
    return client.get("/v1/models", headers={"authorization": "Bearer " + key}).status_code


def seed_registry(g):
    admin_store.write_service_registry_with_audit(
        _stored(), expected_generation=0, updated_by="seed", audit_action="seed",
        audit_actor_kind="user", audit_actor_id="seed", audit_actor_label="seed",
        audit_target_kind="operator_setting", audit_target_id="service_registry", path=g.path)


def member_client(g, *, stale=False, email="member@example.test"):
    """A person with an ordinary plan, signed in by password a moment ago, or a day ago when ``stale``."""
    client = g.client()
    plan = store_plan(f"Member plan {email}", path=g.path, models=["test-model"])
    owner = owner_client(g)
    invite = owner.post("/admin/api/invites", json={"access_plan": plan["id"]}, headers=csrf(owner))
    accepted = client.post("/auth/api/invite/accept", json={
        "token": invite.json()["url"].split("#token=")[1], "email": email, "name": "M", "password": PASSWORD})
    assert accepted.status_code in (200, 201), accepted.text
    if stale:
        old = time.time() - DAY
        session = replace(cookie_session(client), issued_at=old, renewed_at=time.time(), authenticated_at=old)
        put_cookie(client, encode_session_cookie(session, signing_key=SIGNING_KEY))
    return client


def member_headers(client):
    return {**ACT, "x-azizam-csrf": client.get("/me/api/session").json()["csrf_token"]}


# -- HIGH 1: an old session made a key that outlives it --------------------------------------------

def test_an_old_owner_session_cannot_make_a_permanent_app_key_through_the_self_service_route(sg):
    """The review's input: one-day-old owner cookie, POST /me/api/apps then POST /me/api/apps/{id}/keys."""
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY)
    headers = {**csrf(client), **ACT}
    app = client.post("/me/api/apps", json={"name": "attacker persistence"}, headers=headers)
    assert app.status_code == 201
    app_id = app.json()["app"]["id"]
    assert asks_again(client.post(f"/admin/api/apps/{app_id}/keys", json={"label": "probe"}, headers=headers))

    bypass = client.post(f"/me/api/apps/{app_id}/keys", json={"label": "probe"}, headers=headers)

    assert asks_again(bypass), (bypass.status_code, bypass.text[:200])
    assert sg.rows("SELECT * FROM credentials WHERE app_id=?", (app_id,)) == []


def test_after_confirming_the_same_person_makes_the_key(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY)
    set_password(sg, OWNER)
    headers = {**csrf(client), **ACT}
    app_id = client.post("/me/api/apps", json={"name": "mine"}, headers=headers).json()["app"]["id"]
    assert asks_again(client.post(f"/me/api/apps/{app_id}/keys", json={"label": "k"}, headers=headers))

    assert client.post("/auth/api/reauth", json={"password": PASSWORD}, headers=csrf(client)).status_code == 200

    made = client.post(f"/me/api/apps/{app_id}/keys", json={"label": "k"}, headers={**csrf(client), **ACT})
    assert made.status_code == 201, made.text


def test_a_member_is_asked_too_and_a_fresh_member_is_not(sg):
    stale, fresh = member_client(sg, stale=True), member_client(sg, email="fresh@example.test")
    app_id = stale.post("/me/api/apps", json={"name": "mine"}, headers=member_headers(stale)).json()["app"]["id"]
    fresh_app = fresh.post("/me/api/apps", json={"name": "mine"}, headers=member_headers(fresh)).json()["app"]["id"]

    denied = stale.post(f"/me/api/apps/{app_id}/keys", json={"label": "k"}, headers=member_headers(stale))
    allowed = fresh.post(f"/me/api/apps/{fresh_app}/keys", json={"label": "k"}, headers=member_headers(fresh))

    assert asks_again(denied) and sg.rows("SELECT * FROM credentials WHERE app_id=?", (app_id,)) == []
    assert allowed.status_code == 201, allowed.text


def test_the_setup_page_will_not_make_a_first_app_key_for_an_old_session(sg, monkeypatch):
    """The review's second entry point: POST /setup/first-app."""
    async def status(_config):
        return {"generated_at": 0, "banner": "ok", "errors": {}}

    monkeypatch.setattr(sg.proxy.status_endpoint, "get_cached_status", status)
    stale, fresh = stale_owner(sg), owner_client(sg)
    form = lambda client: {"csrf_token": csrf(client)["x-azizam-csrf"], "app_name": "first-app", "key_label": "Laptop"}

    denied = stale.post("/setup/first-app", data=form(stale))
    allowed = fresh.post("/setup/first-app", data=form(fresh))

    assert denied.status_code == 401 and "Sign in again" in denied.text and "opk_" not in denied.text
    assert allowed.status_code == 201 and "opk_" in allowed.text
    assert len(sg.rows("SELECT * FROM credentials WHERE subject_kind='app'")) == 1


def test_a_try_key_is_asked_for_an_administrator_only_when_it_would_make_a_new_one(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=0.0)
    first = client.post("/me/api/try-key", json={}, headers={**csrf(client), **ACT})
    assert first.status_code == 201
    old = time.time() - DAY
    put_cookie(client, encode_session_cookie(
        replace(cookie_session(client), issued_at=old, renewed_at=time.time(), authenticated_at=old),
        signing_key=SIGNING_KEY))

    reused = client.post("/me/api/try-key", json={"reuse_id": first.json()["credential"]["id"]}, headers={**csrf(client), **ACT})
    fresh_mint = client.post("/me/api/try-key", json={"reuse_id": "gone"}, headers={**csrf(client), **ACT})

    assert reused.status_code == 200 and reused.json()["credential"]["reused"] is True
    assert asks_again(fresh_mint)
    assert len(sg.rows("SELECT * FROM credentials WHERE session_id IS NOT NULL")) == 1


def test_a_member_gets_a_try_key_from_an_old_session_without_being_asked(sg):
    stale = member_client(sg, stale=True)

    made = stale.post("/me/api/try-key", json={}, headers=member_headers(stale))

    assert made.status_code == 201, made.text


# -- HIGH 2: a registry write moved where a stored credential is sent --------------------------------

def test_an_old_owner_session_cannot_point_an_engine_that_holds_a_key_at_another_host(sg):
    """The review's input: PUT /admin/api/operator/service-registry with config.url changed."""
    seed_registry(sg)
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY)
    current = client.get("/admin/api/operator/service-registry")
    body = current.json()["service_registry"]
    body["services"]["eng"]["config"]["url"] = NEW_HOST

    response = client.put("/admin/api/operator/service-registry", json=body,
                          headers={**csrf(client), "If-Match": current.headers["etag"]})

    assert asks_again(response), (response.status_code, response.text[:200])
    stored = admin_store.get_service_registry_with_generation(path=sg.path)["value"]
    assert "url" not in stored["services"]["eng"]["config"]


def test_a_confirmed_owner_still_moves_the_engine_and_the_key_follows(sg):
    seed_registry(sg)
    client = owner_client(sg)
    current = client.get("/admin/api/operator/service-registry")
    body = current.json()["service_registry"]
    body["services"]["eng"]["config"]["url"] = NEW_HOST

    response = client.put("/admin/api/operator/service-registry", json=body,
                          headers={**csrf(client), "If-Match": current.headers["etag"]})

    assert response.status_code == 200, response.text
    config = admin_store.get_service_registry_with_generation(path=sg.path)["value"]["services"]["eng"]["config"]
    assert native_lane.credential_for(config, "generate").bound_to(NEW_HOST)


def test_an_edit_that_moves_no_credential_does_not_ask(sg):
    seed_registry(sg)
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY)
    current = client.get("/admin/api/operator/service-registry")
    body = current.json()["service_registry"]
    body["services"]["eng"]["name"] = "renamed"

    response = client.put("/admin/api/operator/service-registry", json=body,
                          headers={**csrf(client), "If-Match": current.headers["etag"]})

    assert not asks_again(response), response.text[:200]


def test_the_granular_service_edit_asks_too(sg):
    seed_registry(sg)
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY)
    etag = client.get("/admin/api/operator/service-registry").headers["etag"]

    response = client.patch("/admin/api/operator/services/eng", json={"base_url": f"{NEW_HOST}/v1"},
                            headers={**csrf(client), "If-Match": etag})

    assert asks_again(response), (response.status_code, response.text[:200])


@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_every_write_path_of_the_registry_refuses_a_stale_session_that_moves_a_key(sg, name):
    """Every route funnels through the one settle step; this is that step with every address the lane dials."""
    seed_registry(sg)
    store = AdminSQLiteStore(sg.path)
    stale = AdminContext(subject="owner", roles=frozenset({"owner"}), actor_kind="user",
                         authenticated_at=int(time.time()) - DAY)
    fresh = replace(stale, authenticated_at=int(time.time()))
    key = AdminContext(subject="owner", roles=frozenset({"owner"}), actor_kind="user", authenticated_at=None)

    with pytest.raises(step_up.StepUpRequired):
        store._settle_credential_bindings(_attacked(name), stale)

    assert store._settle_credential_bindings(_attacked(name), fresh)
    assert store._settle_credential_bindings(_attacked(name), key)           # a key is its own proof


def test_replacing_a_stored_secret_asks_a_stale_session(sg):
    seed_registry(sg)
    store = AdminSQLiteStore(sg.path)
    stale = AdminContext(subject="owner", roles=frozenset({"owner"}), actor_kind="user",
                         authenticated_at=int(time.time()) - DAY)
    command = {"carrier": "config.headers", "operation": "add", "name": "X-Vendor", "value": "a-header-value-1"}

    with pytest.raises(step_up.StepUpRequired):
        store.apply_secret_commands(service_id="eng", commands=[command], actor=stale,
                                    expected_generation=admin_store.get_service_registry_with_generation(path=sg.path)["operator_settings_generation"])

    tables = {row["name"] for row in sg.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "provider_secrets" not in tables or sg.rows("SELECT * FROM provider_secrets WHERE id LIKE 'sref_%'") == []


# -- MED 1: Try keys and sign-out everywhere -------------------------------------------------------

def mint_try_key(client):
    response = client.post("/me/api/try-key", json={}, headers={**csrf(client), **ACT})
    assert response.status_code == 201, response.text
    return response.json()["credential"]["api_key"]


def test_a_try_key_from_an_old_eight_hour_cookie_is_revoked_by_signing_out_everywhere(sg):
    """The review's input: a valid old cookie with no server record, upgraded, then a Try key, then everywhere."""
    victim = owner_client(sg)
    attacker = sg.client()
    forge(sg, attacker, OWNER, issued_ago=600, expires_in=7 * 3600, legacy=True, sid="legacy-no-row", register=False)
    assert registry(sg, "legacy-no-row") == []
    key = mint_try_key(attacker)                                          # the first request also upgrades the cookie
    row = credential(sg, key)
    assert row["session_id"] == "legacy-no-row"
    assert len(registry(sg, cookie_session(attacker).session_id)) == 1    # the upgrade registered the session

    assert victim.post("/auth/api/sign-out-everywhere", headers=csrf(victim)).status_code == 200

    assert not signed_in_state(attacker)
    assert credential(sg, key)["revoked_at"] is not None
    assert runtime_status(attacker, key) == 401


def test_signing_out_everywhere_revokes_a_try_key_even_when_its_session_was_never_registered(sg):
    client = owner_client(sg)
    session = forge(sg, client, OWNER, issued_ago=3600, expires_in=7 * 3600, legacy=True, sid="never-registered", register=False)
    app = admin_store.ensure_personal_app_for_user(session.subject, name="Try", path=sg.path)
    from gateway.runtime_auth import hash_api_key as hashed
    record = admin_store.create_credential_hash(
        subject_kind="app", subject_id=app["id"], hash=hashed("opk_probe"), prefix="opk_pr", session_id="never-registered",
        expires_at=int(time.time()) + DAY, path=sg.path)

    accounts.revoke_person_sessions(session.subject, path=sg.path)

    assert credential(sg, "opk_probe")["revoked_at"] is not None and record["id"]


def test_a_sign_out_between_the_cookie_check_and_the_key_insert_leaves_no_live_key(sg, monkeypatch):
    """The review's input: everywhere lands after session validation and before the credential insert."""
    client = owner_client(sg)
    session = cookie_session(client)
    original = user_runtime.mint_user_app_key

    def revoke_then_mint(*args, **kwargs):
        accounts.revoke_person_sessions(session.subject, path=sg.path)
        return original(*args, **kwargs)

    monkeypatch.setattr(user_runtime, "mint_user_app_key", revoke_then_mint)

    response = client.post("/me/api/try-key", json={}, headers={**csrf(client), **ACT})

    assert response.status_code == 401, response.text
    assert sg.rows("SELECT * FROM credentials WHERE session_id IS NOT NULL AND revoked_at IS NULL") == []


def test_a_sign_out_right_after_the_insert_still_revokes_the_key(sg, monkeypatch):
    client = owner_client(sg)
    session = cookie_session(client)
    key = mint_try_key(client)

    accounts.revoke_person_sessions(session.subject, path=sg.path)

    assert runtime_status(client, key) == 401


# -- MED 2: eight-hour cookies from key claims -----------------------------------------------------

def test_a_key_claimed_cookie_from_before_the_change_is_not_upgraded_to_a_long_session(sg):
    """The review's input: the exact pre-change payload (sub, email, role, iat, exp, sid), seven hours old."""
    old = session_from_claims({"sub": "service_account:legacy-key-owner", "email": "admin key"},
                              role="owner", cookie=sg.config.cookie, now=time.time() - 7 * 3600)
    assert set(old.to_dict()) == {"sub", "email", "role", "iat", "exp", "sid"}
    client = sg.client()
    put_cookie(client, encode_session_cookie(old, signing_key=SIGNING_KEY))

    response = client.get("/health")

    assert [h for h in response.headers.get_list("set-cookie") if h.startswith(f"{COOKIE}=")] == []
    assert cookie_session(client).expires_at == pytest.approx(old.expires_at, abs=1)
    assert AdminSession.from_dict(old.to_dict()).renewable is False


def test_a_user_key_claim_from_before_the_change_is_recognised_by_its_label(sg):
    claim = {"sub": "usr_x", "email": "local-admin", "role": "owner", "iat": 1, "exp": 2, "sid": "s"}

    assert AdminSession.from_dict(claim).renewable is False
    assert AdminSession.from_dict({**claim, "email": "someone@example.test"}).renewable is True   # a person's cookie is upgraded
    assert AdminSession.from_dict({**claim, "rn": 1}).renewable is True                           # one that says so is believed


# -- MED 3: the provider must say the person just signed in ----------------------------------------

def test_a_provider_that_omits_auth_time_does_not_confirm_a_step_up(sg):
    """The review's input: /login?reauth=1, then a valid provider response with no auth_time."""
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY)
    assert asks_again(client.get("/admin/api/settings/effective/export"))

    _, callback = through_provider(sg, client, reauth=True, auth_time=None)

    assert callback.status_code == 303 and callback.headers["location"].startswith("/sign-in/?error=provider")
    assert asks_again(client.get("/admin/api/settings/effective/export"))


def test_a_provider_that_reports_a_fresh_auth_time_still_confirms(sg):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY)

    _, callback = through_provider(sg, client, reauth=True, auth_time=int(time.time()))

    assert callback.status_code == 302
    assert not asks_again(client.get("/admin/api/settings/effective/export"))


# -- MED 4: confirming without a password ----------------------------------------------------------

def test_confirming_a_password_for_a_person_who_has_none_is_throttled_and_never_hashes(sg, monkeypatch):
    """The review's input: 15 POSTs to /auth/api/reauth by an OIDC-only person."""
    client = owner_client(sg)
    hashed = []
    original = accounts.verify_password
    monkeypatch.setattr(accounts, "verify_password", lambda *a, **k: hashed.append(a) or original(*a, **k))

    codes = [client.post("/auth/api/reauth", headers=csrf(client), json={"password": "wrong"}).status_code for _ in range(15)]

    assert hashed == []
    assert codes[:10] == [409] * 10 and set(codes[10:]) == {429}
    assert sg.rows("SELECT * FROM account_login_limits") != []


# -- LOW: the inline view is the export ------------------------------------------------------------

def test_the_inline_effective_configuration_is_gated_like_its_export(sg):
    """The review's input: GET /admin/api/settings/effective returned what the gated export returns."""
    stale, fresh = stale_owner(sg), owner_client(sg)

    denied = stale.get("/admin/api/settings/effective")

    assert asks_again(denied)
    exported = fresh.get("/admin/api/settings/effective/export")
    assert exported.status_code == 200 and fresh.get("/admin/api/settings/effective").json() == exported.json()


# -- the spellings the review tried and found sound ---------------------------------------------------

@pytest.mark.parametrize("method, path", [
    ("GET", "/admin/api/config/export/"),
    ("GET", "/admin/api/config/%65xport"),
    ("HEAD", "/admin/api/config/export"),
    ("OPTIONS", "/admin/api/config/export"),
    ("GET", "/ADMIN/api/config/export"),
    ("GET", "/native/admin/api/config/export"),
    ("GET", "/gateway/v1/admin/api/config/export"),
    ("GET", "/admin/api/settings/effective/"),
])
def test_other_spellings_of_a_gated_route_do_not_give_an_old_session_the_export(sg, method, path):
    client = owner_client(sg)
    forge(sg, client, OWNER, issued_ago=DAY)

    response = client.request(method, path, headers={**csrf(client), "x-http-method-override": "GET"}, follow_redirects=False)

    assert response.status_code not in (200, 201), (method, path, response.status_code)
