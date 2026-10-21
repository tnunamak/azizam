"""A successful sign-in ends the family the browser brought with it (D54, slice A1)."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
import threading

import pytest

from gateway import accounts, admin_store, browser_sessions
from gateway.admin_oidc import encode_session_cookie
from gateway.admin_runtime import create_admin_service_account_key
from gateway.session_policy import DAY
from tests.support.access import store_plan
from tests.test_session_lifecycle import (  # noqa: F401 (fixtures and their dependencies)
    COOKIE, HELPER, OWNER, PASSWORD, SIGNING_KEY, cookie_session, csrf, forge, gateway,
    helper_client, owner_client, person_id, put_cookie, session_cookies, set_password, sg,
    signed_in_state, through_provider,
)


@pytest.mark.parametrize("via", [
    "password", "oidc", "setup-claim", "local-login", "local-owner", "invite-password", "invite-oidc",
])
def test_sign_in_ends_the_session_the_browser_already_had(sg, via):
    if via == "local-owner":
        # First-run can still carry an ordinary person's session, before any owner exists.
        user = admin_store.upsert_oidc_user("local", "earlier", email="earlier@example.test", path=sg.path)
        previous = browser_sessions.issue({"sub": user["id"], "email": user["email"]}, role="none",
                                          cookie=sg.config.cookie, path=sg.path)
        client = sg.client()
        put_cookie(client, encode_session_cookie(previous, signing_key=SIGNING_KEY))
    else:
        client = owner_client(sg)
        if via == "invite-oidc":
            client = helper_client(sg)  # no plan yet; the owner's seeded plan forbids accepting an invite
    previous = cookie_session(client)
    old_copy = sg.client()
    put_cookie(old_copy, client.cookies.get(COOKIE))
    assert old_copy.get("/admin/api/access-plans").status_code in (200, 403)

    if via == "password":
        helper_client(sg)
        set_password(sg, HELPER)
        response = client.post("/auth/api/login", json={"email": HELPER, "password": PASSWORD})
    elif via == "oidc":
        response = sg.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"])
    elif via == "setup-claim":
        key, _, _ = create_admin_service_account_key("claimant", role="owner", path=sg.path)
        response = client.post("/setup/claim", data={"admin_key": key}, follow_redirects=False)
    elif via == "local-login":
        set_password(sg, OWNER)
        response = client.post("/setup/local-login", data={"email": OWNER, "password": PASSWORD},
                               follow_redirects=False)
    elif via == "local-owner":
        response = client.post("/setup/local-owner", data={"email": OWNER, "password": PASSWORD,
                               "password_confirm": PASSWORD}, follow_redirects=False)
    else:
        plan = store_plan("Invited", path=sg.path, models=["test-model"])
        _, token = accounts.create_link("invite", previous.subject, plan_id=plan["id"], path=sg.path)
        payload = {"token": token, "email": HELPER if via == "invite-oidc" else "new@example.test", "name": "Invited"}
        if via == "invite-password":
            payload["password"] = PASSWORD
        response = client.post("/auth/api/invite/accept", json=payload,
                               headers=csrf(client) if via == "invite-oidc" else {})

    assert response.status_code == (302 if via == "oidc" else 303 if via.startswith(("local-", "setup-")) else 200)
    assert len(session_cookies(response)) == 1
    current = browser_sessions.read(client.cookies.get(COOKIE), signing_key=SIGNING_KEY, path=sg.path)
    assert current.family_id != previous.family_id
    assert not accounts.session_allowed(previous.subject, previous.session_id, family_id=previous.family_id, path=sg.path)
    assert old_copy.get("/admin/api/access-plans").status_code == 401


def test_after_a_switch_the_old_cookie_and_try_key_get_401(sg):
    client = owner_client(sg)
    helper_client(sg)
    set_password(sg, HELPER)
    # End the whole family, including a retired id still inside the renewal grace window.
    previous = forge(sg, client, OWNER, issued_ago=10 * DAY, renewed_ago=8 * DAY, authenticated_ago=0)
    retired_copy = sg.client()
    put_cookie(retired_copy, client.cookies.get(COOKIE))
    assert signed_in_state(client)  # rotates the due cookie
    assert cookie_session(client).family_id == previous.family_id
    minted = client.post("/me/api/try-key", json={}, headers={**csrf(client), "x-azizam-user-action": "1"})
    assert minted.status_code == 201
    bearer = {"authorization": "Bearer " + minted.json()["credential"]["api_key"]}
    key_client = sg.client()
    key_status = key_client.get("/v1/models", headers=bearer).status_code
    assert key_status == 200
    current_copy = sg.client()
    put_cookie(current_copy, client.cookies.get(COOKIE))
    assert retired_copy.get("/admin/api/access-plans").status_code == 200
    assert current_copy.get("/admin/api/access-plans").status_code == 200

    assert client.post("/auth/api/login", json={"email": HELPER, "password": PASSWORD}).status_code == 200

    assert retired_copy.get("/admin/api/access-plans").status_code == 401
    assert current_copy.get("/admin/api/access-plans").status_code == 401
    key_status = key_client.get("/v1/models", headers=bearer).status_code
    assert key_status == 401
    assert signed_in_state(client)


def test_two_tabs_signing_in_together_end_the_old_session_and_leave_one_working_cookie(sg, monkeypatch):
    old_browser = owner_client(sg)
    helper_client(sg)
    for email in (OWNER, HELPER):
        set_password(sg, email)
    existing_sessions = {row["session_id"] for row in sg.rows("SELECT session_id FROM account_sessions")}
    previous = cookie_session(old_browser)
    tabs = [sg.client(), sg.client()]
    for tab in tabs:
        put_cookie(tab, old_browser.cookies.get(COOKIE))
    start = threading.Barrier(2)
    issuing = threading.Barrier(2)
    original_issue = browser_sessions.issue

    def issue_together(*args, **kwargs):
        # Neither response can finish before both requests reach session issuance.
        issuing.wait(timeout=20)
        return original_issue(*args, **kwargs)

    monkeypatch.setattr(browser_sessions, "issue", issue_together)

    def sign_in(tab, email):
        start.wait(timeout=20)
        response = tab.post("/auth/api/login", json={"email": email, "password": PASSWORD})
        return tab, email, response

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(sign_in, tab, email) for tab, email in zip(tabs, (OWNER, HELPER))]
        settled = [future.result(timeout=30) for future in as_completed(futures)]

    assert old_browser.get("/admin/api/access-plans").status_code == 401
    families = set()
    for tab, email, response in settled:
        assert response.status_code == 200
        assert response.json() == {"next": "/"}
        assert len(session_cookies(response)) == 1
        session = browser_sessions.read(tab.cookies.get(COOKIE), signing_key=SIGNING_KEY, path=sg.path)
        assert session.subject == person_id(sg, email)
        assert session.family_id != previous.family_id
        assert abs(session.absolute_expires_at - session.issued_at - 90 * DAY) < 1
        families.add(session.family_id)
    assert len(families) == 2  # Addendum 1 permits one extra family, expiring normally.
    assert not accounts.session_allowed(previous.subject, previous.session_id, family_id=previous.family_id, path=sg.path)
    browser = sg.client()
    put_cookie(browser, settled[-1][0].cookies.get(COOKIE))  # last Set-Cookie received wins
    assert signed_in_state(browser)
    live = sg.rows("SELECT session_id FROM account_sessions")
    # Fresh sign-ins have family_id == session_id. The helper's other-device session stays separate.
    assert {row["session_id"] for row in live} - existing_sessions == families


@pytest.mark.parametrize("incoming", ["missing", "forged", "expired", "ended"])
def test_a_stale_or_missing_cookie_does_not_prevent_a_password_sign_in(sg, incoming):
    client = owner_client(sg)
    set_password(sg, OWNER)
    previous = cookie_session(client)
    if incoming == "missing":
        client.cookies.clear()
    elif incoming == "forged":
        put_cookie(client, encode_session_cookie(previous, signing_key=b"different key" * 3))
    elif incoming == "expired":
        put_cookie(client, encode_session_cookie(replace(previous, expires_at=previous.issued_at - 1),
                                                 signing_key=SIGNING_KEY))
    else:
        browser_sessions.end(previous, path=sg.path)

    response = client.post("/auth/api/login", json={"email": OWNER, "password": PASSWORD})

    assert response.status_code == 200
    assert signed_in_state(client)


def test_failed_password_sign_in_keeps_the_incoming_session(sg):
    client = owner_client(sg)
    set_password(sg, OWNER)
    previous = cookie_session(client)

    response = client.post("/auth/api/login", json={"email": OWNER, "password": "wrong"})

    assert response.status_code == 401
    assert accounts.session_allowed(previous.subject, previous.session_id, family_id=previous.family_id, path=sg.path)
    assert signed_in_state(client)


def test_oidc_step_up_ends_the_previous_family_once_and_keeps_the_new_one(sg, monkeypatch):
    client = owner_client(sg)
    previous = cookie_session(client)
    ended = []
    original_end = browser_sessions.end

    def end(session, *, path):
        ended.append(session.family_id)
        return original_end(session, path=path)

    monkeypatch.setattr(browser_sessions, "end", end)
    _, response = through_provider(sg, client, reauth=True)

    assert response.status_code == 302
    assert ended == [previous.family_id]
    assert signed_in_state(client)
    assert cookie_session(client).family_id != previous.family_id
