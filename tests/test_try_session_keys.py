"""Try page keys end with the sign-in session that made them."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gateway import accounts, admin_store
from gateway.admin_oidc import AdminSession
from gateway.admin_runtime import runtime_identity_for_app_credential_hash
from gateway.runtime_auth import hash_api_key
from gateway.user_runtime import (
    UserPortalError,
    build_user_portal_snapshot,
    ensure_try_session_key,
)

NOW = 2_000_000_000
EXPIRES = NOW + 3600


@pytest.fixture
def member(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    accounts.initialize(db)
    user = admin_store.upsert_oidc_user("https://idp.test", "member", email="member@example.test", path=db)

    def session(session_id="session-a", expires_at=EXPIRES):
        return AdminSession(subject=user["id"], email="member@example.test", role="none",
                            issued_at=NOW - 10, expires_at=expires_at, session_id=session_id)

    return SimpleNamespace(db=db, user=user, session=session)


def credential(db, credential_id):
    record = admin_store.get_credential(credential_id, path=db)
    assert record is not None
    return record


def active_try_keys(db):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT id FROM credentials WHERE display_name='Try session' "
                            "AND revoked_at IS NULL ORDER BY id").fetchall()


def test_try_key_expires_with_the_session_and_reuses_while_live(member):
    minted = ensure_try_session_key(member.session(), now=NOW, path=member.db)
    assert minted["reused"] is False and minted["api_key"].startswith("opk_")
    assert minted["expires_at"] == EXPIRES and minted["session_bound"] is True
    assert minted["app_name"] == "Try"
    assert "session_id" not in minted
    stored = credential(member.db, minted["id"])
    assert stored["session_id"] == "session-a" and stored["display_name"] == "Try session"

    again = ensure_try_session_key(member.session(), reuse_id=minted["id"], now=NOW + 60, path=member.db)
    assert again["reused"] is True and again["id"] == minted["id"]
    assert "api_key" not in again
    assert len(active_try_keys(member.db)) == 1
    apps = admin_store.list_apps_for_user(member.user["id"], path=member.db)
    assert [app["name"] for app in apps] == ["Try"]


def test_another_sessions_key_is_not_reused_and_an_expired_session_gets_none(member):
    first = ensure_try_session_key(member.session(), now=NOW, path=member.db)
    other = ensure_try_session_key(member.session("session-b"), reuse_id=first["id"], now=NOW, path=member.db)
    assert other["reused"] is False and other["id"] != first["id"]
    assert credential(member.db, first["id"])["revoked_at"] is None  # Session A still has its key.
    with pytest.raises(UserPortalError) as error:
        ensure_try_session_key(member.session(), reuse_id=first["id"], now=EXPIRES, path=member.db)
    assert error.value.status_code == 401


def test_each_session_keeps_at_most_two_try_keys(member):
    ids = [ensure_try_session_key(member.session(), now=NOW + step, path=member.db)["id"] for step in range(4)]
    assert active_try_keys(member.db) == sorted([(ids[2],), (ids[3],)])


def test_revoking_the_session_revokes_its_try_keys_only(member):
    mine = ensure_try_session_key(member.session(), now=NOW, path=member.db)
    theirs = ensure_try_session_key(member.session("session-b"), now=NOW, path=member.db)
    accounts.revoke_session("session-a", EXPIRES, path=member.db)
    assert credential(member.db, mine["id"])["revoked_at"] is not None
    assert credential(member.db, theirs["id"])["revoked_at"] is None
    assert runtime_identity_for_app_credential_hash(hash_api_key(mine["api_key"]), path=member.db) is None
    assert runtime_identity_for_app_credential_hash(hash_api_key(theirs["api_key"]), path=member.db) is not None


def test_a_session_without_an_id_cannot_get_a_try_key(member):
    with pytest.raises(UserPortalError) as error:
        ensure_try_session_key(member.session(session_id=""), now=NOW, path=member.db)
    assert error.value.code == "login_required"


def test_snapshot_does_not_reveal_the_session_a_key_is_bound_to(member):
    ensure_try_session_key(member.session(), now=NOW, path=member.db)
    snapshot = build_user_portal_snapshot(member.session(), path=member.db)
    keys = snapshot["apps"][0]["credentials"]
    assert keys and keys[0]["session_bound"] is True
    assert "session-a" not in repr(snapshot)


def test_upgrade_revokes_unbound_try_keys_once(member):
    try_app = admin_store.create_app("Try", kind="personal", owner_user_id=member.user["id"], path=member.db)
    other_app = admin_store.create_app("Laptop", kind="personal", owner_user_id=member.user["id"], path=member.db)
    def key(app, name, secret):
        return admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=hash_api_key(secret),
                                                  prefix="opk_test", display_name=name, path=member.db)["id"]
    stale = key(try_app, "Try session", "opk_stale")
    kept_on_try = key(try_app, "Script", "opk_script")
    kept_elsewhere = key(other_app, "Try session", "opk_laptop")
    with closing(sqlite3.connect(member.db)) as conn:
        conn.execute("DROP INDEX credentials_session")
        conn.execute("ALTER TABLE credentials DROP COLUMN session_id")
        conn.commit()
    admin_store.init_db(member.db)
    assert credential(member.db, stale)["revoked_at"] is not None
    assert credential(member.db, kept_on_try)["revoked_at"] is None
    assert credential(member.db, kept_elsewhere)["revoked_at"] is None
    admin_store.init_db(member.db)
    events = admin_store.list_audit(action_prefix="credential.try_session_revoke", path=member.db)
    assert len(events) == 1 and events[0]["metadata"]["credential_ids"] == [stale]


@pytest.fixture
def browser(monkeypatch, member):
    import proxy

    monkeypatch.setattr(proxy.ADMIN_STORE, "path", member.db)
    monkeypatch.setattr(proxy, "_user_self_service_enabled", lambda: True)
    monkeypatch.setattr(proxy, "_default_profile_id_for_session", lambda _session: None)
    monkeypatch.setattr(proxy, "_csrf_token_for_oidc_session", lambda _session: "fixture-csrf")
    session = AdminSession(subject=member.user["id"], email="member@example.test", role="none",
                           issued_at=1, expires_at=4_000_000_000, session_id="browser-session")
    monkeypatch.setattr(proxy, "_oidc_session_from_request", lambda request: (
        session if accounts.session_allowed(session.subject, session.session_id, path=member.db) else None
    ))
    headers = {"X-Azizam-User-Action": "1", "X-Azizam-Csrf": "fixture-csrf"}
    return SimpleNamespace(client=TestClient(proxy.app), headers=headers)


def test_try_key_route_reuses_the_key_and_sign_out_revokes_it(browser, member):
    minted = browser.client.post("/me/api/try-key", headers=browser.headers, json={})
    assert minted.status_code == 201, minted.text
    key = minted.json()["credential"]
    assert key["expires_at"] == 4_000_000_000
    reused = browser.client.post("/me/api/try-key", headers=browser.headers, json={"reuse_id": key["id"]})
    assert reused.status_code == 200, reused.text
    assert reused.json()["credential"]["id"] == key["id"]
    assert "api_key" not in reused.json()["credential"]

    signed_out = browser.client.post("/logout", headers={**browser.headers, "accept": "application/json"})
    assert signed_out.status_code == 200, signed_out.text
    assert credential(member.db, key["id"])["revoked_at"] is not None
    assert browser.client.post("/me/api/try-key", headers=browser.headers, json={}).status_code == 401
