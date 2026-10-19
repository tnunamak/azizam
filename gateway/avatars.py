"""A person's avatar choice, and the sign-in picture the identity provider shared.

The table lives in the account schema (``admin_store.ACCOUNT_SCHEMA``): it is created
with ``CREATE TABLE IF NOT EXISTS``, so an upgrade and a second start both leave it as it is.
It is a table of its own, not columns of ``users``, because the admin people list returns whole
``users`` rows and the sign-in picture URL must not reach it.

The choice is one word from a fixed list. No URL ever comes from a request: the only URL
kept is the ``picture`` claim of a verified ID token, and only when it is https and short.
The browser never loads that URL. ``gateway.me_avatar`` fetches it on the server.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from gateway import admin_store

AUTO = "auto"
PICTURE = "picture"
VARIANTS = tuple(f"v{number}" for number in range(1, 9))
CHOICES = frozenset({AUTO, PICTURE, *VARIANTS})
MAX_PICTURE_URL_CHARS = 2048
_UNSAFE_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")


class AvatarError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def clean_picture_url(value: Any) -> str | None:
    """The claim, when it is an https URL of at most 2048 characters; otherwise None."""
    if not isinstance(value, str) or not value or len(value) > MAX_PICTURE_URL_CHARS:
        return None
    if _UNSAFE_URL_CHARS.search(value):
        return None
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return None
    if parts.scheme != "https" or not hostname or parts.username is not None or parts.password is not None:
        return None
    return value


def _row(conn: sqlite3.Connection, person_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT choice, picture_url, picture_set_at FROM user_avatars WHERE user_id=?", (person_id,),
    ).fetchone()


def get(person_id: str, *, path: Path) -> dict[str, Any]:
    """``{choice, picture_url, picture_set_at}`` with the stored upstream URL. Server-side use only."""
    with admin_store._conn(path) as conn:
        row = _row(conn, person_id)
    if row is None:
        return {"choice": AUTO, "picture_url": None, "picture_set_at": None}
    return {"choice": row["choice"], "picture_url": row["picture_url"], "picture_set_at": row["picture_set_at"]}


def get_many(person_ids: Iterable[str], *, path: Path) -> dict[str, dict[str, Any]]:
    ids = sorted({str(value) for value in person_ids if value})
    found: dict[str, dict[str, Any]] = {}
    with admin_store._conn(path) as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for row in conn.execute(
                "SELECT user_id, choice, picture_url, picture_set_at FROM user_avatars "
                f"WHERE user_id IN ({','.join('?' for _ in chunk)})", chunk,
            ).fetchall():
                found[row["user_id"]] = {
                    "choice": row["choice"], "picture_url": row["picture_url"],
                    "picture_set_at": row["picture_set_at"],
                }
    return {person_id: found.get(person_id) or {"choice": AUTO, "picture_url": None, "picture_set_at": None}
            for person_id in ids}


def list_stored(*, path: Path) -> dict[str, dict[str, Any]]:
    """Every person who has chosen something or has a picture; the rest are ``auto`` without one."""
    with admin_store._conn(path) as conn:
        rows = conn.execute("SELECT user_id, choice, picture_url, picture_set_at FROM user_avatars").fetchall()
    return {row["user_id"]: {"choice": row["choice"], "picture_url": row["picture_url"],
                             "picture_set_at": row["picture_set_at"]} for row in rows}


def picture_path(person_id: str, record: dict[str, Any], *, viewer_id: str) -> str | None:
    """The gateway path of the picture, or None. Never the provider's URL."""
    if not record.get("picture_url"):
        return None
    query = f"v={int(record.get('picture_set_at') or 0)}"
    if person_id != viewer_id:
        query = f"person={person_id}&{query}"
    return f"/me/api/profile/avatar/picture?{query}"


def public_view(person_id: str, record: dict[str, Any], *, viewer_id: str) -> dict[str, Any]:
    """What the browser may see: the choice and a gateway path for the picture."""
    return {"choice": record["choice"], "person_id": person_id,
            "picture_url": picture_path(person_id, record, viewer_id=viewer_id)}


def set_choice(person_id: str, choice: Any, *, path: Path) -> dict[str, Any]:
    if not isinstance(choice, str) or choice not in CHOICES:
        raise AvatarError("invalid_avatar_choice", "Choose a generated avatar or your sign-in picture.")
    now = int(time.time())
    with admin_store.transaction(path) as tx:
        conn = tx.conn
        if conn.execute("SELECT 1 FROM users WHERE id=?", (person_id,)).fetchone() is None:
            raise AvatarError("person_not_found", "This account was not found.", 404)
        row = _row(conn, person_id)
        if choice == PICTURE and (row is None or not row["picture_url"]):
            raise AvatarError("picture_unavailable", "Your sign-in provider has not shared a picture.", 409)
        conn.execute(
            "INSERT INTO user_avatars(user_id, choice, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET choice=excluded.choice, updated_at=excluded.updated_at",
            (person_id, choice, now),
        )
    return get(person_id, path=path)


def record_sign_in_picture(person_id: str, claim: Any, *, path: Path) -> None:
    """Keep the ``picture`` claim of a verified sign-in.

    An absent claim leaves what is stored (some providers send it in some tokens only).
    A claim that is present and fails the checks clears it, so an unsafe URL is never kept.
    """
    if claim is None:
        return
    url = clean_picture_url(claim)
    now = int(time.time())
    with admin_store.transaction(path) as tx:
        conn = tx.conn
        row = _row(conn, person_id)
        if url is None:
            if row is not None and row["picture_url"]:
                conn.execute(
                    "UPDATE user_avatars SET picture_url=NULL, picture_set_at=NULL, "
                    "choice=CASE WHEN choice=? THEN ? ELSE choice END, updated_at=? WHERE user_id=?",
                    (PICTURE, AUTO, now, person_id),
                )
        elif row is None or row["picture_url"] != url:
            conn.execute(
                "INSERT INTO user_avatars(user_id, choice, picture_url, picture_set_at, updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET picture_url=excluded.picture_url, "
                "picture_set_at=excluded.picture_set_at, updated_at=excluded.updated_at",
                (person_id, AUTO, url, now, now),
            )
