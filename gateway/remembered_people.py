"""The names a browser keeps for its sign-in list (decision D54, slice A3).

A browser holds one opaque cookie, ``__Host-azizam-device``: 32 random bytes that name the device. Its SHA-256 is the
key of the device's rows in ``remembered_people``. The page never sees a person id or an email. It gets one tile per
remembered person: an HMAC of the device key and the person id, under a key derived from the gateway's signing key.
A tile is stable on its own device, useless on another, and does not show the id it stands for.

A tile names a row, never a session. Signing in still needs the person's own password or their provider sign-in
(gateway.accounts_api and proxy.py). Removing or disabling a person deletes their rows with the change. A row unused
for 90 days is swept by the next sign-in that writes to the list. A row of someone who can no longer sign in is deleted
the next time the list, a tile sign-in or a forget reads its rows; a row hidden only because its sign-in method is off
stays stored.
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


def box_value(query: str | None) -> bool | None:
    """The sign-in page's box as its query carries it: ``1`` ticked, ``0`` unticked, anything else not sent."""
    return {"1": True, "0": False}.get(query or "")


def _mode_of(record: Mapping[str, Any] | None) -> str:
    """Nothing saved means remember; a value it does not know means nobody."""
    if record is None:
        return REMEMBER
    value = record["value"]
    return REMEMBER if isinstance(value, Mapping) and value.get("mode") == REMEMBER else NOBODY


def memory_mode(path: Path) -> str:
    """The owner's "On shared computers" choice."""
    return _mode_of(admin_store.get_operator_setting(SETTING_KEY, path=path))


def _memory_mode_in(conn) -> str:
    """:func:`memory_mode` as the open write transaction sees it, so a switch cannot land mid-write."""
    return _mode_of(admin_store.get_operator_setting_in(conn, SETTING_KEY))


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


def _name(row: Mapping[str, Any]) -> tuple[str, bool]:
    """The name on the person's account, read now (a rename shows without a new sign-in), and whether it may take a hint.

    A provider's name may take a hint unless it is the address's own local part: beside a hint, ``alice`` for
    alice@corp.test would give the whole address away. A provider that sends no name leaves the address there instead, so
    an address is never the name: the part before the @ stands in for it, and that stand-in takes no hint.
    """
    name = (row["display_name"] or "").strip()
    local = (row["email"] or "").split("@", 1)[0].strip()
    if name and "@" not in name:
        return name, name.casefold() != local.casefold()
    return local or "Person", False


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
    """Two provider names that match need telling apart: the first letter of the address and its domain."""
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    return f"{local[:1]}…@{domain}"


def listing(token: str | None, *, path: Path, signing_key: bytes | None, offered: Callable[[Mapping[str, Any]], bool],
            provider_label: str, current_person_id: str | None = None,
            now_ms: int | None = None) -> list[dict[str, Any]]:
    """What the sign-in page may show for this browser: name, avatar, method, provider and tile, nothing else.

    ``current_person_id`` is the person a live session of this browser is signed in as. Their entry, if it is listed,
    carries ``"you": True``. No other entry carries the key, so a browser without that session gets the list as it was.
    """
    if token is None or not signing_key:
        return []
    device = device_key(token)
    with accounts.connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        live = _live(conn, device, _now_ms() if now_ms is None else now_ms)
    rows = [row for row in live if offered(row)]
    names = {row["person_id"]: _name(row) for row in rows}
    counts = Counter(name.casefold() for name, _ in names.values())
    choices = avatars.get_many(names.keys(), path=path)
    people = []
    for row in rows:
        name, may_hint = names[row["person_id"]]
        choice = choices[row["person_id"]]["choice"]
        entry = {
            "tile": tile_for(signing_key, device, row["person_id"]),
            "name": name,
            "avatar": {"variant": choice if choice in avatars.VARIANTS else _auto_variant(row["person_id"]),
                       "initial": _initial(name)},
            "method": row["method"],
            "provider": provider_label if row["method"] == "sso" else None,
            # Two provider names that match are told apart by the start of their address. A name taken from an
            # address, or one equal to its local part, never shows one: with the hint it would give the address away.
            "hint": _hint(row["email"]) if may_hint and counts[name.casefold()] > 1 else None,
        }
        if row["person_id"] == current_person_id:
            entry["you"] = True
        people.append(entry)
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
         remember: bool | None, from_tile: bool) -> str | None:
    """Apply one successful sign-in to this browser's list, as the sign-in asked.

    ``remember`` is the sign-in page's box: True keeps the person's name on this browser, False takes it off, and None
    (not sent) leaves the list alone. ``from_tile`` marks a sign-in that started from one of this list's tiles: it
    refreshes the name the list already holds and never adds one. A call that writes also sweeps the names unused for 90
    days, on every browser. Returns a new device token only if it stored a name under it; the caller sets it as the cookie.
    """
    if remember is None and not from_tile:
        return None
    fresh = None
    if token is None:
        if remember is not True:
            return None
        fresh = token = new_device_token()
    device = device_key(token)
    now = _now_ms()
    kept = False
    with accounts.connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM remembered_people WHERE last_signed_in_at<?", (now - MAX_AGE_MS,))
        if remember is False:
            conn.execute("DELETE FROM remembered_people WHERE device_hash=? AND person_id=?", (device, person_id))
        elif _memory_mode_in(conn) == REMEMBER:
            known = conn.execute("SELECT 1 FROM remembered_people WHERE device_hash=? AND person_id=?",
                                 (device, person_id)).fetchone()
            if known is not None:
                conn.execute("UPDATE remembered_people SET method=?, provider_issuer=?, last_signed_in_at=? "
                             "WHERE device_hash=? AND person_id=?", (method, issuer, now, device, person_id))
            elif remember and not from_tile:
                conn.execute("INSERT INTO remembered_people(device_hash, person_id, method, provider_issuer, "
                             "last_signed_in_at, created_at) VALUES(?,?,?,?,?,?)",
                             (device, person_id, method, issuer, now, now))
                kept = True
                # At most MAX_PEOPLE names: the one used longest ago goes.
                conn.execute("DELETE FROM remembered_people WHERE device_hash=? AND person_id NOT IN ("
                             "SELECT person_id FROM remembered_people WHERE device_hash=? "
                             "ORDER BY last_signed_in_at DESC, rowid DESC LIMIT ?)", (device, device, MAX_PEOPLE))
    return fresh if kept else None
