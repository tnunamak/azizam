"""Who can sign in through OIDC, and what each kind of person gets.

These tests run the real callback, invite and admin routes against a fake
identity provider (the token exchange and ID-token verification are replaced;
everything after them is the production code). The configuration has the same
shape as the live instance: one listed owner email, the group ``/Trusted`` as an
admin group, no allowed domains, no admin roles.

Each answer in the owner's question "Can any of my Keycloak users log in, and
what access do they get?" is one test below.
"""
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from gateway import accounts, admin_runtime, admin_store
from gateway.admin_oidc import CookieConfig, OIDCConfig
from tests.support.access import store_plan

OWNER = "owner@example.test"
SIGNING_KEY = b"s" * 32


def live_shaped_config(**overrides) -> OIDCConfig:
    base = dict(
        enabled=True, issuer="https://id.example/realms/home", client_id="gateway", client_secret="test-secret",
        redirect_uri="https://ai.example/admin/oidc/callback", scopes=("openid", "email"),
        allowed_email_domains=frozenset(), admin_emails=frozenset({OWNER}),
        admin_groups=frozenset({"/Trusted"}), admin_roles=frozenset(),
        signing_key=SIGNING_KEY, cookie=CookieConfig(secure=False),
        authorization_endpoint="https://id.example/authorize", token_endpoint="https://id.example/token",
        jwks_uri="https://id.example/jwks",
    )
    base.update(overrides)
    return OIDCConfig(**base)


class Gateway:
    """A gateway on a temporary database, with a scriptable identity provider."""

    def __init__(self, proxy, path, monkeypatch, config):
        self.proxy, self.path, self.monkeypatch = proxy, path, monkeypatch
        self.set_config(config)

    def set_config(self, config):
        self.config = config
        self.monkeypatch.setattr(self.proxy, "ADMIN_OIDC_CONFIG", config)
        self.monkeypatch.setattr(self.proxy.ADMIN_AUTH, "signing_key", config.signing_key)
        self.monkeypatch.setattr(self.proxy.ADMIN_AUTH, "cookie", config.cookie)

    def client(self) -> TestClient:
        return TestClient(self.proxy.app, base_url="https://ai.example", client=("127.0.0.1", 50000))

    def sign_in(self, client, *, sub, email, groups=(), email_verified=True):
        """Run the browser flow. Returns the callback response."""
        login = client.get("/login", params={"return_to": "/"}, follow_redirects=False)
        if "redirect_uri=" not in login.headers["location"]:
            login = client.get(login.headers["location"], follow_redirects=False)
        state, record = list(self.proxy.ADMIN_OIDC_STATES.items())[-1]

        async def exchange(code, *, config, pkce):
            return {"id_token": "fake.id.token"}

        async def verify(token, *, config):
            claims = {"iss": config.issuer, "aud": config.client_id, "sub": sub, "email": email,
                      "email_verified": email_verified, "nonce": record.nonce,
                      "exp": 4_000_000_000, "iat": 1_700_000_000}
            if groups:
                claims["groups"] = list(groups)
            return claims

        self.monkeypatch.setattr(self.proxy, "_oidc_exchange_code", exchange)
        self.monkeypatch.setattr(self.proxy, "_oidc_verify_id_token", verify)
        return client.get("/admin/oidc/callback", params={"state": state, "code": "c"},
                          headers={"accept": "text/html"}, follow_redirects=False)

    def user(self, email):
        with accounts.connection(self.path) as conn:
            row = conn.execute("SELECT * FROM users WHERE LOWER(email)=?", (email,)).fetchone()
            return dict(row) if row else None

    def rows(self, sql, values=()):
        with accounts.connection(self.path) as conn:
            return [dict(row) for row in conn.execute(sql, values).fetchall()]


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    import proxy
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", path)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", path)
    monkeypatch.setattr(proxy, "ADMIN_OIDC_STATES", {})
    admin_store.put_operator_setting("instance_presentation", {"base_url": "https://ai.example"}, path=path)
    clients = []
    gw = Gateway(proxy, path, monkeypatch, live_shaped_config())
    original = gw.client
    gw.client = lambda: clients.append(original()) or clients[-1]
    try:
        yield gw
    finally:
        for client in clients:
            client.close()


def signed_in(gateway, *, sub, email, groups=()):
    client = gateway.client()
    response = gateway.sign_in(client, sub=sub, email=email, groups=groups)
    return client, response


def has_working_plan(gateway, email) -> bool:
    """True if the person's browser work is authorized by a complete access plan."""
    user = gateway.user(email)
    app = admin_store.ensure_session_app_for_user(user["id"], name="Browser", path=gateway.path)
    identity = admin_runtime.runtime_identity_for_session_app(app["id"], user["id"], path=gateway.path)
    return identity.access.complete


def roles_of(gateway, email):
    user = gateway.user(email)
    return {(row["role"], row["provenance"]) for row in admin_store.list_admin_roles(
        subject_kind="user", subject_id=user["id"], path=gateway.path)}


# ---------------------------------------------------------------------------
# Q1. A Keycloak user who is not invited, not the listed email, not in /Trusted.
# ---------------------------------------------------------------------------

def test_q1_uninvited_unlisted_keycloak_user_is_turned_away_and_leaves_no_account(gateway):
    client, response = signed_in(gateway, sub="stranger", email="stranger@example.test")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/sign-in/?error=provider")
    assert "opx_admin_session" not in response.headers.get("set-cookie", "")
    assert gateway.user("stranger@example.test") is None
    assert client.get("/me/api/session").json()["authenticated"] is False


def test_q1_even_an_open_invitation_does_not_get_a_keycloak_user_past_the_email_list(gateway):
    plan = store_plan("Family", path=gateway.path, caps=("chat",))
    accounts.create_link("invite", "owner", plan_id=plan["id"], email="friend@example.test", path=gateway.path)

    client, response = signed_in(gateway, sub="friend", email="friend@example.test")

    # The invitation is accepted AFTER sign-in, and sign-in is checked first.
    assert response.headers["location"].startswith("/sign-in/?error=provider")
    assert gateway.user("friend@example.test") is None
    assert gateway.rows("SELECT used_at FROM account_links WHERE kind='invite'")[0]["used_at"] is None


# ---------------------------------------------------------------------------
# Q2. A Keycloak user in /Trusted.
# ---------------------------------------------------------------------------

def test_q2_trusted_group_member_becomes_admin_automatically_with_no_access_plan(gateway):
    client, response = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])

    assert response.status_code == 302
    assert roles_of(gateway, "helper@example.test") == {("admin", "oidc")}
    user = gateway.user("helper@example.test")
    assert admin_store.person_plan_id(user["id"], path=gateway.path) is None
    # Signed in as an admin, but cannot generate: the admin role is not an access plan.
    assert has_working_plan(gateway, "helper@example.test") is False


def test_q2_group_name_must_match_exactly(gateway):
    cases = [("/Trusted", 302), ("Trusted", 303), ("/trusted", 303), ("/Trusted/sub", 303), ("/Untrusted", 303)]
    for index, (name, expected) in enumerate(cases):
        _, response = signed_in(gateway, sub=f"g{index}", email=f"g{index}@example.test", groups=[name])
        # Claims are compared exactly: no case, prefix or sub-group aliases.
        assert response.status_code == expected, name


def test_q2_admin_can_run_the_control_plane_but_not_people_invites_settings_or_sign_in(gateway):
    owner, response = signed_in(gateway, sub="owner", email=OWNER)
    assert response.status_code == 302
    assert any(role == "owner" for role, _ in roles_of(gateway, OWNER))
    admin, response = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    assert response.status_code == 302

    # Admin can manage access plans, as the owner can.
    assert admin.get("/admin/api/access-plans").status_code == 200
    assert owner.get("/admin/api/access-plans").status_code == 200
    # Only the owner can do these.
    for method, path in (("get", "/admin/api/people"), ("get", "/admin/api/invites"),
                         ("get", "/admin/api/settings/instance"), ("put", "/admin/api/oidc-config")):
        def kwargs(client):
            token = client.get("/auth/api/options").json()["signed_in_oidc"]["csrf_token"]
            return {"json": {}, "headers": {"x-azizam-csrf": token}} if method == "put" else {}
        allowed = getattr(owner, method)(path, **kwargs(owner))
        assert allowed.status_code != 403, (path, allowed.text)
        denied = getattr(admin, method)(path, **kwargs(admin))
        assert denied.status_code == 403, (path, denied.text)
        assert "owner" in denied.text.lower()


# ---------------------------------------------------------------------------
# Q3. An invited person signing in with Keycloak.
# ---------------------------------------------------------------------------

def accept(client, token, email):
    options = client.get("/auth/api/options").json()
    csrf = options["signed_in_oidc"]["csrf_token"]
    return client.post("/auth/api/invite/accept", json={"token": token, "email": email, "name": "Friend"},
                       headers={"x-azizam-csrf": csrf})


def test_q3_invited_keycloak_person_gets_the_invitations_plan_when_the_email_list_lets_them_sign_in(gateway):
    # Same instance, but the owner also allows the friend's email domain.
    gateway.set_config(replace(gateway.config, allowed_email_domains=frozenset({"friends.test"})))
    plan = store_plan("Family", path=gateway.path, caps=("chat",))
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email="friend@friends.test",
                                    path=gateway.path)

    client, response = signed_in(gateway, sub="friend", email="friend@friends.test")
    assert response.status_code == 302
    user = gateway.user("friend@friends.test")
    # Signing in alone gives no plan: the invitation is what grants it.
    assert admin_store.person_plan_id(user["id"], path=gateway.path) is None

    accepted = accept(client, token, "friend@friends.test")
    assert accepted.status_code == 200, accepted.text
    assert admin_store.person_plan_id(user["id"], path=gateway.path) == plan["id"]
    assert roles_of(gateway, "friend@friends.test") == set()


def test_q3_invitation_does_not_widen_the_email_list_so_listed_domain_is_what_matters(gateway):
    # Live-shaped config: only OWNER is listed. A friend whose email matches an invite is still refused.
    plan = store_plan("Family", path=gateway.path, caps=("chat",))
    accounts.create_link("invite", "owner", plan_id=plan["id"], email="friend@friends.test", path=gateway.path)
    _, response = signed_in(gateway, sub="friend", email="friend@friends.test")
    assert response.status_code == 303
    assert gateway.user("friend@friends.test") is None


# ---------------------------------------------------------------------------
# Q4. The default plan for any newly admitted person.
# ---------------------------------------------------------------------------

def test_q4_a_newly_admitted_keycloak_person_has_no_plan_when_the_list_is_open(gateway):
    gateway.set_config(replace(gateway.config, admin_emails=frozenset(), admin_groups=frozenset()))
    store_plan("Family", path=gateway.path, caps=("chat",))

    client, response = signed_in(gateway, sub="anyone", email="anyone@example.test")

    assert response.status_code == 302
    user = gateway.user("anyone@example.test")
    assert admin_store.person_plan_id(user["id"], path=gateway.path) is None
    assert roles_of(gateway, "anyone@example.test") == set()
    assert has_working_plan(gateway, "anyone@example.test") is False


def test_q4_the_invite_default_plan_setting_only_preselects_a_plan_for_new_invitations(gateway):
    plan = store_plan("Family", path=gateway.path, caps=("chat",))
    assert admin_store.invite_access_plan(path=gateway.path) is None
    admin_store.put_operator_setting(admin_store.INVITE_ACCESS_PLAN_SETTING_KEY, {"plan_id": plan["id"]},
                                     path=gateway.path)
    assert admin_store.invite_access_plan(path=gateway.path) == plan["id"]
    # It changes no existing person and admits nobody.
    assert gateway.rows("SELECT * FROM account_access") == []


def test_q4_the_first_owner_alone_is_seeded_a_working_plan(gateway):
    _, response = signed_in(gateway, sub="owner", email=OWNER)
    assert response.status_code == 302
    owner = gateway.user(OWNER)
    plan_id = admin_store.person_plan_id(owner["id"], path=gateway.path)
    assert plan_id is not None
    assert admin_store.get_access_plan(plan_id, path=gateway.path)["name"] == admin_store.STARTER_PLAN_NAME
    # The next person does not inherit it.
    _, response = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    assert admin_store.person_plan_id(gateway.user("helper@example.test")["id"], path=gateway.path) is None


# ---------------------------------------------------------------------------
# Q5. Deny by default: anything broader than intended?
# ---------------------------------------------------------------------------

def test_q5_unverified_email_is_refused_even_for_trusted_group_members(gateway):
    client = gateway.client()
    response = gateway.sign_in(client, sub="h", email="h@example.test", groups=["/Trusted"], email_verified=False)
    assert response.status_code == 303
    assert gateway.user("h@example.test") is None


def test_q5_group_claims_alone_never_grant_an_access_plan(gateway):
    client, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted", "/Family"])
    user = gateway.user("helper@example.test")
    assert admin_store.person_plan_id(user["id"], path=gateway.path) is None
    assert has_working_plan(gateway, "helper@example.test") is False


def test_q5_removing_someone_from_trusted_does_not_revoke_their_admin_grant_if_they_are_not_listed(gateway):
    """Finding (low): the revoke happens only after the allow-list check passes.

    A person removed from /Trusted whose email is not otherwise allowed is
    turned away at the callback BEFORE the claim-derived grant is reconciled, so
    the grant row stays active. They cannot start a new session, but a session
    they already hold keeps its admin role until it expires (8 hours).
    """
    client, response = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    assert response.status_code == 302
    assert client.get("/admin/api/access-plans").status_code == 200

    again = gateway.client()
    refused = gateway.sign_in(again, sub="helper", email="helper@example.test", groups=[])
    assert refused.status_code == 303                      # cannot sign in again
    assert roles_of(gateway, "helper@example.test") == {("admin", "oidc")}   # grant not revoked
    assert client.get("/admin/api/access-plans").status_code == 200          # old session still admin


def test_q5_finding_a_trusted_admin_can_mint_a_working_key_under_the_owners_plan(gateway):
    """Finding (design, medium): an admin with no plan can still get a usable key.

    The admin role is not an access plan, and the admin's own browser work is
    denied (see the Q2 test). But POST /admin/api/apps accepts ``owner_user_id``,
    so an admin can create a service App accountable to the OWNER, assign it any
    plan, and mint a key. That key authorizes generation as the owner's person
    plan intersected with the App's plan. Spend and receipts land on the owner.
    Admin is therefore a trusted operator role, not a read-only or plan-less one.
    """
    from gateway.runtime_auth import hash_api_key

    signed_in(gateway, sub="owner", email=OWNER)
    owner_id = gateway.user(OWNER)["id"]
    plan_id = admin_store.person_plan_id(owner_id, path=gateway.path)
    admin, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    assert has_working_plan(gateway, "helper@example.test") is False

    csrf = {"x-azizam-csrf": admin.get("/auth/api/options").json()["signed_in_oidc"]["csrf_token"]}
    created = admin.post("/admin/api/apps", headers=csrf,
                         json={"name": "borrowed", "kind": "service", "access_plan": plan_id, "owner_user_id": owner_id})
    assert created.status_code == 201, created.text
    key = admin.post(f"/admin/api/apps/{created.json()['app']['id']}/keys", headers=csrf, json={"display_name": "k"})
    assert key.status_code == 201, key.text
    identity = admin_runtime.runtime_identity_for_app_credential_hash(
        hash_api_key(key.json()["credential"]["api_key"]), path=gateway.path)
    assert identity is not None and identity.access.complete


def test_q5_finding_an_admin_key_minted_by_a_trusted_admin_outlives_their_group_membership(gateway):
    """Finding (low-medium): removing someone from /Trusted does not revoke keys they minted.

    An admin may mint an admin automation key. It belongs to a service account,
    not to the person, so it keeps working after the person leaves the group.
    """
    gateway.set_config(replace(gateway.config, allowed_email_domains=frozenset({"example.test"})))
    signed_in(gateway, sub="owner", email=OWNER)
    admin, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    csrf = {"x-azizam-csrf": admin.get("/auth/api/options").json()["signed_in_oidc"]["csrf_token"]}
    minted = admin.post("/admin/api/admin-keys", headers=csrf, json={"name": "helper automation", "role": "admin"})
    assert minted.status_code == 201, minted.text
    api_key = minted.json()["credential"]["api_key"]

    # The person is removed from the group and signs in again: the grant is revoked...
    signed_in(gateway, sub="helper", email="helper@example.test", groups=[])
    assert roles_of(gateway, "helper@example.test") == set()
    # ...but the key still works on the admin API.
    bearer = gateway.client().get("/admin/api/access-plans", headers={"authorization": f"Bearer {api_key}"})
    assert bearer.status_code == 200


def test_first_signin_by_a_trusted_member_on_a_fresh_instance_blocks_the_listed_owner_bootstrap(gateway):
    """Finding (low): owner bootstrap only runs while NO role grant exists.

    On a fresh instance, if a /Trusted member signs in before the listed owner
    email, the member becomes admin; the later owner sign-in finds an existing
    grant and is not made owner.
    """
    signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    _, response = signed_in(gateway, sub="owner", email=OWNER)

    assert response.status_code == 302
    assert not any(role == "owner" for role, _ in roles_of(gateway, OWNER))


def test_a_disabled_person_cannot_sign_in_through_keycloak(gateway):
    gateway.set_config(replace(gateway.config, allowed_email_domains=frozenset({"example.test"})))
    signed_in(gateway, sub="owner", email=OWNER)
    signed_in(gateway, sub="friend", email="friend@example.test")
    friend = gateway.user("friend@example.test")
    accounts.change_person(friend["id"], "owner", disabled=True, path=gateway.path)

    client, response = signed_in(gateway, sub="friend", email="friend@example.test")

    assert response.status_code == 403
    assert client.get("/me/api/session").json()["authenticated"] is False


def test_an_invitation_by_password_needs_no_keycloak_account_and_grants_only_its_plan(gateway):
    """Built-in (password) acceptance does not go through the OIDC allow-list."""
    plan = store_plan("Family", path=gateway.path, caps=("chat",))
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email="friend@friends.test",
                                    path=gateway.path)
    client = gateway.client()

    accepted = client.post("/auth/api/invite/accept", json={
        "token": token, "email": "friend@friends.test", "name": "Friend", "password": "a sufficiently long password"})

    assert accepted.status_code == 200, accepted.text
    friend = gateway.user("friend@friends.test")
    assert friend["issuer"] == "local"
    assert admin_store.person_plan_id(friend["id"], path=gateway.path) == plan["id"]
    assert roles_of(gateway, "friend@friends.test") == set()


def test_password_invitations_are_refused_when_the_owner_chose_keycloak_only(gateway):
    plan = store_plan("Family", path=gateway.path, caps=("chat",))
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email="friend@friends.test",
                                    path=gateway.path)
    admin_store.put_operator_setting("sign_in", {"mode": "oidc"}, path=gateway.path)

    refused = gateway.client().post("/auth/api/invite/accept", json={
        "token": token, "email": "friend@friends.test", "name": "Friend", "password": "a sufficiently long password"})

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "builtin_disabled"


def test_group_to_plan_mapping_is_gone_group_claims_are_stored_but_never_grant_a_plan(gateway):
    """The old runtime-profile-mappings (group, role, email, domain -> plan) no longer exist."""
    signed_in(gateway, sub="owner", email=OWNER)

    tables = {row["name"] for row in gateway.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"profiles", "runtime_profile_mappings"}.isdisjoint(tables)
    registered = {getattr(route, "path", "") for route in gateway.proxy.app.routes}
    assert not [path for path in registered if "profile-mapping" in path or "user-access-defaults" in path]

    # Group names are recorded for the person on each sign-in, and nothing reads them back.
    signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted", "/Family"])
    helper = gateway.user("helper@example.test")
    assert {row["claim_value"] for row in gateway.rows(
        "SELECT * FROM user_identity_claims WHERE user_id=?", (helper["id"],))} >= {"/Trusted", "/Family"}
    assert admin_store.person_plan_id(helper["id"], path=gateway.path) is None


def test_self_service_keys_never_exceed_the_persons_own_plan(gateway):
    """A signed-in person can make apps and keys, but each is "Same as my access plan"."""
    from gateway.runtime_auth import hash_api_key

    gateway.set_config(replace(gateway.config, admin_emails=frozenset(), admin_groups=frozenset()))
    client, response = signed_in(gateway, sub="friend", email="friend@example.test")
    assert response.status_code == 302
    friend = gateway.user("friend@example.test")
    csrf = client.get("/auth/api/options").json()["signed_in_oidc"]["csrf_token"]
    headers = {"x-azizam-user-action": "1", "x-azizam-csrf": csrf}

    def mint(name):
        app = client.post("/me/api/apps", headers=headers, json={"name": name})
        assert app.status_code == 201, app.text
        key = client.post(f"/me/api/apps/{app.json()['app']['id']}/keys", headers=headers, json={})
        assert key.status_code == 201, key.text
        return admin_runtime.runtime_identity_for_app_credential_hash(
            hash_api_key(key.json()["credential"]["api_key"]), path=gateway.path)

    # No plan: the key exists but authorizes nothing.
    assert mint("first").access.complete is False
    # With a plan: the key can do what the person's plan allows, and no more.
    plan = store_plan("Family", path=gateway.path, caps=("chat",))
    accounts.change_person(friend["id"], "owner", path=gateway.path, plan_id=plan["id"])
    identity = mint("second")
    assert identity.access.complete and identity.access.plan_ids == (plan["id"],)


def test_a_missing_email_verified_claim_is_not_treated_as_unverified(gateway):
    """Info: only an explicit ``email_verified: false`` is refused; an absent claim passes.

    The invitation path stays safe because it needs a stored verified=True fact,
    which only an explicit true claim records.
    """
    gateway.set_config(replace(gateway.config, allowed_email_domains=frozenset({"example.test"})))
    plan = store_plan("Family", path=gateway.path, caps=("chat",))
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email="friend@example.test",
                                    path=gateway.path)
    client = gateway.client()
    response = gateway.sign_in(client, sub="friend", email="friend@example.test", email_verified=None)
    assert response.status_code == 302

    csrf = client.get("/auth/api/options").json()["signed_in_oidc"]["csrf_token"]
    accepted = client.post("/auth/api/invite/accept", headers={"x-azizam-csrf": csrf},
                           json={"token": token, "email": "friend@example.test", "name": "Friend"})
    assert accepted.status_code == 403
    assert accepted.json()["error"]["code"] == "verified_email_required"


def test_q5_a_listed_person_removed_from_trusted_loses_admin_on_next_sign_in(gateway):
    gateway.set_config(replace(gateway.config, allowed_email_domains=frozenset({"example.test"})))
    _, response = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    assert response.status_code == 302
    assert roles_of(gateway, "helper@example.test") == {("admin", "oidc")}

    _, response = signed_in(gateway, sub="helper", email="helper@example.test", groups=[])
    assert response.status_code == 302
    assert roles_of(gateway, "helper@example.test") == set()
