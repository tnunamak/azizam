"""The names a browser keeps for its sign-in list (decision D54, slice A3).

A browser holds one opaque cookie, ``__Host-azizam-device``: 32 random bytes that name the device. Its SHA-256 is the
key of the device's rows in ``remembered_people``. The page never sees a person id or an email. It gets one tile per
remembered person: an HMAC of the device key and the person id, under a key derived from the gateway's signing key.
A tile is stable on its own device, useless on another, and does not show the id it stands for.

A tile names a row, never a session. Signing in still needs the person's own password or their provider sign-in
(gateway.accounts_api and proxy.py). Removed, disabled and 90-day-old rows are not offered, and they are deleted the
first time they are read.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import Request, Response

from gateway import accounts, admin_store, avatars

SETTING_KEY = "sign_in_memory"
REMEMBER = "remember"
NOBODY = "nobody"
MAX_PEOPLE = 10
MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000
DEVICE_COOKIE = "azizam-device"
DEVICE_MAX_AGE_SECONDS = 400 * 24 * 60 * 60
_TILE_KEY_LABEL = b"azizam remembered-people tile key v1"


def cookie_name(secure: bool) -> str:
    """``__Host-`` makes a browser keep the cookie only over HTTPS, with Secure, Path=/ and no Domain.

    A plain-HTTP gateway (the documented setting for local use) cannot carry a Secure cookie, so it drops the prefix
    and the flag, as gateway.oidc_browser_flow does for its own state cookie.
    """
    return "__Host-" + DEVICE_COOKIE if secure else DEVICE_COOKIE


def new_device_token() -> str:
    return secrets.token_urlsafe(32)


def device_token(request: Request, *, secure: bool) -> str | None:
    return request.cookies.get(cookie_name(secure)) or None


def set_device_cookie(response: Response, token: str, *, secure: bool) -> None:
    response.set_cookie(cookie_name(secure), token, max_age=DEVICE_MAX_AGE_SECONDS, secure=secure,
                        httponly=True, samesite="lax", path="/")


def device_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def memory_mode(path: Path) -> str:
    """The owner's "On shared computers" choice. Nothing saved means remember; a value it does not know means nobody."""
    record = admin_store.get_operator_setting(SETTING_KEY, path=path)
    if record is None:
        return REMEMBER
    value = record["value"]
    return REMEMBER if isinstance(value, Mapping) and value.get("mode") == REMEMBER else NOBODY


def forget_everyone_in(conn) -> None:
    """Remember nobody: every browser's list empties. The caller's transaction commits the owner's setting with it."""
    conn.execute("DELETE FROM remembered_people")


def tile_for(signing_key: bytes, device: str, person_id: str) -> str:
    tile_key = hmac.new(signing_key, _TILE_KEY_LABEL, hashlib.sha256).digest()
    digest = hmac.new(tile_key, device.encode() + b"\x00" + person_id.encode(), hashlib.sha256).digest()
    return "t_" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _live(conn, device: str, now_ms: int) -> list[dict[str, Any]]:
    """This device's rows whose person may still sign in (not removed, disabled, gone or 90 days idle), newest first.

    The others are deleted here, on read. Whether a live row is offered depends on the sign-in methods, which the caller
    decides.
    """
    rows = conn.execute(
        "SELECT r.person_id, r.method, r.provider_issuer, r.last_signed_in_at, "
        "u.id AS user_id, u.email, u.display_name, a.disabled_at, a.removed_at "
        "FROM remembered_people r LEFT JOIN users u ON u.id = r.person_id "
        "LEFT JOIN account_access a ON a.user_id = r.person_id "
        "WHERE r.device_hash = ? ORDER BY r.last_signed_in_at DESC, r.rowid DESC",
        (device,),
    ).fetchall()
    live: list[dict[str, Any]] = []
    for row in rows:
        gone = (row["user_id"] is None or row["removed_at"] is not None or row["disabled_at"] is not None
                or now_ms - row["last_signed_in_at"] > MAX_AGE_MS)
        if gone:
            conn.execute("DELETE FROM remembered_people WHERE device_hash=? AND person_id=?", (device, row["person_id"]))
        else:
            live.append(dict(row))
    return live


def _name(row: Mapping[str, Any]) -> str:
    """The name on the person's account, read now, so a rename shows without a new sign-in.

    A provider that sends no name leaves the address there instead, so an address is never the name: the part before
    the @ stands in for it.
    """
    name = (row["display_name"] or "").strip()
    if name and "@" not in name:
        return name
    return (row["email"] or "").split("@", 1)[0].strip() or "Person"


def _initial(name: str) -> str:
    """The first letter or digit, as the web's getAvatarInitials picks it, with any marks after it."""
    text = unicodedata.normalize("NFC", name)
    for index, char in enumerate(text):
        if unicodedata.category(char)[0] in "LN":
            end = index + 1
            while end < len(text) and unicodedata.category(text[end]).startswith("M"):
                end += 1
            return text[index:end].upper()
    return "?"


def _auto_variant(person_id: str) -> str:
    """The palette the web gives a person with no choice: FNV-1a over the id (person-avatar.tsx)."""
    units = person_id.encode("utf-16-le")
    value = 2166136261
    for index in range(0, len(units), 2):
        value = ((value ^ int.from_bytes(units[index:index + 2], "little")) * 16777619) & 0xFFFFFFFF
    return avatars.VARIANTS[value % len(avatars.VARIANTS)]


def _hint(email: str | None) -> str | None:
    """Only two names that match need telling apart: the first letter of the address and its domain."""
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    return f"{local[:1]}…@{domain}"


def listing(token: str | None, *, path: Path, signing_key: bytes | None, offered: Callable[[Mapping[str, Any]], bool],
            provider_label: str, now_ms: int | None = None) -> list[dict[str, Any]]:
    """What the sign-in page may show for this browser: name, avatar, method, provider and tile, nothing else."""
    if token is None or not signing_key:
        return []
    device = device_key(token)
    with accounts.connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        live = _live(conn, device, _now_ms() if now_ms is None else now_ms)
    rows = [row for row in live if offered(row)]
    names = {row["person_id"]: _name(row) for row in rows}
    counts = Counter(name.casefold() for name in names.values())
    choices = avatars.get_many(names.keys(), path=path)
    people = []
    for row in rows:
        name = names[row["person_id"]]
        choice = choices[row["person_id"]]["choice"]
        people.append({
            "tile": tile_for(signing_key, device, row["person_id"]),
            "name": name,
            "avatar": {"variant": choice if choice in avatars.VARIANTS else _auto_variant(row["person_id"]),
                       "initial": _initial(name)},
            "method": row["method"],
            "provider": provider_label if row["method"] == "sso" else None,
            # Two people with the same name are told apart by the start of their address.
            "hint": _hint(row["email"]) if counts[name.casefold()] > 1 else None,
        })
    return people


def resolve(token: str | None, tile: str, *, path: Path, signing_key: bytes | None) -> dict[str, Any] | None:
    """The live row this browser's tile names, or None. A tile from another device names nothing here."""
    if token is None or not signing_key:
        return None
    device = device_key(token)
    with accounts.connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        live = _live(conn, device, _now_ms())
    for row in live:
        if hmac.compare_digest(tile_for(signing_key, device, row["person_id"]).encode(), tile.encode()):
            return row
    return None


def forget(token: str | None, tile: str, *, path: Path, signing_key: bytes | None) -> None:
    """Take one name off this browser's list. Only the name goes: the person keeps their account and their sign-in."""
    if token is None or not signing_key:
        return
    device = device_key(token)
    with accounts.connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for row in _live(conn, device, _now_ms()):
            if hmac.compare_digest(tile_for(signing_key, device, row["person_id"]).encode(), tile.encode()):
                conn.execute("DELETE FROM remembered_people WHERE device_hash=? AND person_id=?",
                             (device, row["person_id"]))


def keep(*, path: Path, token: str | None, person_id: str, method: str, issuer: str | None,
         remember: bool, from_tile: bool) -> str | None:
    """Record a successful sign-in on this browser's list, when the person asked for it or came from their tile.

    ``remember`` is the sign-in page's box, or the person's own choice. ``from_tile`` only refreshes a name the list
    already holds: a tile sign-in keeps its row, and never adds one. Returns a new device token when one was minted,
    which the caller sets as the cookie.
    """
    if not (remember or from_tile) or memory_mode(path) != REMEMBER:
        return None
    minted = None
    if token is None:
        if not remember:
            return None
        minted = token = new_device_token()
    device = device_key(token)
    now = _now_ms()
    with accounts.connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        known = conn.execute("SELECT 1 FROM remembered_people WHERE device_hash=? AND person_id=?",
                             (device, person_id)).fetchone()
        if known is not None:
            conn.execute("UPDATE remembered_people SET method=?, provider_issuer=?, last_signed_in_at=? "
                         "WHERE device_hash=? AND person_id=?", (method, issuer, now, device, person_id))
        elif remember:
            conn.execute("INSERT INTO remembered_people(device_hash, person_id, method, provider_issuer, "
                         "last_signed_in_at, created_at) VALUES(?,?,?,?,?,?)",
                         (device, person_id, method, issuer, now, now))
            # At most MAX_PEOPLE names: the one used longest ago goes.
            conn.execute("DELETE FROM remembered_people WHERE device_hash=? AND person_id NOT IN ("
                         "SELECT person_id FROM remembered_people WHERE device_hash=? "
                         "ORDER BY last_signed_in_at DESC, rowid DESC LIMIT ?)", (device, device, MAX_PEOPLE))
    return minted
