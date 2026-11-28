"""A person's avatar choice, the sign-in picture the identity provider shared, and a picture the gateway made.

The table lives in the account schema (``admin_store.ACCOUNT_SCHEMA``): it is created
with ``CREATE TABLE IF NOT EXISTS``, so an upgrade and a second start both leave it as it is.
It is a table of its own, not columns of ``users``, because the admin people list returns whole
``users`` rows and the sign-in picture URL must not reach it.

The choice is one word from a fixed list. No URL ever comes from a request: the only URL
kept is the ``picture`` claim of a verified ID token, and only when it is https and short.
The browser never loads that URL. ``gateway.me_avatar`` fetches it on the server.

A picture the gateway made is not a URL either. It is a copy of one of the person's own job pictures, kept
in the avatar cache beside the admin database: one directory per person, one file per template version.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from gateway import admin_store, private_io
from gateway.avatar_generation import TEMPLATE_VERSION

AUTO = "auto"
PICTURE = "picture"
GENERATED = "generated"
VARIANTS = tuple(f"v{number}" for number in range(1, 9))
CHOICES = frozenset({AUTO, PICTURE, GENERATED, *VARIANTS})
# Which picture a person shows: the sign-in picture, the one the gateway made, or none (the palette).
SHOWN_SIGN_IN = "sign-in"
SHOWN_GENERATED = "generated"
# Others (admins, in practice) see a person's picture only when the person chose it.
SHOWN_TO_OTHERS = frozenset({PICTURE, GENERATED})
MAX_PICTURE_URL_CHARS = 2048
MAX_GENERATED_BYTES = 16 * 1024 * 1024
GENERATED_CACHE_DIR = "avatar-cache"
_UNSAFE_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_COLUMNS = ("choice, picture_url, picture_set_at, generated_template, generated_at, "
            "default_job_id, default_attempted_at")
_DEFAULT = {"choice": AUTO, "picture_url": None, "picture_set_at": None,
            "generated_template": None, "generated_at": None,
            "default_job_id": None, "default_attempted_at": None}


class AvatarError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def image_type(data: bytes) -> str | None:
    """The image type the bytes start as, from the file signature; None if it is not an image this gateway serves."""
    for prefix, content_type in _SIGNATURES:
        if data.startswith(prefix):
            return content_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


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


def _record(row: sqlite3.Row) -> dict[str, Any]:
    return {"choice": row["choice"], "picture_url": row["picture_url"], "picture_set_at": row["picture_set_at"],
            "generated_template": row["generated_template"], "generated_at": row["generated_at"],
            "default_job_id": row["default_job_id"], "default_attempted_at": row["default_attempted_at"]}


def _row(conn: sqlite3.Connection, person_id: str) -> sqlite3.Row | None:
    return conn.execute(f"SELECT {_COLUMNS} FROM user_avatars WHERE user_id=?", (person_id,)).fetchone()


def get(person_id: str, *, path: Path) -> dict[str, Any]:
    """The stored record, with the upstream URL. Server-side use only."""
    with admin_store._conn(path) as conn:
        row = _row(conn, person_id)
    return dict(_DEFAULT) if row is None else _record(row)


def get_many(person_ids: Iterable[str], *, path: Path) -> dict[str, dict[str, Any]]:
    ids = sorted({str(value) for value in person_ids if value})
    found: dict[str, dict[str, Any]] = {}
    with admin_store._conn(path) as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for row in conn.execute(
                f"SELECT user_id, {_COLUMNS} FROM user_avatars "
                f"WHERE user_id IN ({','.join('?' for _ in chunk)})", chunk,
            ).fetchall():
                found[row["user_id"]] = _record(row)
    return {person_id: found.get(person_id) or dict(_DEFAULT) for person_id in ids}


def list_stored(*, path: Path) -> dict[str, dict[str, Any]]:
    """Every person who has chosen something or has a picture; the rest are ``auto`` without one."""
    with admin_store._conn(path) as conn:
        rows = conn.execute(f"SELECT user_id, {_COLUMNS} FROM user_avatars").fetchall()
    return {row["user_id"]: _record(row) for row in rows}


def has_current_copy(record: Mapping[str, Any]) -> bool:
    """Whether the person has a picture the gateway made from the current template."""
    return record["generated_at"] is not None and record["generated_template"] == TEMPLATE_VERSION


def has_picture(kind: str, record: Mapping[str, Any]) -> bool:
    """Whether the person has a picture of this kind: the one the gateway made, or the sign-in picture."""
    if kind == SHOWN_GENERATED:
        return has_current_copy(record)
    return bool(record["picture_url"])


def shown_kind(record: Mapping[str, Any]) -> str | None:
    """The picture the person's avatar shows: the one they had made, the sign-in picture, or None (the palette)."""
    choice = record["choice"]
    if choice == GENERATED:
        return SHOWN_GENERATED if has_current_copy(record) else None
    if choice in VARIANTS:
        return None
    return SHOWN_SIGN_IN if record["picture_url"] else None


def _preview_path(kind: str, stamp: Any) -> str:
    """The path that previews one of the person's own pictures, whichever avatar they show."""
    return f"/me/api/profile/avatar/picture?kind={kind}&v={int(stamp or 0)}"


def picture_path(person_id: str, record: Mapping[str, Any], *, viewer_id: str) -> str | None:
    """The gateway path of the picture shown, or None. Never the provider's URL."""
    kind = shown_kind(record)
    if kind is None:
        return None
    stamp = record["generated_at"] if kind == SHOWN_GENERATED else record["picture_set_at"]
    query = f"v={int(stamp or 0)}"
    if person_id != viewer_id:
        query = f"person={person_id}&{query}"
    return f"/me/api/profile/avatar/picture?{query}"


def public_view(person_id: str, record: Mapping[str, Any], *, viewer_id: str, default_due_now: bool | None = None) -> dict[str, Any]:
    """What the browser may see: the choice and a gateway path for the picture shown.

    For the person, it also gives a preview path for each picture they have (None when there is none), so the
    picker can show both whichever one is chosen. ``default_due`` is the page's cue to ask for the automatic picture:
    the caller passes True only when that picture is due and an image route exists for the person now. It stays
    True after a deferred ask, and is False once the attempt is recorded or the person chooses anything. A caller
    that does not answer the cue passes None, and the view leaves the key out.
    """
    view: dict[str, Any] = {"choice": record["choice"], "person_id": person_id,
                            "picture_url": picture_path(person_id, record, viewer_id=viewer_id)}
    if person_id == viewer_id:
        view["sign_in_picture_url"] = _preview_path(SHOWN_SIGN_IN, record["picture_set_at"]) if record["picture_url"] else None
        view["made_picture_url"] = _preview_path(SHOWN_GENERATED, record["generated_at"]) if has_current_copy(record) else None
        # The automatic default is being made: the page shows that until its picture is kept or it fails.
        view["generating"] = record["default_job_id"] is not None
        if default_due_now is not None:
            view["default_due"] = default_due_now
    return view


def generated_dir(person_id: str, *, path: Path) -> Path:
    """Where a person's copy lives: the avatar cache beside the admin database, named by a hash of the person id."""
    digest = hashlib.sha256(person_id.encode()).hexdigest()[:40]
    return path.parent / GENERATED_CACHE_DIR / digest


def read_generated(person_id: str, *, path: Path) -> bytes | None:
    """The copy kept for the current template, or None."""
    try:
        return (generated_dir(person_id, path=path) / TEMPLATE_VERSION).read_bytes()
    except FileNotFoundError:
        return None


def remove_generated_files(person_id: str, *, path: Path) -> None:
    shutil.rmtree(generated_dir(person_id, path=path), ignore_errors=True)


def _write_copy(person_id: str, data: bytes, *, path: Path) -> None:
    """Write the copy for the current template, and nothing else, into the person's cache directory."""
    directory = generated_dir(person_id, path=path)
    target = directory / TEMPLATE_VERSION
    private_io.ensure_private_dir(directory)
    private_io.atomic_write_bytes(target, data)
    for entry in directory.iterdir():
        if entry != target:
            entry.unlink(missing_ok=True)


def store_generated(person_id: str, data: bytes, *, path: Path) -> dict[str, Any]:
    """Keep a copy of a picture the gateway made, and show it as the person's avatar.

    A person who keeps one has settled their default: the automatic picture is never made for them after this, and
    a default still being made publishes nothing.
    """
    if image_type(data) is None or len(data) > MAX_GENERATED_BYTES:
        raise AvatarError("invalid_generated_picture", "That picture cannot be kept as an avatar.")
    if admin_store.get_user(person_id, path=path) is None:
        raise AvatarError("person_not_found", "This account was not found.", 404)
    now = int(time.time())
    with admin_store.transaction(path) as tx:
        # The copy is written inside the transaction, so a default that settles at the same time waits for this keep.
        _write_copy(person_id, data, path=path)
        tx.conn.execute(
            "INSERT INTO user_avatars(user_id, choice, generated_template, generated_at, default_attempted_at, updated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET choice=excluded.choice, generated_template=excluded.generated_template, "
            "generated_at=excluded.generated_at, updated_at=excluded.updated_at, "
            "default_attempted_at=COALESCE(user_avatars.default_attempted_at, excluded.default_attempted_at), "
            "default_job_id=NULL",
            (person_id, GENERATED, TEMPLATE_VERSION, now, now, now),
        )
    return get(person_id, path=path)


def clear_generated(person_id: str, *, path: Path) -> dict[str, Any]:
    """Remove the picture the gateway made, whichever avatar the person shows.

    The made picture's metadata always goes, so no preview of a missing file is offered. A person who showed the
    made picture falls back in order: their sign-in picture if they have one, else the palette. That is the
    automatic choice (AUTO). Their other choices (their sign-in picture, or a palette colour) stay as they are.
    Removing also settles the automatic default, so a default still being made publishes nothing.
    """
    now = int(time.time())
    with admin_store.transaction(path) as tx:
        row = _row(tx.conn, person_id)
        fallback = AUTO if row is None or row["choice"] == GENERATED else row["choice"]
        # Selecting from users means a person who has been removed gets no record back.
        tx.conn.execute(
            "INSERT INTO user_avatars(user_id, choice, default_attempted_at, updated_at) "
            "SELECT id, ?, ?, ? FROM users WHERE id=? "
            "ON CONFLICT(user_id) DO UPDATE SET choice=excluded.choice, generated_template=NULL, generated_at=NULL, "
            "default_attempted_at=COALESCE(user_avatars.default_attempted_at, excluded.default_attempted_at), "
            "default_job_id=NULL, updated_at=excluded.updated_at",
            (fallback, now, now, person_id),
        )
        remove_generated_files(person_id, path=path)
    return get(person_id, path=path)


def is_default_due(record: Mapping[str, Any]) -> bool:
    """A person with no sign-in picture and no chosen avatar, who has not had the one automatic attempt yet."""
    return record["default_attempted_at"] is None and record["choice"] == AUTO and not record["picture_url"]


def default_due(person_id: str, *, path: Path) -> bool:
    """Whether the automatic default is still to be made. It does not record an attempt; ``claim_default`` does."""
    return is_default_due(get(person_id, path=path))


def claim_default(person_id: str, job_id: str | None, *, path: Path) -> bool:
    """Record the one automatic attempt, with the job it started, if the person has not chosen an avatar since.

    ``job_id`` is the job the default started. ``None`` records a start that failed for good, so the attempt is used
    up and never repeated. The check and the record share one write transaction. The call returns True when it
    recorded the attempt. It returns False, and records nothing, when the person is gone or already has an attempt
    or a choice of another kind.
    """
    now = int(time.time())
    with admin_store.transaction(path) as tx:
        if tx.conn.execute("SELECT 1 FROM users WHERE id=?", (person_id,)).fetchone() is None:
            return False
        row = _row(tx.conn, person_id)
        if row is not None and (row["default_attempted_at"] is not None or row["choice"] != AUTO):
            return False
        tx.conn.execute(
            "INSERT INTO user_avatars(user_id, choice, default_attempted_at, default_job_id, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET default_attempted_at=excluded.default_attempted_at, "
            "default_job_id=excluded.default_job_id, updated_at=excluded.updated_at",
            (person_id, AUTO, now, job_id, now),
        )
    return True


def finish_default(person_id: str, job_id: str, data: bytes | None, *, path: Path) -> None:
    """The default's job has stopped. Keep its picture when it made one, then forget the job.

    The picture becomes the person's avatar only while they still have the automatic choice and no sign-in
    picture. Otherwise it is kept for them to choose later. A job that is no longer the current one changes nothing.
    Every choice the person makes (a keep, a pick or a Remove) forgets the job, so a default that finishes after one
    publishes nothing and overwrites nothing.
    """
    now = int(time.time())
    with admin_store.transaction(path) as tx:
        row = _row(tx.conn, person_id)
        if row is None or row["default_job_id"] != job_id:
            return  # Not the current job: it was settled already, or the person was removed.
        picture = data if data is not None and image_type(data) and len(data) <= MAX_GENERATED_BYTES else None
        if picture is not None:
            try:
                _write_copy(person_id, picture, path=path)
            except OSError:
                picture = None  # The picture could not be kept, so the job ends without one.
        if picture is None:
            tx.conn.execute("UPDATE user_avatars SET default_job_id=NULL, updated_at=? WHERE user_id=?",
                            (now, person_id))
            return
        # The made picture is shown only while the person still has the automatic choice and no sign-in picture.
        # Otherwise it is kept, so the person can choose it later.
        choose = row["choice"] == AUTO and not row["picture_url"]
        tx.conn.execute(
            "UPDATE user_avatars SET choice=?, generated_template=?, generated_at=?, default_job_id=NULL, "
            "updated_at=? WHERE user_id=?",
            (GENERATED if choose else row["choice"], TEMPLATE_VERSION, now, now, person_id),
        )


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
        if choice == GENERATED and not _has_current_copy(person_id, row, path=path):
            raise AvatarError("generated_unavailable", "Make an avatar first.", 409)
        # Any choice settles the automatic default, and forgets a job that is still making it.
        conn.execute(
            "INSERT INTO user_avatars(user_id, choice, default_attempted_at, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET choice=excluded.choice, updated_at=excluded.updated_at, "
            "default_attempted_at=COALESCE(user_avatars.default_attempted_at, excluded.default_attempted_at), "
            "default_job_id=NULL",
            (person_id, choice, now, now),
        )
    return get(person_id, path=path)


def _has_current_copy(person_id: str, row: sqlite3.Row | None, *, path: Path) -> bool:
    if row is None or row["generated_at"] is None or row["generated_template"] != TEMPLATE_VERSION:
        return False
    return (generated_dir(person_id, path=path) / TEMPLATE_VERSION).is_file()


def record_sign_in_picture(person_id: str, claim: Any, *, path: Path) -> None:
    """Keep the ``picture`` claim of a verified sign-in.

    An absent claim leaves what is stored (some providers send it in some tokens only).
    A claim that is present and fails the checks clears it, so an unsafe URL is never kept.
    """
    if claim is None:
        return
    with admin_store.transaction(path) as tx:
        record_sign_in_picture_in(tx.conn, person_id, claim)


def record_sign_in_picture_in(
    conn: sqlite3.Connection,
    person_id: str,
    claim: Any,
    *,
    now: int | None = None,
) -> None:
    """Keep a provider picture inside the caller's transaction."""
    if claim is None:
        return
    url = clean_picture_url(claim)
    ts = int(time.time()) if now is None else int(now)
    row = _row(conn, person_id)
    if url is None:
        if row is not None and row["picture_url"]:
            conn.execute(
                "UPDATE user_avatars SET picture_url=NULL, picture_set_at=NULL, "
                "choice=CASE WHEN choice=? THEN ? ELSE choice END, updated_at=? WHERE user_id=?",
                (PICTURE, AUTO, ts, person_id),
            )
    elif row is None or row["picture_url"] != url:
        conn.execute(
            "INSERT INTO user_avatars(user_id, choice, picture_url, picture_set_at, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET picture_url=excluded.picture_url, "
            "picture_set_at=excluded.picture_set_at, updated_at=excluded.updated_at",
            (person_id, AUTO, url, ts, ts),
        )
