"""Secret references: how a stored record points at a secret without holding it (decision D26).

A record holds an opaque ``sref_...`` id and metadata that says the slot is configured; the value lives in
the encrypted, origin-bound store (``gateway/secret_store.py``). Everything here is management-safe: it
creates, replaces, deletes and counts secrets, and never decrypts one. Only ``gateway/runtime_secrets.py``
reads a value back, at the moment an engine request, a process start or a provider config needs it.

An *entry* is one secret slot of a record with a stable, server-issued id (``ent_...``) that does not change
when the entry is reordered, renamed or replaced. Commands name an entry by that id (decision D27).
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping

from gateway import secret_store, session_guard

_db_path: Path | None = None
_db_provider: Callable[[], Path] | None = None


def configure(db_path: Path | None) -> None:
    """Point every secret lookup at one admin database (a test sets it per database; None clears it)."""
    global _db_path
    _db_path = Path(db_path) if db_path is not None else None


def use_database_of(provider: Callable[[], Path]) -> None:
    """Follow a database path that can change (the gateway module's ``ADMIN_DB_PATH``)."""
    global _db_provider
    _db_provider = provider


def database() -> Path:
    """The admin database that holds the secrets: the one set, else the gateway's, else the default."""
    from gateway import admin_store

    if _db_path is not None:
        return _db_path
    if _db_provider is not None:
        return Path(_db_provider())
    return Path(admin_store.DB_PATH)


REF_PREFIX = "sref_"
ENTRY_PREFIX = "ent_"
_REF_RE = re.compile(r"^sref_[0-9a-f]{24}$")
_ENTRY_RE = re.compile(r"^ent_[0-9a-f]{16}$")

# ``put(value, origin) -> ref``: how a caller that holds a transaction or a path stores one secret.
Put = Callable[[str, "str | None"], str]


def new_ref() -> str:
    return REF_PREFIX + secrets.token_hex(12)


def new_entry_id() -> str:
    return ENTRY_PREFIX + secrets.token_hex(8)


def is_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(_REF_RE.fullmatch(value))


def is_entry_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ENTRY_RE.fullmatch(value))


def origin_or_none(url: str) -> str | None:
    """The origin a secret is bound to for an address, or None when the address has no HTTP(S) origin."""
    try:
        return secret_store.credential_origin_from_url(url)
    except ValueError:
        return None


def store(value: str, *, db_path: Path, origin: str | None = None, conn: sqlite3.Connection | None = None,
          now: int | None = None, session: session_guard.BrowserSession | None = None) -> str:
    """Store one secret value and return its new reference. With ``conn`` the write joins the caller's transaction.

    ``session`` is the browser sign-in that asked for it (decision D41), checked in the write's transaction."""
    ref = new_ref()
    secret_store.put_provider_secret(
        ref, value, db_path=db_path, approved_origin=origin, conn=conn, now=now, allow_short=True, keep_suffix=False,
        session=session)
    return ref


def putter(db_path: Path, conn: sqlite3.Connection | None = None, *,
           session: session_guard.BrowserSession | None = None) -> Put:
    """A ``Put`` that stores into ``db_path`` (inside ``conn``'s transaction when given)."""
    if conn is not None:
        secret_store.ensure_provider_secret_schema_on(conn)

    def put(value: str, origin: str | None = None) -> str:
        return store(value, db_path=db_path, origin=origin, conn=conn, session=session)

    return put


def configured(ref: str, *, db_path: Path | None = None) -> bool:
    """Whether a secret is stored under ``ref`` (any store id, a provider key's included). Metadata only: the
    value is never decrypted."""
    if not isinstance(ref, str) or not ref:
        return False
    try:
        return secret_store.provider_secret_status(ref, db_path=db_path or database()).state == "set"
    except Exception:  # noqa: BLE001 - an unreadable store reads as "not configured"
        return False


def delete(ref: str, *, db_path: Path, conn: sqlite3.Connection | None = None) -> None:
    if is_ref(ref):
        secret_store.delete_provider_secret(ref, db_path=db_path, conn=conn)


def is_secret_entry(item: Any) -> bool:
    """A stored slot that holds a reference: a mapping with an entry id and a secret reference."""
    return isinstance(item, Mapping) and is_ref(item.get("secret_ref"))


def entry(ref: str, **public: Any) -> dict[str, Any]:
    """A new secret entry: a fresh stable id, the reference, and the public parts (a name, a prefix)."""
    return {"id": new_entry_id(), **public, "secret_ref": ref}
