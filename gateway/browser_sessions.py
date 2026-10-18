"""Browser sign-ins end to end: issue, read, renew, confirm and end them.

The cookie (gateway.admin_oidc) says who signed in and when it must end. The
record in ``account_sessions`` (gateway.accounts) lets the server end it sooner:
sign-out, sign-out everywhere, a password change, a removed or disabled
person. The owner's policy (gateway.session_policy) says how long either may
last. This module is the one place those three meet, so no route reads a
session cookie on its own.
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from fastapi import Response

from gateway import accounts, admin_store, session_policy
from gateway.admin_oidc import (
    AdminSession, CookieConfig, SessionError, decode_session_cookie,
    encode_session_cookie, session_from_claims,
)
from gateway.session_policy import SessionPolicy


def issue(claims: Mapping[str, Any], *, role: str, cookie: CookieConfig, path: Path,
          register: bool = True, renewable: bool = True, authenticated_at: float | None = None,
          now: float | None = None) -> AdminSession:
    """A new sign-in under the owner's current policy, recorded server-side by default.

    ``register=False`` is for a session with no person behind it. ``renewable=False``
    gives it the fixed life ``cookie.max_age_seconds`` and never renews it.
    ``authenticated_at`` is when the person last really proved who they are, if the
    identity provider says it was before this sign-in (``auth_time``).
    """
    policy = session_policy.load_policy(path)
    if renewable:
        session = session_from_claims(
            claims, role=role, cookie=cookie, now=now,
            idle_seconds=policy.idle_seconds, absolute_seconds=policy.absolute_seconds,
        )
        if authenticated_at is not None:
            session = replace(session, authenticated_at=min(authenticated_at, session.issued_at))
    else:
        session = replace(session_from_claims(claims, role=role, cookie=cookie, now=now), renewable=False)
    if register:
        accounts.initialize(path)
        accounts.register_session(str(claims["sub"]), session.session_id, int(session.expires_at), path=path)
    return session


def read(token: str, *, signing_key: bytes, path: Path, now: float | None = None) -> AdminSession:
    """The session this cookie proves, or ``SessionError``.

    Refuses a forged, expired, idle, past-absolute-deadline or revoked session.
    """
    current = time.time() if now is None else now
    session = decode_session_cookie(token, signing_key=signing_key, now=current)
    if not session_policy.is_usable(session, session_policy.load_policy(path), current):
        raise SessionError("session idle or past its lifetime")
    if not accounts.session_allowed(session.subject, session.session_id, path=path):
        raise SessionError("session revoked or account disabled")
    return session


def advance(session: AdminSession, *, path: Path, now: float | None = None) -> AdminSession | None:
    """Check a decoded session is still live and renew it once a quarter of the idle window has passed.

    Returns the renewed session, or None when nothing is due. Raises ``SessionError`` when the
    session has ended. The server-side record is extended first: a cookie must never outlive it.
    """
    current = time.time() if now is None else now
    policy = session_policy.load_policy(path)
    if not session_policy.is_usable(session, policy, current):
        raise SessionError("session idle or past its lifetime")
    if not accounts.session_allowed(session.subject, session.session_id, path=path):
        raise SessionError("session revoked or account disabled")
    renewed = session_policy.renewal(session, policy, current)
    if renewed is not None:
        accounts.extend_session(renewed.session_id, int(renewed.expires_at), path=path)
    return renewed


def confirm(session: AdminSession, *, path: Path, now: float | None = None) -> AdminSession:
    """The same session after the person proved who they are again."""
    current = time.time() if now is None else now
    confirmed = session_policy.confirmed(session, session_policy.load_policy(path), current)
    accounts.extend_session(confirmed.session_id, int(confirmed.expires_at), path=path)
    return confirmed


def policy(path: Path) -> SessionPolicy:
    return session_policy.load_policy(path)


def set_cookie(response: Response, session: AdminSession, *, cookie: CookieConfig,
               signing_key: bytes, now: float | None = None) -> None:
    """Set the session cookie; its Max-Age is the time left, so the browser forgets it with the server."""
    current = time.time() if now is None else now
    response.set_cookie(
        cookie.name,
        encode_session_cookie(session, signing_key=signing_key),
        max_age=max(1, int(session.expires_at - current)),
        secure=cookie.secure,
        httponly=cookie.http_only,
        samesite=cookie.same_site,
        path=cookie.path,
        domain=cookie.domain,
    )


def end(session: AdminSession, *, path: Path) -> None:
    """Sign this one session out. A copy of the cookie stops working even before it expires.

    The revocation outlasts every copy that could exist. An old cookie records no absolute
    deadline, and a renewed copy of it gets one from the sign-in time, up to the longest
    lifetime an owner may set.
    """
    longest = session.issued_at + session_policy.BOUNDS["absolute_lifetime_days"][1] * session_policy.DAY
    revoke_until = session.latest_expiry if session.absolute_expires_at is not None else max(session.latest_expiry, longest)
    accounts.revoke_session(session.session_id, int(revoke_until), path=path)


def end_everywhere(session: AdminSession, *, path: Path) -> int:
    """Sign the person out of every device, this one included. Returns how many sessions were registered."""
    registered = 0
    if admin_store.get_user(session.subject, path=path) is not None:
        with accounts.connection(path) as conn:
            registered = conn.execute("SELECT COUNT(*) FROM account_sessions WHERE user_id=?",
                                      (session.subject,)).fetchone()[0]
        accounts.revoke_person_sessions(session.subject, path=path)
    # A session of a service account has no person behind it; end this one by id.
    end(session, path=path)
    admin_store.record_audit(
        "account.sessions.revoke_all", actor_kind="user", actor_id=session.subject, actor_label=session.email,
        target_kind="user", target_id=session.subject, metadata={"sessions": registered}, path=path,
    )
    return registered
