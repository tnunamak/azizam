"""Persistence for the MCP OAuth authorization server (gateway/mcp_oauth.py).

Tables live in the admin store's SQLite file, beside the people they belong to.
Secrets never rest in plain text: authorization requests, codes, access and
refresh tokens and client secrets are stored as SHA-256 hashes of
high-entropy random values, so a copy of the database grants nothing.

A *grant* is one connection: one person approved one client. Every code and
token belongs to exactly one grant, and revoking the grant is what revokes
them, so a connection ends at once and as a whole. Re-approving a client makes
a new grant; it never revives an old one.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from gateway.session_guard import BrowserSession, begin_write

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
  client_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  redirect_uris_json TEXT NOT NULL,
  grant_types_json TEXT NOT NULL,
  auth_method TEXT NOT NULL,
  secret_hash TEXT,
  metadata_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  refreshed_at INTEGER NOT NULL,
  fresh_until INTEGER
);
CREATE TABLE IF NOT EXISTS mcp_oauth_requests (
  id_hash TEXT PRIMARY KEY,
  binding_hash TEXT NOT NULL,
  client_id TEXT NOT NULL,
  params_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at INTEGER
);
CREATE TABLE IF NOT EXISTS mcp_oauth_grants (
  id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  client_name TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  resource TEXT NOT NULL,
  scope TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_used_at INTEGER,
  revoked_at INTEGER,
  revoked_reason TEXT
);
CREATE INDEX IF NOT EXISTS mcp_oauth_grants_person ON mcp_oauth_grants(person_id, revoked_at);
CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
  code_hash TEXT PRIMARY KEY,
  grant_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  scope TEXT NOT NULL,
  resource TEXT NOT NULL,
  code_challenge TEXT NOT NULL,
  code_challenge_method TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at INTEGER
);
CREATE TABLE IF NOT EXISTS mcp_oauth_tokens (
  token_hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  grant_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  issued_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at INTEGER
);
CREATE INDEX IF NOT EXISTS mcp_oauth_tokens_grant ON mcp_oauth_tokens(grant_id);
"""

# How long a registered client that never connected anyone is kept.
UNUSED_CLIENT_SECONDS = 30 * 24 * 3600
# Spent and expired rows stay a day so replays are recognised, then go.
SPENT_ROW_SECONDS = 24 * 3600
LAST_USED_DEBOUNCE_SECONDS = 60


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_secret(prefix: str = "", nbytes: int = 32) -> str:
    return prefix + secrets.token_urlsafe(nbytes)


@dataclass(frozen=True)
class ClientRecord:
    client_id: str
    kind: str  # "cimd" (a Client ID Metadata Document URL) or "registered" (RFC 7591)
    name: str
    redirect_uris: tuple[str, ...]
    grant_types: tuple[str, ...]
    auth_method: str
    secret_hash: str | None
    metadata: Mapping[str, Any]
    refreshed_at: int
    fresh_until: int | None


@dataclass(frozen=True)
class GrantRecord:
    id: str
    person_id: str
    client_id: str
    client_name: str
    redirect_uri: str
    resource: str
    scope: str
    created_at: int
    last_used_at: int | None
    revoked_at: int | None


@dataclass(frozen=True)
class CodeRecord:
    code_hash: str
    grant_id: str
    client_id: str
    redirect_uri: str
    scope: str
    resource: str
    code_challenge: str
    code_challenge_method: str
    expires_at: int
    used_at: int | None


@dataclass(frozen=True)
class TokenRecord:
    token_hash: str
    kind: str  # "access" or "refresh"
    grant_id: str
    client_id: str
    scope: str
    issued_at: int
    expires_at: int
    used_at: int | None


def _client(row: sqlite3.Row) -> ClientRecord:
    return ClientRecord(
        client_id=row["client_id"], kind=row["kind"], name=row["name"],
        redirect_uris=tuple(json.loads(row["redirect_uris_json"])),
        grant_types=tuple(json.loads(row["grant_types_json"])),
        auth_method=row["auth_method"], secret_hash=row["secret_hash"],
        metadata=json.loads(row["metadata_json"]),
        refreshed_at=row["refreshed_at"], fresh_until=row["fresh_until"],
    )


def _grant(row: sqlite3.Row) -> GrantRecord:
    return GrantRecord(**{key: row[key] for key in GrantRecord.__dataclass_fields__})


def grants_in(conn: sqlite3.Connection, person_ids: "Iterable[str] | None" = None) -> list[GrantRecord]:
    """Every connection of the given people (``None``: everyone), revoked ones
    included, newest first, read through ``conn`` (an admin database
    connection, for a request that already holds one). Read only: a database
    that has no connections yet gives an empty list and is not changed. Usage
    history names a connection long after it ends."""
    people = None if person_ids is None else sorted({str(person) for person in person_ids})
    if people == []:
        return []
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mcp_oauth_grants'").fetchone() is None:
        return []
    query = "SELECT * FROM mcp_oauth_grants"
    args: list[str] = []
    if people is not None:
        query += " WHERE person_id IN (" + ",".join("?" for _ in people) + ")"
        args = people
    previous, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        return [_grant(row) for row in conn.execute(query + " ORDER BY created_at DESC, id", args).fetchall()]
    finally:
        conn.row_factory = previous


class Store:
    """All SQL for the authorization server. ``path`` is read on every call so
    tests and the gateway can point it at their own admin database."""

    def __init__(self, path: Callable[[], Path], *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._ready: set[str] = set()
        self._lock = threading.Lock()

    def now(self) -> int:
        return int(self._clock())

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        from gateway import private_io
        db_path = self._path()
        private_io.prepare_private_database(db_path)
        path = str(db_path)
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            if path not in self._ready:
                with self._lock:
                    conn.executescript(_SCHEMA)
                    self._ready.add(path)
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------- clients

    def client(self, client_id: str) -> ClientRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM mcp_oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        return _client(row) if row else None

    def put_client(self, *, client_id: str, kind: str, name: str, redirect_uris: list[str],
                   grant_types: list[str], auth_method: str, secret_hash: str | None,
                   metadata: Mapping[str, Any], fresh_until: int | None = None) -> ClientRecord:
        now = self.now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO mcp_oauth_clients(client_id, kind, name, redirect_uris_json, grant_types_json, "
                "auth_method, secret_hash, metadata_json, created_at, refreshed_at, fresh_until) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(client_id) DO UPDATE SET name = excluded.name, "
                "redirect_uris_json = excluded.redirect_uris_json, grant_types_json = excluded.grant_types_json, "
                "auth_method = excluded.auth_method, metadata_json = excluded.metadata_json, "
                "refreshed_at = excluded.refreshed_at, fresh_until = excluded.fresh_until "
                "WHERE mcp_oauth_clients.kind = excluded.kind",
                (client_id, kind, name, json.dumps(redirect_uris), json.dumps(grant_types), auth_method,
                 secret_hash, json.dumps(dict(metadata)), now, now, fresh_until),
            )
            conn.commit()
        record = self.client(client_id)
        assert record is not None
        return record

    # ------------------------------------------------- authorization requests

    def save_request(self, request_id: str, binding: str, client_id: str, params: list[tuple[str, str]],
                     ttl: int) -> None:
        now = self.now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO mcp_oauth_requests(id_hash, binding_hash, client_id, params_json, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (digest(request_id), digest(binding), client_id, json.dumps(params), now, now + ttl),
            )
            conn.commit()

    def peek_request(self, request_id: str, binding: str | None) -> list[tuple[str, str]] | None:
        """The pending request's parameters, or None if it is unknown, spent,
        expired or was started in a different browser."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM mcp_oauth_requests WHERE id_hash = ?", (digest(request_id),)).fetchone()
        if (row is None or row["used_at"] is not None or row["expires_at"] <= self.now()
                or not binding or not secrets.compare_digest(row["binding_hash"], digest(binding))):
            return None
        return [tuple(item) for item in json.loads(row["params_json"])]

    def take_request(self, request_id: str, binding: str | None) -> list[tuple[str, str]] | None:
        """Spend the request once: a second decision, or one from another
        browser, finds nothing."""
        params = self.peek_request(request_id, binding)
        if params is None:
            return None
        with self._conn() as conn:
            spent = conn.execute(
                "UPDATE mcp_oauth_requests SET used_at = ? WHERE id_hash = ? AND used_at IS NULL",
                (self.now(), digest(request_id)),
            ).rowcount
            conn.commit()
        return params if spent == 1 else None

    # ------------------------------------------------------------- grants

    def create_grant(self, *, person_id: str, client: ClientRecord, redirect_uri: str, resource: str,
                     scope: str, session: BrowserSession | None) -> GrantRecord:
        """Record that a person approved a client. ``session`` is the browser sign-in that approved it,
        checked in the insert's own transaction (``gateway/session_guard.py``); ``SessionEnded`` if it ended."""
        grant_id = new_secret("mcg_", 12)
        with self._conn() as conn:
            begin_write(conn, session)
            conn.execute(
                "INSERT INTO mcp_oauth_grants(id, person_id, client_id, client_name, redirect_uri, resource, scope, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (grant_id, person_id, client.client_id, client.name, redirect_uri, resource, scope, self.now()),
            )
            conn.commit()
        grant = self.grant(grant_id)
        assert grant is not None
        return grant

    def grant(self, grant_id: str) -> GrantRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM mcp_oauth_grants WHERE id = ?", (grant_id,)).fetchone()
        return _grant(row) if row else None

    def active_grant(self, grant_id: str) -> GrantRecord | None:
        grant = self.grant(grant_id)
        return grant if grant is not None and grant.revoked_at is None else None

    def grants_for_person(self, person_id: str) -> list[GrantRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM mcp_oauth_grants WHERE person_id = ? AND revoked_at IS NULL ORDER BY created_at DESC",
                (person_id,),
            ).fetchall()
        return [_grant(row) for row in rows]

    def revoke_grant(self, grant_id: str, reason: str, *, person_id: str | None = None) -> bool:
        """End a connection. With ``person_id``, only that person's own."""
        query = "UPDATE mcp_oauth_grants SET revoked_at = ?, revoked_reason = ? WHERE id = ? AND revoked_at IS NULL"
        args: list[Any] = [self.now(), reason, grant_id]
        if person_id is not None:
            query += " AND person_id = ?"
            args.append(person_id)
        with self._conn() as conn:
            changed = conn.execute(query, args).rowcount
            conn.commit()
        return changed == 1

    def revoke_person_grants(self, person_id: str, reason: str) -> int:
        """End every connection a person made (they were removed)."""
        with self._conn() as conn:
            changed = conn.execute(
                "UPDATE mcp_oauth_grants SET revoked_at = ?, revoked_reason = ? "
                "WHERE person_id = ? AND revoked_at IS NULL",
                (self.now(), reason, person_id),
            ).rowcount
            conn.commit()
        return changed

    def touch_grant(self, grant: GrantRecord) -> None:
        now = self.now()
        if grant.last_used_at is not None and now - grant.last_used_at < LAST_USED_DEBOUNCE_SECONDS:
            return
        try:
            with self._conn() as conn:
                conn.execute("UPDATE mcp_oauth_grants SET last_used_at = ? WHERE id = ?", (now, grant.id))
                conn.commit()
        except sqlite3.Error:
            pass  # recency is best effort; it must never fail a request

    # -------------------------------------------------------------- codes

    def save_code(self, code: str, *, grant_id: str, client_id: str, redirect_uri: str, scope: str,
                  resource: str, code_challenge: str, code_challenge_method: str, ttl: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO mcp_oauth_codes(code_hash, grant_id, client_id, redirect_uri, scope, resource, "
                "code_challenge, code_challenge_method, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (digest(code), grant_id, client_id, redirect_uri, scope, resource, code_challenge,
                 code_challenge_method, self.now() + ttl),
            )
            conn.commit()

    def code(self, code: str) -> CodeRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM mcp_oauth_codes WHERE code_hash = ?", (digest(code),)).fetchone()
        return CodeRecord(**{key: row[key] for key in CodeRecord.__dataclass_fields__}) if row else None

    def spend_code(self, code_hash: str) -> bool:
        """Exactly one redemption wins, even when two race."""
        with self._conn() as conn:
            spent = conn.execute(
                "UPDATE mcp_oauth_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
                (self.now(), code_hash, self.now()),
            ).rowcount
            conn.commit()
        return spent == 1

    # ------------------------------------------------------------- tokens

    def save_token(self, value: str, *, kind: str, grant_id: str, client_id: str, scope: str, ttl: int) -> None:
        now = self.now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO mcp_oauth_tokens(token_hash, kind, grant_id, client_id, scope, issued_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (digest(value), kind, grant_id, client_id, scope, now, now + ttl),
            )
            conn.commit()

    def token(self, value: str) -> TokenRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM mcp_oauth_tokens WHERE token_hash = ?", (digest(value),)).fetchone()
        return TokenRecord(**{key: row[key] for key in TokenRecord.__dataclass_fields__}) if row else None

    def spend_refresh_token(self, token_hash: str) -> bool:
        """Rotate: the old refresh token works once. Exactly one racer wins."""
        with self._conn() as conn:
            spent = conn.execute(
                "UPDATE mcp_oauth_tokens SET used_at = ? WHERE token_hash = ? AND kind = 'refresh' "
                "AND used_at IS NULL AND expires_at > ?",
                (self.now(), token_hash, self.now()),
            ).rowcount
            conn.commit()
        return spent == 1

    # ------------------------------------------------------------ hygiene

    def prune(self) -> None:
        now = self.now()
        with self._conn() as conn:
            conn.execute("DELETE FROM mcp_oauth_requests WHERE expires_at < ?", (now - SPENT_ROW_SECONDS,))
            conn.execute("DELETE FROM mcp_oauth_codes WHERE expires_at < ?", (now - SPENT_ROW_SECONDS,))
            conn.execute("DELETE FROM mcp_oauth_tokens WHERE expires_at < ?", (now - SPENT_ROW_SECONDS,))
            conn.execute(
                "DELETE FROM mcp_oauth_tokens WHERE grant_id IN "
                "(SELECT id FROM mcp_oauth_grants WHERE revoked_at IS NOT NULL)")
            conn.execute(
                "DELETE FROM mcp_oauth_clients WHERE kind = 'registered' AND created_at < ? "
                "AND client_id NOT IN (SELECT client_id FROM mcp_oauth_grants)",
                (now - UNUSED_CLIENT_SECONDS,),
            )
            conn.commit()
