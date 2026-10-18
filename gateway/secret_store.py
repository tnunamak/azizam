"""Encrypted write-only provider secret store."""

from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import stat
import tempfile
import time
import fcntl
import hashlib
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gateway import sqlite_startup

DEFAULT_KEY_FILE = "provider-secrets.key"


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


def put_provider_secret(secret_id: str, value: str, *, db_path: Path, key_path: Path | None = None, now: int | None = None, approved_origin: str | None = None, conn: sqlite3.Connection | None = None, allow_short: bool = False, keep_suffix: bool = True) -> SecretStatus:
    """Store one secret. With ``conn``, write inside the caller's open transaction and leave the commit to
    the caller; the caller must have run :func:`ensure_provider_secret_schema` before it began.

    A provider key must be longer than 4 characters and keeps its last 4 as a display suffix. A secret
    entry of a record (a header, an environment value, a command argument, a URL credential) may be any
    non-empty text (``allow_short``) and keeps no suffix (``keep_suffix=False``): nothing of it is ever shown.
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
        conn.execute(insert, row)
    else:
        with closing(sqlite_startup.connect(db_path)) as own:
            own.execute(insert, row)
            own.commit()
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


def rebind_provider_secret(conn: sqlite3.Connection, secret_id: str, origin: str, *, db_path: Path) -> bool:
    """Bind a stored secret to ``origin`` (it is encrypted again under the new binding), inside the caller's transaction.

    False when it is already bound to that origin. Raises ``KeyError`` when it is missing."""
    value, stored_origin = read_provider_secret_on(conn, secret_id, db_path=db_path)
    if stored_origin == _normalize_origin(origin):
        return False
    put_provider_secret(secret_id, value, db_path=db_path, approved_origin=origin, conn=conn, allow_short=True, keep_suffix=False)
    return True


def delete_provider_secret(secret_id: str, *, db_path: Path, conn: sqlite3.Connection | None = None) -> SecretStatus:
    """Delete one secret. With ``conn``, delete inside the caller's open transaction (see :func:`put_provider_secret`)."""
    secret_id = _normalize_secret_id(secret_id)
    if conn is not None:
        conn.execute("DELETE FROM provider_secrets WHERE id = ?", (secret_id,))
        return SecretStatus(secret_id, "missing", None, None)
    ensure_provider_secret_schema(db_path)
    with closing(sqlite_startup.connect(db_path)) as own:
        own.execute("DELETE FROM provider_secrets WHERE id = ?", (secret_id,))
        own.commit()
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
