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
    assert accounts.profile_for_email(user["email"], path=path) == new["id"]


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
    state = {"session": None, "oidc_enabled": True}

    def auth(request):
        role = request.headers.get("x-test-role")
        if not role:
            raise AdminAuthError("sign in", status_code=401, code="login_required")
        return AdminContext(subject="owner", roles=frozenset({role}), auth_method="session", csrf_token="owner-csrf")

    app = FastAPI()
    app.include_router(accounts_api.create_account_router(
        path_provider=lambda: path, auth=auth,
        session_reader=lambda request: state["session"],
        cookie_provider=lambda: cookie, signing_key_provider=lambda: SIGNING_KEY,
        oidc_provider=lambda: {"enabled": state["oidc_enabled"], "label": "Test identity"},
    ))
    with TestClient(app) as client:
        yield client, state


OWNER_HEADERS = {"x-test-role": "owner", "x-ai-gateway-csrf": "owner-csrf"}


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
    csrf = {"x-ai-gateway-csrf": csrf_token_for_session(session, signing_key=SIGNING_KEY)}
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
