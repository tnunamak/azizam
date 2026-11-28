"""Subscription connections of this gateway: metadata rows, pending sign-in attempts and encrypted token sets.

A connection belongs to the instance (the operator connected a plan once, for the gateway) and records which owner made
it. Rows hold no secret. The token set (access token, refresh token, expiry) and a pending attempt's opaque mechanism state are
encrypted with the provider-secret store (AES-GCM, bound to the upstream origin). The secrets live in the admin
database's ``provider_secrets`` table, because a browser sign-in is checked in the same transaction as the write
(decision D41) and only that database has the sign-ins; the metadata rows live in their own file next to it.
Every secret write names the browser sign-in that asked for it, or None when no browser did (a token refresh).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from gateway.reasoning_efforts import EFFORTS
from gateway import log_scrub, private_io, secret_store, session_guard

def store_path(admin_db_path: Path) -> Path:
    """The subscription database beside the admin database: the one definition of its file name."""
    return Path(admin_db_path).parent / "subscriptions.db"


def cached_model_discovery(path: Path) -> dict[str, dict[str, Any]]:
    """Read only cached model facts; never create a database or load credentials.

    Disconnected connections retain their discoveries. Readiness is an execution
    fact, so a disconnect must not erase the logical model's identity.
    """
    if not path.is_file():
        return {}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='subscription_connections'").fetchone():
            return {}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(subscription_connections)")}
        identity = "connection_id" if "connection_id" in columns else "kind || '-owner'"
        rows = conn.execute(f"SELECT {identity}, models_json, status FROM subscription_connections").fetchall()
        out = {}
        for connection_id, encoded, status in rows:
            models = json.loads(encoded or "{}")
            if isinstance(models, list):
                models = {slug: [] for slug in models}
            out[connection_id] = {"models": models, "status": status}
        return out
    finally:
        conn.close()


CONNECTED = "connected"
NEEDS_RECONNECT = "needs_reconnect"
DISCONNECTED = "disconnected"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_hosts (
  kind TEXT PRIMARY KEY,
  host_id TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription_connections (
  kind TEXT PRIMARY KEY,
  connected_by TEXT NOT NULL,
  client_id TEXT NOT NULL,
  host_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('connected', 'needs_reconnect', 'disconnected')),
  scopes TEXT NOT NULL DEFAULT '',
  account_email TEXT,
  account_subject TEXT,
  access_expires_at INTEGER,
  earliest_refresh_at INTEGER,
  models_json TEXT NOT NULL DEFAULT '{}',
  models_at INTEGER,
  connected_at INTEGER,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription_attempts (
  attempt_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  state TEXT NOT NULL UNIQUE,
  nonce TEXT NOT NULL,
  client_id TEXT NOT NULL,
  host_id TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  session_id TEXT,
  session_family TEXT
);
"""

_CONNECTION_COLUMNS = (
    "connection_id", "generation", "kind", "connected_by", "client_id", "host_id", "status", "scopes", "account_email", "account_subject",
    "access_expires_at", "earliest_refresh_at", "models_json", "models_at", "connected_at", "updated_at",
    "last_ok_at", "last_error_code", "last_error_at", "reconnect_reason", "usage_json", "top_effort",
)
# Why a connection needs the owner: its refresh was refused, or it was ended by something else (a replace, a revoke).
REFRESH_REFUSED = "refresh_refused"
REVOKED = "revoked"
# A good request stamps last_ok_at at most this often per connection: one cheap UPDATE, not one per request.
LAST_OK_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    owner_id: str
    kind: str
    state: str
    mechanism_state: Mapping[str, Any] = field(repr=False)
    expires_at: int
    # The browser sign-in that started the attempt (None when no browser did). Its deadlines are not kept: the
    # local callback listener checks only that this sign-in has not been revoked or retired.
    session: session_guard.BrowserSession | None = None
    operation: str = "connect"


def token_secret_id(kind: str) -> str:
    return f"subscription/{kind}"


def _verifier_secret_id(attempt_id: str) -> str:
    return f"subscription-attempt/{attempt_id}"


def _usage(encoded: str | None) -> dict[str, Any] | None:
    """The stored usage reading, or None when there is none or it cannot be read."""
    try:
        usage = json.loads(encoded) if encoded else None
    except ValueError:
        return None
    if not isinstance(usage, dict) or not isinstance(usage.get("windows"), list):
        return None
    return usage


class SubscriptionStore:
    """All SQL for subscription connections. ``path`` is read on every call, like the other gateway stores."""

    def __init__(self, path: Callable[[], Path], *, clock: Callable[[], float] = time.time,
                 secrets_path: Callable[[], Path] | None = None) -> None:
        self._path = path
        self._secrets_path = secrets_path or path
        self._clock = clock
        self._ready: set[str] = set()
        self._lock = threading.Lock()

    def now(self) -> int:
        return int(self._clock())

    def state_dir(self) -> Path:
        """The directory of this store's file: where a connection's own working files live."""
        return Path(self._path()).parent

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        db_path = self._path()
        private_io.prepare_private_database(db_path)
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            if str(db_path) not in self._ready:
                with self._lock:
                    conn.executescript(_SCHEMA)
                    for column in ("session_id", "session_family"):  # a file made before these columns existed
                        try:
                            conn.execute(f"ALTER TABLE subscription_attempts ADD COLUMN {column} TEXT")
                        except sqlite3.OperationalError:
                            pass
                    columns = {item[1] for item in conn.execute("PRAGMA table_info(subscription_connections)")}
                    if "connection_id" not in columns:
                        conn.execute("ALTER TABLE subscription_connections ADD COLUMN connection_id TEXT")
                    for column, kind in (("last_ok_at", "INTEGER"), ("last_error_code", "TEXT"), ("last_error_at", "INTEGER"),
                                         ("reconnect_reason", "TEXT"), ("usage_json", "TEXT"), ("top_effort", "TEXT")):  # a file made before these columns existed
                        if column not in columns:
                            conn.execute(f"ALTER TABLE subscription_connections ADD COLUMN {column} {kind}")
                    if "generation" not in columns:
                        conn.execute("ALTER TABLE subscription_connections ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
                    conn.execute("UPDATE subscription_connections SET connection_id = kind || '-owner' WHERE connection_id IS NULL")
                    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS subscription_connection_identity ON subscription_connections(connection_id)")
                    conn.commit()
                    self._ready.add(str(db_path))
            try:
                yield conn
            except BaseException:
                secret_store.transaction_ended(conn, False)
                raise
            else:
                secret_store.transaction_ended(conn, not conn.in_transaction)
        finally:
            conn.close()

    # --------------------------------------------------------------- instance host

    def host_id(self, kind: str, scheme: str) -> str:
        """The per-instance host id of this gateway for ``kind``: made once, then stable."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT host_id FROM subscription_hosts WHERE kind = ?", (kind,)).fetchone()
            if row is None:
                value = f"{scheme}:{uuid.uuid4()}"
                conn.execute("INSERT INTO subscription_hosts(kind, host_id, created_at) VALUES (?, ?, ?)",
                             (kind, value, self.now()))
                conn.commit()
                return value
            conn.commit()
            return str(row["host_id"])

    def set_host_id(self, kind: str, host_id: str) -> None:
        """Use a host id an earlier registration already holds (an import), so the provider sees one registration."""
        with self._conn() as conn:
            conn.execute("INSERT INTO subscription_hosts(kind, host_id, created_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(kind) DO UPDATE SET host_id = excluded.host_id", (kind, host_id, self.now()))
            conn.commit()

    # ------------------------------------------------------------------- attempts

    def begin_attempt(self, *, owner_id: str, kind: str, state: str, mechanism_state: Mapping[str, Any],
                      ttl: int, session: session_guard.BrowserSession | None, attempt_id: str | None = None) -> str:
        attempt_id = attempt_id or uuid.uuid4().hex
        now = self.now()
        secret_store.put_provider_secret(
            _verifier_secret_id(attempt_id), json.dumps({"mechanism_state": dict(mechanism_state)}), db_path=self._secrets_path(), allow_short=True,
            keep_suffix=False, session=session)
        with self._conn() as conn:
            # One open attempt per kind: a new start replaces the old one.
            for old in conn.execute("SELECT attempt_id FROM subscription_attempts WHERE kind = ?", (kind,)).fetchall():
                self._forget_verifier(old["attempt_id"])
            conn.execute("DELETE FROM subscription_attempts WHERE kind = ?", (kind,))
            conn.execute(
                "INSERT INTO subscription_attempts(attempt_id, owner_id, kind, state, nonce, client_id, host_id, "
                "redirect_uri, created_at, expires_at, session_id, session_family) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, owner_id, kind, state, "", "", "", "", now, now + ttl,
                 session.session_id if session else None, session.family_id if session else None))
            conn.execute("DELETE FROM subscription_attempts WHERE expires_at < ?", (now - 3600,))
            conn.commit()
        return attempt_id

    def _forget_verifier(self, attempt_id: str) -> None:
        # Clean-up of a secret that is spent or replaced: it runs even when the sign-in has ended.
        secret_store.delete_provider_secret(_verifier_secret_id(attempt_id), db_path=self._secrets_path(), session=None)

    def take_attempt(self, *, attempt_id: str | None = None, state: str | None = None,
                     owner_id: str | None = None, consume: bool = True) -> Attempt | None:
        """Read an owner's open attempt, spending it unless consume=False (a pending poll)."""
        if (attempt_id is None) == (state is None):
            raise ValueError("name the attempt by its id or its state")
        column, value = ("attempt_id", attempt_id) if attempt_id is not None else ("state", state)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(f"SELECT * FROM subscription_attempts WHERE {column} = ?", (value,)).fetchone()
            if row is None or (owner_id is not None and row["owner_id"] != owner_id):
                conn.commit()
                return None
            if consume:
                conn.execute("DELETE FROM subscription_attempts WHERE attempt_id = ?", (row["attempt_id"],))
            conn.commit()
        try:
            raw_state = secret_store.get_provider_secret(_verifier_secret_id(row["attempt_id"]), db_path=self._secrets_path())
        except KeyError:
            return None
        log_scrub.remember(raw_state, log_scrub.secret_source(_verifier_secret_id(row["attempt_id"])))
        try:
            stored = json.loads(raw_state)
        except ValueError:
            stored = None
        if isinstance(stored, dict) and isinstance(stored.get("mechanism_state"), dict):
            mechanism_state = stored["mechanism_state"]
        else:
            # Pending attempts written before opaque mechanism state used these metadata columns.
            mechanism_state = {key: row[key] for key in ("nonce", "client_id", "host_id", "redirect_uri")}
            mechanism_state["verifier"] = raw_state
        log_scrub.hold(log_scrub.secret_source(_verifier_secret_id(row["attempt_id"])),
                       [value for value in mechanism_state.values() if isinstance(value, str)])
        if consume:
            self._forget_verifier(row["attempt_id"])
        if row["expires_at"] < self.now():
            return None
        opened_by = (session_guard.BrowserSession(row["owner_id"], row["session_id"], row["session_family"] or row["session_id"])
                     if row["session_id"] else None)
        return Attempt(row["attempt_id"], row["owner_id"], row["kind"], row["state"], mechanism_state,
                       int(row["expires_at"]), opened_by)

    # ---------------------------------------------------------------- connections

    def connection(self, kind: str) -> dict[str, Any] | None:
        if not self._path().exists():
            return None  # a read never creates the file
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM subscription_connections WHERE kind = ? OR connection_id = ?", (kind, kind)).fetchone()
        return self._connection_dict(row)

    def connections(self) -> list[dict[str, Any]]:
        if not self._path().exists():
            return []
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM subscription_connections ORDER BY kind").fetchall()
        return [self._connection_dict(row) for row in rows]

    @staticmethod
    def _connection_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = {key: row[key] for key in _CONNECTION_COLUMNS}
        stored = json.loads(out.pop("models_json") or "{}")
        # One JSON object: model slug -> the reasoning efforts it advertises (an empty list when it advertises none).
        efforts = stored if isinstance(stored, dict) else {slug: [] for slug in stored}
        out["models"] = list(efforts)
        out["efforts"] = {slug: list(levels) for slug, levels in efforts.items()}
        out["usage"] = _usage(out.pop("usage_json"))
        return out

    def save_connection(self, *, kind: str, connected_by: str, client_id: str, host_id: str, tokens: dict[str, Any] | None,
                        origin: str, scopes: list[str], email: str | None, subject: str | None,
                        session: session_guard.BrowserSession | None,
                        models: Mapping[str, list[str]] | None = None) -> None:
        """Invalidate the previous lease before publishing credentials, then connect a new generation."""
        previous = self.connection(kind)
        if previous is not None:
            invalidated = self.invalidate(kind, expected_generation=previous["generation"], status=NEEDS_RECONNECT, session=session)
            if invalidated is None:
                from gateway.provider_connections import SubscriptionError
                raise SubscriptionError(503, "plan_connection_invalid", "The connection changed. Try again with its current state.")
        if tokens is not None:
            self.save_tokens(kind, tokens, origin=origin, session=session)
        elif session is not None:
            with sqlite3.connect(str(self._secrets_path())) as checks:
                session_guard.begin_write(checks, session, now=self.now())
                checks.commit()
        now = self.now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO subscription_connections(connection_id, generation, kind, connected_by, client_id, host_id, status, scopes, "
                "account_email, account_subject, access_expires_at, earliest_refresh_at, models_json, models_at, "
                "connected_at, updated_at, last_ok_at) VALUES (?, 1, ?, ?, ?, ?, 'connected', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET generation=subscription_connections.generation + 1, connected_by=excluded.connected_by, client_id=excluded.client_id, "
                "host_id=excluded.host_id, status='connected', scopes=excluded.scopes, "
                "account_email=excluded.account_email, account_subject=excluded.account_subject, "
                "access_expires_at=excluded.access_expires_at, earliest_refresh_at=excluded.earliest_refresh_at, "
                "models_json=excluded.models_json, models_at=excluded.models_at, connected_at=excluded.connected_at, "
                "updated_at=excluded.updated_at, reconnect_reason=NULL, last_error_code=NULL, last_error_at=NULL, "
                "last_ok_at=excluded.last_ok_at, usage_json=NULL",
                (self.connection_id(kind), kind, connected_by, client_id, host_id, " ".join(scopes), email, subject,
                 (tokens or {}).get("expires_at"), (tokens or {}).get("earliest_refresh_at"),
                 json.dumps(dict(models or {})), now if models is not None else None, now, now,
                 now if models is not None else None))
            conn.commit()

    def save_facts(self, kind: str, owner_id: str, facts, *, session: session_guard.BrowserSession | None,
                   models: Mapping[str, list[str]] | None = None) -> None:
        metadata = facts.metadata
        self.save_connection(kind=kind, connected_by=owner_id,
                             client_id=str(metadata.get("client_id") or ""), host_id=str(metadata.get("host_id") or ""),
                             tokens=dict(facts.secret_state) if facts.secret_state is not None else None,
                             origin=facts.origin, scopes=list(metadata.get("scopes") or []),
                             email=metadata.get("email"), subject=metadata.get("subject"), session=session, models=models)

    def connection_id(self, kind: str) -> str:
        row = self.connection(kind)
        return row["connection_id"] if row else f"{kind}-owner"

    def require_generation(self, connection_id: str, generation: int) -> dict[str, Any]:
        from gateway.provider_connections import SubscriptionError
        row = self.connection(connection_id)
        if row is None or row["status"] != CONNECTED or row["generation"] != generation:
            raise SubscriptionError(503, "plan_connection_invalid", "The connection changed. Try again with its current state.")
        return row

    def invalidate(self, kind: str, *, expected_generation: int | None = None, status: str = DISCONNECTED,
                   session: session_guard.BrowserSession | None = None, reason: str | None = None) -> dict[str, Any] | None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT generation FROM subscription_connections WHERE kind=? OR connection_id=?", (kind, kind)).fetchone()
            if row is None or (expected_generation is not None and row[0] != expected_generation):
                return None
            if session is not None:
                if self._path() == self._secrets_path():
                    session_guard.require_live(conn, session)
                else:
                    with sqlite3.connect(str(self._secrets_path())) as checks:
                        checks.row_factory = sqlite3.Row
                        session_guard.require_live(checks, session)
            why = (reason or REVOKED) if status == NEEDS_RECONNECT else None
            conn.execute("UPDATE subscription_connections SET status=?, generation=generation+1, updated_at=?, reconnect_reason=? "
                         "WHERE kind=? OR connection_id=?", (status, self.now(), why, kind, kind))
            row = conn.execute("SELECT * FROM subscription_connections WHERE kind=? OR connection_id=?", (kind, kind)).fetchone()
            conn.commit()
        return self._connection_dict(row)

    def save_tokens(self, kind: str, tokens: dict[str, Any], *, origin: str,
                    session: session_guard.BrowserSession | None, expected_generation: int | None = None) -> None:
        from gateway.provider_connections import SubscriptionError
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_generation is not None:
                row = conn.execute("SELECT generation, status FROM subscription_connections WHERE kind=?", (kind,)).fetchone()
                if row is None or row[0] != expected_generation or row[1] != CONNECTED:
                    raise SubscriptionError(503, "plan_connection_invalid", "The connection changed. Try again with its current state.")
            if self._path() == self._secrets_path():
                secret_store.ensure_provider_secret_schema_on(conn)
            secret_store.put_provider_secret(
                token_secret_id(kind), json.dumps(tokens, separators=(",", ":")), db_path=self._secrets_path(),
                approved_origin=origin, allow_short=True, keep_suffix=False, session=session,
                conn=conn if self._path() == self._secrets_path() else None)
            conn.execute("UPDATE subscription_connections SET access_expires_at=?, earliest_refresh_at=?, updated_at=? WHERE kind=?",
                         (tokens.get("expires_at"), tokens.get("earliest_refresh_at"), self.now(), kind))
            conn.commit()

    def load_tokens(self, kind: str, *, origin: str, expected_generation: int | None = None) -> dict[str, Any] | None:
        if expected_generation is not None:
            row = self.connection(kind)
            if row is None or row["generation"] != expected_generation:
                return None
        try:
            raw = secret_store.get_provider_secret(token_secret_id(kind), db_path=self._secrets_path(), expected_origin=origin)
        except KeyError:
            return None
        tokens = json.loads(raw)
        log_scrub.hold(log_scrub.secret_source(token_secret_id(kind)),
                       [tokens.get("access_token"), tokens.get("refresh_token")])
        return tokens

    def drop_tokens(self, kind: str, *, session: session_guard.BrowserSession | None,
                    expected_generation: int | None = None) -> None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_generation is not None:
                row = conn.execute("SELECT generation FROM subscription_connections WHERE kind=?", (kind,)).fetchone()
                if row is None or row[0] != expected_generation:
                    return
            if self._path() == self._secrets_path():
                secret_store.ensure_provider_secret_schema_on(conn)
            secret_store.delete_provider_secret(token_secret_id(kind), db_path=self._secrets_path(), session=session,
                                                conn=conn if self._path() == self._secrets_path() else None)
            conn.commit()

    def set_status(self, kind: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE subscription_connections SET status = ?, updated_at = ? WHERE kind = ?",
                         (status, self.now(), kind))
            conn.commit()

    def set_top_effort(self, kind: str, effort: str | None) -> None:
        if effort is not None and (not isinstance(effort, str) or effort not in EFFORTS):
            raise ValueError("Top effort must be a known reasoning effort or null.")
        with self._conn() as conn:
            changed = conn.execute("UPDATE subscription_connections SET top_effort=?, updated_at=? "
                                   "WHERE kind=? OR connection_id=?", (effort, self.now(), kind, kind))
            if not changed.rowcount:
                raise ValueError("Connect the subscription before setting its top effort.")
            conn.commit()

    def set_models(self, kind: str, models: Mapping[str, list[str]]) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE subscription_connections SET models_json = ?, models_at = ?, updated_at = ?, last_ok_at = ?, "
                         "last_error_code = NULL, last_error_at = NULL WHERE kind = ?",
                         (json.dumps(dict(models)), self.now(), self.now(), self.now(), kind))
            conn.commit()

    def record_error(self, kind: str, code: str) -> None:
        """Remember the last failure of this connection (a code and a time; never a message or a secret)."""
        with self._conn() as conn:
            conn.execute("UPDATE subscription_connections SET last_error_code = ?, last_error_at = ? WHERE kind = ? OR connection_id = ?",
                         (code, self.now(), kind, kind))
            conn.commit()

    def record_usage(self, kind: str, snapshot: Mapping[str, Any]) -> None:
        """Keep the plan's latest usage reading (numbers and times only, from SubscriptionKind.usage_snapshot): one UPDATE.

        Writes finish in any order. A reading stamped with the sign-in ``generation`` it came from is kept only
        while that sign-in is current, and never replaces a reading observed later (``captured_at``). A stored
        reading without a readable time, as older files keep, is replaced."""
        generation = snapshot.get("generation")
        captured_at = snapshot.get("captured_at")
        with self._conn() as conn:
            conn.execute(
                "UPDATE subscription_connections SET usage_json = ? WHERE (kind = ? OR connection_id = ?) "
                "AND (? IS NULL OR generation = ?) "
                "AND (? IS NULL OR coalesce(CASE WHEN json_valid(usage_json) THEN json_extract(usage_json, '$.captured_at') END, 0) <= ?)",
                (json.dumps(dict(snapshot), separators=(",", ":")), kind, kind, generation, generation, captured_at, captured_at))
            conn.commit()

    def record_ok(self, kind: str) -> None:
        """Stamp the last good use, at most once per interval, and clear the last error: it is over."""
        now = self.now()
        with self._conn() as conn:
            conn.execute("UPDATE subscription_connections SET last_error_code = NULL, last_error_at = NULL "
                         "WHERE (kind = ? OR connection_id = ?) AND last_error_at IS NOT NULL", (kind, kind))
            conn.execute("UPDATE subscription_connections SET last_ok_at = ? WHERE (kind = ? OR connection_id = ?) "
                         "AND (last_ok_at IS NULL OR last_ok_at <= ?)", (now, kind, kind, now - LAST_OK_INTERVAL_SECONDS))
            conn.commit()


    def _service_config(self, kind, credential_kind: str) -> dict[str, Any]:
        """The registry config of a connection's credential service: the one shape a connect, an import and the start-up backfill seed."""
        return {"provider_id": kind.id, "remote_provider": True, "base_url": kind.upstream.get("base_url", ""),
                "credential_source": {"kind": "cli-session" if credential_kind == "runner" else "connection",
                                      "connection_id": self.connection_id(kind.id)},
                "request_protocol": kind.protocol, "billing_mode": kind.billing_mode, "cost_class": kind.billing_mode,
                "models": []}

    def register_service(self, kind, owner_id: str, credential, *, session) -> str | None:
        from gateway import admin_store
        return admin_store.bind_subscription_connection(
            owner_id, self.connection_id(kind.id), kind.model_prefix, self._service_config(kind, credential.kind),
            session=session, path=self._secrets_path())

    def backfill_service(self, kind, owner_id: str) -> bool:
        """Seed the credential service of a stored connection that has none (a connection made before the registry kept one).

        No secret is read: the credential kind is the one the kind's mechanism declares it hands out (a bearer, hence
        ``connection``, unless it says otherwise); a plan is never touched. True when the registry gained a row.
        """
        from gateway import admin_store
        from gateway.provider_connections import CONNECT_MECHANISMS
        credential_kind = getattr(CONNECT_MECHANISMS.get(kind.mechanism), "credential_kind", "bearer")
        return admin_store.backfill_connection_service(
            owner_id, self.connection_id(kind.id), kind.model_prefix, self._service_config(kind, credential_kind),
            path=self._secrets_path())


    def owner_plan(self, owner_id: str) -> dict[str, Any]:
        with sqlite3.connect(str(self._secrets_path())) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='account_access'").fetchone():
                return {}
            row = conn.execute("SELECT p.id, p.name FROM account_access a JOIN access_plans p ON p.id=a.plan_id WHERE a.user_id=?",
                               (owner_id,)).fetchone()
        return {"access_plan_id": row[0], "access_plan_name": row[1]} if row else {}
