"""Browser-local sign-ins. Only the active cookie authorizes ordinary requests.

Family-keyed entries survive token rotation and expiry, so an expired person can
still appear in the menu. The browser group is stored as a hash; session facts
stay on the server and are rechecked before issuing an active cookie.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import asdict

from gateway import accounts, admin_store, browser_sessions, session_policy, transport_budgets
from gateway.admin_oidc import AdminSession, SessionError

MAX_PEOPLE = transport_budgets.section("browser_people")["signed_in_max"]   # registry/transport_budgets.yml
GROUP_COOKIE_MAX_AGE = int(session_policy.BOUNDS["absolute_lifetime_days"][1] * session_policy.DAY)


def cookie_name(cookie):
    return "__Host-gateway-people" if cookie.secure else "gateway-people"


def group(request, cookie):
    raw = getattr(request.state, "people_group", None) or request.cookies.get(cookie_name(cookie), "")
    return hashlib.sha256(raw.encode()).hexdigest() if raw else ""


def join_in(conn, request, session, *, cookie, signing_key, path):
    """Attach both sign-ins atomically; replacing one person's entry never consumes another slot."""
    if not session.renewable:
        return
    if not group(request, cookie):
        request.state.people_group = secrets.token_urlsafe(32)
    gid = group(request, cookie)
    previous = browser_sessions.decode_incoming(request, cookie=cookie, signing_key=signing_key)
    if previous and previous.subject != session.subject and previous.renewable:
        try:
            browser_sessions._check_in(conn, previous, session_policy.load_policy(path), time.time())
        except SessionError:
            pass
        else:
            if conn.execute("SELECT 1 FROM browser_people WHERE group_id=? AND person_id=?",
                            (gid, previous.subject)).fetchone() is None:
                _save_in(conn, gid, previous)
    exists = conn.execute("SELECT 1 FROM browser_people WHERE group_id=? AND person_id=?", (gid, session.subject)).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM browser_people WHERE group_id=?", (gid,)).fetchone()[0]
    if not exists and count >= MAX_PEOPLE:
        from gateway.accounts import AccountError
        raise AccountError("browser_people_limit", "Remove a person before adding another. This browser can hold five people.", 409)
    old = conn.execute("SELECT session_json FROM browser_people WHERE group_id=? AND person_id=?", (gid, session.subject)).fetchone()
    prior = AdminSession(**json.loads(old[0])) if old else previous if previous and previous.subject == session.subject else None
    if prior:
        if prior.family_id != session.family_id:
            accounts.revoke_family_in(conn, prior.family_id, int(browser_sessions._revoke_until(prior)), session_id=prior.session_id)
    _save_in(conn, gid, session)
    admin_store._record_audit_in_transaction(conn, "account.browser.add", actor_kind="user", actor_id=session.subject,
        target_kind="user", target_id=session.subject, actor_label=session.email, metadata=None, now=int(time.time()))


def _save_in(conn, gid, session):
    conn.execute("INSERT INTO browser_people(group_id,person_id,family_id,session_json) VALUES(?,?,?,?) "
                 "ON CONFLICT(group_id,person_id) DO UPDATE SET family_id=excluded.family_id, session_json=excluded.session_json",
                 (gid, session.subject, session.family_id, json.dumps(asdict(session))))


def remember_rotation(request, session, *, cookie, path):
    gid = group(request, cookie)
    if gid:
        with admin_store.transaction(path) as tx:
            # A response already in flight must not resurrect a removed entry or replace a newer rotation.
            row = tx.conn.execute("SELECT session_json FROM browser_people WHERE group_id=? AND family_id=?", (gid, session.family_id)).fetchone()
            if row:
                old = AdminSession(**json.loads(row[0]))
                verdict = accounts.session_verdict_in(tx.conn, session.subject, session.session_id, session.family_id)
                if verdict == accounts.LIVE and session.last_renewed >= old.last_renewed:
                    _save_in(tx.conn, gid, session)


def set_group_cookie(response, request, cookie):
    raw = getattr(request.state, "people_group", None)
    if raw:
        response.set_cookie(cookie_name(cookie), raw, secure=cookie.secure, httponly=True, samesite="lax", path="/",
                            max_age=GROUP_COOKIE_MAX_AGE)


def rows(request, *, cookie, path):
    with accounts.connection(path, invalidate_asset_cache=False) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM browser_people WHERE group_id=?", (group(request, cookie),))]


def target(request, person_id, *, cookie, path):
    row = next((row for row in rows(request, cookie=cookie, path=path) if row["person_id"] == person_id), None)
    if row is None:
        raise SessionError("person is not signed in on this browser")
    return AdminSession(**json.loads(row["session_json"]))


def needs_signin(session, *, path, now=None, conn=None):
    """Read current roles, so a newly promoted administrator also needs fresh proof."""
    roles = admin_store.effective_user_roles_in(conn, session.subject) if conn is not None else _roles(session.subject, path)
    return bool(roles & {"owner", "admin", "superadmin"}) and not session_policy.is_recently_authenticated(
        session, session_policy.load_policy(path), time.time() if now is None else now)


def switch(request, person_id, *, cookie, path):
    current = time.time()
    with admin_store.transaction(path) as tx:
        row = tx.conn.execute("SELECT session_json FROM browser_people WHERE group_id=? AND person_id=?",
                              (group(request, cookie), person_id)).fetchone()
        if row is None:
            raise SessionError("person is not signed in on this browser")
        session = AdminSession(**json.loads(row[0]))
        browser_sessions._check_in(tx.conn, session, session_policy.load_policy(path), current)
        if needs_signin(session, path=path, now=current, conn=tx.conn):
            return None
        # A switch rotates the token without claiming a new authentication time.
        rotated = browser_sessions._rotate_in(tx.conn, session, session, current)
        if rotated is None:
            raise SessionError("session changed while switching")
        _save_in(tx.conn, group(request, cookie), rotated)
    admin_store.record_audit("account.browser.switch", actor_kind="user", actor_id=session.subject,
                            target_kind="user", target_id=session.subject, path=path)
    return rotated


def remove(request, person_id, *, actor, cookie, path):
    session = target(request, person_id, cookie=cookie, path=path)
    browser_sessions.end(session, path=path)
    with admin_store.transaction(path) as tx:
        tx.conn.execute("DELETE FROM browser_people WHERE group_id=? AND person_id=?", (group(request, cookie), person_id))
        admin_store._record_audit_in_transaction(tx.conn, "account.browser.remove", actor_kind="user", actor_id=actor.subject,
                                              target_kind="user", target_id=person_id, actor_label=actor.email, metadata=None, now=int(time.time()))


def listing(request, active, *, cookie, path):
    result = []
    for row in rows(request, cookie=cookie, path=path):
        session = AdminSession(**json.loads(row["session_json"]))
        user = admin_store.get_user(session.subject, path=path)
        current = time.time()
        # The menu observes saved facts, not a cookie presented by the browser. Marking an inactive
        # successor used here would turn a lost renewal response into a false replay detection.
        live = session_policy.is_usable(session, session_policy.load_policy(path), current) and accounts.session_verdict(
            session.subject, session.session_id, session.family_id, path=path, now=current,
        ) in {accounts.LIVE, accounts.GRACE, accounts.LOST}
        roles = _roles(session.subject, path)
        result.append({"id": session.subject, "name": (user or {}).get("display_name") or session.email,
                       "current": bool(active and active.subject == session.subject), "live": live,
                       "privileged": bool(roles & {"owner", "admin", "superadmin"})})
    return result


def _roles(person_id, path):
    with accounts.connection(path, invalidate_asset_cache=False) as conn:
        return admin_store.effective_user_roles_in(conn, person_id)
