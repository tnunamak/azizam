"""A failed sign-in rolls back every success write on each browser path, and concurrent sign-ins all succeed."""

import threading
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from gateway import accounts, admin_store, browser_sessions, remembered_people
from gateway.admin_oidc import encode_session_cookie
from tests.test_signin_who_can_enter import gateway  # noqa: F401
from tests.support.access import store_plan


PASSWORD_EMAIL = "password@example.test"
PASSWORD = "a sufficiently long test password"
OIDC_EMAIL = "trusted@example.test"
INVITE_EMAIL = "invited@example.test"
CLAIM_EMAIL = "first-owner@example.test"
DEVICE_TOKEN = "known-device-token"


def _client(gateway):
    client = TestClient(
        gateway.proxy.app,
        base_url="https://ai.example",
        client=("127.0.0.1", 50000),
        raise_server_exceptions=False,
    )
    client.cookies.set(
        remembered_people.cookie_name(gateway.config.cookie.secure),
        DEVICE_TOKEN,
        domain="ai.example",
        path="/",
    )
    return client


def _seed_person(gateway, flow):
    if flow in {"password", "setup_password"}:
        user = admin_store.claim_first_local_owner(
            issuer="local",
            subject=PASSWORD_EMAIL,
            email=PASSWORD_EMAIL,
            display_name="Password User",
            verifier=accounts.hash_password(PASSWORD),
            granted_by="test",
            path=gateway.path,
        )
        admin_store.seed_first_owner_plan(user["id"], path=gateway.path)
        method, issuer = "password", None
    else:
        user = admin_store.upsert_oidc_user(
            gateway.config.issuer,
            "failure-injection-person",
            email=OIDC_EMAIL,
            display_name="Trusted User",
            path=gateway.path,
        )
        method, issuer = "sso", gateway.config.issuer

    person_id = str(user["id"])
    remembered_people.keep(
        path=gateway.path,
        token=DEVICE_TOKEN,
        person_id=person_id,
        method=method,
        issuer=issuer,
        remember=True,
        from_tile=False,
    )
    with accounts.connection(gateway.path) as conn:
        conn.execute("UPDATE users SET last_login_at=1 WHERE id=?", (person_id,))
        accounts.record_signin_refusal_in(conn, person_id)
        if flow == "oidc":
            conn.execute(
                "INSERT INTO user_identity_claims(user_id,claim_kind,claim_value,updated_at) "
                "VALUES(?,?,?,?)",
                (person_id, "oidc_group", "/Old", 1),
            )
    return person_id


def _seed_incoming_session(gateway, client, person_id):
    user = admin_store.get_user(person_id, path=gateway.path)
    session = browser_sessions.issue(
        {"sub": person_id, "email": user["email"] or ""}, role="none",
        cookie=gateway.config.cookie, path=gateway.path,
    )
    family = "incoming-family"
    session = replace(session, family=family)
    old_id = "incoming-retired-session"
    with accounts.connection(gateway.path) as conn:
        conn.execute(
            "INSERT INTO account_session_lineage(session_id,family_id,retired_at,expires_at,used_at,superseded) "
            "VALUES(?,?,?,?,?,?)",
            (old_id, family, int(session.issued_at) - 120, int(session.expires_at), int(session.issued_at) - 120, 0),
        )
        conn.execute(
            "INSERT INTO account_session_lineage(session_id,family_id,retired_at,expires_at,used_at,superseded) "
            "VALUES(?,?,?,?,?,?)",
            # Model an established session so request middleware does not record its first use
            # outside the sign-in transaction while the test exercises a failed sign-in.
            (session.session_id, family, None, int(session.expires_at), int(session.issued_at), 0),
        )
    client.cookies.set(
        gateway.config.cookie.name,
        encode_session_cookie(session, signing_key=gateway.config.signing_key),
        domain="ai.example",
        path="/",
    )


def _seed_invite(gateway):
    plan = store_plan("Invited plan", path=gateway.path, caps=("chat",))
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email=INVITE_EMAIL, path=gateway.path)
    return token


def _state_snapshot(gateway):
    tables = (
        "users", "account_access", "account_signin_refusals", "account_sessions",
        "account_session_revocations", "account_session_lineage", "account_oidc_verification",
        "user_identity_claims", "admin_role_assignments", "signin_rule_plans", "apps",
        "session_apps", "credentials", "remembered_people", "user_avatars", "audit_events",
        "access_plans", "prompt_memory_preferences",
    )
    with accounts.connection(gateway.path) as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in tables
        }


def _throttle_snapshot(gateway):
    with accounts.connection(gateway.path, invalidate_asset_cache=False) as conn:
        return [tuple(row) for row in conn.execute("SELECT * FROM account_login_limits ORDER BY bucket")]


def _inject_write_failure(gateway, flow, failure):
    triggers = {
        ("password", "login_timestamp"): "BEFORE UPDATE OF last_login_at ON users",
        ("password", "login_audit"): "BEFORE INSERT ON audit_events WHEN NEW.action='account.login'",
        ("password", "refusal_clear"): "BEFORE DELETE ON account_signin_refusals",
        ("password", "incoming_revoke"): "BEFORE INSERT ON account_session_revocations",
        ("password", "session_issue"): "BEFORE INSERT ON account_sessions",
        ("password", "remembered_people"): "BEFORE UPDATE ON remembered_people",
        ("setup_password", "refusal_clear"): "BEFORE DELETE ON account_signin_refusals",
        ("setup_password", "setup_audit"): "BEFORE INSERT ON audit_events WHEN NEW.action='setup.local_owner_login'",
        ("setup_password", "incoming_revoke"): "BEFORE INSERT ON account_session_revocations",
        ("setup_password", "session_issue"): "BEFORE INSERT ON account_sessions",
        ("oidc", "user_update"): "BEFORE UPDATE OF last_login_at ON users",
        ("oidc", "verification"): "BEFORE INSERT ON account_oidc_verification",
        ("oidc", "refusal_clear"): "BEFORE DELETE ON account_signin_refusals",
        ("oidc", "identity_claims"): "BEFORE DELETE ON user_identity_claims",
        ("oidc", "admin_grant"): "BEFORE INSERT ON admin_role_assignments",
        ("oidc", "rule_plan"): "BEFORE INSERT ON signin_rule_plans",
        ("oidc", "avatar"): "BEFORE INSERT ON user_avatars",
        ("oidc", "app_provision"): "BEFORE INSERT ON apps WHEN NEW.kind='personal'",
        ("oidc", "app_audit"): "BEFORE INSERT ON audit_events WHEN NEW.action='app.self_create'",
        ("oidc", "bootstrap_owner_grant"): "BEFORE INSERT ON admin_role_assignments WHEN NEW.role='owner'",
        ("oidc", "starter_plan"): "BEFORE INSERT ON access_plans WHEN NEW.name='Local only'",
        ("oidc", "confirm_lineage"): "BEFORE INSERT ON account_session_lineage",
        ("oidc", "incoming_revoke"): "BEFORE INSERT ON account_session_revocations",
        ("oidc", "session_issue"): "BEFORE INSERT ON account_sessions",
        ("oidc", "remembered_people"): "BEFORE UPDATE ON remembered_people",
        ("invite", "account"): "BEFORE INSERT ON users",
        ("invite", "credential"): "BEFORE INSERT ON credentials",
        ("invite", "invite_consume"): "BEFORE UPDATE OF used_at ON account_links",
        ("invite", "invite_audit"): "BEFORE INSERT ON audit_events WHEN NEW.action='account.invite.accept'",
        ("invite", "incoming_revoke"): "BEFORE INSERT ON account_session_revocations",
        ("invite", "session_issue"): "BEFORE INSERT ON account_sessions",
        ("setup_claim", "owner_grant"): "BEFORE INSERT ON admin_role_assignments WHEN NEW.role='owner'",
        ("setup_claim", "claim_audit"): "BEFORE INSERT ON audit_events WHEN NEW.action='setup.local_owner_create'",
        ("setup_claim", "starter_plan"): "BEFORE INSERT ON access_plans WHEN NEW.name='Local only'",
        ("setup_claim", "app_provision"): "BEFORE INSERT ON apps WHEN NEW.kind='personal'",
        ("setup_claim", "app_audit"): "BEFORE INSERT ON audit_events WHEN NEW.action='app.self_create'",
        ("setup_claim", "incoming_revoke"): "BEFORE INSERT ON account_session_revocations",
        ("setup_claim", "session_issue"): "BEFORE INSERT ON account_sessions",
    }
    event = triggers[(flow, failure)]
    with accounts.connection(gateway.path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_signin_write " + event + " BEGIN "
            "SELECT RAISE(ABORT, 'injected sign-in write failure'); END"
        )


@pytest.mark.parametrize(
    "flow,failure",
    [
        ("password", failure) for failure in (
            "login_timestamp", "login_audit", "refusal_clear", "incoming_revoke", "session_issue",
            "remembered_people",
        )
    ] + [
        ("setup_password", failure) for failure in (
            "refusal_clear", "setup_audit", "incoming_revoke", "session_issue",
        )
    ] + [
        ("oidc", failure) for failure in (
            "user_update", "verification", "refusal_clear", "identity_claims", "admin_grant",
            "rule_plan", "avatar", "app_provision", "app_audit", "incoming_revoke", "session_issue",
            "remembered_people", "confirm_lineage", "bootstrap_owner_grant", "starter_plan",
        )
    ] + [
        ("invite", failure) for failure in (
            "account", "credential", "invite_consume", "invite_audit", "incoming_revoke", "session_issue",
        )
    ] + [
        ("setup_claim", failure) for failure in (
            "owner_grant", "claim_audit", "starter_plan", "app_provision", "app_audit", "incoming_revoke",
            "session_issue",
        )
    ],
)
def test_signin_write_failure_leaves_all_signin_state_unchanged(gateway, flow, failure):
    person_id = _seed_person(gateway, flow)
    client = _client(gateway)
    _seed_incoming_session(gateway, client, person_id)

    if flow == "oidc" and failure == "rule_plan":
        plan = store_plan("Atomic sign-in", path=gateway.path, caps=("chat",))
        gateway.set_rules([{
            "id": "trusted", "field": "group", "values": ["/Trusted"],
            "admit": True, "admin": True, "plan": plan["id"],
        }])
    if flow == "oidc" and failure in {"bootstrap_owner_grant", "starter_plan"}:
        gateway.set_config(replace(gateway.config, admin_emails=frozenset({OIDC_EMAIL})))

    token = _seed_invite(gateway) if flow == "invite" else None
    before = _state_snapshot(gateway)
    throttle_before = _throttle_snapshot(gateway)
    _inject_write_failure(gateway, flow, failure)

    if flow == "password":
        response = client.post(
            "/auth/api/login",
            json={"email": PASSWORD_EMAIL, "password": PASSWORD, "remember": True},
        )
    elif flow == "setup_password":
        response = client.post(
            "/setup/local-login", data={"email": PASSWORD_EMAIL, "password": PASSWORD},
        )
    elif flow == "invite":
        response = client.post(
            "/auth/api/invite/accept",
            json={"token": token, "email": INVITE_EMAIL, "name": "Invited Person", "password": PASSWORD},
        )
    elif flow == "setup_claim":
        response = client.post(
            "/setup/local-owner",
            data={"email": CLAIM_EMAIL, "password": PASSWORD, "password_confirm": PASSWORD},
        )
    else:
        response = gateway.sign_in(
            client,
            sub="failure-injection-person",
            email=OIDC_EMAIL,
            groups=() if failure in {"bootstrap_owner_grant", "starter_plan"} else ("/Trusted",),
            start={"remember": "1", **({"reauth": "1"} if failure == "confirm_lineage" else {})},
            picture="https://avatar.example/picture.png" if failure == "avatar" else None,
        )

    session_cookie = gateway.config.cookie.name
    issued_cookies = [
        value for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{session_cookie}=") or value.startswith("azizam-device=")
    ]
    assert issued_cookies == []
    if flow == "password":
        assert response.status_code == 503
        assert response.json() == {
            "error": {
                "code": "signin_unavailable",
                "message": "Sign-in could not be completed. Try again.",
            }
        }
    elif flow == "oidc":
        assert response.status_code == 303
        assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["provider"]
    else:
        assert response.status_code == 500
    assert "injected sign-in write failure" not in response.text
    assert _state_snapshot(gateway) == before
    # Abuse throttles must commit before the sign-in transaction so failed writes still count.
    # The first-owner claim takes no throttle, so only the other flows make one.
    if flow != "setup_claim":
        assert _throttle_snapshot(gateway) != throttle_before


def test_a_failed_provider_sign_in_expires_the_browser_binding_cookie(gateway):
    client = gateway.client()
    login = client.get("/login", params={"return_to": "/"}, follow_redirects=False)
    if "redirect_uri=" not in login.headers["location"]:
        login = client.get(login.headers["location"], follow_redirects=False)
    state, record = list(gateway.proxy.ADMIN_OIDC_STATES.items())[-1]

    failure = client.get("/admin/oidc/callback", params={"state": state, "error": "access_denied"},
                         headers={"accept": "text/html"}, follow_redirects=False)

    assert failure.status_code == 303 and failure.headers["location"].startswith("/sign-in/?error=provider")
    binding = [value for value in failure.headers.get_list("set-cookie")
               if value.startswith(f"{record.browser_cookie_name}=")]
    assert len(binding) == 1 and "max-age=0" in binding[0].lower()


def test_concurrent_password_sign_ins_for_one_person_all_succeed(gateway):
    person_id = _seed_person(gateway, "password")
    clients = [_client(gateway) for _ in range(4)]
    start, responses, errors = threading.Barrier(len(clients)), [], []

    def sign_in(client):
        try:
            start.wait()
            responses.append(client.post(
                "/auth/api/login", json={"email": PASSWORD_EMAIL, "password": PASSWORD, "remember": True},
            ))
        except Exception as exc:  # noqa: BLE001  (a thread's failure must reach the assertion)
            errors.append(exc)

    threads = [threading.Thread(target=sign_in, args=(client,)) for client in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert [response.status_code for response in responses] == [200] * len(clients)
    assert all(
        any(value.startswith(f"{gateway.config.cookie.name}=") for value in response.headers.get_list("set-cookie"))
        for response in responses
    )
    assert len(gateway.rows("SELECT session_id FROM account_sessions WHERE user_id=?", (person_id,))) == len(clients)
