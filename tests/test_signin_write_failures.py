"""Failed sign-in writes return a safe error without changing remembered people."""

import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from gateway import accounts, admin_store, remembered_people
from tests.test_signin_who_can_enter import gateway  # noqa: F401


PASSWORD_EMAIL = "password@example.test"
PASSWORD = "a sufficiently long test password"
OIDC_EMAIL = "trusted@example.test"
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
    if flow == "password":
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

    remembered_people.keep(
        path=gateway.path,
        token=DEVICE_TOKEN,
        person_id=str(user["id"]),
        method=method,
        issuer=issuer,
        remember=True,
        from_tile=False,
    )
    return str(user["id"])


def _remembered_rows(gateway):
    return gateway.rows(
        "SELECT person_id,method,provider_issuer,last_signed_in_at,created_at FROM remembered_people "
        "WHERE device_hash=? ORDER BY person_id",
        (remembered_people.device_key(DEVICE_TOKEN),),
    )


def _inject_write_failure(gateway, flow, failure):
    if failure == "session":
        def fail_session(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        gateway.monkeypatch.setattr(accounts, "register_session", fail_session)
    elif failure == "remembered":
        def fail_remembered(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        gateway.monkeypatch.setattr(remembered_people, "keep", fail_remembered)
    elif flow == "password":
        original = accounts._audit

        def fail_audit(conn, action, *args, **kwargs):
            if action == "account.login":
                raise RuntimeError("injected audit failure")
            return original(conn, action, *args, **kwargs)

        gateway.monkeypatch.setattr(accounts, "_audit", fail_audit)
    else:
        original = admin_store.record_audit

        def fail_audit(action, *args, **kwargs):
            if action == "app.self_create":
                raise RuntimeError("injected audit failure")
            return original(action, *args, **kwargs)

        gateway.monkeypatch.setattr(admin_store, "record_audit", fail_audit)


@pytest.mark.parametrize("flow", ["password", "oidc"])
@pytest.mark.parametrize("failure", ["session", "remembered", "audit"])
def test_signin_write_failure_is_clean_and_keeps_remembered_people_unchanged(
    gateway, flow, failure,
):
    _seed_person(gateway, flow)
    before = _remembered_rows(gateway)
    _inject_write_failure(gateway, flow, failure)
    client = _client(gateway)

    if flow == "password":
        response = client.post(
            "/auth/api/login",
            json={"email": PASSWORD_EMAIL, "password": PASSWORD, "remember": True},
        )
    else:
        response = gateway.sign_in(
            client,
            sub="failure-injection-person",
            email=OIDC_EMAIL,
            groups=("/Trusted",),
            start={"remember": "1"},
        )

    session_cookie = gateway.config.cookie.name
    auth_cookies = [
        value for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{session_cookie}=") and "max-age=0" not in value.lower()
    ]
    assert auth_cookies == []
    if flow == "password":
        assert response.status_code == 503
        assert response.json() == {
            "error": {
                "code": "signin_unavailable",
                "message": "Sign-in could not be completed. Try again.",
            }
        }
    else:
        assert response.status_code == 303
        assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["provider"]
    assert "database is locked" not in response.text
    assert "injected audit failure" not in response.text
    assert _remembered_rows(gateway) == before
