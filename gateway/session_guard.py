"""One check for every credential a browser sign-in creates (decision D41).

A person can sign out everywhere between the moment a request passes the cookie check and the moment
it writes. Without a check in the write's own transaction, that write leaves a working key (or MCP
grant) behind for a sign-in that no longer exists. Every writer of such a credential takes a
``BrowserSession`` (or ``None`` for a write no browser sign-in started: the CLI, an admin key, an
invitation link) and calls ``begin_write`` first. A sign-out that commits before the write makes
``begin_write`` raise; one that commits after finds the new credential to revoke.

``tests/test_credential_writers_check_the_session.py`` finds every writer of the credential tables and
fails when one does not go through this module.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserSession:
    """The sign-in that started a write: the person, the current session id and the sign-in's family."""

    user_id: str
    session_id: str
    family_id: str

    @classmethod
    def of(cls, session, user_id: str) -> "BrowserSession":
        """The sign-in behind an ``AdminSession`` (anything with ``session_id`` and ``family_id``)."""
        return cls(str(user_id), str(session.session_id), str(session.family_id))


class SessionEnded(Exception):
    """The sign-in that started the write was revoked, retired or removed before the write."""

    code = "login_required"
    message = "Sign in again to do this."
    status_code = 401

    def __init__(self) -> None:
        super().__init__(self.message)


def require_live(conn: sqlite3.Connection, session: BrowserSession | None) -> None:
    """Inside the caller's open write transaction: raise ``SessionEnded`` when the sign-in has ended.

    With ``session=None`` nothing is checked: no browser sign-in started the write.
    """
    if session is None:
        return
    # accounts imports admin_store, which imports this module: bind it at call time.
    from gateway import accounts

    try:
        accounts.require_live_session_in(conn, session.user_id, session.session_id, session.family_id)
    except accounts.AccountError:
        raise SessionEnded() from None


def begin_write(conn: sqlite3.Connection, session: BrowserSession | None) -> None:
    """Open the write transaction, then ``require_live`` inside it; a refusal rolls it back.

    With ``session=None`` nothing is opened and nothing is checked.
    """
    if session is None:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        require_live(conn, session)
    except BaseException:
        conn.rollback()
        raise
