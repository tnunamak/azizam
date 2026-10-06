"""Hermetic account lifecycle tests using temporary SQLite and ASGI clients."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from gateway import accounts, accounts_api, admin_store
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.admin_oidc import CookieConfig, csrf_token_for_session, session_from_claims
from gateway.runtime_auth import hash_api_key

PASSWORD = "a sufficiently long password"
SIGNING_KEY = b"account-test-key-32-bytes-long!!!"


@pytest.fixture
def account_db(tmp_path):
    path = tmp_path / "accounts.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    plan = admin_store.create_profile("Member plan", {"allow": {"model_groups": ["test-model"]}}, path=path)
    return path, plan


@pytest.fixture
def fast_password_work(monkeypatch):
    # Production work parameters are independently checked below. Lifecycle tests
    # exercise the real KDF with smaller cost to keep race tests practical.
    monkeypatch.setattr(accounts, "_SCRYPT_N", 2**10)


def invited_member(account_db, email="member@example.test"):
    path, plan = account_db
    _, token = accounts.create_link("invite", "owner", profile_id=plan["id"], email=email, path=path)
    return accounts.accept_invite(token, email, "Member", PASSWORD, path=path)


def rows(path, sql, values=()):
    with accounts.connection(path) as conn:
        return [dict(row) for row in conn.execute(sql, values).fetchall()]


def expect_error(code, operation):
    with pytest.raises(accounts.AccountError) as caught:
        operation()
    assert caught.value.code == code
    return caught.value


def test_password_scrypt_random_salt_nonplaintext_and_constant_time_compare(monkeypatch):
    assert accounts._SCRYPT_N == 2**17
    first = accounts.hash_password(PASSWORD)
    second = accounts.hash_password(PASSWORD)
    assert first != second
    assert first.startswith("scrypt$v1$")
    assert PASSWORD not in first
    comparisons = []
    original = accounts.hmac.compare_digest
    monkeypatch.setattr(accounts.hmac, "compare_digest", lambda a, b: comparisons.append((a, b)) or original(a, b))
    assert accounts.verify_password(PASSWORD, first)
    assert not accounts.verify_password("wrong password", first)
    assert len(comparisons) == 2
    assert all(len(a) == len(b) == 32 for a, b in comparisons)


@pytest.mark.parametrize("password", [None, "", "short", "x" * 1025, 123])
def test_invalid_passwords_rejected_before_kdf(password, monkeypatch):
    monkeypatch.setattr(accounts.hashlib, "scrypt", lambda *args, **kwargs: pytest.fail("invalid password reached KDF"))
    expect_error("invalid_password", lambda: accounts.hash_password(password))


@pytest.mark.parametrize("verifier", ["", "scrypt$v2$00$00", "scrypt$v1$bad$bad", "unsupported$stuff"])
def test_invalid_verifiers_fail_closed(verifier):
    assert not accounts.verify_password(PASSWORD, verifier)


def test_known_and_unknown_login_do_the_same_password_work(account_db, fast_password_work, monkeypatch):
    path, _ = account_db
    invited_member(account_db)
    calls = []
    original = accounts.hashlib.scrypt
    monkeypatch.setattr(accounts.hashlib, "scrypt", lambda *args, **kwargs: calls.append(kwargs) or original(*args, **kwargs))
    expect_error("invalid_login", lambda: accounts.login("member@example.test", "wrong password", "one", path=path))
    expect_error("invalid_login", lambda: accounts.login("missing@example.test", "wrong password", "two", path=path))
    assert len(calls) == 2
    assert {k: v for k, v in calls[0].items() if k != "salt"} == {k: v for k, v in calls[1].items() if k != "salt"}
    user = accounts.login(" MEMBER@EXAMPLE.TEST ", PASSWORD, "three", path=path)
    assert "verifier" not in user


def test_invite_returns_key_once_and_stores_only_hashes(account_db, fast_password_work):
    path, plan = account_db
    link, token = accounts.create_link("invite", "owner", profile_id=plan["id"], path=path)
    assert "token_hash" not in link
    assert token not in str(accounts.list_invites(path=path))
    user, app, credential = accounts.accept_invite(token, "Person@Example.Test", " Person ", PASSWORD, path=path)
    assert user["email"] == "person@example.test"
    assert user["display_name"] == "Person"
    assert app["owner_user_id"] == user["id"]
    assert app["default_profile_id"] == plan["id"]
    saved = rows(path, "SELECT * FROM credentials WHERE id=?", (credential["id"],))[0]
    assert saved["hash"] == hash_api_key(credential["api_key"])
    assert credential["api_key"] not in str(saved)
    assert rows(path, "SELECT token_hash FROM account_links")[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    expect_error("link_unavailable", lambda: accounts.inspect_invite(token, path=path))
    expect_error("link_unavailable", lambda: accounts.accept_invite(token, user["email"], "Person", PASSWORD, path=path))


def test_invite_consumption_is_atomic_under_race(account_db, fast_password_work, monkeypatch):
    path, plan = account_db
    _, token = accounts.create_link("invite", "owner", profile_id=plan["id"], path=path)
    barrier = threading.Barrier(2)
    original = accounts.hash_password

    def synchronized_hash(password):
        result = original(password)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(accounts, "hash_password", synchronized_hash)

    def accept(email):
        try:
            return accounts.accept_invite(token, email, "Member", PASSWORD, path=path)
        except accounts.AccountError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(accept, ["one@example.test", "two@example.test"]))
    assert sum(isinstance(result, tuple) for result in results) == 1
    assert "link_unavailable" in results
    assert len(rows(path, "SELECT * FROM account_access")) == 1
    assert len(rows(path, "SELECT * FROM apps")) == 1
    assert len(rows(path, "SELECT * FROM credentials")) == 1


def test_failed_invite_transaction_rolls_back_every_created_record(account_db, fast_password_work):
    path, plan = account_db
    _, token = accounts.create_link("invite", "owner", profile_id=plan["id"], path=path)
    with accounts.connection(path) as conn:
        conn.execute("CREATE TRIGGER fail_credential BEFORE INSERT ON credentials BEGIN SELECT RAISE(ABORT, 'forced failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
        accounts.accept_invite(token, "member@example.test", "Member", PASSWORD, path=path)
    for table in ("users", "local_password_credentials", "account_access", "apps", "credentials"):
        assert rows(path, f"SELECT * FROM {table}") == []
    assert accounts.inspect_invite(token, path=path)["used_at"] is None
    assert rows(path, "SELECT * FROM audit_events WHERE action='account.invite.accept'") == []


def test_targeted_invite_cannot_be_claimed_by_other_email(account_db, fast_password_work):
    path, plan = account_db
    _, token = accounts.create_link("invite", "owner", profile_id=plan["id"], email=" Intended@Example.Test ", path=path)
    expect_error("invite_email_mismatch", lambda: accounts.accept_invite(token, "other@example.test", "Other", PASSWORD, path=path))
    assert rows(path, "SELECT * FROM users") == []
    assert accounts.inspect_invite(token, path=path)["email"] == "intended@example.test"


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_expired_and_revoked_invites_fail_before_password_work(account_db, monkeypatch, state):
    path, plan = account_db
    link, token = accounts.create_link("invite", "owner", profile_id=plan["id"], path=path)
    if state == "revoked":
        accounts.revoke_invite(link["id"], "owner", path=path)
    else:
        with accounts.connection(path) as conn:
            conn.execute("UPDATE account_links SET expires_at=0 WHERE id=?", (link["id"],))
    monkeypatch.setattr(accounts, "hash_password", lambda *args: pytest.fail("unavailable invite reached KDF"))
    expect_error("link_unavailable", lambda: accounts.accept_invite(token, "member@example.test", "Member", PASSWORD, path=path))


def test_throttle_persists_email_bucket_across_ips_and_connection_reopen(account_db):
    path, _ = account_db
    for index in range(10):
        accounts.throttle(f"ip-{index}", " MEMBER@EXAMPLE.TEST ", path=path, now=1000)
    assert expect_error("login_rate_limited", lambda: accounts.throttle("new-ip", "member@example.test", path=path, now=1001)).status == 429
    accounts.initialize(path)
    expect_error("login_rate_limited", lambda: accounts.throttle("another-ip", "member@example.test", path=path, now=1002))
    assert "member@example.test" not in str(rows(path, "SELECT * FROM account_login_limits"))
    accounts.throttle("new-ip", "member@example.test", path=path, now=1900)


def test_throttle_persists_ip_bucket_across_email_addresses(account_db):
    path, _ = account_db
    for index in range(60):
        accounts.throttle("same-ip", f"member-{index}@example.test", path=path, now=1000)
    expect_error("login_rate_limited", lambda: accounts.throttle("same-ip", "fresh@example.test", path=path, now=1001))


def test_plan_change_updates_every_app_and_existing_key(account_db, fast_password_work):
    path, plan = account_db
    user, first_app, _ = invited_member(account_db)
    second_app = admin_store.create_app("Second", kind="personal", owner_user_id=user["id"], default_profile_id=plan["id"], path=path)
    for index, app in enumerate((first_app, second_app)):
        admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=f"hash-{index}", prefix="test", profile_id=plan["id"], path=path)
    new = admin_store.create_profile("Restricted", {"allow": {"model_groups": []}}, path=path)
    accounts.change_person(user["id"], "owner", profile_id=new["id"], path=path)
    assert accounts.access(user["id"], path=path)["profile_id"] == new["id"]
    assert {row["default_profile_id"] for row in rows(path, "SELECT * FROM apps")} == {new["id"]}
    assert {row["profile_id"] for row in rows(path, "SELECT * FROM credentials")} == {new["id"]}
    assert accounts.profile_for_user(user["id"], path=path) == new["id"]


@pytest.mark.parametrize("remove", [False, True])
def test_disable_and_remove_revoke_apps_keys_sessions_and_pending_resets(account_db, fast_password_work, remove):
    path, _ = account_db
    user, _, _ = invited_member(account_db)
    admin_store.create_credential_hash(subject_kind="user", subject_id=user["id"], hash="control-key", prefix="test", path=path)
    accounts.register_session(user["id"], "old-session", int(time.time()) + 3600, path=path)
    _, reset = accounts.create_link("reset", "owner", user_id=user["id"], path=path)
    accounts.change_person(user["id"], "owner", disabled=True, remove=remove, path=path)
    assert not accounts.session_allowed(user["id"], "old-session", path=path)
    assert rows(path, "SELECT * FROM account_sessions") == []
    assert all(row["revoked_at"] is not None for row in rows(path, "SELECT * FROM credentials"))
    assert all(row["disabled_at"] is not None for row in rows(path, "SELECT * FROM apps"))
    expect_error("link_unavailable", lambda: accounts.reset_password(reset, PASSWORD, path=path))
    expect_error("invalid_login", lambda: accounts.login(user["email"], PASSWORD, "ip", path=path))
    if remove:
        assert user["id"] not in {person["id"] for person in accounts.people(path=path)}
        expect_error("person_not_found", lambda: accounts.change_person(user["id"], "owner", disabled=False, path=path))
        assert rows(path, "SELECT * FROM local_password_credentials") == []
    else:
        accounts.change_person(user["id"], "owner", disabled=False, path=path)
        assert all(row["revoked_at"] is not None for row in rows(path, "SELECT * FROM credentials"))
        assert not accounts.session_allowed(user["id"], "old-session", path=path)
        accounts.login(user["email"], PASSWORD, "new-ip", path=path)
        accounts.register_session(user["id"], "new-session", int(time.time()) + 3600, path=path)
        assert accounts.session_allowed(user["id"], "new-session", path=path)


def test_password_reset_is_single_use_revokes_earlier_reset_and_all_old_sessions(account_db, fast_password_work):
    path, _ = account_db
    user, _, _ = invited_member(account_db)
    accounts.register_session(user["id"], "old-session", int(time.time()) + 3600, path=path)
    _, first = accounts.create_link("reset", "owner", user_id=user["id"], path=path)
    _, second = accounts.create_link("reset", "owner", user_id=user["id"], path=path)
    expect_error("link_unavailable", lambda: accounts.reset_password(first, PASSWORD, path=path))
    new_password = "a completely different password"
    accounts.reset_password(second, new_password, path=path)
    expect_error("link_unavailable", lambda: accounts.reset_password(second, PASSWORD, path=path))
    assert not accounts.session_allowed(user["id"], "old-session", path=path)
    expect_error("invalid_login", lambda: accounts.login(user["email"], PASSWORD, "ip", path=path))
    assert accounts.login(user["email"], new_password, "ip", path=path)["id"] == user["id"]


@pytest.fixture
def account_client(account_db):
    path, _ = account_db
    cookie = CookieConfig(secure=False)
    state = {"session": None, "oidc_enabled": True, "signing_key": SIGNING_KEY}

    def auth(request):
        role = request.headers.get("x-test-role")
        if not role:
            raise AdminAuthError("sign in", status_code=401, code="login_required")
        return AdminContext(subject="owner", roles=frozenset({role}), auth_method="session", csrf_token="owner-csrf")

    app = FastAPI()
    app.include_router(accounts_api.create_account_router(
        path_provider=lambda: path, auth=auth,
        session_reader=lambda request: state["session"],
        cookie_provider=lambda: cookie, signing_key_provider=lambda: state["signing_key"],
        oidc_provider=lambda: {"enabled": state["oidc_enabled"], "label": "Test identity"},
    ))
    with TestClient(app) as client:
        yield client, state


OWNER_HEADERS = {"x-test-role": "owner", "x-azizam-csrf": "owner-csrf"}


@pytest.mark.parametrize("endpoint", ["/admin/api/people", "/admin/api/invites", "/admin/api/operator/sign-in"])
def test_people_invites_and_signin_settings_are_owner_only(account_client, endpoint):
    client, _ = account_client
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers={"x-test-role": "none"}).status_code == 403
    assert client.get(endpoint, headers={"x-test-role": "admin"}).status_code == 403
    assert client.get(endpoint, headers=OWNER_HEADERS).status_code == 200


@pytest.mark.parametrize("method,endpoint,payload", [
    ("POST", "/admin/api/invites", {"profile_id": "unused"}),
    ("DELETE", "/admin/api/invites/unused", None),
    ("PATCH", "/admin/api/people/unused", {"disabled": True}),
    ("DELETE", "/admin/api/people/unused", None),
    ("POST", "/admin/api/people/unused/reset", {}),
    ("PUT", "/admin/api/operator/sign-in", {"mode": "both"}),
])
def test_owner_mutations_require_csrf_before_accessing_records(account_client, method, endpoint, payload):
    client, _ = account_client
    response = client.request(method, endpoint, json=payload, headers={"x-test-role": "owner"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "admin_csrf_required"
    assert response.headers["cache-control"] == "no-store"


def test_owner_invite_api_create_inspect_revoke_and_no_secret_list(account_db, account_client):
    _, plan = account_db
    client, _ = account_client
    response = client.post("/admin/api/invites", headers=OWNER_HEADERS, json={"profile_id": plan["id"], "email": "person@example.test", "expires_in": 60})
    assert response.status_code == 200
    body = response.json()
    token = body["url"].split("token=", 1)[1]
    public = client.get("/auth/api/invite", params={"token": token})
    assert public.status_code == 200
    assert public.json()["email"] == "person@example.test"
    assert public.json()["plan"]["id"] == plan["id"]
    listing = client.get("/admin/api/invites", headers=OWNER_HEADERS)
    assert token not in listing.text
    assert "token_hash" not in listing.text
    assert client.delete(f'/admin/api/invites/{body["invite"]["id"]}', headers=OWNER_HEADERS).status_code == 200
    assert client.get("/auth/api/invite", params={"token": token}).status_code == 404


def test_builtin_invite_login_session_cookie_and_no_store(account_db, account_client, fast_password_work):
    path, plan = account_db
    client, _ = account_client
    _, token = accounts.create_link("invite", "owner", profile_id=plan["id"], path=path)
    accepted = client.post("/auth/api/invite/accept", json={"token": token, "email": "person@example.test", "name": "Person", "password": PASSWORD})
    assert accepted.status_code == 200
    assert accepted.json()["next"] == "/ui/"
    assert accepted.json()["credential"]["api_key"]
    assert "httponly" in accepted.headers["set-cookie"].lower()
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.headers["referrer-policy"] == "no-referrer"
    login = client.post("/auth/api/login", json={"email": "person@example.test", "password": PASSWORD})
    assert login.status_code == 200
    assert "set-cookie" in login.headers
    assert len(rows(path, "SELECT * FROM account_sessions")) == 2


def test_api_login_rejects_other_origin_and_reports_persistent_throttle(account_db, account_client, fast_password_work):
    path, _ = account_db
    client, _ = account_client
    payload = {"email": "missing@example.test", "password": PASSWORD}
    assert client.post("/auth/api/login", json=payload, headers={"origin": "https://other.example"}).status_code == 403
    for _ in range(10):
        assert client.post("/auth/api/login", json=payload).status_code == 401
    limited = client.post("/auth/api/login", json=payload)
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "900"
    assert limited.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("mode,builtin,oidc", [("built-in", True, False), ("oidc", False, True), ("both", True, True)])
def test_signin_mode_controls_public_options_and_builtin_routes(account_db, account_client, mode, builtin, oidc, fast_password_work):
    path, plan = account_db
    client, _ = account_client
    set_mode = client.put("/admin/api/operator/sign-in", json={"mode": mode}, headers=OWNER_HEADERS)
    assert set_mode.status_code == 200
    options = client.get("/auth/api/options").json()
    assert options["mode"] == mode
    assert options["built_in"] is builtin
    assert options["oidc"] is oidc
    response = client.post("/auth/api/login", json={"email": "missing@example.test", "password": PASSWORD})
    assert response.status_code == (401 if builtin else 403)
    _, token = accounts.create_link("invite", "owner", profile_id=plan["id"], path=path)
    accepted = client.post("/auth/api/invite/accept", json={"token": token, "email": "person@example.test", "name": "Person", "password": PASSWORD})
    assert accepted.status_code == (200 if builtin else 403)


def test_oidc_mode_requires_configured_provider_and_invalid_settings_fail(account_client):
    client, state = account_client
    state["oidc_enabled"] = False
    for mode in ("oidc", "both"):
        assert client.put("/admin/api/operator/sign-in", json={"mode": mode}, headers=OWNER_HEADERS).status_code == 409
    assert client.put("/admin/api/operator/sign-in", json={"mode": "built-in"}, headers=OWNER_HEADERS).status_code == 200
    assert client.put("/admin/api/operator/sign-in", json={"mode": "anything"}, headers=OWNER_HEADERS).status_code == 422
    assert client.post("/admin/api/invites", json={"profile_id": "unused", "expires_in": True}, headers=OWNER_HEADERS).status_code == 422
    assert client.patch("/admin/api/people/unused", json={"disabled": "false"}, headers=OWNER_HEADERS).status_code == 422


def test_oidc_targeted_invite_requires_session_csrf_verified_email_and_matching_identity(account_db, account_client, monkeypatch):
    path, plan = account_db
    client, state = account_client
    user = admin_store.upsert_oidc_user("https://identity.example", "subject-one", email="person@example.test", path=path)
    _, token = accounts.create_link("invite", "owner", profile_id=plan["id"], email=user["email"], path=path)
    payload = {"token": token, "email": user["email"], "name": "Person"}
    assert client.post("/auth/api/invite/accept", json=payload).status_code == 401
    session = session_from_claims({"sub": user["id"], "email": user["email"]}, role="none", cookie=CookieConfig(secure=False))
    state["session"] = session
    assert client.post("/auth/api/invite/accept", json=payload).status_code == 403
    csrf = {"x-azizam-csrf": csrf_token_for_session(session, signing_key=SIGNING_KEY)}
    response = client.post("/auth/api/invite/accept", json=payload, headers=csrf)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "verified_email_required"
    # Verification is persisted as an account fact populated by the validated
    # OIDC callback, rather than trusting a request's email or verification flag.
    accounts.record_oidc_verification(user["id"], True, path=path)
    wrong = client.post("/auth/api/invite/accept", json={**payload, "email": "other@example.test"}, headers=csrf)
    assert wrong.status_code == 403
    monkeypatch.setattr(accounts, "hash_password", lambda *args: pytest.fail("OIDC binding must not create password"))
    accepted = client.post("/auth/api/invite/accept", json=payload, headers=csrf)
    assert accepted.status_code == 200
    assert rows(path, "SELECT * FROM local_password_credentials") == []
    assert rows(path, "SELECT * FROM account_access")[0]["user_id"] == user["id"]
    assert client.post("/auth/api/invite/accept", json=payload, headers=csrf).status_code == 404


def test_owner_and_admin_accounts_are_protected_from_people_actions(account_db):
    path, _ = account_db
    for role in ("owner", "admin", "superadmin"):
        user = admin_store.upsert_oidc_user("https://identity.example", role, email=f"{role}@example.test", path=path)
        admin_store.assign_admin_role(role=role, subject_kind="user", subject_id=user["id"], path=path)
        expect_error("protected_person", lambda: accounts.change_person(user["id"], "owner", disabled=True, path=path))
        expect_error("protected_person", lambda: accounts.change_person(user["id"], "owner", remove=True, path=path))


def test_proxy_rejects_revoked_session_for_portal_and_browser_before_and_after_reenable(account_db, fast_password_work, monkeypatch):
    import proxy
    from gateway.admin_oidc import encode_session_cookie
    from gateway.admin_runtime import AdminSQLiteStore

    path, _ = account_db
    user, _, _ = invited_member(account_db)
    cookie = CookieConfig(secure=False)
    session = session_from_claims({"sub": user["id"], "email": user["email"]}, role="none", cookie=cookie)
    accounts.register_session(user["id"], session.session_id, int(session.expires_at), path=path)
    monkeypatch.setattr(proxy, "ADMIN_STORE", AdminSQLiteStore(path))
    monkeypatch.setattr(proxy, "_browser_admin_session_signing_key", lambda: SIGNING_KEY)
    monkeypatch.setattr(proxy, "_browser_admin_cookie_config", lambda: cookie)
    raw = encode_session_cookie(session, signing_key=SIGNING_KEY)
    client = TestClient(proxy.app)
    client.cookies.set(cookie.name, raw)
    assert client.get("/me/api/session").status_code == 200
    accounts.change_person(user["id"], "owner", disabled=True, path=path)
    denied = client.get("/me/api/session")
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "session_revoked"
    assert "max-age=0" in denied.headers["set-cookie"].lower()
    accounts.change_person(user["id"], "owner", disabled=False, path=path)
    client.cookies.set(cookie.name, raw)
    assert client.get("/me/api/session").status_code == 401
    client.cookies.set(cookie.name, raw)
    redirected = client.get("/ui/", follow_redirects=False)
    assert redirected.status_code == 303
    assert redirected.headers["location"] == "/ui/sign-in/"
    client.cookies.set(cookie.name, raw)
    assert client.get("/auth/api/options").status_code == 200


def test_invite_acceptance_without_session_signing_key_does_not_consume_link_or_create_account(account_db, account_client, fast_password_work):
    path, plan = account_db
    client, state = account_client
    _, token = accounts.create_link("invite", "owner", profile_id=plan["id"], path=path)
    state["signing_key"] = None
    response = client.post("/auth/api/invite/accept", json={"token": token, "email": "member@example.test", "name": "Member", "password": PASSWORD})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "sessions_unavailable"
    assert accounts.inspect_invite(token, path=path)["used_at"] is None
    for table in ("users", "account_access", "apps", "credentials"):
        assert rows(path, f"SELECT * FROM {table}") == []


def test_proxy_http_full_member_lifecycle_from_owner_invitation_through_reset_and_removal(account_db, fast_password_work, monkeypatch):
    import proxy
    from gateway.admin_oidc import encode_session_cookie
    from gateway.admin_runtime import AdminSQLiteStore

    path, plan = account_db
    owner = admin_store.upsert_oidc_user("https://identity.example", "owner", email="owner@example.test", path=path)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=owner["id"], path=path)
    cookie = CookieConfig(secure=False)
    owner_session = session_from_claims({"sub": owner["id"], "email": owner["email"]}, role="owner", cookie=cookie)
    store = AdminSQLiteStore(path)
    monkeypatch.setattr(proxy, "ADMIN_STORE", store)
    # The existing routers close over this authenticator instance. Changing its
    # dependencies preserves the real HTTP authentication and CSRF behavior.
    monkeypatch.setattr(proxy.ADMIN_AUTH, "store", store)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", SIGNING_KEY)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "cookie", cookie)
    monkeypatch.setattr(proxy, "_browser_admin_session_signing_key", lambda: SIGNING_KEY)
    monkeypatch.setattr(proxy, "_browser_admin_cookie_config", lambda: cookie)
    admin = TestClient(proxy.app)
    member = TestClient(proxy.app)
    admin.cookies.set(cookie.name, encode_session_cookie(owner_session, signing_key=SIGNING_KEY))
    owner_csrf = {"x-azizam-csrf": csrf_token_for_session(owner_session, signing_key=SIGNING_KEY)}
    invitation = admin.post("/admin/api/invites", headers=owner_csrf, json={"profile_id": plan["id"], "email": "member@example.test"})
    assert invitation.status_code == 200
    token = invitation.json()["url"].split("token=", 1)[1]
    assert member.get("/auth/api/invite", params={"token": token}).status_code == 200
    accepted = member.post("/auth/api/invite/accept", json={"token": token, "email": "member@example.test", "name": "Member", "password": PASSWORD})
    assert accepted.status_code == 200
    first_key = accepted.json()["credential"]["id"]
    snapshot = member.get("/me/api/session").json()
    assert snapshot["authenticated"] is True
    user_id = snapshot["user"]["id"]
    assert snapshot["csrf_token"]
    assert snapshot["apps"][0]["default_profile_id"] == plan["id"]
    assert member.get("/admin/api/people").status_code == 403
    member.cookies.clear()
    logged_in = member.post("/auth/api/login", json={"email": "member@example.test", "password": PASSWORD})
    assert logged_in.status_code == 200
    old_cookie = member.cookies.get(cookie.name)
    changed_plan = admin_store.create_profile("Changed plan", {"allow": {"model_groups": []}}, path=path)
    changed = admin.patch(f"/admin/api/people/{user_id}", headers=owner_csrf, json={"profile_id": changed_plan["id"]})
    assert changed.status_code == 200
    assert member.get("/me/api/session").json()["apps"][0]["default_profile_id"] == changed_plan["id"]
    assert rows(path, "SELECT profile_id FROM credentials WHERE id=?", (first_key,))[0]["profile_id"] == changed_plan["id"]
    assert admin.patch(f"/admin/api/people/{user_id}", headers=owner_csrf, json={"disabled": True}).status_code == 200
    assert member.get("/me/api/session").status_code == 401
    assert member.post("/auth/api/login", json={"email": "member@example.test", "password": PASSWORD}).status_code == 401
    assert admin.patch(f"/admin/api/people/{user_id}", headers=owner_csrf, json={"disabled": False}).status_code == 200
    member.cookies.set(cookie.name, old_cookie, domain="testserver.local", path="/")
    assert member.get("/me/api/session").status_code == 401
    assert rows(path, "SELECT revoked_at FROM credentials WHERE id=?", (first_key,))[0]["revoked_at"] is not None
    assert member.post("/auth/api/login", json={"email": "member@example.test", "password": PASSWORD}).status_code == 200
    reset = admin.post(f"/admin/api/people/{user_id}/reset", json={}, headers=owner_csrf)
    assert reset.status_code == 200
    reset_token = reset.json()["url"].split("reset=", 1)[1]
    pre_reset_cookie = member.cookies.get(cookie.name)
    new_password = "a replacement member password"
    assert member.post("/auth/api/password-reset", json={"token": reset_token, "password": new_password}).status_code == 200
    member.cookies.set(cookie.name, pre_reset_cookie, domain="testserver.local", path="/")
    assert member.get("/me/api/session").status_code == 401
    assert member.post("/auth/api/login", json={"email": "member@example.test", "password": new_password}).status_code == 200
    assert admin.delete(f"/admin/api/people/{user_id}", headers=owner_csrf).status_code == 200
    assert member.get("/me/api/session").status_code == 401
    assert user_id not in {person["id"] for person in admin.get("/admin/api/people").json()["people"]}


def test_oidc_users_with_same_email_in_different_issuers_do_not_share_account_plans(account_db, monkeypatch):
    import proxy
    from gateway.admin_runtime import AdminSQLiteStore

    path, plan = account_db
    first = admin_store.upsert_oidc_user("https://first-identity.example", "shared-subject", email="shared@example.test", path=path)
    second = admin_store.upsert_oidc_user("https://second-identity.example", "shared-subject", email="shared@example.test", path=path)
    _, token = accounts.create_link("invite", "owner", profile_id=plan["id"], email=first["email"], path=path)
    accounts.accept_invite(token, first["email"], "First", None, oidc_user_id=first["id"], path=path)
    monkeypatch.setattr(proxy, "ADMIN_STORE", AdminSQLiteStore(path))
    monkeypatch.setattr(proxy, "_default_profile_id_for_user", lambda **kwargs: "unassigned-fallback")
    sessions = [session_from_claims({"sub": user["id"], "email": user["email"]}, role="none", cookie=CookieConfig()) for user in (first, second)]
    assert accounts.profile_for_user(first["id"], path=path) == plan["id"]
    assert accounts.profile_for_user(second["id"], path=path) is None
    assert proxy._default_profile_id_for_session(sessions[0]) == plan["id"]
    assert proxy._default_profile_id_for_session(sessions[1]) == "unassigned-fallback"
    second_plan = admin_store.create_profile("Second issuer plan", {}, path=path)
    _, second_token = accounts.create_link("invite", "owner", profile_id=second_plan["id"], email=second["email"], path=path)
    accounts.accept_invite(second_token, second["email"], "Second", None, oidc_user_id=second["id"], path=path)
    assert proxy._default_profile_id_for_session(sessions[0]) == plan["id"]
    assert proxy._default_profile_id_for_session(sessions[1]) == second_plan["id"]


def test_owner_can_copy_pending_invite_after_listing_and_router_reload_without_persisting_plaintext(account_db, account_client):
    path, plan = account_db
    client, state = account_client
    created = client.post("/admin/api/invites", headers=OWNER_HEADERS, json={"profile_id": plan["id"]})
    assert created.status_code == 200
    url = created.json()["url"]
    token = url.split("token=", 1)[1]
    listed = client.get("/admin/api/invites", headers=OWNER_HEADERS).json()["invites"]
    link_id = listed[0]["id"]
    assert token not in str(listed)
    assert "token_hash" not in str(listed)
    accounts.initialize(path)
    with TestClient(client.app) as reloaded:
        copied = reloaded.get(f"/admin/api/invites/{link_id}/link", headers=OWNER_HEADERS)
        assert copied.status_code == 200
        assert copied.json()["url"] == url
        assert copied.headers["cache-control"] == "no-store"
        assert reloaded.get(f"/admin/api/invites/{link_id}/link").status_code == 401
        for role in ("none", "admin"):
            assert reloaded.get(f"/admin/api/invites/{link_id}/link", headers={"x-test-role": role}).status_code == 403
    stored = rows(path, "SELECT * FROM account_links WHERE id=?", (link_id,))[0]
    assert stored["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(stored)
    assert token not in str(rows(path, "SELECT * FROM audit_events"))
    assert token not in str(state)
    assert len(rows(path, "SELECT * FROM audit_events WHERE action='account.invite.copy'")) == 1


@pytest.mark.parametrize("state", ["expired", "revoked", "used"])
def test_owner_cannot_copy_unavailable_invites(account_db, account_client, fast_password_work, state):
    path, plan = account_db
    client, _ = account_client
    created = client.post("/admin/api/invites", headers=OWNER_HEADERS, json={"profile_id": plan["id"]}).json()
    link_id = created["invite"]["id"]
    token = created["url"].split("token=", 1)[1]
    if state == "expired":
        with accounts.connection(path) as conn:
            conn.execute("UPDATE account_links SET expires_at=0 WHERE id=?", (link_id,))
    elif state == "revoked":
        assert client.delete(f"/admin/api/invites/{link_id}", headers=OWNER_HEADERS).status_code == 200
    else:
        accounts.accept_invite(token, "person@example.test", "Person", PASSWORD, path=path)
    copied = client.get(f"/admin/api/invites/{link_id}/link", headers=OWNER_HEADERS)
    assert copied.status_code == 404
    assert "url" not in copied.json()
    assert rows(path, "SELECT * FROM audit_events WHERE action='account.invite.copy'") == []


def test_oidc_invite_binding_reassigns_all_preexisting_apps_and_keys_to_invited_plan(account_db, account_client):
    path, plan = account_db
    client, state = account_client
    user = admin_store.upsert_oidc_user("https://identity.example", "existing-subject", email="person@example.test", path=path)
    legacy_plan = admin_store.create_profile("Legacy OIDC plan", {}, path=path)
    for index in range(2):
        app = admin_store.create_app(f"Existing app {index}", kind="personal", owner_user_id=user["id"], default_profile_id=legacy_plan["id"], path=path)
        admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=f"preexisting-{index}", prefix="test", profile_id=legacy_plan["id"], path=path)
    accounts.record_oidc_verification(user["id"], True, path=path)
    session = session_from_claims({"sub": user["id"], "email": user["email"]}, role="none", cookie=CookieConfig(secure=False))
    state["session"] = session
    _, token = accounts.create_link("invite", "owner", profile_id=plan["id"], email=user["email"], path=path)
    response = client.post("/auth/api/invite/accept", json={"token": token, "email": user["email"], "name": "Person"}, headers={"x-azizam-csrf": csrf_token_for_session(session, signing_key=SIGNING_KEY)})
    assert response.status_code == 200
    apps = rows(path, "SELECT * FROM apps WHERE owner_user_id=?", (user["id"],))
    keys = rows(path, "SELECT * FROM credentials")
    assert len(apps) == 3
    assert len(keys) == 3
    assert {app["default_profile_id"] for app in apps} == {plan["id"]}
    assert {key["profile_id"] for key in keys} == {plan["id"]}
    assert accounts.profile_for_user(user["id"], path=path) == plan["id"]


def test_plan_with_pending_invite_cannot_delete_until_revoked(account_db):
    path, plan = account_db
    invite, _ = accounts.create_link("invite", "owner", profile_id=plan["id"], path=path)
    with pytest.raises(ValueError, match="pending invitations"):
        admin_store.delete_profile(plan["id"], path=path)
    accounts.revoke_invite(invite["id"], "owner", path=path)
    assert admin_store.delete_profile(plan["id"], path=path)["id"] == plan["id"]
    assert accounts.list_invites(path=path)[0]["profile_id"] is None


def test_people_excludes_key_claim_placeholders_and_reports_protected_oidc_users(account_db):
    from gateway.admin_runtime import LOCAL_ADMIN_KEY_ISSUER
    path, _ = account_db
    admin_store.upsert_oidc_user(LOCAL_ADMIN_KEY_ISSUER, "claim-placeholder", path=path)
    user = admin_store.upsert_oidc_user("https://identity.example", "real-owner", email="owner@example.test", path=path)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=user["id"], path=path)
    result = accounts.people(path=path)
    assert len(result) == 1
    assert result[0]["id"] == user["id"]
    assert result[0]["protected"] is True
    assert result[0]["builtin"] is False


@pytest.mark.parametrize("target", ["/ui/", "/ui/invite/", "/ui/sign-in/"])
def test_oidc_returns_to_the_product_account_page(target):
    import proxy
    assert proxy._safe_admin_return_to(target) == target
    assert proxy._safe_admin_return_to("//evil.example" + target) == "/ui/home/"


def test_account_connection_closes_and_rolls_back_failed_work(account_db):
    path, _ = account_db
    with accounts.connection(path) as conn:
        conn.execute("INSERT INTO account_login_limits VALUES('temporary',1,1)")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        with accounts.connection(path) as failed:
            failed.execute("INSERT INTO account_login_limits VALUES('rolled-back',1,1)")
            raise RuntimeError("transaction failed")
    assert rows(path, "SELECT bucket FROM account_login_limits") == [{"bucket": "temporary"}]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed.execute("SELECT 1")


def test_public_signin_options_and_invite_inspection_use_saved_instance_name_live(account_db, account_client):
    path, plan = account_db
    client, _ = account_client
    _, token = accounts.create_link("invite", "owner", profile_id=plan["id"], path=path)

    def assert_public_name(name):
        options = client.get("/auth/api/options")
        invite = client.get("/auth/api/invite", params={"token": token})
        assert options.status_code == invite.status_code == 200
        assert options.json()["instance_name"] == name
        assert invite.json()["instance_name"] == name
        assert options.headers["cache-control"] == "no-store"
        assert invite.headers["cache-control"] == "no-store"

    assert_public_name("Azizam")
    for name in ("Our team's gateway", "Renamed gateway"):
        admin_store.put_operator_setting("instance_presentation", {
            "name": name, "base_url": "https://gateway.example.test", "theme_default": "dark",
        }, path=path)
        assert_public_name(name)


@pytest.mark.parametrize("target,expected", [
    ("/ui/home/", "/ui/home/"),
    ("/ui/usage/?receipt=abc#details", "/ui/usage/?receipt=abc#details"),
    ("/admin/services?service=tts", "/ui/engines/?service=tts"),
    ("/ui/admin/profiles/?plan=family", "/ui/people/?tab=plans&plan=family"),
    ("/me?tab=keys", "/ui/home/?tab=keys"),
    ("https://evil.example", "/ui/home/"), ("//evil.example", "/ui/home/"), (None, "/ui/home/"),
])
def test_builtin_login_uses_safe_product_return_path(account_db, account_client, fast_password_work, target, expected):
    from urllib.parse import parse_qs, urlsplit
    client, _ = account_client
    user, _, _ = invited_member(account_db)
    options = client.get("/auth/api/options", params={"return_to": target} if target else {}).json()
    assert parse_qs(urlsplit(options["oidc_url"]).query)["return_to"] == [expected]
    response = client.post("/auth/api/login", json={"email": user["email"], "password": PASSWORD, "return_to": target})
    assert response.status_code == 200
    assert response.json()["next"] == expected
