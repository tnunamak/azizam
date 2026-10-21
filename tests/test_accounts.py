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
from tests.support.access import store_plan

PASSWORD = "a sufficiently long password"
SIGNING_KEY = b"account-test-key-32-bytes-long!!!"


@pytest.fixture
def account_db(tmp_path):
    path = tmp_path / "accounts.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    plan = store_plan("Member plan", path=path, models=["test-model"])
    return path, plan


@pytest.fixture
def fast_password_work(monkeypatch):
    # Production work parameters are independently checked below. Lifecycle tests
    # exercise the real KDF with smaller cost to keep race tests practical.
    monkeypatch.setattr(accounts, "_SCRYPT_N", 2**10)


def invited_member(account_db, email="member@example.test"):
    path, plan = account_db
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email=email, path=path)
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


def test_short_passwords_are_accepted(monkeypatch):
    monkeypatch.setattr(accounts, "_SCRYPT_N", 2 ** 10)
    for password in ("a", "short", "x" * 11, "x" * 1024):
        assert accounts.verify_password(password, accounts.hash_password(password))


@pytest.mark.parametrize("password", [None, "", "x" * 1025, 123])
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
    link, token = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
    assert "token_hash" not in link
    assert token not in str(accounts.list_invites(path=path))
    user, app, credential = accounts.accept_invite(token, "Person@Example.Test", " Person ", PASSWORD, path=path)
    assert user["email"] == "person@example.test"
    assert user["display_name"] == "Person"
    assert app["owner_user_id"] == user["id"]
    assert app["access_plan"] == "person"
    assert accounts.plan_for_user(user["id"], path=path) == plan["id"]
    saved = rows(path, "SELECT * FROM credentials WHERE id=?", (credential["id"],))[0]
    assert saved["created_by"] == user["id"]
    assert saved["hash"] == hash_api_key(credential["api_key"])
    assert credential["api_key"] not in str(saved)
    assert rows(path, "SELECT token_hash FROM account_links")[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    expect_error("link_unavailable", lambda: accounts.inspect_invite(token, path=path))
    expect_error("link_unavailable", lambda: accounts.accept_invite(token, user["email"], "Person", PASSWORD, path=path))


def test_invite_consumption_is_atomic_under_race(account_db, fast_password_work, monkeypatch):
    path, plan = account_db
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
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
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
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
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email=" Intended@Example.Test ", path=path)
    expect_error("invite_email_mismatch", lambda: accounts.accept_invite(token, "other@example.test", "Other", PASSWORD, path=path))
    assert rows(path, "SELECT * FROM users") == []
    assert accounts.inspect_invite(token, path=path)["email"] == "intended@example.test"


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_expired_and_revoked_invites_fail_before_password_work(account_db, monkeypatch, state):
    path, plan = account_db
    link, token = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
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


def test_plan_change_applies_to_every_app_that_uses_the_persons_plan(account_db, fast_password_work):
    path, plan = account_db
    user, first_app, _ = invited_member(account_db)
    second_app = admin_store.create_app("Second", kind="personal", owner_user_id=user["id"], access_plan="person", path=path)
    pinned = admin_store.create_app("Pinned", kind="personal", owner_user_id=user["id"], access_plan=plan["id"], path=path)
    new = store_plan("Restricted", path=path, models=[])
    accounts.change_person(user["id"], "owner", plan_id=new["id"], path=path)
    assert accounts.access(user["id"], path=path)["plan_id"] == new["id"]
    assert accounts.plan_for_user(user["id"], path=path) == new["id"]
    assigned = {row["id"]: row["access_plan"] for row in rows(path, "SELECT * FROM apps")}
    assert assigned[first_app["id"]] == assigned[second_app["id"]] == "person"
    assert assigned[pinned["id"]] == plan["id"]


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
    assert all((row["disabled_at"] is not None) == remove for row in rows(path, "SELECT * FROM apps"))
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
        return AdminContext(subject="owner", roles=frozenset({role}), auth_method="session", csrf_token="owner-csrf",
                            actor_kind=request.headers.get("x-test-actor-kind", "user"),
                            actor_id=request.headers.get("x-test-actor-id"),
                            actor_label=request.headers.get("x-test-actor-label"))

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


@pytest.mark.parametrize("endpoint", ["/admin/api/people", "/admin/api/invites"])
def test_people_and_invites_are_owner_only(account_client, endpoint):
    client, _ = account_client
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers={"x-test-role": "none"}).status_code == 403
    assert client.get(endpoint, headers={"x-test-role": "admin"}).status_code == 403
    assert client.get(endpoint, headers=OWNER_HEADERS).status_code == 200


@pytest.mark.parametrize("method,endpoint,payload", [
    ("POST", "/admin/api/invites", {"access_plan": "unused"}),
    ("DELETE", "/admin/api/invites/unused", None),
    ("PATCH", "/admin/api/people/unused", {"disabled": True}),
    ("DELETE", "/admin/api/people/unused", None),
    ("POST", "/admin/api/people/unused/reset", {}),
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
    response = client.post("/admin/api/invites", headers=OWNER_HEADERS, json={"access_plan": plan["id"], "email": "person@example.test", "expires_in": 60})
    assert response.status_code == 200
    body = response.json()
    token = body["url"].split("token=", 1)[1]
    public = client.post("/auth/api/invite", json={"token": token})
    assert public.status_code == 200
    assert public.json()["email"] == "person@example.test"
    assert public.json()["plan"]["id"] == plan["id"]
    listing = client.get("/admin/api/invites", headers=OWNER_HEADERS)
    assert token not in listing.text
    assert "token_hash" not in listing.text
    assert client.delete(f'/admin/api/invites/{body["invite"]["id"]}', headers=OWNER_HEADERS).status_code == 200
    assert client.post("/auth/api/invite", json={"token": token}).status_code == 404


def test_builtin_invite_login_session_cookie_and_no_store(account_db, account_client, fast_password_work):
    path, plan = account_db
    client, _ = account_client
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
    accepted = client.post("/auth/api/invite/accept", json={"token": token, "email": "person@example.test", "name": "Person", "password": PASSWORD})
    assert accepted.status_code == 200
    assert accepted.json()["next"] == "/"
    assert accepted.json()["credential"]["api_key"]
    assert "httponly" in accepted.headers["set-cookie"].lower()
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.headers["referrer-policy"] == "no-referrer"
    login = client.post("/auth/api/login", json={"email": "person@example.test", "password": PASSWORD})
    assert login.status_code == 200
    assert "set-cookie" in login.headers
    assert len(rows(path, "SELECT * FROM account_sessions")) == 2


@pytest.mark.parametrize("password", ["", "x" * 1025])
def test_invite_accept_rejects_password_outside_boundary(account_db, account_client, fast_password_work, password):
    path, plan = account_db
    client, _ = account_client
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
    response = client.post("/auth/api/invite/accept", json={"token": token, "email": "person@example.test", "name": "Person", "password": password})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_password"


def test_invite_accept_accepts_one_character_password(account_db, account_client, fast_password_work):
    path, plan = account_db
    client, _ = account_client
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
    response = client.post("/auth/api/invite/accept", json={"token": token, "email": "person@example.test", "name": "Person", "password": "x"})
    assert response.status_code == 200
    assert accounts.login("person@example.test", "x", "ip", path=path)["id"]


@pytest.mark.parametrize("password", ["", "x" * 1025])
def test_password_reset_rejects_password_outside_boundary(account_db, account_client, fast_password_work, password):
    path, _ = account_db
    client, _ = account_client
    user = invited_member(account_db)
    _, token = accounts.create_link("reset", "owner", user_id=user["id"], path=path)
    response = client.post("/auth/api/password-reset", json={"token": token, "password": password})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_password"


def test_password_reset_accepts_one_character_password(account_db, account_client, fast_password_work):
    path, _ = account_db
    client, _ = account_client
    user = invited_member(account_db)
    _, token = accounts.create_link("reset", "owner", user_id=user["id"], path=path)
    response = client.post("/auth/api/password-reset", json={"token": token, "password": "x"})
    assert response.status_code == 200
    assert accounts.login(user["email"], "x", "ip", path=path)["id"] == user["id"]


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
    admin_store.put_operator_setting("sign_in", {"mode": mode}, path=path)
    options = client.get("/auth/api/options").json()
    assert options["mode"] == mode
    assert options["built_in"] is builtin
    assert options["oidc"] is oidc
    response = client.post("/auth/api/login", json={"email": "missing@example.test", "password": PASSWORD})
    # Only a correct password learns that built-in sign-in is off (see the owner-password tests).
    assert response.status_code == 401
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
    accepted = client.post("/auth/api/invite/accept", json={"token": token, "email": "person@example.test", "name": "Person", "password": PASSWORD})
    assert accepted.status_code == (200 if builtin else 403)


def test_both_with_no_provider_turned_on_reads_as_built_in_only(account_db, account_client):
    path, _ = account_db
    client, state = account_client
    state["oidc_enabled"] = False
    admin_store.put_operator_setting("sign_in", {"mode": "both"}, path=path)
    options = client.get("/auth/api/options").json()
    assert options["built_in"] is True and options["oidc"] is False


def test_invalid_account_request_bodies_fail(account_client):
    client, _ = account_client
    assert client.post("/admin/api/invites", json={"access_plan": "unused", "expires_in": True}, headers=OWNER_HEADERS).status_code == 422
    assert client.patch("/admin/api/people/unused", json={"disabled": "false"}, headers=OWNER_HEADERS).status_code == 422


def test_oidc_targeted_invite_requires_session_csrf_verified_email_and_matching_identity(account_db, account_client, monkeypatch):
    path, plan = account_db
    client, state = account_client
    user = admin_store.upsert_oidc_user("https://identity.example", "subject-one", email="person@example.test", path=path)
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email=user["email"], path=path)
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


def test_proxy_treats_revoked_session_as_signed_out_before_and_after_reenable(account_db, fast_password_work, monkeypatch):
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
    client = TestClient(proxy.app, client=("127.0.0.1", 50000))
    client.cookies.set(cookie.name, raw)
    assert client.get("/me/api/session").json()["authenticated"] is True
    accounts.change_person(user["id"], "owner", disabled=True, path=path)
    # The session reader rejects it; the response clears the revoked cookie.
    denied = client.get("/me/api/session")
    assert denied.status_code == 200
    assert denied.json()["authenticated"] is False
    assert "max-age=0" in denied.headers["set-cookie"].lower()
    accounts.change_person(user["id"], "owner", disabled=False, path=path)
    client.cookies.set(cookie.name, raw)
    assert client.get("/me/api/session").json()["authenticated"] is False
    client.cookies.set(cookie.name, raw)
    assert client.get("/me/api/home").status_code == 401
    client.cookies.set(cookie.name, raw)
    options = client.get("/auth/api/options")
    assert options.status_code == 200
    assert "max-age=0" in options.headers["set-cookie"].lower()


def test_invite_acceptance_without_session_signing_key_does_not_consume_link_or_create_account(account_db, account_client, fast_password_work):
    path, plan = account_db
    client, state = account_client
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
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
    admin = TestClient(proxy.app, client=("127.0.0.1", 50000))
    member = TestClient(proxy.app, client=("127.0.0.1", 50000))
    admin.cookies.set(cookie.name, encode_session_cookie(owner_session, signing_key=SIGNING_KEY))
    owner_csrf = {"x-azizam-csrf": csrf_token_for_session(owner_session, signing_key=SIGNING_KEY)}
    invitation = admin.post("/admin/api/invites", headers=owner_csrf, json={"access_plan": plan["id"], "email": "member@example.test"})
    assert invitation.status_code == 200
    token = invitation.json()["url"].split("token=", 1)[1]
    assert member.post("/auth/api/invite", json={"token": token}).status_code == 200
    accepted = member.post("/auth/api/invite/accept", json={"token": token, "email": "member@example.test", "name": "Member", "password": PASSWORD})
    assert accepted.status_code == 200
    first_key = accepted.json()["credential"]["id"]
    snapshot = member.get("/me/api/session").json()
    assert snapshot["authenticated"] is True
    user_id = snapshot["user"]["id"]
    assert snapshot["csrf_token"]
    assert snapshot["apps"][0]["access_plan"]["id"] == plan["id"]
    assert snapshot["apps"][0]["access_plan"]["same_as_person"] is True
    assert member.get("/admin/api/people").status_code == 403
    member.cookies.clear()
    logged_in = member.post("/auth/api/login", json={"email": "member@example.test", "password": PASSWORD})
    assert logged_in.status_code == 200
    old_cookie = member.cookies.get(cookie.name)
    changed_plan = store_plan("Changed plan", path=path, models=[])
    changed = admin.patch(f"/admin/api/people/{user_id}", headers=owner_csrf, json={"access_plan": changed_plan["id"]})
    assert changed.status_code == 200
    assert member.get("/me/api/session").json()["apps"][0]["access_plan"]["name"] == "Changed plan"
    assert admin.patch(f"/admin/api/people/{user_id}", headers=owner_csrf, json={"disabled": True}).status_code == 200
    assert member.get("/me/api/session").json()["authenticated"] is False
    assert member.post("/auth/api/login", json={"email": "member@example.test", "password": PASSWORD}).status_code == 401
    assert admin.patch(f"/admin/api/people/{user_id}", headers=owner_csrf, json={"disabled": False}).status_code == 200
    member.cookies.set(cookie.name, old_cookie, domain="testserver.local", path="/")
    assert member.get("/me/api/session").json()["authenticated"] is False
    assert rows(path, "SELECT revoked_at FROM credentials WHERE id=?", (first_key,))[0]["revoked_at"] is not None
    assert member.post("/auth/api/login", json={"email": "member@example.test", "password": PASSWORD}).status_code == 200
    reset = admin.post(f"/admin/api/people/{user_id}/reset", json={}, headers=owner_csrf)
    assert reset.status_code == 200
    reset_token = reset.json()["url"].split("reset=", 1)[1]
    pre_reset_cookie = member.cookies.get(cookie.name)
    new_password = "a replacement member password"
    assert member.post("/auth/api/password-reset", json={"token": reset_token, "password": new_password}).status_code == 200
    member.cookies.set(cookie.name, pre_reset_cookie, domain="testserver.local", path="/")
    assert member.get("/me/api/session").json()["authenticated"] is False
    assert member.post("/auth/api/login", json={"email": "member@example.test", "password": new_password}).status_code == 200
    mcp_store = proxy.MCP_OAUTH.deps.store
    mcp_client = mcp_store.put_client(client_id="azm_client_t", kind="registered", name="Agent",
                                      redirect_uris=["https://agent.example/cb"], grant_types=["authorization_code"],
                                      auth_method="none", secret_hash=None, metadata={})
    connection = mcp_store.create_grant(person_id=user_id, client=mcp_client, redirect_uri="https://agent.example/cb",
                                        resource="https://gw.example/mcp", scope="mcp", session=None)
    assert admin.delete(f"/admin/api/people/{user_id}", headers=owner_csrf).status_code == 200
    assert member.get("/me/api/session").json()["authenticated"] is False
    assert user_id not in {person["id"] for person in admin.get("/admin/api/people").json()["people"]}
    assert mcp_store.active_grant(connection.id) is None  # removal ends their MCP connections


def test_oidc_users_with_same_email_in_different_issuers_do_not_share_account_plans(account_db):
    path, plan = account_db
    first = admin_store.upsert_oidc_user("https://first-identity.example", "shared-subject", email="shared@example.test", path=path)
    second = admin_store.upsert_oidc_user("https://second-identity.example", "shared-subject", email="shared@example.test", path=path)
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email=first["email"], path=path)
    accounts.accept_invite(token, first["email"], "First", None, oidc_user_id=first["id"], path=path)
    assert accounts.plan_for_user(first["id"], path=path) == plan["id"]
    assert accounts.plan_for_user(second["id"], path=path) is None
    second_plan = store_plan("Second issuer plan", path=path)
    _, second_token = accounts.create_link("invite", "owner", plan_id=second_plan["id"], email=second["email"], path=path)
    accounts.accept_invite(second_token, second["email"], "Second", None, oidc_user_id=second["id"], path=path)
    assert accounts.plan_for_user(first["id"], path=path) == plan["id"]
    assert accounts.plan_for_user(second["id"], path=path) == second_plan["id"]


def test_owner_can_copy_pending_invite_after_listing_and_router_reload_without_persisting_plaintext(account_db, account_client):
    path, plan = account_db
    client, state = account_client
    created = client.post("/admin/api/invites", headers=OWNER_HEADERS, json={"access_plan": plan["id"]})
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
    created = client.post("/admin/api/invites", headers=OWNER_HEADERS, json={"access_plan": plan["id"]}).json()
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


def test_oidc_invite_binding_gives_preexisting_apps_the_invited_plan(account_db, account_client):
    path, plan = account_db
    client, state = account_client
    user = admin_store.upsert_oidc_user("https://identity.example", "existing-subject", email="person@example.test", path=path)
    for index in range(2):
        app = admin_store.create_app(f"Existing app {index}", kind="personal", owner_user_id=user["id"], access_plan="person", path=path)
        admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=f"preexisting-{index}", prefix="test", path=path)
    accounts.record_oidc_verification(user["id"], True, path=path)
    session = session_from_claims({"sub": user["id"], "email": user["email"]}, role="none", cookie=CookieConfig(secure=False))
    state["session"] = session
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email=user["email"], path=path)
    response = client.post("/auth/api/invite/accept", json={"token": token, "email": user["email"], "name": "Person"}, headers={"x-azizam-csrf": csrf_token_for_session(session, signing_key=SIGNING_KEY)})
    assert response.status_code == 200
    apps = rows(path, "SELECT * FROM apps WHERE owner_user_id=?", (user["id"],))
    assert len(apps) == 3
    assert len(rows(path, "SELECT * FROM credentials")) == 3
    assert {app["access_plan"] for app in apps} == {"person"}
    assert accounts.plan_for_user(user["id"], path=path) == plan["id"]


def test_plan_with_pending_invite_cannot_delete_until_revoked(account_db):
    path, plan = account_db
    invite, _ = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
    with pytest.raises(ValueError, match="1 open invitation"):
        admin_store.delete_access_plan(plan["id"], path=path)
    accounts.revoke_invite(invite["id"], "owner", path=path)
    assert admin_store.delete_access_plan(plan["id"], path=path)["id"] == plan["id"]
    assert accounts.list_invites(path=path)[0]["plan_id"] is None


def test_plan_delete_error_lists_every_kind_of_user(account_db):
    path, plan = account_db
    person = admin_store.upsert_oidc_user("https://identity.example", "plan-user", email="plan-user@example.test", display_name="Plan User", path=path)
    accounts.change_person(person["id"], "owner", plan_id=plan["id"], path=path)
    app = admin_store.create_app("Disabled app", kind="personal", owner_user_id=person["id"], access_plan=plan["id"], path=path)
    admin_store.disable_app(app["id"], path=path)
    accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
    with pytest.raises(ValueError) as error:
        admin_store.delete_access_plan(plan["id"], path=path)
    message = str(error.value)
    assert message.startswith("Member plan is used by ")
    assert "1 app (Disabled app (disabled))" in message
    assert "1 person (Plan User)" in message
    assert "1 open invitation" in message
    assert admin_store.get_access_plan(plan["id"], path=path) is not None


def test_deleting_a_plan_clears_it_from_removed_people(account_db):
    path, plan = account_db
    person = admin_store.upsert_oidc_user("https://identity.example", "gone", email="gone@example.test", path=path)
    accounts.change_person(person["id"], "owner", plan_id=plan["id"], path=path)
    accounts.change_person(person["id"], "owner", remove=True, path=path)
    assert admin_store.delete_access_plan(plan["id"], path=path)["id"] == plan["id"]
    assert accounts.access(person["id"], path=path)["plan_id"] is None


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


@pytest.mark.parametrize("target", ["/", "/invite/", "/sign-in/"])
def test_oidc_returns_to_the_product_account_page(target):
    import proxy
    assert proxy._safe_admin_return_to(target) == target
    assert proxy._safe_admin_return_to("//evil.example" + target) == "/"


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
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)

    def assert_public_name(name):
        options = client.get("/auth/api/options")
        invite = client.post("/auth/api/invite", json={"token": token})
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
    ("/", "/"),
    ("/usage/?receipt=abc#details", "/usage/?receipt=abc#details"),
    ("/people/?tab=plans&plan=family", "/people/?tab=plans&plan=family"),
    ("/ui/home/", "/"), ("/admin/services?service=tts", "/"), ("/me?tab=keys", "/"),
    ("https://evil.example", "/"), ("//evil.example", "/"), (None, "/"),
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


def test_invite_inspection_requires_post_body(account_client, account_db):
    client, _ = account_client
    path, plan = account_db
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
    assert client.get("/auth/api/invite", params={"token": token}).status_code == 405
    response = client.post("/auth/api/invite", json={"token": token})
    assert response.status_code == 200
    assert token not in response.text
    assert client.post("/auth/api/invite", json={"token": token}, headers={"origin": "https://other.test"}).status_code == 403


def test_reenable_preserves_independently_disabled_apps(account_db, fast_password_work):
    path, _ = account_db
    user, app, _ = invited_member(account_db)
    independently_disabled = admin_store.create_app("Independent", kind="personal", owner_user_id=user["id"], access_plan="person", path=path)
    with accounts.connection(path) as conn:
        conn.execute("UPDATE apps SET disabled_at=123 WHERE id=?", (independently_disabled["id"],))
    accounts.change_person(user["id"], "owner", disabled=True, path=path)
    assert admin_store.get_app(app["id"], path=path)["disabled_at"] is None
    accounts.change_person(user["id"], "owner", disabled=False, path=path)
    assert admin_store.get_app(independently_disabled["id"], path=path)["disabled_at"] == 123
    assert admin_store.get_app(app["id"], path=path)["disabled_at"] is None


def test_invitation_audits_first_credential_in_acceptance_transaction(account_db, fast_password_work):
    import json
    path, _ = account_db
    user, app, credential = invited_member(account_db)
    acceptance = rows(path, "SELECT * FROM audit_events WHERE action='account.invite.accept'")[0]
    creation = rows(path, "SELECT * FROM audit_events WHERE action='credential.create'")[0]
    assert json.loads(acceptance["metadata_json"])["credential_id"] == credential["id"]
    assert creation["actor_id"] == user["id"]
    assert creation["target_kind"] == "credential"
    assert creation["target_id"] == credential["id"]
    assert json.loads(creation["metadata_json"]) == {"app_id": app["id"], "prefix": credential["prefix"], "source": "invite"}
    assert credential["api_key"] not in str(creation)


def test_invite_audit_failure_rolls_back_first_credential(account_db, fast_password_work, monkeypatch):
    path, plan = account_db
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], path=path)
    original = admin_store._record_audit_in_transaction
    def fail_creation(conn, action, **kwargs):
        if action == "credential.create":
            raise RuntimeError("audit write failed")
        return original(conn, action, **kwargs)
    monkeypatch.setattr(admin_store, "_record_audit_in_transaction", fail_creation)
    with pytest.raises(RuntimeError, match="audit write failed"):
        accounts.accept_invite(token, "person@example.test", "Person", PASSWORD, path=path)
    assert rows(path, "SELECT * FROM users") == []
    assert rows(path, "SELECT * FROM credentials") == []
    assert accounts.inspect_invite(token, path=path)["used_at"] is None


def test_people_and_invite_audits_preserve_service_actor(account_db, fast_password_work):
    path, plan = account_db
    user, _, _ = invited_member(account_db)
    actor = {"actor_kind": "service_account", "actor_label": "Owner automation"}
    invite, _ = accounts.create_link("invite", "svc_owner", plan_id=plan["id"], path=path, **actor)
    accounts.revoke_invite(invite["id"], "svc_owner", path=path, **actor)
    accounts.change_person(user["id"], "svc_owner", disabled=True, path=path, **actor)
    events = rows(path, "SELECT * FROM audit_events WHERE actor_id='svc_owner'")
    assert len(events) == 3
    assert all(event["actor_kind"] == "service_account" and event["actor_label"] == "Owner automation" for event in events)


def test_invite_api_audits_owner_service_principal(account_client, account_db):
    client, _ = account_client
    path, plan = account_db
    headers = {**OWNER_HEADERS, "x-test-actor-kind": "service_account", "x-test-actor-id": "svc_owner", "x-test-actor-label": "Owner service"}
    response = client.post("/admin/api/invites", json={"access_plan": plan["id"]}, headers=headers)
    assert response.status_code == 200
    event = rows(path, "SELECT * FROM audit_events WHERE action='account.invite.create'")[0]
    assert (event["actor_kind"], event["actor_id"], event["actor_label"]) == ("service_account", "svc_owner", "Owner service")


def test_failed_reset_attempts_on_other_links_do_not_block_a_valid_reset(account_db, account_client, fast_password_work):
    path, _ = account_db
    client, _ = account_client
    user, _, _ = invited_member(account_db)
    for attempt in range(11):
        bogus = {"token": f"not-a-real-reset-link-{attempt}", "password": PASSWORD}
        assert client.post("/auth/api/password-reset", json=bogus).status_code != 429
    _, token = accounts.create_link("reset", "owner", user_id=user["id"], path=path)
    new_password = "a password after someone else's typos"
    assert client.post("/auth/api/password-reset", json={"token": token, "password": new_password}).status_code == 200
    assert accounts.login(user["email"], new_password, "ip", path=path)["id"] == user["id"]


def test_one_reset_link_is_still_throttled_on_repeated_attempts(account_db, account_client, fast_password_work):
    client, _ = account_client
    payload = {"token": "a-guessed-reset-link", "password": PASSWORD}
    for _ in range(10):
        assert client.post("/auth/api/password-reset", json=payload).status_code != 429
    assert client.post("/auth/api/password-reset", json=payload).status_code == 429
