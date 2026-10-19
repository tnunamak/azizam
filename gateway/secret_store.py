"""Encrypted write-only provider secret store."""

from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import stat
import tempfile
import threading
import time
import fcntl
import hashlib
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gateway import log_scrub, session_guard, sqlite_startup

DEFAULT_KEY_FILE = "provider-secrets.key"

# What must wait for a commit: the scrubber forgets a deleted secret only once its deletion is durable (a rolled-back
# deletion leaves the secret stored and so still to be scrubbed). Keyed by connection; the connection is held so its id
# is not reused. Holds no secret value. ``transaction_begun`` drops what a transaction nobody settled left behind.
_PENDING: "dict[int, tuple[sqlite3.Connection, list[Callable[[], None]]]]" = {}
_PENDING_LOCK = threading.Lock()


def transaction_begun(conn: sqlite3.Connection) -> None:
    """A new write transaction starts on ``conn``: whatever an earlier, unsettled one deferred is dropped."""
    with _PENDING_LOCK:
        _PENDING.pop(id(conn), None)


def transaction_ended(conn: sqlite3.Connection, committed: bool) -> None:
    """The write transaction on ``conn`` committed (the deferred actions run) or rolled back (they are dropped)."""
    with _PENDING_LOCK:
        _, actions = _PENDING.pop(id(conn), (conn, []))
    if committed:
        for action in actions:
            action()


def _after_commit(conn: sqlite3.Connection | None, action: Callable[[], None]) -> None:
    """Run ``action`` now (no caller transaction; the caller has committed) or once the caller's transaction commits."""
    if conn is None:
        action()
        return
    with _PENDING_LOCK:
        _PENDING.setdefault(id(conn), (conn, []))[1].append(action)


@contextmanager
def provider_mutation_lock(db_path: Path):
    """Serialize provider registry/policy/secret mutations across workers.

    Registry CAS alone cannot protect the secret and provider-policy writes
    around create/remove/key/model/caps/plans changes. Hold this only for
    synchronous local writes, never across request-body reads or provider calls.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    path = db_path.parent / ".provider-mutation.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class SecretStatus:
    id: str
    state: str
    suffix: str | None
    updated_at: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "suffix": self.suffix,
            "updated_at": self.updated_at,
        }


def ensure_provider_secret_schema_on(conn: sqlite3.Connection) -> None:
    """Create or upgrade the secret table on ``conn``, inside whatever transaction it holds."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_secrets (
          id TEXT PRIMARY KEY,
          nonce_b64 TEXT NOT NULL,
          ciphertext_b64 TEXT NOT NULL,
          suffix TEXT NOT NULL,
          approved_origin TEXT,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_secrets)")}
    if "approved_origin" not in columns:
        conn.execute("ALTER TABLE provider_secrets ADD COLUMN approved_origin TEXT")


def ensure_provider_secret_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite_startup.connect(path)) as conn:
        ensure_provider_secret_schema_on(conn)
        conn.commit()


def put_provider_secret(secret_id: str, value: str, *, db_path: Path, key_path: Path | None = None, now: int | None = None, approved_origin: str | None = None, conn: sqlite3.Connection | None = None, allow_short: bool = False, keep_suffix: bool = True, session: session_guard.BrowserSession | None = None) -> SecretStatus:
    """Store one secret. With ``conn``, write inside the caller's open transaction and leave the commit to
    the caller; the caller must have run :func:`ensure_provider_secret_schema` before it began.

    A provider key must be longer than 4 characters and keeps its last 4 as a display suffix. A secret
    entry of a record (a header, an environment value, a command argument, a URL credential) may be any
    non-empty text (``allow_short``) and keeps no suffix (``keep_suffix=False``): nothing of it is ever shown.

    ``session`` is the browser sign-in that asked for the write (decision D41). It is checked in the write's
    own transaction, so a sign-out that committed first stores nothing. None for a write no browser started
    (a migration).
    """
    secret_id = _normalize_secret_id(secret_id)
    approved_origin = _normalize_origin(approved_origin) if approved_origin else None
    if not value:
        raise ValueError("secret value is required")
    if len(value) <= 4 and not allow_short:
        raise ValueError("secret value must be longer than 4 characters")
    if conn is None:
        ensure_provider_secret_schema(db_path)
    key = load_or_create_secret_key(db_path.parent / DEFAULT_KEY_FILE if key_path is None else key_path)
    nonce = os.urandom(12)
    aad = _secret_aad(secret_id, approved_origin)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), aad)
    timestamp = int(time.time() if now is None else now)
    suffix = value[-4:] if keep_suffix else ""
    insert = """
            INSERT INTO provider_secrets(id, nonce_b64, ciphertext_b64, suffix, approved_origin, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              nonce_b64=excluded.nonce_b64,
              ciphertext_b64=excluded.ciphertext_b64,
              suffix=excluded.suffix,
              approved_origin=excluded.approved_origin,
              updated_at=excluded.updated_at
            """
    row = (
        secret_id,
        base64.b64encode(nonce).decode("ascii"),
        base64.b64encode(ciphertext).decode("ascii"),
        suffix,
        approved_origin,
        timestamp,
        timestamp,
    )
    if conn is not None:
        session_guard.require_live(conn, session)
        conn.execute(insert, row)
    else:
        with closing(sqlite_startup.connect(db_path)) as own:
            own.row_factory = sqlite3.Row   # the sign-in check reads rows by name
            session_guard.begin_write(own, session)
            own.execute(insert, row)
            own.commit()
    # Registered at once, committed or not: a value that was never stored is scrubbed for nothing, and a rolled-back
    # replacement leaves the stored value in the retired ones. The registry never drops a value because of this call.
    log_scrub.hold(log_scrub.secret_source(secret_id), [value])
    return SecretStatus(secret_id, "set", suffix, timestamp)


def get_provider_secret(secret_id: str, *, db_path: Path, key_path: Path | None = None, expected_origin: str | None = None) -> str:
    secret_id = _normalize_secret_id(secret_id)
    expected_origin = _normalize_origin(expected_origin) if expected_origin else None
    ensure_provider_secret_schema(db_path)
    key = load_secret_key(db_path.parent / DEFAULT_KEY_FILE if key_path is None else key_path)
    with closing(sqlite_startup.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT nonce_b64, ciphertext_b64, approved_origin FROM provider_secrets WHERE id = ?",
            (secret_id,),
        ).fetchone()
    if row is None:
        raise KeyError(secret_id)
    stored_origin = str(row[2]) if row[2] is not None else None
    if expected_origin is not None and stored_origin != expected_origin:
        raise PermissionError("provider secret is not approved for this endpoint origin")
    nonce = base64.b64decode(row[0])
    ciphertext = base64.b64decode(row[1])
    return AESGCM(key).decrypt(nonce, ciphertext, _secret_aad(secret_id, stored_origin)).decode("utf-8")


def provider_secret_origin(secret_id: str, *, db_path: Path) -> str | None:
    """The origin a stored secret is bound to (metadata only; the value is not decrypted), or None when it has none or is missing."""
    secret_id = _normalize_secret_id(secret_id)
    ensure_provider_secret_schema(db_path)
    with closing(sqlite_startup.connect(db_path)) as conn:
        row = conn.execute("SELECT approved_origin FROM provider_secrets WHERE id = ?", (secret_id,)).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def read_provider_secret_on(conn: sqlite3.Connection, secret_id: str, *, db_path: Path) -> tuple[str, str | None]:
    """``(value, bound origin)`` of a stored secret, read inside the caller's transaction (a migration). Raises ``KeyError``
    when it is missing. The caller decides what the value may be used for."""
    secret_id = _normalize_secret_id(secret_id)
    key = load_secret_key(db_path.parent / DEFAULT_KEY_FILE)
    row = conn.execute("SELECT nonce_b64, ciphertext_b64, approved_origin FROM provider_secrets WHERE id = ?", (secret_id,)).fetchone()
    if row is None:
        raise KeyError(secret_id)
    stored_origin = str(row[2]) if row[2] is not None else None
    value = AESGCM(key).decrypt(base64.b64decode(row[0]), base64.b64decode(row[1]), _secret_aad(secret_id, stored_origin)).decode("utf-8")
    return value, stored_origin


def bind_provider_secrets(items: "list[tuple[str, Any]]", *, db_path: Path, guard: "Callable[[sqlite3.Connection], None] | None" = None) -> int:
    """Bind stored secrets to an origin, all or none, in one transaction. Returns how many changed.

    Each item is ``(secret_id, origin)``; ``origin`` is an origin text, or a function of the secret's own value that returns
    one (a URL kept whole has its address inside the secret). The value is read and sealed again inside this function
    and goes nowhere else. Raises ``KeyError`` for a missing secret and ``ValueError`` when no origin results; nothing
    is changed then.

    This is the one function that opens the binding transaction. ``guard(conn)``, when given, runs first, inside it, on the
    same connection (so it reads the registry and the sign-in tables as they are at the moment of the write); whatever it
    raises ends the call with nothing bound. The destination check of ``confirm_address`` is a guard, and so is the check that
    the signed-in session is still live (``session_guard.require_live``).
    """
    ensure_provider_secret_schema(db_path)
    changed = 0
    with closing(sqlite_startup.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row   # the sign-in check reads rows by name
        try:
            conn.execute("BEGIN IMMEDIATE")
            transaction_begun(conn)
            if guard is not None:
                guard(conn)
            for secret_id, origin in items:
                value, stored_origin = read_provider_secret_on(conn, secret_id, db_path=db_path)
                target = origin(value) if callable(origin) else origin
                if not target:
                    raise ValueError("a stored credential has no address to bind to")
                if stored_origin == _normalize_origin(target):
                    continue
                put_provider_secret(secret_id, value, db_path=db_path, approved_origin=target, conn=conn, allow_short=True, keep_suffix=False)
                changed += 1
            conn.commit()
        except BaseException:
            conn.rollback()
            transaction_ended(conn, False)
            raise
        transaction_ended(conn, True)
    return changed


def delete_provider_secret(secret_id: str, *, db_path: Path, conn: sqlite3.Connection | None = None,
                           session: session_guard.BrowserSession | None = None) -> SecretStatus:
    """Delete one secret. With ``conn``, delete inside the caller's open transaction (see :func:`put_provider_secret`).

    ``session`` is the browser sign-in that asked for the delete, checked in its transaction (decision D41).
    Leave it out for clean-up of a secret this same request just stored or a save has just replaced: that must
    run even when the sign-in has ended.
    """
    secret_id = _normalize_secret_id(secret_id)
    forget = lambda: log_scrub.forget(log_scrub.secret_source(secret_id))  # noqa: E731 - the registry is bounded by the secrets that exist
    if conn is not None:
        session_guard.require_live(conn, session)
        conn.execute("DELETE FROM provider_secrets WHERE id = ?", (secret_id,))
        _after_commit(conn, forget)  # not before the caller's commit: a rollback leaves the secret stored
        return SecretStatus(secret_id, "missing", None, None)
    ensure_provider_secret_schema(db_path)
    with closing(sqlite_startup.connect(db_path)) as own:
        own.row_factory = sqlite3.Row   # the sign-in check reads rows by name
        session_guard.begin_write(own, session)
        own.execute("DELETE FROM provider_secrets WHERE id = ?", (secret_id,))
        own.commit()
    _after_commit(None, forget)
    return SecretStatus(secret_id, "missing", None, None)


def provider_secret_status(secret_id: str, *, db_path: Path) -> SecretStatus:
    secret_id = _normalize_secret_id(secret_id)
    ensure_provider_secret_schema(db_path)
    with closing(sqlite_startup.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT suffix, updated_at FROM provider_secrets WHERE id = ?",
            (secret_id,),
        ).fetchone()
    if row is None:
        return SecretStatus(secret_id, "missing", None, None)
    return SecretStatus(secret_id, "set", str(row[0]), int(row[1]))


def provider_secret_revision(secret_id: str, *, db_path: Path) -> str:
    secret_id = _normalize_secret_id(secret_id)
    ensure_provider_secret_schema(db_path)
    with closing(sqlite_startup.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT nonce_b64, ciphertext_b64 FROM provider_secrets WHERE id = ?",
            (secret_id,),
        ).fetchone()
    if row is None:
        return "missing"
    payload = f"{secret_id}\0{row[0]}\0{row[1]}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def export_provider_secret_env(db_path: Path, secret_refs: dict[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for env_name, secret_id in secret_refs.items():
        env[str(env_name)] = get_provider_secret(str(secret_id), db_path=db_path)
    return env


def credential_origin_from_url(value: str) -> str:
    return _normalize_origin(value)


def load_secret_key(path: Path) -> bytes:
    if path.exists():
        _ensure_key_file_permissions(path)
        raw = path.read_bytes()
        key = base64.b64decode(raw)
        if len(key) != 32:
            raise ValueError(f"provider secret key file {path} is invalid")
        return key
    raise FileNotFoundError(path)


def load_or_create_secret_key(path: Path) -> bytes:
    if path.exists():
        return load_secret_key(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = AESGCM.generate_key(bit_length=256)
    # Publish a complete key atomically; a concurrent first write never reads
    # the other writer's empty, newly-created file.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.") as handle:
        handle.write(base64.b64encode(key))
        handle.flush()
        os.fsync(handle.fileno())
        try:
            os.link(handle.name, path)
        except FileExistsError:
            return load_secret_key(path)
    _ensure_key_file_permissions(path)
    return key


def _ensure_key_file_permissions(path: Path) -> None:
    file_stat = path.lstat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("provider secret key must be a regular file")
    mode = stat.S_IMODE(file_stat.st_mode)
    if mode != 0o600:
        os.chmod(path, 0o600)


def _normalize_secret_id(value: str) -> str:
    secret_id = str(value or "").strip()
    if not secret_id:
        raise ValueError("secret id is required")
    return secret_id


def _normalize_origin(value: str | None) -> str:
    parsed = urlsplit(str(value or "").strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise ValueError("provider credential origin requires an HTTP(S) URL")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("provider credential origin has an invalid port") from None
    if port is None:
        port = 443 if scheme == "https" else 80
    authority_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{authority_host}:{port}"


def _secret_aad(secret_id: str, approved_origin: str | None) -> bytes:
    origin = approved_origin or ""
    return f"{secret_id}\0{origin}".encode("utf-8")
