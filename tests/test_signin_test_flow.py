"""Test sign-in from Settings: it tries a connection, names what went wrong, signs no one in.

The identity provider is a fake: only token exchange and ID-token verification
are replaced. The start page, the callback, the test registry, the verdict and
the owner link are production code.
"""
from dataclasses import replace

import httpx
import pytest

from gateway import accounts, admin_store, signin_api, signin_test
from tests.support.access import store_plan
from tests.test_signin_settings_api import (  # noqa: F401  (sg is a fixture)
    HELPER, ISSUER, get_snapshot, helper_client, owner_client, preview, proposal, provider_off, rule, save, sg,
    user_id,
)
from tests.test_signin_who_can_enter import OWNER, TRUSTED, csrf_for, domain_rule, signed_in

OTHER_ISSUER = "https://id2.example/realms/home"


@pytest.fixture(autouse=True)
def discovery(monkeypatch):
    """A provider whose discovery document exists only for the addresses below."""
    reachable = {ISSUER, OTHER_ISSUER}

    def fetch(url):
        base = url.removesuffix("/.well-known/openid-configuration")
        if base not in reachable:
            raise httpx.ConnectError("no route to host")
        return {"issuer": base, "authorization_endpoint": base + "/auth", "token_endpoint": base + "/token",
                "jwks_uri": base + "/certs"}

    monkeypatch.setattr(signin_api, "default_fetch_json", fetch)
    return reachable


def begin(client, snapshot, **provider):
    body = proposal(snapshot, **provider)["provider"]
    response = client.post("/admin/api/signin/test", json={"provider": body}, headers=csrf_for(client))
    assert response.status_code == 200, response.text
    return response.json()


def finish(g, client, test, *, sub="owner", email=OWNER, groups=None, name=None, email_verified=True,
           error=None, description=None, exchange_error=None, claims=None):
    """The popup: open the start page, then have the provider redirect back to the callback."""
    started = client.get(test["start_url"], follow_redirects=False)
    if started.status_code != 302:
        return started
    state, record = list(g.proxy.ADMIN_OIDC_STATES.items())[-1]
    assert record.test_id == test["test_id"]

    async def exchange(code, *, config, pkce):
        if exchange_error is not None:
            raise exchange_error
        return {"id_token": "fake.id.token"}

    async def verify(token, *, config):
        token_claims = {"iss": config.issuer, "aud": config.client_id, "sub": sub, "email": email,
                        "email_verified": email_verified, "nonce": record.nonce, "exp": 4_000_000_000,
                        "iat": 1_700_000_000}
        if name:
            token_claims["name"] = name
        if groups is not None:
            token_claims["groups"] = list(groups)
        token_claims.update(claims or {})
        return token_claims

    g.monkeypatch.setattr(g.proxy, "_oidc_exchange_code", exchange)
    g.monkeypatch.setattr(g.proxy, "_oidc_verify_id_token", verify)
    params = {"state": state, "code": "c"} if error is None else {"state": state, "error": error}
    if description:
        params["error_description"] = description
    return client.get("/admin/oidc/callback", params=params, follow_redirects=False)


def status(client, test):
    response = client.get(f"/admin/api/signin/test/{test['test_id']}")
    assert response.status_code == 200, response.text
    return response.json()


def http_error(code, body):
    request = httpx.Request("POST", "https://id.example/token")
    return httpx.HTTPStatusError("failed", request=request, response=httpx.Response(code, json=body, request=request))


def counts(g):
    return {
        "users": len(g.rows("SELECT id FROM users")),
        "apps": len(g.rows("SELECT id FROM apps")),
        "sessions": len(g.rows("SELECT session_id FROM account_sessions")),
    }


# ---------------------------------------------------------------------------
# A passing test
# ---------------------------------------------------------------------------

def test_a_passing_test_shows_what_the_provider_sent_and_creates_no_person_app_or_session(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    before = counts(sg)
    test = begin(owner, snapshot)
    assert status(owner, test) == {"status": "waiting"}

    popup = finish(sg, owner, test, sub="newcomer", email="new@example.test", name="New Person",
                   groups=["/Family"])

    assert popup.status_code == 200 and "close this window" in popup.text.lower()
    assert "opx_admin_session" not in popup.headers.get("set-cookie", "")
    result = status(owner, test)
    assert result["status"] == "passed"
    assert result["identity"] == {
        "name": "New Person", "email": "new@example.test", "email_verified": True, "groups": ["/Family"],
        "roles": [], "claims": ["aud", "email", "email_verified", "exp", "groups", "iat", "iss", "name", "nonce", "sub"],
        "groups_claim": "groups"}
    assert result["warnings"] == [] and result["can_link"] is False
    assert counts(sg) == before                      # nobody was signed in, made or given an app


def test_a_test_remembers_the_groups_and_email_it_saw_for_the_pickers(sg):
    owner = owner_client(sg)
    test = begin(owner, get_snapshot(owner))
    finish(sg, owner, test, sub="newcomer", email="new@elsewhere.test", groups=["/Brand-new"])

    seen = get_snapshot(owner)["seen"]

    assert {"value": "/Brand-new", "count": 0, "note": "seen in a test"} in seen["group"]
    assert {"value": "new@elsewhere.test", "count": 0, "note": "seen in a test"} in seen["email"]
    assert {"value": "elsewhere.test", "count": 0, "note": "seen in a test"} in seen["domain"]


def test_a_provider_that_sends_no_groups_passes_with_a_warning(sg):
    owner = owner_client(sg)
    test = begin(owner, get_snapshot(owner))
    finish(sg, owner, test, groups=None)

    result = status(owner, test)

    assert result["status"] == "passed" and result["warnings"] == ["no_groups"]
    assert "No claim named \"groups\"" in result["details"]
    # Testing the saved connection as saved records the pass and the missing groups.
    provider = get_snapshot(owner)["provider"]
    assert provider["tested_at"] == result["tested_at"] and provider["groups_missing"] is True


def test_a_test_of_a_changed_connection_does_not_count_as_a_test_of_the_saved_one(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    test = begin(owner, snapshot, client_id="another-client")
    finish(sg, owner, test)

    assert status(owner, test)["status"] == "passed"
    assert get_snapshot(owner)["provider"]["tested_at"] is None


def test_a_test_works_while_the_provider_is_off(sg):
    owner = owner_client(sg)
    provider_off(sg)
    test = begin(owner, get_snapshot(owner))

    finish(sg, owner, test)

    assert status(owner, test)["status"] == "passed"


def test_cancelling_a_test_ends_it(sg):
    owner = owner_client(sg)
    test = begin(owner, get_snapshot(owner))

    cancelled = owner.delete(f"/admin/api/signin/test/{test['test_id']}", headers=csrf_for(owner))

    assert cancelled.status_code == 204
    gone = owner.get(f"/admin/api/signin/test/{test['test_id']}")
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "test_not_found"
    assert finish(sg, owner, test).status_code == 404            # the popup has nothing to finish


def test_a_second_test_replaces_the_first(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    first = begin(owner, snapshot)
    second = begin(owner, snapshot)
    assert owner.get(f"/admin/api/signin/test/{first['test_id']}").status_code == 404
    assert status(owner, second) == {"status": "waiting"}


def test_the_callback_of_a_test_needs_the_session_of_the_owner_who_started_it(sg):
    owner = owner_client(sg)
    test = begin(owner, get_snapshot(owner))
    owner.get(test["start_url"], follow_redirects=False)
    state, record = list(sg.proxy.ADMIN_OIDC_STATES.items())[-1]
    owner.cookies.delete("opx_admin_session")

    async def exchange(code, *, config, pkce):
        return {"id_token": "x"}

    sg.monkeypatch.setattr(sg.proxy, "_oidc_exchange_code", exchange)
    owner.get("/admin/oidc/callback", params={"state": state, "code": "c"}, follow_redirects=False)

    run = signin_test.peek(test["test_id"])
    assert run.status == "failed" and run.cause == "refused"


# ---------------------------------------------------------------------------
# Failures name one cause in plain words
# ---------------------------------------------------------------------------

def test_a_wrong_client_secret_is_named_with_the_providers_name(sg):
    owner = owner_client(sg)
    test = begin(owner, get_snapshot(owner), name="Family account")

    finish(sg, owner, test, exchange_error=http_error(401, {"error": "invalid_client",
                                                            "error_description": "Invalid client credentials"}))

    result = status(owner, test)
    assert result["status"] == "failed" and result["cause"] == "wrong_secret"
    assert result["message"].startswith("Family account did not accept the client secret")
    assert "invalid_client" in result["details"]


def test_the_provider_is_named_by_its_address_when_the_owner_gave_no_name(sg):
    owner = owner_client(sg)
    test = begin(owner, get_snapshot(owner), name="")

    finish(sg, owner, test, exchange_error=http_error(401, {"error": "invalid_client"}))

    assert status(owner, test)["message"].startswith("id.example did not accept the client secret")


@pytest.mark.parametrize("error,description", [
    ("invalid_request", "Invalid parameter: redirect_uri"),
    ("invalid_redirect_uri", None),
])
def test_a_refused_redirect_address_is_named(sg, error, description):
    owner = owner_client(sg)
    test = begin(owner, get_snapshot(owner), name="Keycloak")

    finish(sg, owner, test, error=error, description=description)

    result = status(owner, test)
    assert result["cause"] == "redirect_not_allowed"
    assert result["message"].startswith("Keycloak refused the redirect address")


def test_a_token_request_that_names_the_redirect_is_the_same_cause(sg):
    owner = owner_client(sg)
    test = begin(owner, get_snapshot(owner))

    finish(sg, owner, test, exchange_error=http_error(400, {"error": "invalid_grant",
                                                            "error_description": "Incorrect redirect_uri"}))

    assert status(owner, test)["cause"] == "redirect_not_allowed"


def test_an_unverified_email_is_named(sg):
    owner = owner_client(sg)
    test = begin(owner, get_snapshot(owner), name="Keycloak")

    finish(sg, owner, test, email_verified=False)

    result = status(owner, test)
    assert result["cause"] == "unverified_email"
    assert result["message"] == ("Keycloak signed you in, but says your email is not verified. "
                                 "Azizam turns away unverified emails.")


def test_an_address_that_cannot_be_reached_fails_at_once_and_the_popup_just_closes(sg):
    owner = owner_client(sg)
    test = begin(owner, get_snapshot(owner), address="https://down.example/realms/home")

    result = status(owner, test)
    page = owner.get(test["start_url"], follow_redirects=False)

    assert result["status"] == "failed" and result["cause"] == "unreachable"
    assert result["message"] == ("Azizam could not reach down.example. Check the address and that "
                                 "this host can reach it.")
    assert page.status_code == 200 and "close this window" in page.text.lower()


def test_a_provider_that_goes_away_during_the_test_is_unreachable(sg):
    owner = owner_client(sg)
    test = begin(owner, get_snapshot(owner))

    finish(sg, owner, test, exchange_error=httpx.ConnectError("gone"))

    assert status(owner, test)["cause"] == "unreachable"


def test_any_other_refusal_says_what_the_provider_said(sg):
    owner = owner_client(sg)
    test = begin(owner, get_snapshot(owner), name="Keycloak")

    finish(sg, owner, test, error="access_denied", description="The user denied the request")

    result = status(owner, test)
    assert result["cause"] == "refused"
    assert result["message"] == "Keycloak refused the sign-in: The user denied the request."
    assert result["details"] == "error=access_denied\nerror_description=The user denied the request"


def test_a_test_needs_an_address_a_client_id_and_a_secret(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    for fields, code in (({"address": "nope"}, "address_invalid"), ({"client_id": ""}, "client_id_required")):
        body = proposal(snapshot, **fields)["provider"]
        refused = owner.post("/admin/api/signin/test", json={"provider": body}, headers=csrf_for(owner))
        assert refused.status_code == 400 and refused.json()["error"]["code"] == code
    sg.set_config(replace(sg.config, enabled=False, client_secret=""))
    body = proposal(snapshot)["provider"]
    refused = owner.post("/admin/api/signin/test", json={"provider": body}, headers=csrf_for(owner))
    assert refused.json()["error"]["code"] == "client_secret_required"


# ---------------------------------------------------------------------------
# The verdict: who would the rules let in
# ---------------------------------------------------------------------------

def verdict(client, snapshot, test, **changes):
    return preview(client, proposal(snapshot, **changes), test["test_id"])["test_verdict"]


def test_the_owner_testing_with_their_own_provider_account_is_told_they_can_always_sign_in(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    test = begin(owner, snapshot)
    finish(sg, owner, test, sub="owner", email=OWNER)

    assert verdict(owner, snapshot, test) == {"kind": "owner_linked"}


def test_another_persons_account_is_scored_as_that_person_with_the_rules_that_matched(sg):
    owner = owner_client(sg)
    helper_client(sg)
    snapshot = get_snapshot(owner)
    test = begin(owner, snapshot)
    finish(sg, owner, test, sub="helper", email=HELPER, groups=["/Trusted"])

    result = verdict(owner, snapshot, test)

    assert result["kind"] == "person" and result["person"] == {"id": user_id(sg, HELPER), "name": HELPER}
    assert result["outcome"] == {"admitted": True, "admin": True, "plan": None, "rule_ids": ["trusted"],
                                 "invited": False}


def test_someone_new_is_scored_against_the_rules_in_the_form_not_the_saved_ones(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    test = begin(owner, snapshot)
    finish(sg, owner, test, sub="mum", email="mum@example.test", groups=["/Family"])

    kept_out = verdict(owner, snapshot, test)
    wider = {"fallback": "closed", "rules": [*snapshot["rules"]["rules"],
                                             rule("group", ["/Family"], admit=True, id="family")]}
    let_in = verdict(owner, snapshot, test, rules=wider)

    assert kept_out["kind"] == "new" and kept_out["outcome"]["admitted"] is False
    assert kept_out["host"] == "id.example" and kept_out["issuer_changed"] is False
    assert let_in["outcome"] == {"admitted": True, "admin": False, "plan": None, "rule_ids": ["family"],
                                 "invited": False}


def test_someone_with_an_open_invitation_is_told_they_would_be_let_in_because_they_were_invited(sg):
    plan = store_plan("Family", path=sg.path, caps=("chat",))
    accounts.create_link("invite", "owner", plan_id=plan["id"], email="mum@example.test", path=sg.path)
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    test = begin(owner, snapshot)
    finish(sg, owner, test, sub="mum", email="mum@example.test")

    result = verdict(owner, snapshot, test)

    assert result["kind"] == "new"
    assert result["outcome"] == {"admitted": True, "admin": False, "plan": None, "rule_ids": [], "invited": True}


def test_an_unverified_invitation_email_does_not_count(sg):
    plan = store_plan("Family", path=sg.path, caps=("chat",))
    accounts.create_link("invite", "owner", plan_id=plan["id"], email="mum@example.test", path=sg.path)
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    test = begin(owner, snapshot)
    finish(sg, owner, test, sub="mum", email="mum@example.test", claims={"email_verified": None})

    assert verdict(owner, snapshot, test)["outcome"]["admitted"] is False


def test_a_new_address_makes_everyone_a_new_person_and_the_owners_old_link_stale(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    test = begin(owner, snapshot, address=OTHER_ISSUER)
    finish(sg, owner, test, sub="owner-at-id2", email=OWNER)
    as_owner = verdict(owner, snapshot, test, address=OTHER_ISSUER)
    mum = begin(owner, snapshot, address=OTHER_ISSUER)
    finish(sg, owner, mum, sub="mum", email="mum@example.test")
    as_mum = verdict(owner, snapshot, mum, address=OTHER_ISSUER)

    assert as_owner == {"kind": "owner_unlinked", "email": OWNER, "old_link_stale": True}
    assert as_mum["kind"] == "new" and as_mum["issuer_changed"] is True and as_mum["host"] == "id2.example"


# ---------------------------------------------------------------------------
# Linking the owner's provider identity to the owner
# ---------------------------------------------------------------------------

LOCAL_OWNER = "tim@example.test"
LOCAL_PASSWORD = "a long enough password"


def local_owner_client(g):
    """An owner who made their account with a password, before any provider existed."""
    verifier = accounts.hash_password(LOCAL_PASSWORD)
    owner = admin_store.claim_first_local_owner(issuer="local", subject=LOCAL_OWNER, email=LOCAL_OWNER,
                                                display_name="Tim", verifier=verifier, granted_by="test",
                                                path=g.path)
    admin_store.seed_first_owner_plan(owner["id"], path=g.path)
    client = g.client()
    login = client.post("/auth/api/login", json={"email": LOCAL_OWNER, "password": LOCAL_PASSWORD})
    assert login.status_code == 200, login.text
    return client, owner


def test_a_password_owner_who_tests_with_their_own_provider_account_is_offered_the_link(sg):
    owner, record = local_owner_client(sg)
    snapshot = get_snapshot(owner)
    test = begin(owner, snapshot)
    finish(sg, owner, test, sub="tims-provider-id", email=LOCAL_OWNER)

    assert status(owner, test)["can_link"] is True
    assert verdict(owner, snapshot, test) == {"kind": "owner_unlinked", "email": LOCAL_OWNER, "old_link_stale": False}


def test_linking_makes_that_provider_account_sign_in_as_the_owner_without_renaming_them(sg):
    owner, record = local_owner_client(sg)
    snapshot = get_snapshot(owner)
    test = begin(owner, snapshot)
    finish(sg, owner, test, sub="tims-provider-id", email=LOCAL_OWNER)

    linked = owner.post("/admin/api/signin/link-owner", json={"test_id": test["test_id"]}, headers=csrf_for(owner))

    assert linked.status_code == 200, linked.text
    assert verdict(owner, snapshot, test) == {"kind": "owner_linked"}
    assert status(owner, test)["can_link"] is False
    before = counts(sg)
    # A real sign-in with that provider account is the owner, not a second roleless account.
    client = sg.client()
    response = sg.sign_in(client, sub="tims-provider-id", email="tim@provider.example")
    assert response.status_code == 302
    assert counts(sg)["users"] == before["users"]
    assert admin_store.get_user(record["id"], path=sg.path)["email"] == LOCAL_OWNER
    assert client.get("/admin/api/session").json()["actor"]["subject"] == record["id"]
    assert "owner" in client.get("/admin/api/session").json()["actor"]["roles"]


def test_without_the_link_the_same_provider_account_is_kept_out_as_a_stranger(sg):
    local_owner_client(sg)
    response = sg.sign_in(sg.client(), sub="tims-provider-id", email=LOCAL_OWNER)
    assert response.status_code == 303                       # not linked, no rule, not invited


def test_only_the_owners_own_unlinked_account_can_be_linked(sg):
    owner, _ = local_owner_client(sg)
    helper_client(sg)
    snapshot = get_snapshot(owner)
    cases = [
        ("someone else's existing account", {"sub": "helper", "email": HELPER, "groups": ["/Trusted"]}),
        ("a stranger", {"sub": "mum", "email": "mum@example.test"}),
        ("the owner's email, unverified", {"sub": "tims-provider-id", "email": LOCAL_OWNER, "claims": {"email_verified": None}}),
    ]
    for label, identity in cases:
        test = begin(owner, snapshot)
        finish(sg, owner, test, **identity)
        refused = owner.post("/admin/api/signin/link-owner", json={"test_id": test["test_id"]}, headers=csrf_for(owner))
        assert refused.status_code == 409, label
        assert refused.json()["error"]["code"] == "not_linkable", label
    assert sg.rows("SELECT * FROM oidc_identity_links") == []


def test_linking_needs_a_passed_test_of_your_own(sg):
    owner, _ = local_owner_client(sg)
    snapshot = get_snapshot(owner)
    waiting = begin(owner, snapshot)
    for test_id in (waiting["test_id"], "test_unknown"):
        refused = owner.post("/admin/api/signin/link-owner", json={"test_id": test_id}, headers=csrf_for(owner))
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "test_not_passed"


def test_one_provider_account_cannot_be_linked_to_two_people(sg):
    owner, record = local_owner_client(sg)
    other = admin_store.upsert_oidc_user("local", "someone", email="someone@example.test", path=sg.path)
    from gateway import signin_admin
    signin_admin.link_identity(sg.path, ISSUER, "taken", other["id"], linked_by=None)

    with pytest.raises(signin_admin.SignInError) as caught:
        signin_admin.link_identity(sg.path, ISSUER, "taken", record["id"], linked_by=None)
    assert caught.value.code == "identity_taken"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover(client, address):
    response = client.post("/admin/api/signin/discover", json={"address": address}, headers=csrf_for(client))
    assert response.status_code == 200, response.text
    return response.json()


def test_discovery_finds_the_endpoints_from_the_address_alone(sg):
    owner = owner_client(sg)

    assert discover(owner, ISSUER + "/") == {
        "ok": True, "host": "id.example", "issuer": ISSUER, "authorization_endpoint": ISSUER + "/auth",
        "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/certs"}


def test_discovery_fails_quietly_for_an_unreachable_or_unsafe_address(sg, monkeypatch):
    owner = owner_client(sg)
    calls = []
    monkeypatch.setattr(signin_api, "default_fetch_json", lambda url: calls.append(url) or {})

    assert discover(owner, "https://nothing.example")["ok"] is False          # a document that does not describe it
    assert discover(owner, "http://evil.example")["ok"] is False             # plain http away from this machine
    assert discover(owner, "https://user:pw@evil.example")["ok"] is False
    assert discover(owner, "")["ok"] is False
    assert calls == ["https://nothing.example/.well-known/openid-configuration"]


# ---------------------------------------------------------------------------
# Save needs a passed test of exactly this connection
# ---------------------------------------------------------------------------

def test_a_changed_connection_saves_after_a_passed_test_of_that_connection(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    test = begin(owner, snapshot, client_id="new-client", client_secret="new-secret")
    finish(sg, owner, test)
    body = proposal(snapshot, client_id="new-client", client_secret="new-secret")

    saved = save(owner, snapshot, body, test_id=test["test_id"])

    assert saved.status_code == 200, saved.text
    provider = saved.json()["snapshot"]["provider"]
    assert provider["client_id"] == "new-client" and provider["tested_at"] is not None
    assert preview(owner, proposal(get_snapshot(owner)))["blockers"] == []


def test_a_test_of_one_connection_does_not_allow_saving_another(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    test = begin(owner, snapshot, client_id="new-client")
    finish(sg, owner, test)

    for changed in ({"client_id": "different-client"}, {"client_id": "new-client", "client_secret": "other-secret"},
                    {"client_id": "new-client", "groups_claim": "roles"}):
        refused = save(owner, snapshot, proposal(snapshot, **changed), test_id=test["test_id"])
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "test_required", changed


def test_a_failed_or_unfinished_test_does_not_allow_saving(sg):
    owner = owner_client(sg)
    snapshot = get_snapshot(owner)
    waiting = begin(owner, snapshot, client_id="new-client")

    refused = save(owner, snapshot, proposal(snapshot, client_id="new-client"), test_id=waiting["test_id"])
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "test_required"


def test_turning_the_provider_on_for_the_first_time_needs_a_pass_and_then_adds_the_button(sg):
    owner = owner_client(sg)
    provider_off(sg)
    snapshot = get_snapshot(owner)
    assert snapshot["methods"]["provider"] is False
    on = proposal(snapshot, methods={"password": True, "provider": True})
    assert save(owner, snapshot, on).json()["error"]["code"] == "test_required"
    test = begin(owner, snapshot)
    finish(sg, owner, test)

    saved = save(owner, snapshot, on, test_id=test["test_id"])

    assert saved.status_code == 200, saved.text
    assert saved.json()["applied"]["provider_turned_on"] is True
    assert saved.json()["snapshot"]["methods"] == {"password": True, "provider": True}
    assert sg.client().get("/auth/api/options").json()["oidc"] is True


def test_an_address_change_is_saved_with_a_passed_test_and_the_old_accounts_are_not_signed_out_as_members(sg):
    sg.set_rules([TRUSTED, domain_rule("example.test")])
    owner = owner_client(sg)
    member, _ = signed_in(sg, sub="member", email="member@example.test")
    snapshot = get_snapshot(owner)
    test = begin(owner, snapshot, address=OTHER_ISSUER)
    finish(sg, owner, test, sub="owner-at-id2", email=OWNER)

    saved = save(owner, snapshot, proposal(snapshot, address=OTHER_ISSUER), test_id=test["test_id"])

    assert saved.status_code == 200, saved.text
    assert saved.json()["snapshot"]["provider"]["address"] == OTHER_ISSUER
    assert member.get("/me/api/session").json()["authenticated"] is True
    # The old accounts stay as people, but nobody at the new address matches them.
    assert admin_store.find_oidc_user(ISSUER, "member", path=sg.path) is not None
    assert admin_store.find_oidc_user(OTHER_ISSUER, "member", path=sg.path) is None


def test_the_one_time_note_from_the_upgrade_can_be_dismissed_and_stays_dismissed(sg):
    from gateway import signin_admin
    signin_admin.write_status(sg.path, {"notice": "removed_owner_rule"})
    owner = owner_client(sg)
    assert get_snapshot(owner)["notice"] == "removed_owner_rule"

    dismissed = owner.post("/admin/api/signin/notice/dismiss", headers=csrf_for(owner))

    assert dismissed.status_code == 200 and dismissed.json()["notice"] is None
    assert get_snapshot(owner)["notice"] is None
