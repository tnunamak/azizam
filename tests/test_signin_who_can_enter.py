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
    # A service-registry write through the real app fires the hot reload, which
    # swaps `proxy.config` and every backend constant for ones built from this
    # test's temporary registry. Registering each name with monkeypatch puts the
    # shipped values back at teardown; without it the next test in the same
    # worker sees a registry with no speech, transcription or kobold services.
    monkeypatch.setattr(proxy, "config", proxy.config)
    for name in proxy._derive_backend_constants(proxy.config):
        monkeypatch.setattr(proxy, name, getattr(proxy, name))
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


def test_q5_removing_someone_from_trusted_revokes_their_admin_grant_and_session_even_when_not_listed(gateway):
    """A person removed from /Trusted whose email is not otherwise allowed is refused.

    The refusal used to happen BEFORE the claim-derived grant was reconciled, so
    the grant and any session they held stayed admin for up to 8 hours. A refused
    sign-in now ends the provider-derived grant and every session of that person.
    """
    client, response = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    assert response.status_code == 302
    assert client.get("/admin/api/access-plans").status_code == 200

    again = gateway.client()
    refused = gateway.sign_in(again, sub="helper", email="helper@example.test", groups=[])
    assert refused.status_code == 303                      # cannot sign in again
    assert roles_of(gateway, "helper@example.test") == set()                 # grant revoked
    assert client.get("/admin/api/access-plans").status_code in {401, 403}   # old session ended


def test_a_refused_sign_in_keeps_grants_the_owner_gave_by_hand(gateway):
    """Hand-given grants are the owner's decision; only provider-derived ones go."""
    client, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    user = gateway.user("helper@example.test")
    admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=user["id"],
                                  granted_by="owner", path=gateway.path)
    refused = gateway.sign_in(gateway.client(), sub="helper", email="helper@example.test", groups=[])
    assert refused.status_code == 303
    assert roles_of(gateway, "helper@example.test") == {("admin", "manual")}
    assert client.get("/admin/api/access-plans").status_code in {401, 403}   # sessions still end


def test_an_unverified_email_refusal_also_ends_provider_derived_access(gateway):
    client, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    refused = gateway.sign_in(gateway.client(), sub="helper", email="helper@example.test",
                              groups=["/Trusted"], email_verified=False)
    assert refused.status_code == 303
    assert roles_of(gateway, "helper@example.test") == set()
    assert client.get("/admin/api/access-plans").status_code in {401, 403}


def test_a_refusal_for_an_unknown_person_creates_nothing(gateway):
    refused = gateway.sign_in(gateway.client(), sub="nobody", email="nobody@example.test")
    assert refused.status_code == 303
    assert gateway.user("nobody@example.test") is None


def csrf_for(client):
    return {"x-azizam-csrf": client.get("/auth/api/options").json()["signed_in_oidc"]["csrf_token"]}


def owner_and_admin(gateway):
    owner, _ = signed_in(gateway, sub="owner", email=OWNER)
    owner_id = gateway.user(OWNER)["id"]
    plan_id = admin_store.person_plan_id(owner_id, path=gateway.path)
    admin, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    return owner, owner_id, plan_id, admin


def test_q5_a_trusted_admin_cannot_create_an_app_accountable_to_the_owner(gateway):
    """An administrator may not make the owner answer for an app.

    POST /admin/api/apps accepted ``owner_user_id``, so an administrator with no
    plan could create an app accountable to the owner, give it any plan, mint a
    key, and spend as the owner. Only the owner may create an app for someone else.
    """
    _, owner_id, plan_id, admin = owner_and_admin(gateway)
    before = len(gateway.rows("SELECT id FROM apps"))
    refused = admin.post("/admin/api/apps", headers=csrf_for(admin),
                         json={"name": "borrowed", "kind": "service", "access_plan": plan_id, "owner_user_id": owner_id})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    assert len(gateway.rows("SELECT id FROM apps")) == before


def test_an_administrator_who_names_no_owner_is_accountable_for_the_app_themselves(gateway):
    """With no owner_user_id the app belongs to the signed-in administrator, never to the owner."""
    _, _, plan_id, admin = owner_and_admin(gateway)
    created = admin.post("/admin/api/apps", headers=csrf_for(admin),
                         json={"name": "mine", "kind": "service", "access_plan": plan_id})
    assert created.status_code == 201, created.text
    helper_id = gateway.user("helper@example.test")["id"]
    assert created.json()["app"]["owner_user_id"] == helper_id


def test_an_administrator_without_a_plan_gets_no_usable_key_for_their_own_app(gateway):
    """The app is accountable to the administrator, who has no plan, so its key authorizes nothing."""
    from gateway.runtime_auth import hash_api_key

    _, _, plan_id, admin = owner_and_admin(gateway)
    assert has_working_plan(gateway, "helper@example.test") is False
    created = admin.post("/admin/api/apps", headers=csrf_for(admin),
                         json={"name": "mine", "kind": "service", "access_plan": plan_id})
    key = admin.post(f"/admin/api/apps/{created.json()['app']['id']}/keys", headers=csrf_for(admin),
                     json={"display_name": "k"})
    assert key.status_code == 201, key.text
    identity = admin_runtime.runtime_identity_for_app_credential_hash(
        hash_api_key(key.json()["credential"]["api_key"]), path=gateway.path)
    assert identity is None or not identity.access.complete


def test_an_administrator_cannot_mint_a_key_on_an_app_the_owner_answers_for(gateway):
    owner, owner_id, plan_id, admin = owner_and_admin(gateway)
    app = owner.post("/admin/api/apps", headers=csrf_for(owner),
                     json={"name": "owners", "kind": "service", "access_plan": plan_id, "owner_user_id": owner_id})
    assert app.status_code == 201, app.text
    refused = admin.post(f"/admin/api/apps/{app.json()['app']['id']}/keys", headers=csrf_for(admin),
                         json={"display_name": "k"})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"


def test_an_administrator_cannot_change_the_plan_or_owner_of_an_app_the_owner_answers_for(gateway):
    owner, owner_id, plan_id, admin = owner_and_admin(gateway)
    app = owner.post("/admin/api/apps", headers=csrf_for(owner),
                     json={"name": "owners", "kind": "service", "access_plan": "person", "owner_user_id": owner_id})
    app_id = app.json()["app"]["id"]
    refused = admin.patch(f"/admin/api/apps/{app_id}", headers=csrf_for(admin), json={"access_plan": plan_id})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    helper_id = gateway.user("helper@example.test")["id"]
    refused = admin.patch(f"/admin/api/apps/{app_id}", headers=csrf_for(admin), json={"owner_user_id": helper_id})
    assert refused.status_code == 403, refused.text
    assert gateway.rows("SELECT access_plan, owner_user_id FROM apps WHERE id=?", (app_id,)) == [
        {"access_plan": "person", "owner_user_id": owner_id}]
    # The owner may still change it.
    assert owner.patch(f"/admin/api/apps/{app_id}", headers=csrf_for(owner), json={"access_plan": plan_id}).status_code == 200


def test_an_administrator_may_still_change_the_plan_of_their_own_app(gateway):
    _, _, plan_id, admin = owner_and_admin(gateway)
    created = admin.post("/admin/api/apps", headers=csrf_for(admin),
                         json={"name": "mine", "kind": "service", "access_plan": "person"})
    assert created.status_code == 201, created.text
    changed = admin.patch(f"/admin/api/apps/{created.json()['app']['id']}", headers=csrf_for(admin),
                          json={"access_plan": plan_id})
    assert changed.status_code == 200, changed.text


def test_the_owner_can_still_create_an_app_accountable_to_someone_else(gateway):
    owner, _, plan_id, admin = owner_and_admin(gateway)
    helper_id = gateway.user("helper@example.test")["id"]
    created = owner.post("/admin/api/apps", headers=csrf_for(owner),
                         json={"name": "for helper", "kind": "service", "access_plan": plan_id, "owner_user_id": helper_id})
    assert created.status_code == 201, created.text
    assert created.json()["app"]["owner_user_id"] == helper_id


def mint_admin_key(client, role="admin", name="helper automation"):
    minted = client.post("/admin/api/admin-keys", headers=csrf_for(client), json={"name": name, "role": role})
    assert minted.status_code == 201, minted.text
    return minted.json()["credential"]["api_key"]


def bearer_status(gateway, api_key, path="/admin/api/access-plans"):
    return gateway.client().get(path, headers={"authorization": f"Bearer {api_key}"}).status_code


def test_q5_an_admin_key_minted_by_a_trusted_admin_stops_when_they_leave_the_group(gateway):
    """An admin-scoped key works only while the person who minted it still holds a permitting role."""
    gateway.set_config(replace(gateway.config, allowed_email_domains=frozenset({"example.test"})))
    signed_in(gateway, sub="owner", email=OWNER)
    admin, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    api_key = mint_admin_key(admin)
    assert bearer_status(gateway, api_key) == 200

    # The person is removed from the group and signs in again: the grant is revoked...
    signed_in(gateway, sub="helper", email="helper@example.test", groups=[])
    assert roles_of(gateway, "helper@example.test") == set()
    # ...and the key they minted no longer works on the admin API.
    assert bearer_status(gateway, api_key) == 403


def test_a_key_minted_by_another_key_stops_with_the_person_at_the_root(gateway):
    gateway.set_config(replace(gateway.config, allowed_email_domains=frozenset({"example.test"})))
    signed_in(gateway, sub="owner", email=OWNER)
    admin, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    first = mint_admin_key(admin, name="first")
    child = gateway.client().post("/admin/api/admin-keys", headers={"authorization": f"Bearer {first}"},
                                  json={"name": "second", "role": "admin"})
    assert child.status_code == 201, child.text
    second = child.json()["credential"]["api_key"]
    assert bearer_status(gateway, second) == 200
    signed_in(gateway, sub="helper", email="helper@example.test", groups=[])
    assert bearer_status(gateway, second) == 403
    assert bearer_status(gateway, first) == 403


def test_an_owner_key_minted_by_the_owner_and_a_command_line_key_keep_working(gateway):
    from gateway.admin_runtime import create_admin_service_account_key

    owner, _ = signed_in(gateway, sub="owner", email=OWNER)
    owner_key = mint_admin_key(owner, role="owner", name="owner automation")
    cli_key, _, _ = create_admin_service_account_key("ops", role="owner", path=gateway.path)
    assert bearer_status(gateway, owner_key) == 200
    assert bearer_status(gateway, cli_key) == 200
    assert bearer_status(gateway, owner_key, "/admin/api/people") == 200


def test_an_owner_key_is_lowered_when_its_minter_is_no_longer_an_owner(gateway):
    """A key never carries more authority than the person who made it holds now."""
    owner, _ = signed_in(gateway, sub="owner", email=OWNER)
    owner_id = gateway.user(OWNER)["id"]
    owner_key = mint_admin_key(owner, role="owner", name="owner automation")
    admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=owner_id, path=gateway.path)
    for row in admin_store.list_admin_roles(subject_kind="user", subject_id=owner_id, path=gateway.path):
        if row["role"] == "owner":
            admin_store.revoke_admin_role(row["id"], path=gateway.path)
    assert bearer_status(gateway, owner_key) == 200                      # still an administrator's key
    assert bearer_status(gateway, owner_key, "/admin/api/people") == 403  # no longer owner reach


def test_first_signin_by_a_trusted_member_on_a_fresh_instance_does_not_block_the_listed_owner(gateway):
    """Owner bootstrap applies while no OWNER grant exists, not while no grant of any kind exists."""
    signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    _, response = signed_in(gateway, sub="owner", email=OWNER)

    assert response.status_code == 302
    assert any(role == "owner" for role, _ in roles_of(gateway, OWNER))
    assert admin_store.person_plan_id(gateway.user(OWNER)["id"], path=gateway.path) is not None


def test_owner_bootstrap_does_not_apply_once_an_owner_exists(gateway):
    other = "second@example.test"
    gateway.set_config(replace(gateway.config, admin_emails=frozenset({OWNER, other})))
    signed_in(gateway, sub="owner", email=OWNER)
    signed_in(gateway, sub="second", email=other)
    assert not any(role == "owner" for role, _ in roles_of(gateway, other))


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


def test_a_person_who_loses_the_group_while_still_allowed_loses_their_other_sessions(gateway):
    gateway.set_config(replace(gateway.config, allowed_email_domains=frozenset({"example.test"})))
    old, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    assert old.get("/admin/api/access-plans").status_code == 200
    signed_in(gateway, sub="helper", email="helper@example.test", groups=[])
    assert old.get("/admin/api/access-plans").status_code in {401, 403}


def test_a_key_whose_named_grantor_cannot_be_found_has_no_authority(gateway):
    from gateway.admin_runtime import generate_admin_api_key as make_key
    from gateway.runtime_auth import hash_api_key

    account = admin_store.ensure_service_account("orphan", path=gateway.path)
    key = make_key()
    admin_store.create_credential_hash(subject_kind="service_account", subject_id=account["id"],
                                       hash=hash_api_key(key), prefix=key[:8], path=gateway.path)
    admin_store.assign_admin_role(role="admin", subject_kind="service_account", subject_id=account["id"],
                                  granted_by="usr_nobody", path=gateway.path)
    assert bearer_status(gateway, key) == 403
    assert admin_store.effective_admin_roles("service_account", account["id"], path=gateway.path) == frozenset()


# ---------------------------------------------------------------------------
# Review findings: key ownership, stale provider grants, refusal reach, plan writes.
# ---------------------------------------------------------------------------

def test_a_dashboard_key_never_takes_over_a_service_account_by_name(gateway):
    """An administrator who names a key like the operator's key gets a refusal, not that account."""
    from gateway.admin_runtime import create_admin_service_account_key

    signed_in(gateway, sub="owner", email=OWNER)
    admin, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    deploy_key, _, _ = create_admin_service_account_key("deploy", role="admin", path=gateway.path)

    refused = admin.post("/admin/api/admin-keys", headers=csrf_for(admin), json={"name": "deploy", "role": "admin"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "admin_key_name_taken"
    grantors = {row["granted_by"] for row in gateway.rows(
        "SELECT r.granted_by FROM admin_role_assignments r JOIN service_accounts s ON s.id = r.subject_id "
        "WHERE s.name = 'deploy'")}
    assert grantors == {"cli"}
    # The administrator is demoted; the operator's key does not notice.
    signed_in(gateway, sub="helper", email="helper@example.test", groups=[])
    assert bearer_status(gateway, deploy_key) == 200


def test_two_dashboard_keys_cannot_share_a_name(gateway):
    signed_in(gateway, sub="owner", email=OWNER)
    admin, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    mint_admin_key(admin, name="same")
    again = admin.post("/admin/api/admin-keys", headers=csrf_for(admin), json={"name": "same", "role": "admin"})
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "admin_key_name_taken"


def test_the_owner_cannot_turn_an_admin_key_into_an_owner_key_by_reusing_its_name(gateway):
    owner, _ = signed_in(gateway, sub="owner", email=OWNER)
    admin, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    mint_admin_key(admin, name="shared")
    again = owner.post("/admin/api/admin-keys", headers=csrf_for(owner), json={"name": "shared", "role": "owner"})
    assert again.status_code == 409, again.text


def test_a_service_accounts_grantor_cannot_be_overwritten(gateway):
    account = admin_store.ensure_service_account("acct", path=gateway.path)
    admin_store.assign_admin_role(role="admin", subject_kind="service_account", subject_id=account["id"],
                                  granted_by="cli", path=gateway.path)
    with pytest.raises(PermissionError):
        admin_store.assign_admin_role(role="admin", subject_kind="service_account", subject_id=account["id"],
                                      granted_by="usr_someone", path=gateway.path)
    assert [row["granted_by"] for row in admin_store.list_admin_roles(
        subject_kind="service_account", subject_id=account["id"], path=gateway.path)] == ["cli"]


def test_the_command_line_will_not_attach_to_an_account_the_dashboard_made(gateway):
    from gateway.admin_runtime import create_admin_service_account_key

    signed_in(gateway, sub="owner", email=OWNER)
    admin, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    mint_admin_key(admin, name="theirs")
    with pytest.raises(PermissionError):
        create_admin_service_account_key("theirs", role="owner", path=gateway.path)
    assert gateway.rows("SELECT COUNT(*) AS n FROM credentials WHERE created_by = 'cli'") == [{"n": 0}]


def test_a_key_is_capped_by_its_own_minter_even_if_its_account_gains_an_operator_grant(gateway):
    """The cap follows the credential, so a later grant on the same account cannot lift it."""
    signed_in(gateway, sub="owner", email=OWNER)
    admin, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    key = mint_admin_key(admin, name="capped")
    account = gateway.rows("SELECT id FROM service_accounts WHERE name = 'capped'")[0]["id"]
    admin_store.assign_admin_role(role="owner", subject_kind="service_account", subject_id=account,
                                  granted_by="cli", path=gateway.path)
    assert bearer_status(gateway, key) == 200
    assert bearer_status(gateway, key, "/admin/api/people") == 403


# -- identity-provider grants age out

def _age_provider_grants(gateway, seconds):
    with accounts.connection(gateway.path) as conn:
        conn.execute("UPDATE admin_role_assignments SET last_confirmed_at = last_confirmed_at - ? "
                     "WHERE provenance = 'oidc'", (seconds,))


def test_a_group_administrator_who_never_signs_in_again_loses_the_grant_and_their_keys(gateway):
    signed_in(gateway, sub="owner", email=OWNER)
    admin, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    key = mint_admin_key(admin)
    assert admin.get("/admin/api/access-plans").status_code == 200
    assert bearer_status(gateway, key) == 200

    _age_provider_grants(gateway, 9 * 3600)   # nine hours without a confirming sign-in

    assert admin.get("/admin/api/access-plans").status_code == 403
    assert bearer_status(gateway, key) == 403


def test_signing_in_again_confirms_the_provider_grant(gateway):
    signed_in(gateway, sub="owner", email=OWNER)
    signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    _age_provider_grants(gateway, 9 * 3600)
    fresh, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    assert fresh.get("/admin/api/access-plans").status_code == 200


def test_a_hand_given_grant_does_not_age_out(gateway):
    client, _ = signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    user = gateway.user("helper@example.test")
    admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=user["id"],
                                  granted_by="owner", path=gateway.path)
    _age_provider_grants(gateway, 100 * 3600)
    assert admin_store.effective_admin_roles("user", user["id"], path=gateway.path) == frozenset({"admin"})


def test_the_provider_grant_column_is_added_to_an_existing_database(tmp_path):
    """Rehearse the additive migration on a database shaped like the one before it."""
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    user = admin_store.upsert_oidc_user("https://id.example", "u", email="u@example.test", path=path)
    admin_store.reconcile_oidc_admin_roles(user["id"], {"admin"}, path=path)
    manual = admin_store.upsert_oidc_user("https://id.example", "m", email="m@example.test", path=path)
    admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=manual["id"], path=path)
    with accounts.connection(path) as conn:
        conn.execute("ALTER TABLE admin_role_assignments DROP COLUMN last_confirmed_at")
        before = [dict(r) for r in conn.execute("SELECT id, role, subject_id, provenance FROM admin_role_assignments")]

    admin_store.init_db(path)
    admin_store.init_db(path)   # idempotent

    with accounts.connection(path) as conn:
        after = [dict(r) for r in conn.execute(
            "SELECT id, role, subject_id, provenance, last_confirmed_at FROM admin_role_assignments ORDER BY id")]
    assert [{k: v for k, v in row.items() if k != "last_confirmed_at"} for row in after] == before
    by_provenance = {row["provenance"]: row["last_confirmed_at"] for row in after}
    assert by_provenance["oidc"] is not None          # one session length of grace
    assert by_provenance["manual"] is None
    assert admin_store.effective_admin_roles("user", user["id"], path=path) == frozenset({"admin"})


# -- a refused sign-in reaches MCP connections and personal keys

def _personal_key(gateway, email):
    from gateway.runtime_auth import hash_api_key

    user = gateway.user(email)
    app = admin_store.ensure_personal_app_for_user(user["id"], name="personal", path=gateway.path)
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=hash_api_key("opk_personal"),
                                       prefix="opk_perso", created_by=user["id"], path=gateway.path)
    return hash_api_key("opk_personal")


def _mcp_grant(gateway, email):
    from gateway import mcp_oauth_store

    store = mcp_oauth_store.Store(lambda: gateway.path)
    client = store.put_client(client_id="c1", kind="dcr", name="Client", redirect_uris=["https://c.example/cb"],
                              grant_types=["authorization_code"], auth_method="none", secret_hash=None, metadata={})
    return store, store.create_grant(person_id=gateway.user(email)["id"], client=client,
                                     redirect_uri="https://c.example/cb", resource="https://ai.example/mcp", scope="mcp")


def test_a_refused_sign_in_revokes_mcp_connections(gateway):
    signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    store, grant = _mcp_grant(gateway, "helper@example.test")
    assert store.active_grant(grant.id) is not None
    gateway.sign_in(gateway.client(), sub="helper", email="helper@example.test", groups=[])
    assert store.active_grant(grant.id) is None


def test_a_refused_sign_in_pauses_personal_keys_until_a_sign_in_succeeds(gateway):
    signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    key_hash = _personal_key(gateway, "helper@example.test")
    before = admin_runtime.runtime_identity_for_app_credential_hash(key_hash, path=gateway.path)
    assert before is not None and before.denial is None

    gateway.sign_in(gateway.client(), sub="helper", email="helper@example.test", groups=[])
    paused = admin_runtime.runtime_identity_for_app_credential_hash(key_hash, path=gateway.path)
    assert paused is not None and paused.denial == "person_signin_refused"

    gateway.set_config(replace(gateway.config, allowed_email_domains=frozenset({"example.test"})))
    signed_in(gateway, sub="helper", email="helper@example.test", groups=[])
    again = admin_runtime.runtime_identity_for_app_credential_hash(key_hash, path=gateway.path)
    assert again is not None and again.denial is None


# -- plan writes are the owner's

PLAN_DOCUMENT = {
    "name": "Widened", "can_use": {"chat": {"items": "all"}},
    "direct_engine_access": {"engines": [], "operations": []}, "paid_providers": [],
    "provider_fallback": False, "caps": {"usd_per_request": None, "usd_per_day": None, "usd_per_month": None},
    "can_load_models": False, "receipt_detail": "basic", "media_metadata": "none",
}


def test_an_administrator_cannot_write_access_plans_but_can_read_them(gateway):
    owner, _, plan_id, admin = owner_and_admin(gateway)
    assert admin.get("/admin/api/access-plans").status_code == 200
    assert admin.get(f"/admin/api/access-plans/{plan_id}").status_code == 200
    for response in (
        admin.post("/admin/api/access-plans", headers=csrf_for(admin), json=PLAN_DOCUMENT),
        admin.put(f"/admin/api/access-plans/{plan_id}", headers=csrf_for(admin), json=PLAN_DOCUMENT),
        admin.delete(f"/admin/api/access-plans/{plan_id}", headers=csrf_for(admin)),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "owner_required"
    created = owner.post("/admin/api/access-plans", headers=csrf_for(owner), json=PLAN_DOCUMENT)
    assert created.status_code == 201, created.text


# -- keys minted from a claimed browser session

def test_a_key_minted_from_a_claimed_key_session_holds_its_roles(gateway):
    from gateway.admin_runtime import create_admin_service_account_key

    claim_key, _, _ = create_admin_service_account_key("claimant", role="owner", path=gateway.path)
    client = gateway.client()
    claimed = client.post("/setup/claim", data={"admin_key": claim_key}, follow_redirects=False)
    assert claimed.status_code == 303, claimed.text
    csrf = {"x-azizam-csrf": client.get("/admin/api/session").json()["actor"]["csrf_token"]}
    minted = client.post("/admin/api/admin-keys", headers=csrf, json={"name": "from session", "role": "owner"})
    assert minted.status_code == 201, minted.text
    child = minted.json()["credential"]["api_key"]
    assert bearer_status(gateway, child) == 200
    assert bearer_status(gateway, child, "/admin/api/people") == 200
    assert not any(str(row["granted_by"]).startswith("service_account:") for row in gateway.rows(
        "SELECT granted_by FROM admin_role_assignments"))


# -- owner bootstrap

def test_the_local_owner_can_be_claimed_beside_a_group_administrator_but_not_after_an_owner(gateway):
    signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    first = admin_store.claim_first_local_owner(issuer="local", subject="a@example.test", email="a@example.test",
                                                display_name="a", verifier="v", granted_by="setup", path=gateway.path)
    assert first is not None
    second = admin_store.claim_first_local_owner(issuer="local", subject="b@example.test", email="b@example.test",
                                                 display_name="b", verifier="v", granted_by="setup", path=gateway.path)
    assert second is None


def test_a_revoked_owner_still_blocks_both_first_owner_paths(gateway):
    owner = admin_store.upsert_oidc_user("https://id.example", "o", email="o@example.test", path=gateway.path)
    grant = admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=owner["id"], path=gateway.path)
    admin_store.revoke_admin_role(grant["id"], path=gateway.path)
    assert admin_store.claim_first_owner(owner["id"], granted_by="oidc-bootstrap", path=gateway.path) is False
    assert admin_store.claim_first_local_owner(issuer="local", subject="x@example.test", email="x@example.test",
                                               display_name="x", verifier="v", granted_by="setup",
                                               path=gateway.path) is None


def test_two_first_owner_claims_make_one_owner(gateway):
    first = admin_store.upsert_oidc_user("https://id.example", "1", email="1@example.test", path=gateway.path)
    second = admin_store.upsert_oidc_user("https://id.example", "2", email="2@example.test", path=gateway.path)
    assert admin_store.claim_first_owner(first["id"], granted_by="oidc-bootstrap", path=gateway.path) is True
    assert admin_store.claim_first_owner(second["id"], granted_by="oidc-bootstrap", path=gateway.path) is False
    assert len([r for r in admin_store.list_admin_roles(path=gateway.path) if r["role"] == "owner"]) == 1


def test_an_app_scoped_grant_is_not_a_control_plane_role(gateway):
    user = admin_store.upsert_oidc_user("https://id.example", "s", email="s@example.test", path=gateway.path)
    app = admin_store.create_app("scoped", kind="service", owner_user_id=user["id"], access_plan="person",
                                 path=gateway.path)
    admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=user["id"],
                                  scope_app_id=app["id"], path=gateway.path)
    assert admin_store.effective_admin_roles("user", user["id"], path=gateway.path) == frozenset()


def test_an_app_scoped_grant_on_a_service_account_is_not_a_control_plane_role(gateway):
    """A key's grant for one App must not count as a global role, same as for a person."""
    owner = admin_store.upsert_oidc_user("https://id.example", "so", email="so@example.test", path=gateway.path)
    app = admin_store.create_app("scoped", kind="service", owner_user_id=owner["id"], access_plan="person",
                                 path=gateway.path)
    account = admin_store.ensure_service_account("scoped key", path=gateway.path)
    admin_store.assign_admin_role(role="owner", subject_kind="service_account", subject_id=account["id"],
                                  scope_app_id=app["id"], path=gateway.path)
    assert admin_store.effective_admin_roles("service_account", account["id"], path=gateway.path) == frozenset()
    admin_store.assign_admin_role(role="admin", subject_kind="service_account", subject_id=account["id"],
                                  path=gateway.path)
    assert admin_store.effective_admin_roles(
        "service_account", account["id"], path=gateway.path) == frozenset({"admin"})


def _person_with_scoped_owner_grant(gateway):
    user = admin_store.upsert_oidc_user("https://id.example", "sc", email="sc@example.test", path=gateway.path)
    app = admin_store.create_app("mine", kind="service", owner_user_id=user["id"], access_plan="person",
                                 path=gateway.path)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=user["id"],
                                  scope_app_id=app["id"], path=gateway.path)
    return user, app


def test_receipts_and_media_treat_an_app_scoped_owner_grant_as_no_role(gateway, monkeypatch):
    """Who may see everyone's receipts and media follows the same roles as the control plane."""
    from types import SimpleNamespace

    from gateway import me_media, user_runtime

    user, app = _person_with_scoped_owner_grant(gateway)
    assert user_runtime._live_role_for_user(user["id"], path=gateway.path) == "none"
    assert me_media._live_role_for_user(user["id"], gateway.path) == "none"

    seen = {}
    monkeypatch.setattr(user_runtime.receipt_store, "recent", lambda **kw: seen.update(kw) or [])
    user_runtime.build_user_activity_snapshot(SimpleNamespace(subject=user["id"]), path=gateway.path)
    assert seen["app_ids"] == [app["id"]]  # not None: the feed is limited to their own App

    monkeypatch.setattr(user_runtime.receipt_store, "get", lambda *a, **kw: {"app_id": "someone-elses-app"})
    with pytest.raises(user_runtime.UserPortalError) as refused:
        user_runtime.get_user_receipt(SimpleNamespace(subject=user["id"]), "req-1", path=gateway.path)
    assert refused.value.code == "receipt_not_found"


def test_receipts_and_media_drop_a_provider_grant_nobody_confirmed_lately(gateway):
    from gateway import me_media, user_runtime

    user = admin_store.upsert_oidc_user("https://id.example", "old", email="old@example.test", path=gateway.path)
    admin_store.reconcile_oidc_admin_roles(user["id"], {"admin"}, path=gateway.path)
    assert user_runtime._live_role_for_user(user["id"], path=gateway.path) == "admin"
    _age_provider_grants(gateway, 9 * 3600)
    assert user_runtime._live_role_for_user(user["id"], path=gateway.path) == "none"
    assert me_media._live_role_for_user(user["id"], gateway.path) == "none"


# -- provider-spend keys are the owner's

def test_only_the_owner_can_mint_provider_spend_keys(gateway):
    """A LiteLLM virtual key spends the owner's provider money, so an administrator cannot mint one."""
    owner, _, _, admin = owner_and_admin(gateway)
    body = {"key_alias": "spender", "models": ["x"]}
    refused = admin.post("/admin/api/operator/litellm/keys", headers=csrf_for(admin), json=body)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    # The owner gets past the check (this gateway has no LiteLLM, so it then says so).
    allowed = owner.post("/admin/api/operator/litellm/keys", headers=csrf_for(owner), json=body)
    assert allowed.status_code != 403, allowed.text


# -- an administrator cannot cut off someone who outranks them

def _credential_id_of_key(gateway, api_key):
    from gateway.runtime_auth import hash_api_key

    return gateway.rows("SELECT id FROM credentials WHERE hash=?", (hash_api_key(api_key),))[0]["id"]


def test_an_administrator_cannot_revoke_the_owners_admin_key(gateway):
    owner, _, _, admin = owner_and_admin(gateway)
    owner_key = mint_admin_key(owner, role="owner", name="owner automation")
    credential_id = _credential_id_of_key(gateway, owner_key)
    for refused in (
        admin.post(f"/admin/api/credentials/{credential_id}/revoke", headers=csrf_for(admin)),
        admin.delete(f"/admin/api/credentials/{credential_id}", headers=csrf_for(admin)),
    ):
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "credential_outranks_you"
    assert bearer_status(gateway, owner_key, "/admin/api/people") == 200
    # The owner may revoke it, and an administrator may revoke a key of their own.
    own_key = mint_admin_key(admin)
    assert admin.post(f"/admin/api/credentials/{_credential_id_of_key(gateway, own_key)}/revoke",
                      headers=csrf_for(admin)).status_code == 200
    assert owner.post(f"/admin/api/credentials/{credential_id}/revoke", headers=csrf_for(owner)).status_code == 200


def test_an_administrator_cannot_revoke_or_disable_what_belongs_to_the_owner(gateway):
    owner, owner_id, plan_id, admin = owner_and_admin(gateway)
    app_id = owner.post("/admin/api/apps", headers=csrf_for(owner), json={
        "name": "owners", "kind": "service", "access_plan": plan_id, "owner_user_id": owner_id}).json()["app"]["id"]
    key = owner.post(f"/admin/api/apps/{app_id}/keys", headers=csrf_for(owner), json={"display_name": "k"})
    credential_id = key.json()["credential"]["id"]
    refused = admin.post(f"/admin/api/credentials/{credential_id}/revoke", headers=csrf_for(admin))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "credential_outranks_you"
    disabled = admin.delete(f"/admin/api/apps/{app_id}", headers=csrf_for(admin))
    assert disabled.status_code == 403, disabled.text
    assert disabled.json()["error"]["code"] == "app_outranks_you"
    assert gateway.rows("SELECT revoked_at FROM credentials WHERE id=?", (credential_id,))[0]["revoked_at"] is None
    assert gateway.rows("SELECT disabled_at FROM apps WHERE id=?", (app_id,))[0]["disabled_at"] is None


def test_an_administrator_can_still_revoke_a_members_app_key(gateway):
    owner, _, plan_id, admin = owner_and_admin(gateway)
    member_id = admin_store.upsert_oidc_user("https://id.example", "m", email="m@example.test", path=gateway.path)["id"]
    app_id = owner.post("/admin/api/apps", headers=csrf_for(owner), json={
        "name": "members", "kind": "service", "access_plan": plan_id, "owner_user_id": member_id}).json()["app"]["id"]
    key = owner.post(f"/admin/api/apps/{app_id}/keys", headers=csrf_for(owner), json={"display_name": "k"})
    revoked = admin.post(f"/admin/api/credentials/{key.json()['credential']['id']}/revoke", headers=csrf_for(admin))
    assert revoked.status_code == 200, revoked.text
    assert admin.delete(f"/admin/api/apps/{app_id}", headers=csrf_for(admin)).status_code == 200


# -- a refusal always sticks

def test_a_refusal_is_recorded_even_when_the_cleanup_before_it_fails(gateway, monkeypatch):
    signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    user = gateway.user("helper@example.test")

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(admin_store, "reconcile_oidc_admin_roles", broken)
    monkeypatch.setattr(accounts, "revoke_person_sessions", broken)
    with pytest.raises(RuntimeError):
        gateway.proxy._end_provider_access_after_refusal(gateway.config.issuer, {"sub": "helper"})
    assert gateway.rows("SELECT user_id FROM account_signin_refusals") == [{"user_id": user["id"]}]


# -- a local password sign-in does not lift a provider refusal

def test_a_local_password_sign_in_does_not_lift_the_refusal_of_the_providers_account(gateway):
    """The password account and the provider account are two records that share an email.

    Only a successful provider sign-in lifts a provider refusal. A password sign-in
    is a different account and must leave the refused one paused.
    """
    from gateway.runtime_auth import hash_api_key

    plan = store_plan("Family", path=gateway.path, caps=("chat",))
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email="helper@example.test",
                                    path=gateway.path)
    accepted = gateway.client().post("/auth/api/invite/accept", json={
        "token": token, "email": "helper@example.test", "name": "Helper", "password": "a sufficiently long password"})
    assert accepted.status_code == 200, accepted.text

    gateway.set_config(replace(gateway.config, allowed_email_domains=frozenset({"example.test"})))
    signed_in(gateway, sub="helper", email="helper@example.test", groups=["/Trusted"])
    oidc_user = gateway.rows("SELECT * FROM users WHERE issuer != 'local' AND email = 'helper@example.test'")[0]
    app = admin_store.ensure_personal_app_for_user(oidc_user["id"], name="personal", path=gateway.path)
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=hash_api_key("opk_personal"),
                                       prefix="opk_perso", created_by=oidc_user["id"], path=gateway.path)
    key_hash = hash_api_key("opk_personal")
    gateway.sign_in(gateway.client(), sub="helper", email="helper@example.test", groups=[], email_verified=False)
    paused = admin_runtime.runtime_identity_for_app_credential_hash(key_hash, path=gateway.path)
    assert paused is not None and paused.denial == "person_signin_refused"

    login = gateway.client().post("/auth/api/login", json={
        "email": "helper@example.test", "password": "a sufficiently long password"})
    assert login.status_code == 200, login.text

    still = admin_runtime.runtime_identity_for_app_credential_hash(key_hash, path=gateway.path)
    assert still is not None and still.denial == "person_signin_refused"
    assert gateway.rows("SELECT user_id FROM account_signin_refusals") == [{"user_id": oidc_user["id"]}]
