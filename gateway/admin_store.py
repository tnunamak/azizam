"""SQLite-backed durable store for admin / control-plane state.

This module separates *durable facts* (who exists, what they have, what was
done) from *policy decisions* (what they're allowed to do right now). The
policy evaluator lives in :mod:`gateway.policy`; this store persists the
access-plan documents (:mod:`gateway.access_plans`) and who holds them.

It also separates the *data plane* (apps making API calls, data-plane
credentials attached to an app) from the *control plane* (humans and service
accounts making admin mutations, admin credentials attached to a user or
service account).

Layout mirrors ``receipt_store.py``: stdlib ``sqlite3``, WAL mode, integrity
check on startup, short-lived connections, explicit schema version.

Storage invariants
------------------

* Raw API keys / provider secrets are NEVER stored. Callers hash the secret
  and pass us the hash + key prefix. We store the hash; the prefix is only
  used to render a non-secret display value (``svc_live_abcd…``).
* Stable generated IDs use prefixes per entity kind (``usr_``, ``svc_``,
  ``app_``, ``plan_``, ``cred_``, ``aud_``).
* OIDC users are unique by ``(issuer, subject)`` — re-logins update profile
  data but preserve the row ID, so anything pointing at the user (apps,
  credentials, role assignments) stays stable.

The HTTP surface (OIDC callback, dashboard, admin API) is intentionally out
of scope here — this module is the durable substrate those routes call into.
"""

from __future__ import annotations

from gateway.compat import get_env
from gateway.sqlite_startup import check_quarantine_pending, connect, store_startup

import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

_default_db = Path(__file__).resolve().parent.parent / "data" / "admin.db"
DB_PATH = Path(get_env("AZIZAM_ADMIN_DB")) if get_env("AZIZAM_ADMIN_DB") else _default_db

SCHEMA_VERSION = 7
# Schema 7 replaced profiles with access plans (gateway.access_plan_migration).
_PRE_ACCESS_PLANS_VERSION = 6
# The OIDC client secret lives in the encrypted secret store; the
# oidc_provider_config setting holds only this reference to it.
OIDC_CLIENT_SECRET_REF = "client_secret_ref"
# The fixed secret id makes the startup move restart-safe.
OIDC_MOVED_CLIENT_SECRET_ID = "oidc-client-secret"

SUBJECT_KINDS = ("user", "service_account", "app")
APP_KINDS = ("personal", "service")
IDENTITY_CLAIM_KINDS = ("oidc_group", "oidc_role")
# Credentials attached to an app are data-plane (a runtime client uses them
# to call /v1/chat/completions etc.). Credentials attached to a user or
# service account are control-plane (used to call admin APIs).
_DATA_PLANE_SUBJECTS = frozenset({"app"})
_CONTROL_PLANE_SUBJECTS = frozenset({"user", "service_account"})
INTERNAL_ACTIVITY_ACTIONS = frozenset({
    "operator.safe_restart",
    "operator.safe-restart",
    "safe_restart",
    "safe-restart",
    "deploy.smoke",
    "deploy_smoke",
    "service_account.deploy_smoke",
    "service-account.deploy-smoke",
})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  issuer TEXT NOT NULL,
  subject TEXT NOT NULL,
  email TEXT,
  display_name TEXT,
  created_at INTEGER NOT NULL,
  last_login_at INTEGER,
  UNIQUE(issuer, subject)
);

CREATE TABLE IF NOT EXISTS user_identity_claims (
  user_id TEXT NOT NULL,
  claim_kind TEXT NOT NULL,
  claim_value TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(user_id, claim_kind, claim_value),
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS user_identity_claims_lookup
  ON user_identity_claims(user_id, claim_kind);

CREATE TABLE IF NOT EXISTS service_accounts (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  created_at INTEGER NOT NULL,
  disabled_at INTEGER
);

-- An access plan document (gateway.access_plans). People, apps and
-- invitations refer to it by its opaque id; the name is only a label.
CREATE TABLE IF NOT EXISTS access_plans (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  plan_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Every App has an accountable person (owner_user_id) and one assignment:
-- an access plan id, or 'person' ("Same as its person"). NULL is only the
-- state after a failed assignment write, and denies every request.
CREATE TABLE IF NOT EXISTS apps (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  access_plan TEXT,
  created_at INTEGER NOT NULL,
  disabled_at INTEGER,
  FOREIGN KEY(owner_user_id) REFERENCES users(id)
);

-- The App that attributes a person's own signed-in work (the "Azizam" App).
-- It holds no credentials: browser sessions act through it without a key.
CREATE TABLE IF NOT EXISTS session_apps (
  user_id TEXT PRIMARY KEY,
  app_id TEXT NOT NULL UNIQUE,
  FOREIGN KEY(user_id) REFERENCES users(id),
  FOREIGN KEY(app_id) REFERENCES apps(id)
);

CREATE TABLE IF NOT EXISTS credentials (
  id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  app_id TEXT,
  hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  display_name TEXT,
  scopes_json TEXT,
  created_at INTEGER NOT NULL,
  created_by TEXT,
  expires_at INTEGER,
  revoked_at INTEGER,
  last_used_at INTEGER,
  -- A key bound to a browser sign-in session; revoked when it signs out.
  session_id TEXT,
  FOREIGN KEY(app_id) REFERENCES apps(id)
);
CREATE INDEX IF NOT EXISTS credentials_subject ON credentials(subject_kind, subject_id);
CREATE INDEX IF NOT EXISTS credentials_app ON credentials(app_id);
CREATE INDEX IF NOT EXISTS credentials_active ON credentials(revoked_at);
-- A session App never holds a credential, whoever asks: a key on it would let
-- a key holder act as that person's signed-in work.
CREATE TRIGGER IF NOT EXISTS credentials_never_on_session_apps
BEFORE INSERT ON credentials
WHEN EXISTS (SELECT 1 FROM session_apps WHERE app_id = COALESCE(NEW.app_id, NEW.subject_id))
BEGIN
  SELECT RAISE(ABORT, 'session Apps never hold credentials');
END;
CREATE TRIGGER IF NOT EXISTS credentials_never_move_to_session_apps
BEFORE UPDATE OF app_id, subject_id ON credentials
WHEN EXISTS (SELECT 1 FROM session_apps WHERE app_id = COALESCE(NEW.app_id, NEW.subject_id))
BEGIN
  SELECT RAISE(ABORT, 'session Apps never hold credentials');
END;

CREATE TABLE IF NOT EXISTS admin_role_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  scope_app_id TEXT,
  granted_at INTEGER NOT NULL,
  granted_by TEXT,
  provenance TEXT NOT NULL DEFAULT 'manual',
  revoked_at INTEGER,
  UNIQUE(role, subject_kind, subject_id, scope_app_id),
  FOREIGN KEY(scope_app_id) REFERENCES apps(id)
);
CREATE INDEX IF NOT EXISTS admin_role_subject
  ON admin_role_assignments(subject_kind, subject_id);

CREATE TABLE IF NOT EXISTS local_password_credentials (
  user_id TEXT PRIMARY KEY,
  verifier TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS operator_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  updated_by TEXT
);
CREATE INDEX IF NOT EXISTS operator_settings_updated_at
  ON operator_settings(updated_at);

-- Each setting has an independent server-owned revision. The registry keeps
-- its existing generation contract; forms opt in to revisions per resource.
CREATE TABLE IF NOT EXISTS operator_settings_generations (
  setting_key TEXT PRIMARY KEY,
  operator_settings_generation INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_memory_preferences (
  user_id TEXT PRIMARY KEY,
  remember INTEGER NOT NULL,
  retention TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  updated_by TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id)
);


CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  actor_kind TEXT,
  actor_id TEXT,
  actor_label TEXT,
  action TEXT NOT NULL,
  target_kind TEXT,
  target_id TEXT,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS audit_created_at ON audit_events(created_at);
CREATE INDEX IF NOT EXISTS audit_target ON audit_events(target_kind, target_id);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
"""

# Built-in identities, single-use links and sign-in state (gateway.accounts).
ACCOUNT_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_access (
 user_id TEXT PRIMARY KEY REFERENCES users(id), plan_id TEXT REFERENCES access_plans(id),
 disabled_at INTEGER, removed_at INTEGER, require_registered_session INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS account_sessions (
 session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS account_session_revocations (
 session_id TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS account_links (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 plan_id TEXT REFERENCES access_plans(id), user_id TEXT REFERENCES users(id), email TEXT,
 created_by TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 used_at INTEGER, revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS account_oidc_verification (
 user_id TEXT PRIMARY KEY REFERENCES users(id), email_verified INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS account_login_limits (
 bucket TEXT PRIMARY KEY, started_at INTEGER NOT NULL, attempts INTEGER NOT NULL
);
"""

# Run after any migration to schema 7: these name columns only schema 7 has.
_ACCESS_PLAN_DDL = """
CREATE INDEX IF NOT EXISTS apps_owner_user ON apps(owner_user_id);
CREATE TRIGGER IF NOT EXISTS apps_access_plan_exists_insert
BEFORE INSERT ON apps
WHEN NEW.access_plan IS NOT NULL AND NEW.access_plan <> 'person'
  AND NOT EXISTS (SELECT 1 FROM access_plans WHERE id = NEW.access_plan)
BEGIN
  SELECT RAISE(ABORT, 'access plan not found');
END;
CREATE TRIGGER IF NOT EXISTS apps_access_plan_exists_update
BEFORE UPDATE OF access_plan ON apps
WHEN NEW.access_plan IS NOT NULL AND NEW.access_plan <> 'person'
  AND NOT EXISTS (SELECT 1 FROM access_plans WHERE id = NEW.access_plan)
BEGIN
  SELECT RAISE(ABORT, 'access plan not found');
END;
CREATE TRIGGER IF NOT EXISTS access_plans_delete_unassigned
BEFORE DELETE ON access_plans
WHEN EXISTS (SELECT 1 FROM apps WHERE access_plan = OLD.id)
BEGIN
  SELECT RAISE(ABORT, 'access plan is assigned');
END;
"""


# ---------------------------------------------------------------------------
# connection plumbing
# ---------------------------------------------------------------------------


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")


def _exec_schema(conn: sqlite3.Connection) -> None:
    # Refuse a newer database before any DDL can touch it.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone():
        stored = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if stored is not None and int(stored[0]) > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(
                f"admin_store schema version mismatch: db={stored[0]} code={SCHEMA_VERSION}"
            )
    conn.executescript(_SCHEMA)
    conn.executescript(ACCOUNT_SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    role_columns = {column[1] for column in conn.execute("PRAGMA table_info(admin_role_assignments)")}
    # Claims stored before v4 folded case and path separators, so they could
    # match a mapping the person is not in. Sign-in stores exact claims again.
    purge_folded_claims = (row is not None and int(row[0]) < 4) or "provenance" not in role_columns
    if row is None:
        conn.execute(
            "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
        )
    elif int(row[0]) > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            f"admin_store schema version mismatch: db={row[0]} code={SCHEMA_VERSION}"
        )
    elif int(row[0]) < SCHEMA_VERSION:
        # SQLite does not add columns through CREATE TABLE IF NOT EXISTS.
        # Keep the event-time actor label available to upgraded installations.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_events)")}
        if "actor_label" not in columns:
            conn.execute("ALTER TABLE audit_events ADD COLUMN actor_label TEXT")
        # Schema 7 rebuilds tables, so it runs in its own transaction below.
        conn.execute("UPDATE schema_version SET version = ?", (_PRE_ACCESS_PLANS_VERSION,))
    # Grants made before provenance tracking stay manual. This migration is
    # intentionally idempotent even for databases with an inconsistent version.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(admin_role_assignments)")}
    if "provenance" not in columns:
        conn.execute(
            "ALTER TABLE admin_role_assignments "
            "ADD COLUMN provenance TEXT NOT NULL DEFAULT 'manual'"
        )
    if purge_folded_claims:
        conn.execute("DELETE FROM user_identity_claims")
    _drop_identity_profile_grants(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(credentials)")}
    if "created_by" not in columns:
        conn.execute("ALTER TABLE credentials ADD COLUMN created_by TEXT")
    if "session_id" not in columns:
        conn.execute("ALTER TABLE credentials ADD COLUMN session_id TEXT")
        _revoke_unbound_try_keys(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS credentials_session ON credentials(session_id)")
    # Keys made on a session App before the trigger existed stop working.
    conn.execute(
        "UPDATE credentials SET revoked_at = ? WHERE revoked_at IS NULL "
        "AND COALESCE(app_id, subject_id) IN (SELECT app_id FROM session_apps)",
        (_now(),),
    )
    _freeze_missing_prompt_memory_preferences(conn, _prompt_memory_default(conn))
    # Seed existing documents once. INSERT OR IGNORE preserves every revision
    # across restarts, including the registry's existing CAS generation.
    conn.execute(
        "INSERT OR IGNORE INTO operator_settings_generations("
        "setting_key, operator_settings_generation) "
        "SELECT key, 1 FROM operator_settings"
    )
    if row is not None and int(row[0]) < 5:
        from gateway.reasoning_transform_migration import migrate_registry

        migrate_registry(conn)
    if row is not None and int(row[0]) < 6:
        from gateway import stt_chunk_transform_migration

        stt_chunk_transform_migration.migrate_registry(conn)
    conn.commit()
    _backfill_credential_creators(conn)
    current = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if int(current[0]) == _PRE_ACCESS_PLANS_VERSION:
        _migrate_to_access_plans(conn)
        # Rebuilt tables lost their indexes and triggers; the schema restores them.
        conn.executescript(_SCHEMA)
        conn.execute("CREATE INDEX IF NOT EXISTS credentials_session ON credentials(session_id)")
    conn.executescript(_ACCESS_PLAN_DDL)


def access_plans_backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak.pre-access-plans")


def _migrate_to_access_plans(conn: sqlite3.Connection) -> None:
    """Schema 6 -> 7: profiles become access plans (design v4).

    A copy of the database is written beside it first. On any error nothing
    changes and the gateway refuses to start with the reason.
    """
    from gateway import access_plan_migration

    db_file = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    backup = access_plans_backup_path(db_file)
    if not backup.exists():
        target = sqlite3.connect(str(backup))
        try:
            conn.backup(target)
        finally:
            target.close()
        backup.chmod(0o600)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        report = access_plan_migration.migrate(conn)
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise access_plan_migration.MigrationError(f"rows refer to missing rows: {problems[:5]}")
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        conn.commit()
    except access_plan_migration.MigrationError as exc:
        conn.rollback()
        raise sqlite3.DatabaseError(
            f"admin.db cannot move to access plans: {exc}. Nothing was changed; "
            f"a copy is at {backup}."
        ) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    for line in report.lines():
        print(f"[admin_store] {line}", flush=True)
    print(f"[admin_store] the database before this change is at {backup}", flush=True)


def _backfill_credential_creators(conn: sqlite3.Connection) -> None:
    """Recover legacy App-key makers with bounded, restart-safe commits.

    Audit lookup runs outside the write transaction. Unknown makers stay NULL;
    a recorded actor other than the App owner explicitly loses person scope.
    """
    cursor = ""
    resolved = 0
    while True:
        rows = conn.execute(
            "SELECT c.id, a.owner_user_id FROM credentials c LEFT JOIN apps a "
            "ON a.id=COALESCE(c.app_id,c.subject_id) "
            "WHERE c.subject_kind='app' AND c.created_by IS NULL AND c.id>? "
            "ORDER BY c.id LIMIT 100", (cursor,),
        ).fetchall()
        if not rows:
            break
        updates = []
        for credential_id, owner in rows:
            events = conn.execute(
                "SELECT actor_kind, actor_id FROM audit_events WHERE target_id=? "
                "AND action IN ('credential.create','credential.self_create') "
                "AND target_kind IN ('credential','account_link')", (credential_id,),
            ).fetchall()
            if events:
                creator = owner if owner and all(kind == "user" and actor == owner
                                                for kind, actor in events) else ""
                updates.append((creator, credential_id))
        if updates:
            conn.execute("BEGIN IMMEDIATE")
            try:
                changed = conn.executemany(
                    "UPDATE credentials SET created_by=? WHERE id=? AND created_by IS NULL", updates,
                ).rowcount
                resolved += changed
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        cursor = rows[-1][0]
    unresolved = conn.execute(
        "SELECT COUNT(*) FROM credentials WHERE subject_kind='app' AND created_by IS NULL"
    ).fetchone()[0]
    if resolved or unresolved:
        print(f"[admin_store] legacy App-key creators: resolved={resolved} unresolved={unresolved}", flush=True)


def _prompt_memory_default(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value_json FROM operator_settings WHERE key='prompt_memory'"
    ).fetchone()
    if row is None:
        return True
    try:
        value = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return True
    return bool(value.get("default_remember", True)) if isinstance(value, Mapping) else True


def _insert_account_access(conn: sqlite3.Connection, user_id: str) -> None:
    """A new person starts with no access plan; the owner chooses one."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_access'").fetchone():
        conn.execute("INSERT OR IGNORE INTO account_access(user_id) VALUES (?)", (user_id,))


def _insert_prompt_memory_preference(
    conn: sqlite3.Connection, user_id: str, now: int,
) -> None:
    value = _prompt_memory_default(conn)
    conn.execute(
        "INSERT OR IGNORE INTO prompt_memory_preferences"
        "(user_id, remember, retention, updated_at, updated_by) VALUES (?, ?, 'never', ?, NULL)",
        (user_id, 1 if value else 0, now),
    )


def _freeze_missing_prompt_memory_preferences(
    conn: sqlite3.Connection, remember: bool, *, now: int | None = None,
) -> None:
    ts = _now() if now is None else int(now)
    conn.execute(
        "INSERT OR IGNORE INTO prompt_memory_preferences"
        "(user_id, remember, retention, updated_at, updated_by) "
        "SELECT id, ?, 'never', ?, NULL FROM users",
        (1 if remember else 0, ts),
    )


TRY_APP_NAME = "Try"
TRY_KEY_LABEL = "Try session"


def _revoke_unbound_try_keys(conn: sqlite3.Connection) -> None:
    """Revoke Try page keys minted before they were bound to a sign-in session.

    They never expired, and their secret lived only in a closed page, so no
    one can still use them as intended. Runs once, when the column appears.
    """
    ts = _now()
    ids = [row[0] for row in conn.execute(
        "SELECT c.id FROM credentials c JOIN apps a ON a.id = c.app_id "
        "WHERE c.subject_kind = 'app' AND c.revoked_at IS NULL AND c.display_name = ? "
        "AND lower(a.name) = lower(?) AND a.owner_user_id IS NOT NULL ORDER BY c.id",
        (TRY_KEY_LABEL, TRY_APP_NAME),
    )]
    if not ids:
        return
    conn.executemany("UPDATE credentials SET revoked_at = ? WHERE id = ?", [(ts, i) for i in ids])
    _record_audit_in_transaction(
        conn, "credential.try_session_revoke",
        actor_kind=None, actor_id=None, actor_label=None,
        target_kind="credential", target_id=None,
        metadata={"reason": "unbound_try_key", "credential_ids": ids}, now=ts,
    )


def _drop_identity_profile_grants(conn: sqlite3.Connection) -> None:
    """Drop the pre-People grant table, but never a grant someone still holds.

    People access plans (``account_access``) replaced these rows. A row left
    here would silently stop granting its plan, so refuse to start instead.
    """
    table = "legacy_identity_profile_grants"
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        return
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if count:
        raise sqlite3.DatabaseError(
            f"admin.db table {table} still holds {count} access-plan grant(s). "
            "Assign each listed person a plan in People (account_access), "
            f"delete the rows from {table}, then start Azizam again."
        )
    conn.execute(f"DROP TABLE {table}")


def secure_state_path(path: Path) -> None:
    """Create private SQLite state and tighten existing database sidecars.

    SQLite inherits database permissions for new WAL/SHM files. Precreating the
    database avoids a process-wide umask change in this multithreaded gateway.
    """
    check_quarantine_pending(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    for state_file in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        try:
            state_file.chmod(0o600)
        except FileNotFoundError:
            pass


def init_db(path: Path = DB_PATH) -> None:
    """Check existing data safely, then initialize the schema.

    Idempotent: running it twice on the same file is a no-op.
    """
    with store_startup(path):
        secure_state_path(path)
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            _apply_pragmas(conn)
            _exec_schema(conn)
            _move_oidc_client_secret_to_store(conn, path)
        finally:
            conn.close()


def _move_oidc_client_secret_to_store(conn: sqlite3.Connection, path: Path) -> None:
    """Move a plaintext OIDC client secret from settings into the secret store.

    The secret store writes through its own connection, so this runs after the
    schema transaction. It is idempotent: the secret id is fixed, and the
    setting changes only while it still holds the same literal. An empty
    literal is an explicit "no secret" override, not a secret, and stays.
    """
    key = "oidc_provider_config"

    def literal() -> tuple[dict[str, Any], str] | None:
        row = conn.execute("SELECT value_json FROM operator_settings WHERE key=?", (key,)).fetchone()
        try:
            value = json.loads(row[0]) if row else None
        except (TypeError, json.JSONDecodeError):
            return None
        secret = value.get("client_secret") if isinstance(value, dict) else None
        return (value, secret) if isinstance(secret, str) and secret else None

    found = literal()
    if found is None:
        return
    from gateway import secret_store

    secret_store.put_provider_secret(OIDC_MOVED_CLIENT_SECRET_ID, found[1], db_path=path)
    # Overwrite the literal's freed bytes, then fold the WAL into the file.
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = literal()
        if current is None or current[1] != found[1]:
            conn.rollback()
            return
        value = current[0]
        del value["client_secret"]
        value[OIDC_CLIENT_SECRET_REF] = OIDC_MOVED_CLIENT_SECRET_ID
        ts = _now()
        conn.execute(
            "UPDATE operator_settings SET value_json=?, updated_at=? WHERE key=?",
            (json.dumps(value, ensure_ascii=False, sort_keys=True), ts, key),
        )
        _advance_setting_revision(conn, key)
        _record_audit_in_transaction(
            conn, "operator.oidc_provider_config.secret_moved",
            actor_kind=None, actor_id=None, actor_label=None,
            target_kind="operator_setting", target_id=key,
            metadata={"secret_ref": OIDC_MOVED_CLIENT_SECRET_ID}, now=ts,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


@contextmanager
def _conn(path: Path = DB_PATH):
    secure_state_path(path)
    conn = connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def runtime_auth_connection(path: Path = DB_PATH):
    """Own one connection for a synchronous runtime identity lookup.

    This scope must not span an await or escape to another thread. SELECTs
    remain in autocommit, so later reads see committed changes; there is no
    request-spanning transaction or cached authorization state.
    """
    with _conn(path) as conn:
        yield conn


def schema_version(path: Path = DB_PATH) -> int | None:
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None


# ---------------------------------------------------------------------------
# id helpers
# ---------------------------------------------------------------------------


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _now() -> int:
    return int(time.time())


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def upsert_oidc_user(
    issuer: str,
    subject: str,
    *,
    email: str | None = None,
    display_name: str | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Insert or update a user keyed by ``(issuer, subject)``.

    Updates ``email``/``display_name``/``last_login_at`` while preserving the
    generated ``id`` so downstream references (apps, credentials, role
    assignments) stay intact.
    """
    if not issuer or not subject:
        raise ValueError("issuer and subject are required")
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE issuer = ? AND subject = ?",
            (issuer, subject),
        ).fetchone()
        if row is None:
            user_id = _gen_id("usr")
            conn.execute(
                "INSERT INTO users(id, issuer, subject, email, display_name, "
                "created_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, issuer, subject, email, display_name, ts, ts),
            )
            _insert_prompt_memory_preference(conn, user_id, ts)
            _insert_account_access(conn, user_id)
        else:
            user_id = row["id"]
            conn.execute(
                "UPDATE users SET email = COALESCE(?, email), "
                "display_name = COALESCE(?, display_name), last_login_at = ? "
                "WHERE id = ?",
                (email, display_name, ts, user_id),
            )
            # A person always has an access row; signing in repairs a missing one.
            _insert_account_access(conn, user_id)
        conn.commit()
        out = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return _row_to_dict(out)  # type: ignore[return-value]


def get_user(user_id: str, *, path: Path = DB_PATH) -> dict[str, Any] | None:
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return _row_to_dict(row)


def list_users(*, path: Path = DB_PATH) -> list[dict[str, Any]]:
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at ASC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def find_user_by_email(
    email: str,
    *,
    issuer: str | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Return the user row with this email (lowercased), or None.

    When ``issuer`` is supplied the lookup is scoped to that issuer, which
    lets the local-owner sign-in path ignore an OIDC user that happens to
    share an email with the local break-glass account.
    """
    needle = (email or "").strip().lower()
    if not needle:
        return None
    sql = "SELECT * FROM users WHERE LOWER(email) = ?"
    params: list[Any] = [needle]
    if issuer is not None:
        sql += " AND issuer = ?"
        params.append(issuer)
    sql += " ORDER BY created_at ASC LIMIT 1"
    with _conn(path) as conn:
        row = conn.execute(sql, params).fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# local password credentials (web-first first-run owner / break-glass admin)
# ---------------------------------------------------------------------------


def set_local_password_verifier(
    user_id: str,
    verifier: str,
    *,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Store (or replace) the password verifier attached to a local user.

    Raw passwords are never persisted — the caller computes the verifier
    string with :func:`gateway.local_owner.hash_password` and hands us the
    opaque result.
    """
    if not user_id:
        raise ValueError("user_id is required")
    if not verifier or not isinstance(verifier, str):
        raise ValueError("verifier is required")
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        existing = conn.execute(
            "SELECT user_id, created_at FROM local_password_credentials "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO local_password_credentials"
                "(user_id, verifier, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, verifier, ts, ts),
            )
        else:
            conn.execute(
                "UPDATE local_password_credentials "
                "SET verifier = ?, updated_at = ? WHERE user_id = ?",
                (verifier, ts, user_id),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM local_password_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def claim_first_local_owner(
    *,
    issuer: str,
    subject: str,
    email: str,
    display_name: str,
    verifier: str,
    granted_by: str,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Atomically create the first local owner, or return None if claimed.

    The caller may do expensive password hashing before this helper; the helper
    owns the durable first-owner check and every write that makes the claim
    observable.
    """
    if not issuer or not subject:
        raise ValueError("issuer and subject are required")
    if not verifier or not isinstance(verifier, str):
        raise ValueError("verifier is required")
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM admin_role_assignments "
            "WHERE revoked_at IS NULL AND role IN ('owner', 'admin', 'superadmin') "
            "LIMIT 1"
        ).fetchone()
        if existing is not None:
            conn.rollback()
            return None

        row = conn.execute(
            "SELECT id FROM users WHERE issuer = ? AND subject = ?",
            (issuer, subject),
        ).fetchone()
        if row is None:
            user_id = _gen_id("usr")
            conn.execute(
                "INSERT INTO users(id, issuer, subject, email, display_name, "
                "created_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, issuer, subject, email, display_name, ts, ts),
            )
            _insert_prompt_memory_preference(conn, user_id, ts)
            _insert_account_access(conn, user_id)
        else:
            user_id = row["id"]
            conn.execute(
                "UPDATE users SET email = COALESCE(?, email), "
                "display_name = COALESCE(?, display_name), last_login_at = ? "
                "WHERE id = ?",
                (email, display_name, ts, user_id),
            )
            # A person always has an access row; signing in repairs a missing one.
            _insert_account_access(conn, user_id)

        conn.execute(
            "INSERT INTO local_password_credentials"
            "(user_id, verifier, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "verifier = excluded.verifier, updated_at = excluded.updated_at",
            (user_id, verifier, ts, ts),
        )
        conn.execute(
            "INSERT INTO admin_role_assignments(role, subject_kind, "
            "subject_id, scope_app_id, granted_at, granted_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("owner", "user", user_id, None, ts, granted_by),
        )
        _record_audit_in_transaction(
            conn,
            "setup.local_owner_create",
            actor_kind="user",
            actor_id=user_id,
            actor_label=display_name or email or user_id,
            target_kind="user",
            target_id=user_id,
            metadata={"email": email, "via": "setup_page"},
            now=ts,
        )
        conn.commit()
        out = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return _row_to_dict(out)  # type: ignore[return-value]


def get_local_password_verifier(
    user_id: str, *, path: Path = DB_PATH
) -> dict[str, Any] | None:
    if not user_id:
        return None
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM local_password_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return _row_to_dict(row)


def delete_local_password_verifier(
    user_id: str, *, path: Path = DB_PATH
) -> bool:
    """Drop the verifier for a user. Returns True if a row was removed."""
    if not user_id:
        return False
    with _conn(path) as conn:
        cur = conn.execute(
            "DELETE FROM local_password_credentials WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# OIDC runtime identity claims
# ---------------------------------------------------------------------------


def _normalize_access_value(value: object) -> str:
    return " ".join(str(value or "").strip().lower().strip("/").split())


def _normalize_identity_value(kind: str, value: object) -> str:
    # Authorization claims retain their case, separators, and full path.
    raw = str(value or "")
    if kind == "oidc_group":
        from gateway.admin_oidc import canonical_oidc_group

        return canonical_oidc_group(raw) if raw else ""
    return raw


def replace_user_identity_claims(
    user_id: str,
    claims_by_kind: Mapping[str, Iterable[str]],
    *,
    now: int | None = None,
    path: Path = DB_PATH,
) -> None:
    """Replace exact OIDC authorization claims observed for a user."""
    if not user_id:
        raise ValueError("user_id is required")
    ts = _now() if now is None else int(now)
    rows: list[tuple[str, str]] = []
    for kind, values in claims_by_kind.items():
        if kind not in IDENTITY_CLAIM_KINDS:
            continue
        for value in values:
            normalized = _normalize_identity_value(kind, value)
            if normalized:
                rows.append((kind, normalized))

    with _conn(path) as conn:
        for kind in IDENTITY_CLAIM_KINDS:
            conn.execute(
                "DELETE FROM user_identity_claims "
                "WHERE user_id = ? AND claim_kind = ?",
                (user_id, kind),
            )
        conn.executemany(
            "INSERT OR IGNORE INTO user_identity_claims"
            "(user_id, claim_kind, claim_value, updated_at) VALUES (?, ?, ?, ?)",
            [(user_id, kind, value, ts) for kind, value in rows],
        )
        conn.commit()


def list_user_identity_claims(
    user_id: str, *, path: Path = DB_PATH
) -> list[dict[str, Any]]:
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM user_identity_claims WHERE user_id = ? "
            "ORDER BY claim_kind ASC, claim_value ASC",
            (user_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# service accounts
# ---------------------------------------------------------------------------


def create_service_account(
    name: str,
    *,
    description: str | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    if not name:
        raise ValueError("service account name is required")
    ts = _now() if now is None else int(now)
    svc_id = _gen_id("svc")
    with _conn(path) as conn:
        conn.execute(
            "INSERT INTO service_accounts(id, name, description, created_at) "
            "VALUES (?, ?, ?, ?)",
            (svc_id, name, description, ts),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM service_accounts WHERE id = ?", (svc_id,)
        ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def ensure_service_account(
    name: str,
    *,
    description: str | None = None,
    allowed_roles: Iterable[str] | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Reuse an identity only if the issuer may mint its effective roles.

    ``allowed_roles=None`` is reserved for trusted local operator callers.
    Authorization precedes all mutations, including creation of a new identity.
    """
    if not name:
        raise ValueError("service account name is required")
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM service_accounts WHERE name = ?", (name,)
        ).fetchone()
        if row is not None:
            if allowed_roles is not None:
                roles = {
                    str(grant[0]) for grant in conn.execute(
                        "SELECT role FROM admin_role_assignments "
                        "WHERE subject_kind='service_account' AND subject_id=? "
                        "AND revoked_at IS NULL", (row["id"],)
                    )
                }
                if roles - set(allowed_roles):
                    raise PermissionError("cannot mint a key for a higher-privilege principal")
            return _row_to_dict(row)  # type: ignore[return-value]
        svc_id = _gen_id("svc")
        conn.execute(
            "INSERT INTO service_accounts(id, name, description, created_at) "
            "VALUES (?, ?, ?, ?)", (svc_id, name, description, ts),
        )
        conn.commit()
        return _row_to_dict(conn.execute(
            "SELECT * FROM service_accounts WHERE id=?", (svc_id,)
        ).fetchone())  # type: ignore[return-value]


def get_service_account(
    svc_id: str, *, path: Path = DB_PATH
) -> dict[str, Any] | None:
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM service_accounts WHERE id = ?", (svc_id,)
        ).fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# access plans
# ---------------------------------------------------------------------------

PERSON_ASSIGNMENT = "person"


def _plan_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys()}
    data["plan"] = json.loads(data.pop("plan_json"))
    return data


def _validated_plan_json(plan_id: str, doc: Mapping[str, Any]) -> tuple[str, str]:
    from gateway.access_plans import plan_from_document, plan_to_document

    plan = plan_from_document({**dict(doc), "id": plan_id})
    return plan.name, json.dumps(plan_to_document(plan), ensure_ascii=False, sort_keys=True)


def create_access_plan(
    doc: Mapping[str, Any], *, now: int | None = None, path: Path = DB_PATH,
) -> dict[str, Any]:
    """Store a new plan document. The server assigns the id (design §3.1)."""
    plan_id = _gen_id("plan")
    name, payload = _validated_plan_json(plan_id, {k: v for k, v in doc.items() if k != "id"})
    ts = _now() if now is None else int(now)
    try:
        with _conn(path) as conn:
            conn.execute(
                "INSERT INTO access_plans(id, name, plan_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (plan_id, name, payload, ts, ts),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM access_plans WHERE id = ?", (plan_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"access plan name {name!r} is already in use") from exc
    return _plan_row_to_dict(row)


def get_access_plan(
    plan_id: str, *, path: Path = DB_PATH, connection: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    with _conn(path) if connection is None else nullcontext(connection) as conn:
        row = conn.execute("SELECT * FROM access_plans WHERE id = ?", (plan_id,)).fetchone()
    return _plan_row_to_dict(row) if row else None


def list_access_plans(*, path: Path = DB_PATH) -> list[dict[str, Any]]:
    with _conn(path) as conn:
        rows = conn.execute("SELECT * FROM access_plans ORDER BY name COLLATE NOCASE, id").fetchall()
    return [_plan_row_to_dict(row) for row in rows]


def update_access_plan(
    plan_id: str, doc: Mapping[str, Any], *, now: int | None = None, path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Replace a plan's document. The id never changes (design §2.12)."""
    name, payload = _validated_plan_json(plan_id, {k: v for k, v in doc.items() if k != "id"})
    ts = _now() if now is None else int(now)
    try:
        with _conn(path) as conn:
            cur = conn.execute(
                "UPDATE access_plans SET name = ?, plan_json = ?, updated_at = ? WHERE id = ?",
                (name, payload, ts, plan_id),
            )
            if cur.rowcount == 0:
                return None
            conn.commit()
            row = conn.execute("SELECT * FROM access_plans WHERE id = ?", (plan_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"access plan name {name!r} is already in use") from exc
    return _plan_row_to_dict(row)


def access_plan_users(conn: sqlite3.Connection, plan_id: str) -> dict[str, list[str] | int]:
    """Who holds a plan: people, apps (by name) and open invitations."""
    apps = [f"{r[0]} (disabled)" if r[1] is not None else str(r[0]) for r in conn.execute(
        "SELECT name, disabled_at FROM apps WHERE access_plan = ? ORDER BY name", (plan_id,),
    )]
    people: list[str] = []
    invites = 0
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_access'").fetchone():
        people = [str(r[0] or r[1]) for r in conn.execute(
            "SELECT u.display_name, u.email FROM account_access a JOIN users u ON u.id = a.user_id "
            "WHERE a.plan_id = ? AND a.removed_at IS NULL ORDER BY u.created_at", (plan_id,),
        )]
        invites = conn.execute(
            "SELECT COUNT(*) FROM account_links WHERE plan_id = ? AND kind = 'invite' AND used_at IS NULL "
            "AND revoked_at IS NULL AND expires_at > ?", (plan_id, _now()),
        ).fetchone()[0]
    return {"people": people, "apps": apps, "invites": invites}


def delete_access_plan(plan_id: str, *, path: Path = DB_PATH) -> dict[str, Any] | None:
    """Delete a plan nobody holds. Raises ValueError naming its holders."""
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM access_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            conn.rollback()
            return None
        users = access_plan_users(conn, plan_id)
        parts = []
        if users["people"]:
            parts.append(f"{len(users['people'])} {'person' if len(users['people']) == 1 else 'people'} "
                         f"({', '.join(users['people'][:5])})")
        if users["apps"]:
            parts.append(f"{len(users['apps'])} app{'' if len(users['apps']) == 1 else 's'} "
                         f"({', '.join(users['apps'][:5])})")
        if users["invites"]:
            parts.append(f"{users['invites']} open invitation{'' if users['invites'] == 1 else 's'}")
        if parts:
            conn.rollback()
            raise ValueError(f"{row['name']} is used by {' and '.join(parts)}. Move them to another plan first.")
        # Closed invitations and removed people keep no claim on the plan.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_access'").fetchone():
            conn.execute("UPDATE account_links SET plan_id = NULL WHERE plan_id = ?", (plan_id,))
            conn.execute("UPDATE account_access SET plan_id = NULL WHERE plan_id = ?", (plan_id,))
        conn.execute("DELETE FROM access_plans WHERE id = ?", (plan_id,))
        conn.commit()
    return _plan_row_to_dict(row)


def instance_owner_id(*, path: Path = DB_PATH) -> str | None:
    """The first person granted the owner role: the default accountable person."""
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT r.subject_id FROM admin_role_assignments r JOIN users u ON u.id = r.subject_id "
            "WHERE r.role = 'owner' AND r.subject_kind = 'user' AND r.revoked_at IS NULL "
            "ORDER BY r.granted_at, r.id LIMIT 1",
        ).fetchone()
    return str(row[0]) if row else None


def person_plan_id(user_id: str, *, path: Path = DB_PATH) -> str | None:
    """A person's access plan id, or None (no plan, or removed)."""
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT plan_id FROM account_access WHERE user_id = ? AND removed_at IS NULL", (user_id,),
        ).fetchone()
    return row[0] if row else None


STARTER_PLAN_NAME = "Local only"


def seed_first_owner_plan(user_id: str, *, path: Path = DB_PATH) -> str | None:
    """Give the first owner of a fresh instance one working plan.

    Without it the owner would be refused on their own gateway until they had
    built a plan (there is no built-in default; design §2.8). The plan allows
    every local model and workflow, no paid providers, no direct engine
    access. It is an ordinary plan the owner can edit or delete; it is not a
    fallback for anyone else. Does nothing if any plan exists or the person
    already holds one. Returns the new plan id.
    """
    from gateway.access_plans import CAPABILITIES, AccessPlan, CapabilityGrant, plan_to_document

    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM access_plans LIMIT 1").fetchone() is not None:
            conn.rollback()
            return None
        _insert_account_access(conn, user_id)
        if conn.execute("SELECT plan_id FROM account_access WHERE user_id = ?", (user_id,)).fetchone()[0] is not None:
            conn.rollback()
            return None
        plan_id = _gen_id("plan")
        document = plan_to_document(AccessPlan(
            id=plan_id, name=STARTER_PLAN_NAME,
            can_use={capability: CapabilityGrant(all_items=True) for capability in CAPABILITIES},
            can_load_models=True,
        ))
        ts = _now()
        conn.execute(
            "INSERT INTO access_plans(id, name, plan_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (plan_id, STARTER_PLAN_NAME, json.dumps(document, ensure_ascii=False, sort_keys=True), ts, ts),
        )
        conn.execute("UPDATE account_access SET plan_id = ? WHERE user_id = ?", (plan_id, user_id))
        conn.commit()
    return plan_id


INVITE_ACCESS_PLAN_SETTING_KEY = "invite_access_plan"


def invite_access_plan(*, path: Path = DB_PATH) -> str | None:
    """The plan preselected for new invitations; None means "Ask each time".

    A stored plan that was deleted reads as "Ask each time".
    """
    setting = get_operator_setting(INVITE_ACCESS_PLAN_SETTING_KEY, path=path)
    plan_id = (setting or {}).get("value", {}).get("plan_id") if isinstance((setting or {}).get("value"), Mapping) else None
    if plan_id and get_access_plan(str(plan_id), path=path) is not None:
        return str(plan_id)
    return None


def _check_app_assignment(conn: sqlite3.Connection, access_plan: str | None) -> None:
    if access_plan is None:
        raise ValueError("an app needs an access plan: a plan id or 'person'")
    if access_plan != PERSON_ASSIGNMENT and conn.execute(
        "SELECT 1 FROM access_plans WHERE id = ?", (access_plan,)
    ).fetchone() is None:
        raise ValueError("access plan not found")


def set_app_access_plan(app_id: str, access_plan: str, *, path: Path = DB_PATH) -> dict[str, Any] | None:
    """Assign an app a plan id or 'person'. No operation clears an assignment."""
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _check_app_assignment(conn, access_plan)
        cur = conn.execute("UPDATE apps SET access_plan = ? WHERE id = ?", (access_plan, app_id))
        if cur.rowcount == 0:
            conn.rollback()
            return None
        conn.commit()
        row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# apps
# ---------------------------------------------------------------------------


def create_app(
    name: str,
    *,
    kind: str,
    owner_user_id: str,
    access_plan: str,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Create an App with its accountable person and its assignment.

    ``access_plan`` is a plan id or ``"person"`` ("Same as its person"). The
    row and its assignment commit together, so an App never exists without
    one (design §2.4).
    """
    if kind not in APP_KINDS:
        raise ValueError(f"app kind must be one of {APP_KINDS}, got {kind!r}")
    if not owner_user_id:
        raise ValueError("an app needs an accountable person (owner_user_id)")

    ts = _now() if now is None else int(now)
    app_id = _gen_id("app")
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _check_app_assignment(conn, access_plan)
        conn.execute(
            "INSERT INTO apps(id, kind, name, owner_user_id, access_plan, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (app_id, kind, name, owner_user_id, access_plan, ts),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM apps WHERE id = ?", (app_id,)
        ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def ensure_personal_app_for_user(
    user_id: str,
    *,
    name: str | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Return the user's personal app, creating one if missing.

    A user has at most one *personal* app — it's the home for their own
    data-plane keys ("the key in your editor"). A new one is "Same as its
    person". Service apps are separate and created explicitly.
    """
    if not user_id:
        raise ValueError("user_id is required")
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM apps WHERE kind = 'personal' AND owner_user_id = ? "
            "AND id NOT IN (SELECT app_id FROM session_apps) "
            "ORDER BY created_at ASC LIMIT 1",
            (user_id,),
        ).fetchone()
    if row is not None:
        return _row_to_dict(row)  # type: ignore[return-value]
    return create_app(
        name or f"personal-{user_id}",
        kind="personal",
        owner_user_id=user_id,
        access_plan=PERSON_ASSIGNMENT,
        now=now,
        path=path,
    )


def ensure_session_app_for_user(
    user_id: str,
    *,
    name: str,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Return the keyless App for a person's signed-in work, creating it once.

    It is a personal App so receipts, jobs, and Library ownership attribute the
    work to the person, but it is not the person's key home: it never holds
    credentials and ``ensure_personal_app_for_user`` never returns it.
    """
    if not user_id:
        raise ValueError("user_id is required")
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT apps.* FROM session_apps JOIN apps ON apps.id = session_apps.app_id "
            "WHERE session_apps.user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            app_id = _gen_id("app")
            conn.execute(
                "INSERT INTO apps(id, kind, name, owner_user_id, access_plan, created_at) "
                "VALUES (?, 'personal', ?, ?, 'person', ?)",
                (app_id, name, user_id, _now() if now is None else int(now)),
            )
            conn.execute(
                "INSERT INTO session_apps(user_id, app_id) VALUES (?, ?)",
                (user_id, app_id),
            )
            row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
        elif row["access_plan"] is None:
            # A failed assignment write leaves an App that denies; the next
            # sign-in repairs it (design §2.4).
            conn.execute("UPDATE apps SET access_plan = 'person' WHERE id = ?", (row["id"],))
            row = conn.execute("SELECT * FROM apps WHERE id = ?", (row["id"],)).fetchone()
        conn.commit()
    return _row_to_dict(row)  # type: ignore[return-value]


def get_session_app_for_user(user_id: str, *, path: Path = DB_PATH) -> dict[str, Any] | None:
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT apps.* FROM session_apps JOIN apps ON apps.id = session_apps.app_id "
            "WHERE session_apps.user_id = ?",
            (user_id,),
        ).fetchone()
    return _row_to_dict(row)


def is_session_app(
    app_id: str, *, path: Path = DB_PATH,
    connection: sqlite3.Connection | None = None,
) -> bool:
    with _conn(path) if connection is None else nullcontext(connection) as conn:
        return conn.execute(
            "SELECT 1 FROM session_apps WHERE app_id = ?", (app_id,)
        ).fetchone() is not None


def get_app(
    app_id: str, *, path: Path = DB_PATH,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    with _conn(path) if connection is None else nullcontext(connection) as conn:
        row = conn.execute(
            "SELECT * FROM apps WHERE id = ?", (app_id,)
        ).fetchone()
    return _row_to_dict(row)


def update_app(
    app_id: str,
    *,
    name: str | None = None,
    access_plan: str | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Rename an App or change its assignment. An assignment is never cleared."""
    assignments: list[str] = []
    params: list[Any] = []
    if name is not None:
        if not name:
            raise ValueError("app name is required")
        assignments.append("name = ?")
        params.append(name)
    if access_plan is not None:
        assignments.append("access_plan = ?")
        params.append(access_plan)
    if not assignments:
        return get_app(app_id, path=path)

    params.append(app_id)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if access_plan is not None:
            _check_app_assignment(conn, access_plan)
        cur = conn.execute(
            f"UPDATE apps SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        if cur.rowcount == 0:
            return None
        conn.commit()
        row = conn.execute(
            "SELECT * FROM apps WHERE id = ?", (app_id,)
        ).fetchone()
    return _row_to_dict(row)


def disable_app(
    app_id: str, *, now: int | None = None, path: Path = DB_PATH
) -> dict[str, Any] | None:
    """Mark an App disabled. Idempotent; returns the current row when found."""
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM apps WHERE id = ?", (app_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE apps SET disabled_at = COALESCE(disabled_at, ?) WHERE id = ?",
            (ts, app_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM apps WHERE id = ?", (app_id,)
        ).fetchone()
    return _row_to_dict(row)


def enable_app(
    app_id: str, *, path: Path = DB_PATH
) -> dict[str, Any] | None:
    """Clear the disabled_at flag on an App. Idempotent; returns the current row when found."""
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM apps WHERE id = ?", (app_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE apps SET disabled_at = NULL WHERE id = ?",
            (app_id,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM apps WHERE id = ?", (app_id,)
        ).fetchone()
    return _row_to_dict(row)


def list_apps(*, path: Path = DB_PATH) -> list[dict[str, Any]]:
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM apps ORDER BY created_at ASC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def list_apps_for_user(
    user_id: str, *, path: Path = DB_PATH
) -> list[dict[str, Any]]:
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM apps WHERE owner_user_id = ? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def create_credential_hash(
    *,
    subject_kind: str,
    subject_id: str,
    hash: str,
    prefix: str,
    app_id: str | None = None,
    display_name: str | None = None,
    scopes: Iterable[str] | None = None,
    created_by: str | None = None,
    expires_at: int | None = None,
    session_id: str | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Persist a credential hash.

    Subject invariants:

    * ``subject_kind == "app"`` is a data-plane credential. ``app_id`` must
      match ``subject_id`` (the app is its own subject).
    * ``subject_kind in {"user", "service_account"}`` is a control-plane /
      admin credential. ``app_id`` must be unset — admin keys are not scoped
      to a single app at credential level (use role assignments for that).
    """
    if subject_kind not in SUBJECT_KINDS:
        raise ValueError(
            f"subject_kind must be one of {SUBJECT_KINDS}, got {subject_kind!r}"
        )
    if not subject_id:
        raise ValueError("subject_id is required")
    if not hash or not prefix:
        raise ValueError("hash and prefix are required")

    if subject_kind in _DATA_PLANE_SUBJECTS:
        if app_id is None:
            app_id = subject_id
        elif app_id != subject_id:
            raise ValueError(
                "data-plane credentials must have app_id == subject_id"
            )
    else:  # control plane
        if app_id is not None:
            raise ValueError(
                "admin credentials must not be bound to an app_id; "
                "use role assignments to scope admin access to an app"
            )

    ts = _now() if now is None else int(now)
    cred_id = _gen_id("cred")
    scopes_json = (
        json.dumps(sorted({s for s in scopes if s})) if scopes else None
    )
    with _conn(path) as conn:
        if subject_kind in _DATA_PLANE_SUBJECTS and conn.execute(
            "SELECT 1 FROM session_apps WHERE app_id = ?", (app_id,)
        ).fetchone():
            raise ValueError("this App is a person's signed-in App and cannot hold API keys")
        conn.execute(
            "INSERT INTO credentials(id, subject_kind, subject_id, app_id, "
            "hash, prefix, display_name, scopes_json, "
            "created_at, created_by, expires_at, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cred_id,
                subject_kind,
                subject_id,
                app_id,
                hash,
                prefix,
                display_name,
                scopes_json,
                ts,
                created_by,
                expires_at,
                session_id,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM credentials WHERE id = ?", (cred_id,)
        ).fetchone()
    return _credential_row_to_dict(row)


def revoke_credential(
    cred_id: str, *, now: int | None = None, path: Path = DB_PATH
) -> bool:
    """Mark a credential revoked. Idempotent; returns True if state changed."""
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        cur = conn.execute(
            "UPDATE credentials SET revoked_at = ? "
            "WHERE id = ? AND revoked_at IS NULL",
            (ts, cred_id),
        )
        conn.commit()
        return cur.rowcount > 0


def revoke_older_session_credentials(
    app_id: str, session_id: str, *, keep: int, now: int | None = None, path: Path = DB_PATH
) -> int:
    """Revoke an App's keys bound to one sign-in, except the newest ``keep``.

    Insertion order (rowid) breaks ties between keys made in the same second.
    """
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        cur = conn.execute(
            "UPDATE credentials SET revoked_at = ? WHERE id IN ("
            "SELECT id FROM credentials WHERE app_id = ? AND session_id = ? "
            "AND revoked_at IS NULL ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
            (ts, app_id, session_id, keep),
        )
        conn.commit()
        return cur.rowcount


# Coarsen ``last_used_at`` writes: this fires on the data-plane auth hot path
# (every authenticated request), so we only persist a new timestamp when the
# stored one is missing or older than this many seconds. The portal shows a
# relative "used N ago", which doesn't need second-level precision, and this
# keeps the write rate to at most one UPDATE per key per minute under load.
TOUCH_LAST_USED_INTERVAL_SECONDS = 60


def touch_credential_last_used(
    cred_id: str,
    *,
    now: int | None = None,
    min_interval: int = TOUCH_LAST_USED_INTERVAL_SECONDS,
    path: Path = DB_PATH,
    connection: sqlite3.Connection | None = None,
) -> bool:
    """Record that a credential was just used (debounced).

    Updates ``last_used_at`` only when it is null or older than ``min_interval``
    seconds, so a busy key doesn't issue a write on every request. Returns True
    when a row was actually updated. Best-effort: callers on the auth hot path
    should not fail a request if this raises.
    """
    ts = _now() if now is None else int(now)
    cutoff = ts - max(0, int(min_interval))
    with _conn(path) if connection is None else nullcontext(connection) as conn:
        cur = conn.execute(
            "UPDATE credentials SET last_used_at = ? "
            "WHERE id = ? AND (last_used_at IS NULL OR last_used_at <= ?)",
            (ts, cred_id, cutoff),
        )
        conn.commit()
        return cur.rowcount > 0


def get_credential(
    cred_id: str, *, path: Path = DB_PATH
) -> dict[str, Any] | None:
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM credentials WHERE id = ?", (cred_id,)
        ).fetchone()
    return _credential_row_to_dict(row) if row else None


def find_credential_by_hash(
    hash: str, *, path: Path = DB_PATH,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Look up a credential by stored hash. Caller hashes the secret first."""
    with _conn(path) if connection is None else nullcontext(connection) as conn:
        row = conn.execute(
            "SELECT * FROM credentials WHERE hash = ?", (hash,)
        ).fetchone()
    return _credential_row_to_dict(row) if row else None


def find_runtime_credential_by_hash(
    hash: str, *, path: Path = DB_PATH,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Look up a credential and, in the same query, whether its App is a
    session App (``on_session_app``)."""
    with _conn(path) if connection is None else nullcontext(connection) as conn:
        row = conn.execute(
            "SELECT credentials.*, EXISTS(SELECT 1 FROM session_apps WHERE session_apps.app_id = "
            "COALESCE(NULLIF(credentials.app_id, ''), credentials.subject_id)) AS on_session_app "
            "FROM credentials WHERE hash = ?", (hash,)
        ).fetchone()
    if row is None:
        return None
    credential = _credential_row_to_dict(row)
    credential["on_session_app"] = bool(credential["on_session_app"])
    return credential


# One statement is one snapshot (design §2.13 a): the credential or session
# App, the App, its accountable person's account state and both plans.
_RUNTIME_ACCESS_SELECT = """
SELECT a.id AS app_id, a.name AS app_name, a.kind AS app_kind, a.owner_user_id,
       a.disabled_at AS app_disabled_at, a.access_plan,
       u.id AS person_row, u.issuer AS person_issuer,
       acc.user_id AS access_row, acc.disabled_at AS person_disabled_at,
       acc.removed_at AS person_removed_at, acc.plan_id AS person_plan_id,
       pp.plan_json AS person_plan_json,
       ap.plan_json AS app_plan_json,
       EXISTS(SELECT 1 FROM session_apps s WHERE s.app_id = a.id) AS on_session_app
FROM apps a
LEFT JOIN users u ON u.id = a.owner_user_id
LEFT JOIN account_access acc ON acc.user_id = a.owner_user_id
LEFT JOIN access_plans pp ON pp.id = acc.plan_id
LEFT JOIN access_plans ap ON ap.id = CASE WHEN a.access_plan = 'person' THEN acc.plan_id ELSE a.access_plan END
"""


def runtime_access_row(
    conn: sqlite3.Connection, *, key_hash: str | None = None, app_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve an App key (by hash) or a session App (by id) in one SELECT.

    For a key the row also carries the credential columns (``cred_*``).
    """
    if key_hash is not None:
        row = conn.execute(
            "SELECT c.id AS cred_id, c.subject_kind AS cred_subject_kind, c.revoked_at AS cred_revoked_at, "
            "c.expires_at AS cred_expires_at, c.created_by AS cred_created_by, "
            "c.display_name AS cred_display_name, q.* FROM credentials c "
            f"JOIN ({_RUNTIME_ACCESS_SELECT}) q ON q.app_id = COALESCE(NULLIF(c.app_id, ''), c.subject_id) "
            "WHERE c.hash = ?", (key_hash,),
        ).fetchone()
    elif app_id is not None:
        row = conn.execute(f"SELECT * FROM ({_RUNTIME_ACCESS_SELECT}) WHERE app_id = ?", (app_id,)).fetchone()
    else:
        raise ValueError("key_hash or app_id is required")
    if row is None:
        return None
    data = {k: row[k] for k in row.keys()}
    data["on_session_app"] = bool(data["on_session_app"])
    return data


def list_active_credentials(
    *,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    app_id: str | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """List credentials that are not revoked and not expired.

    Filters are AND-ed. ``now`` is the comparison instant for expiry; defaults
    to current time.
    """
    ts = _now() if now is None else int(now)
    clauses = ["revoked_at IS NULL", "(expires_at IS NULL OR expires_at > ?)"]
    params: list[Any] = [ts]
    if subject_kind is not None:
        clauses.append("subject_kind = ?")
        params.append(subject_kind)
    if subject_id is not None:
        clauses.append("subject_id = ?")
        params.append(subject_id)
    if app_id is not None:
        clauses.append("app_id = ?")
        params.append(app_id)
    where = " AND ".join(clauses)
    with _conn(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM credentials WHERE {where} ORDER BY created_at ASC",
            params,
        ).fetchall()
    return [_credential_row_to_dict(r) for r in rows]


def list_credentials(*, path: Path = DB_PATH) -> list[dict[str, Any]]:
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM credentials ORDER BY created_at ASC"
        ).fetchall()
    return [_credential_row_to_dict(r) for r in rows]


def _credential_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys()}
    scopes = data.pop("scopes_json", None)
    if scopes:
        try:
            data["scopes"] = json.loads(scopes)
        except json.JSONDecodeError:
            data["scopes"] = []
    else:
        data["scopes"] = []
    return data


# ---------------------------------------------------------------------------
# admin roles
# ---------------------------------------------------------------------------


def assign_admin_role(
    *,
    role: str,
    subject_kind: str,
    subject_id: str,
    scope_app_id: str | None = None,
    granted_by: str | None = None,
    provenance: str = "manual",
    actor: Any = None,
    actor_kind: str = "system",
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Grant an admin role to a user or service account.

    Optional ``scope_app_id`` narrows the role to a single app — e.g.
    "owner-of(app_xyz)" rather than a global role. ``(role, subject, scope)``
    is unique; re-granting the same triple is idempotent and un-revokes if
    needed.
    """
    if subject_kind not in _CONTROL_PLANE_SUBJECTS:
        raise ValueError(
            f"admin roles can only be granted to {sorted(_CONTROL_PLANE_SUBJECTS)}, "
            f"got {subject_kind!r}"
        )
    if not role or not subject_id:
        raise ValueError("role and subject_id are required")
    if provenance not in {"manual", "oidc"}:
        raise ValueError("provenance must be manual or oidc")
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM admin_role_assignments "
            "WHERE role = ? AND subject_kind = ? AND subject_id = ? "
            "AND COALESCE(scope_app_id, '') = COALESCE(?, '')",
            (role, subject_kind, subject_id, scope_app_id),
        ).fetchone()
        if row is not None:
            if provenance == "oidc" and row["provenance"] == "manual" and row["revoked_at"] is None:
                return _row_to_dict(row)  # type: ignore[return-value]
            conn.execute(
                "UPDATE admin_role_assignments SET revoked_at = NULL, "
                "granted_at = ?, granted_by = ?, provenance = ? WHERE id = ?",
                (ts, granted_by, provenance, row["id"]),
            )
            assignment_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO admin_role_assignments(role, subject_kind, "
                "subject_id, scope_app_id, granted_at, granted_by, provenance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (role, subject_kind, subject_id, scope_app_id, ts, granted_by, provenance),
            )
            assignment_id = cur.lastrowid
        granted = conn.execute("SELECT * FROM admin_role_assignments WHERE id=?", (assignment_id,)).fetchone()
        _audit_admin_role(conn, "admin_role.grant", granted, ts, actor=actor,
                          actor_kind=actor_kind, actor_id=granted_by)
        conn.commit()
        out = conn.execute(
            "SELECT * FROM admin_role_assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
    return _row_to_dict(out)  # type: ignore[return-value]


def reconcile_oidc_admin_roles(
    subject_id: str,
    roles: Iterable[str],
    *,
    now: int | None = None,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Replace claim-derived global grants while preserving every manual grant."""
    desired = set(roles)
    if not subject_id or desired - {"owner", "admin", "superadmin"}:
        raise ValueError("valid user and control-plane roles are required")
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM admin_role_assignments "
            "WHERE subject_kind='user' AND subject_id=? AND scope_app_id IS NULL",
            (subject_id,),
        ).fetchall()
        for row in rows:
            if row["provenance"] == "oidc" and row["role"] not in desired and row["revoked_at"] is None:
                conn.execute(
                    "UPDATE admin_role_assignments SET revoked_at=? "
                    "WHERE id=? AND revoked_at IS NULL", (ts, row["id"]),
                )
                _audit_admin_role(conn, "admin_role.revoke", row, ts,
                                  actor_kind="system", actor_id="oidc")
        for role in sorted(desired):
            matching = [row for row in rows if row["role"] == role]
            if any(row["provenance"] == "manual" and row["revoked_at"] is None for row in matching):
                continue
            existing = next((row for row in matching if row["provenance"] == "oidc"), None)
            if existing is not None:
                if existing["revoked_at"] is None:
                    continue
                conn.execute(
                    "UPDATE admin_role_assignments SET revoked_at=NULL, granted_at=? "
                    "WHERE id=?", (ts, existing["id"]),
                )
                assignment_id = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO admin_role_assignments(role,subject_kind,subject_id,"
                    "scope_app_id,granted_at,granted_by,provenance) "
                    "VALUES (?,'user',?,NULL,?,'oidc','oidc')", (role, subject_id, ts),
                )
                assignment_id = cur.lastrowid
            granted = conn.execute("SELECT * FROM admin_role_assignments WHERE id=?", (assignment_id,)).fetchone()
            _audit_admin_role(conn, "admin_role.grant", granted, ts,
                              actor_kind="system", actor_id="oidc")
        conn.commit()
        return [_row_to_dict(row) for row in conn.execute(
            "SELECT * FROM admin_role_assignments WHERE subject_kind='user' "
            "AND subject_id=? AND revoked_at IS NULL", (subject_id,)
        )]  # type: ignore[misc]


def revoke_admin_role(
    assignment_id: int, *, actor: Any = None, actor_kind: str = "system",
    actor_id: str | None = None, now: int | None = None, path: Path = DB_PATH
) -> bool:
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM admin_role_assignments WHERE id=?", (assignment_id,)).fetchone()
        cur = conn.execute(
            "UPDATE admin_role_assignments SET revoked_at = ? "
            "WHERE id = ? AND revoked_at IS NULL",
            (ts, assignment_id),
        )
        if cur.rowcount:
            _audit_admin_role(conn, "admin_role.revoke", row, ts, actor=actor,
                              actor_kind=actor_kind, actor_id=actor_id)
        conn.commit()
        return cur.rowcount > 0


def _audit_admin_role(conn, action, row, now, *, actor=None, actor_kind="system", actor_id=None):
    _record_audit_in_transaction(
        conn, action, actor_kind=actor.actor_kind if actor else actor_kind,
        actor_id=actor.audit_actor_id if actor else actor_id,
        actor_label=actor.actor_label if actor else None,
        target_kind="admin_role_assignment", target_id=str(row["id"]),
        metadata={"role": row["role"], "subject_kind": row["subject_kind"],
                  "subject_id": row["subject_id"], "scope_app_id": row["scope_app_id"],
                  "provenance": "oidc-claim" if row["provenance"] == "oidc" else "manual"},
        now=now,
    )


def list_admin_roles(
    *,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    include_revoked: bool = False,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if not include_revoked:
        clauses.append("revoked_at IS NULL")
    if subject_kind is not None:
        clauses.append("subject_kind = ?")
        params.append(subject_kind)
    if subject_id is not None:
        clauses.append("subject_id = ?")
        params.append(subject_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _conn(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM admin_role_assignments{where} "
            "ORDER BY granted_at ASC",
            params,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def owner_display_name(*, path: Path = DB_PATH) -> str | None:
    """Display name of the earliest active gateway owner that has a clean one.

    Returns ``None`` when no owner has one. Email addresses are sign-in
    identities, not a published contact, and must never leak through this
    field. Setup stores the local owner's email as its display name, and OIDC
    sign-in falls back to the email when there is no ``name`` claim, so a name
    that equals the user's email or contains ``@`` is skipped.
    """
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT u.display_name AS name, u.email AS email FROM admin_role_assignments r "
            "JOIN users u ON u.id = r.subject_id "
            "WHERE r.role = 'owner' AND r.subject_kind = 'user' "
            "AND r.revoked_at IS NULL AND r.scope_app_id IS NULL "
            "ORDER BY r.granted_at ASC, r.id ASC"
        ).fetchall()
    for row in rows:
        name = str(row["name"] or "").strip()
        email = str(row["email"] or "").strip()
        if not name or "@" in name or (email and name.casefold() == email.casefold()):
            continue
        return name
    return None


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def record_audit(
    action: str,
    *,
    actor_kind: str | None = None,
    actor_id: str | None = None,
    actor_label: str | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Append an audit event. Append-only; callers never update or delete."""
    if not action:
        raise ValueError("action is required")
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        aud_id = _record_audit_in_transaction(
            conn,
            action,
            actor_kind=actor_kind,
            actor_id=actor_id,
            actor_label=actor_label,
            target_kind=target_kind,
            target_id=target_id,
            metadata=metadata,
            now=ts,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM audit_events WHERE id = ?", (aud_id,)
        ).fetchone()
    return _audit_row_to_dict(row)


def _record_audit_in_transaction(
    conn: sqlite3.Connection,
    action: str,
    *,
    actor_kind: str | None,
    actor_id: str | None,
    actor_label: str | None,
    target_kind: str | None,
    target_id: str | None,
    metadata: Mapping[str, Any] | None,
    now: int,
) -> str:
    """Insert one audit fact into a transaction the caller owns."""
    if not action:
        raise ValueError("action is required")
    aud_id = _gen_id("aud")
    payload = (
        json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True)
        if metadata
        else None
    )
    conn.execute(
        "INSERT INTO audit_events(id, created_at, actor_kind, actor_id, actor_label, "
        "action, target_kind, target_id, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            aud_id,
            now,
            actor_kind,
            actor_id,
            actor_label.strip()[:256] if isinstance(actor_label, str) else None,
            action,
            target_kind,
            target_id,
            payload,
        ),
    )
    return aud_id


def list_audit(
    *,
    target_kind: str | None = None,
    target_id: str | None = None,
    actor_id: str | None = None,
    action_prefix: str | None = None,
    exclude_access_audit: bool = False,
    exclude_internal_activity: bool = False,
    exclude_service_account_activity: bool = False,
    before_created_at: int | None = None,
    before_id: str | None = None,
    limit: int = 100,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Query audit events with optional filters and keyset cursor pagination.

    Filters:
      target_kind / target_id — exact match (unchanged).
      actor_id               — exact match on actor_id column.
      action_prefix          — matches action LIKE '<prefix>%'.
      before_created_at + before_id — keyset cursor; both must be supplied
        together.  Returns events strictly older than the given position using
        the composite tuple comparison ``(created_at, id) < (before_created_at,
        before_id)`` which is consistent with the ORDER BY created_at DESC,
        id DESC.

    Returns up to *limit* rows sorted newest-first.  To check whether more
    rows exist the caller should request ``limit + 1`` rows and inspect the
    result length.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if target_kind is not None:
        clauses.append("target_kind = ?")
        params.append(target_kind)
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(target_id)
    if actor_id is not None:
        clauses.append("actor_id = ?")
        params.append(actor_id)
    if action_prefix is not None:
        clauses.append("action LIKE ?")
        params.append(action_prefix + "%")
    if exclude_access_audit:
        clauses.append("action NOT IN (?, ?)")
        params.extend(("media.library.everyone", "usage.summary.everyone"))
    if exclude_internal_activity:
        placeholders = ", ".join("?" for _ in INTERNAL_ACTIVITY_ACTIONS)
        clauses.append(f"action NOT IN ({placeholders})")
        params.extend(sorted(INTERNAL_ACTIVITY_ACTIONS))
    if exclude_service_account_activity:
        clauses.append("(actor_kind IS NULL OR actor_kind != ?)")
        params.append("service_account")
    if before_created_at is not None and before_id is not None:
        # Composite keyset: (created_at, id) < (before_created_at, before_id)
        # This is correct for ORDER BY created_at DESC, id DESC because we
        # want rows that are "earlier" in that ordering.
        clauses.append(
            "(created_at < ? OR (created_at = ? AND id < ?))"
        )
        params.extend([int(before_created_at), int(before_created_at), before_id])
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with _conn(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM audit_events{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [_audit_row_to_dict(r) for r in rows]


def count_service_account_audit(*, path: Path = DB_PATH) -> int:
    """Count service-account audit events for the activity summary group."""
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE actor_kind = ?",
            ("service_account",),
        ).fetchone()
    return int(row[0]) if row else 0


def get_audit_event(event_id: str, *, path: Path = DB_PATH) -> dict[str, Any] | None:
    """Return one audit event by id, or None when it is not present."""
    if not event_id:
        return None
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM audit_events WHERE id = ?", (event_id,)
        ).fetchone()
    return _audit_row_to_dict(row) if row else None


def _audit_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys()}
    raw = data.pop("metadata_json", None)
    if raw:
        try:
            data["metadata"] = json.loads(raw)
        except json.JSONDecodeError:
            data["metadata"] = None
    else:
        data["metadata"] = None
    return data


# ---------------------------------------------------------------------------
# operator settings / runtime overrides
# ---------------------------------------------------------------------------


def put_operator_setting(
    key: str,
    value: Mapping[str, Any],
    *,
    updated_by: str | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Persist one non-secret operator override document by key."""
    setting_key = str(key or "").strip()
    if not setting_key:
        raise ValueError("operator setting key is required")
    if not isinstance(value, Mapping):
        raise ValueError("operator setting value must be a mapping")
    if setting_key == "service_registry":
        raise ValueError(
            "service_registry must be written through the registry transaction primitive"
        )
    ts = _now() if now is None else int(now)
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if setting_key == "prompt_memory":
            _freeze_missing_prompt_memory_preferences(conn, _prompt_memory_default(conn), now=ts)
            if value.get("never_keep"):
                _record_prompt_erasure(conn, "*", ts)
        conn.execute(
            "INSERT INTO operator_settings(key, value_json, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value_json = excluded.value_json, "
            "updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by",
            (setting_key, payload, ts, updated_by),
        )
        _advance_setting_revision(conn, setting_key)
        conn.commit()
        row = conn.execute(
            _SETTING_SELECT + " WHERE s.key = ?", (setting_key,)
        ).fetchone()
    return _operator_setting_row_to_dict(row)


def put_operator_setting_with_audit(
    key: str,
    value: Mapping[str, Any],
    *,
    updated_by: str | None,
    audit_action: str,
    audit_actor_kind: str | None,
    audit_actor_id: str | None,
    audit_actor_label: str | None,
    audit_target_kind: str | None,
    audit_target_id: str | None,
    audit_metadata: Mapping[str, Any] | None = None,
    expected_revision: str | None = None,
    validate_in_transaction: Callable[[sqlite3.Connection], None] | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Commit one ordinary setting and its audit fact in one transaction."""
    setting_key = str(key or "").strip()
    if not setting_key:
        raise ValueError("operator setting key is required")
    if setting_key == "service_registry":
        raise ValueError(
            "service_registry must be written through the registry transaction primitive"
        )
    if not isinstance(value, Mapping):
        raise ValueError("operator setting value must be a mapping")
    ts = _now() if now is None else int(now)
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                "SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key=?",
                (setting_key,),
            ).fetchone()
            revision = str(current[0]) if current else "0"
            if expected_revision is not None and expected_revision != revision:
                raise SettingsConflictError("Settings changed elsewhere; reload to see the latest")
            if validate_in_transaction is not None:
                validate_in_transaction(conn)
            if setting_key == "prompt_memory":
                _freeze_missing_prompt_memory_preferences(conn, _prompt_memory_default(conn), now=ts)
                if value.get("never_keep"):
                    _record_prompt_erasure(conn, "*", ts)
            conn.execute(
                "INSERT INTO operator_settings(key, value_json, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
                "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (setting_key, payload, ts, updated_by),
            )
            _advance_setting_revision(conn, setting_key)
            _record_audit_in_transaction(
                conn, audit_action,
                actor_kind=audit_actor_kind,
                actor_id=audit_actor_id,
                actor_label=audit_actor_label,
                target_kind=audit_target_kind,
                target_id=audit_target_id,
                metadata=audit_metadata,
                now=ts,
            )
            row = conn.execute(
                _SETTING_SELECT + " WHERE s.key = ?", (setting_key,)
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return _operator_setting_row_to_dict(row)


class SettingsConflictError(ValueError):
    """An operator setting changed since the client loaded its draft."""


_SETTING_SELECT = (
    "SELECT s.*, COALESCE(g.operator_settings_generation, 0) AS revision "
    "FROM operator_settings s LEFT JOIN operator_settings_generations g ON g.setting_key=s.key"
)


def _advance_setting_revision(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(
        "INSERT INTO operator_settings_generations(setting_key,operator_settings_generation) VALUES(?,1) "
        "ON CONFLICT(setting_key) DO UPDATE SET operator_settings_generation=operator_settings_generation+1",
        (key,),
    )


def operator_setting_revision(record: Mapping[str, Any] | None) -> str:
    return str(record["revision"]) if record else "0"


class OperatorSettingsGenerationConflict(ValueError):
    """A registry document changed after the caller read its ETag."""


def get_service_registry_with_generation(*, path: Path = DB_PATH) -> dict[str, Any] | None:
    """Return the one active registry document and its server-owned CAS value."""
    record = get_service_registry_json_with_generation(path=path)
    if record is None:
        return None
    record["value"] = json.loads(record.pop("value_json"))
    return record


def get_service_registry_json_with_generation(*, path: Path = DB_PATH) -> dict[str, Any] | None:
    """Read the encoded document and generation together, without parsing JSON."""
    # One statement, so the document and its CAS generation come from the same
    # snapshot. Two SELECTs could straddle a concurrent commit and pair stale
    # JSON with the new generation, letting a later CAS write overwrite it.
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT s.value_json, s.updated_at, s.updated_by, g.operator_settings_generation "
            "FROM operator_settings AS s "
            "LEFT JOIN operator_settings_generations AS g ON g.setting_key = s.key "
            "WHERE s.key = 'service_registry'"
        ).fetchone()
    if row is None:
        return None
    generation = row["operator_settings_generation"]
    return {
        "key": "service_registry", "value_json": row["value_json"], "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "operator_settings_generation": int(generation) if generation is not None else 0,
    }


def write_service_registry(
    value: Mapping[str, Any], *, updated_by: str | None = None,
    expected_generation: int, now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Atomically write the active registry and advance its only CAS counter.

    This is the sole production writer for ``operator.service_registry``.  It
    intentionally owns no artifact/reload effect: those occur after commit.
    """
    if not isinstance(value, Mapping):
        raise ValueError("service registry value must be a mapping")
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        next_generation = write_service_registry_in_transaction(
            conn, value, updated_by=updated_by,
            expected_generation=expected_generation, now=ts,
        )
        conn.commit()
    return {"key": "service_registry", "value": dict(value), "updated_at": ts,
            "updated_by": updated_by, "operator_settings_generation": next_generation}


def write_service_registry_with_audit(
    value: Mapping[str, Any],
    *,
    expected_generation: int,
    updated_by: str | None,
    audit_action: str,
    audit_actor_kind: str | None,
    audit_actor_id: str | None,
    audit_actor_label: str | None,
    audit_target_kind: str | None,
    audit_target_id: str | None,
    audit_metadata: Mapping[str, Any] | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Commit one registry revision, generation, and audit fact atomically.

    This is the production writer for ordinary registry mutations.  Artifact
    publication and reload stay outside this transaction because they observe
    the committed registry rather than participating in SQLite atomicity.
    """
    if not isinstance(value, Mapping):
        raise ValueError("service registry value must be a mapping")
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            next_generation = write_service_registry_in_transaction(
                conn,
                value,
                updated_by=updated_by,
                expected_generation=expected_generation,
                now=ts,
            )
            _record_audit_in_transaction(
                conn,
                audit_action,
                actor_kind=audit_actor_kind,
                actor_id=audit_actor_id,
                actor_label=audit_actor_label,
                target_kind=audit_target_kind,
                target_id=audit_target_id,
                metadata=audit_metadata,
                now=ts,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "key": "service_registry",
        "value": dict(value),
        "updated_at": ts,
        "updated_by": updated_by,
        "operator_settings_generation": next_generation,
    }


def write_service_registry_in_transaction(
    conn: sqlite3.Connection,
    value: Mapping[str, Any],
    *,
    updated_by: str | None,
    expected_generation: int,
    now: int,
) -> int:
    """Write the registry and increment its generation in a caller-owned transaction.

    Lifecycle cutover/deactivation need their pointer, audit, and registry
    update to commit together.  This is deliberately the only internal escape
    hatch: callers must already hold ``BEGIN IMMEDIATE``.
    """
    generation_row = conn.execute(
        "SELECT operator_settings_generation FROM operator_settings_generations "
        "WHERE setting_key = 'service_registry'"
    ).fetchone()
    generation = int(generation_row[0]) if generation_row else 0
    if generation != expected_generation:
        raise OperatorSettingsGenerationConflict(
            f"operator settings generation mismatch: current={generation}"
        )
    next_generation = generation + 1
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO operator_settings(key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        ("service_registry", payload, now, updated_by),
    )
    conn.execute(
        "INSERT INTO operator_settings_generations(setting_key, operator_settings_generation) VALUES (?, ?) "
        "ON CONFLICT(setting_key) DO UPDATE SET operator_settings_generation=excluded.operator_settings_generation",
        ("service_registry", next_generation),
    )
    return next_generation


def get_operator_setting(
    key: str,
    *,
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    setting_key = str(key or "").strip()
    if not setting_key:
        return None
    with _conn(path) as conn:
        row = conn.execute(
            _SETTING_SELECT + " WHERE s.key = ?", (setting_key,)
        ).fetchone()
    return _operator_setting_row_to_dict(row) if row is not None else None


def get_prompt_memory_preference(
    user_id: str,
    *,
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    if not user_id:
        return None
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM prompt_memory_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    data = _row_to_dict(row)
    if data is None:
        return None
    data["remember"] = bool(data.get("remember"))
    return data


def ensure_prompt_memory_preference(
    user_id: str,
    *,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Persist the household default for a person who has no saved choice."""
    if not user_id:
        raise ValueError("user_id is required")
    existing = get_prompt_memory_preference(user_id, path=path)
    if existing is not None:
        return existing
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("BEGIN IMMEDIATE")
        _insert_prompt_memory_preference(conn, user_id, ts)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM prompt_memory_preferences WHERE user_id = ?", (user_id,),
        ).fetchone()
    data = _row_to_dict(row)
    if data is None:
        raise RuntimeError("prompt memory preference initialization failed")
    data["remember"] = bool(data.get("remember"))
    return data


def _record_prompt_erasure(conn: sqlite3.Connection, person_id: str, cutoff: int) -> None:
    """Persist retry intent and the late-write fence in the setting transaction."""
    conn.execute("CREATE TABLE IF NOT EXISTS prompt_memory_erasure_cutoffs ("
                 "person_id TEXT PRIMARY KEY, cutoff_at INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS prompt_memory_erasure_intents ("
                 "person_id TEXT PRIMARY KEY, cutoff_at INTEGER NOT NULL)")
    for table in ("prompt_memory_erasure_cutoffs", "prompt_memory_erasure_intents"):
        conn.execute(
            f"INSERT INTO {table}(person_id,cutoff_at) VALUES(?,?) "
            "ON CONFLICT(person_id) DO UPDATE SET cutoff_at=MAX(cutoff_at,excluded.cutoff_at)",
            (person_id, cutoff),
        )


def put_prompt_memory_preference(
    user_id: str,
    *,
    remember: bool,
    retention: str,
    updated_by: str | None = None,
    validate_in_transaction: Callable[[sqlite3.Connection], None] | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    if not user_id:
        raise ValueError("user_id is required")
    if retention not in {"3_months", "1_year", "never"}:
        raise ValueError("retention must be one of 3_months, 1_year, never")
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if validate_in_transaction is not None:
            validate_in_transaction(conn)
        if not remember:
            _record_prompt_erasure(conn, user_id, ts)
        conn.execute(
            "INSERT INTO prompt_memory_preferences"
            "(user_id, remember, retention, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "remember=excluded.remember, retention=excluded.retention, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (user_id, 1 if remember else 0, retention, ts, updated_by),
        )
        row = conn.execute("SELECT * FROM prompt_memory_preferences WHERE user_id=?", (user_id,)).fetchone()
        conn.commit()
    # Return this write's timestamp even if a later request changes the setting.
    record = _row_to_dict(row)
    if record is None:
        raise RuntimeError("prompt memory preference write failed")
    record["remember"] = bool(record["remember"])
    return record


def list_operator_settings(*, path: Path = DB_PATH) -> list[dict[str, Any]]:
    with _conn(path) as conn:
        rows = conn.execute(
            _SETTING_SELECT + " ORDER BY s.key ASC"
        ).fetchall()
    return [_operator_setting_row_to_dict(row) for row in rows]


def _operator_setting_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["value_json"])
    except json.JSONDecodeError:
        value = {}
    if not isinstance(value, dict):
        value = {}
    return {
        "key": row["key"],
        "value": value,
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "revision": str(row["revision"]),
    }


__all__ = [
    "DB_PATH",
    "SCHEMA_VERSION",
    "SUBJECT_KINDS",
    "APP_KINDS",
    "IDENTITY_CLAIM_KINDS",
    "init_db",
    "secure_state_path",
    "schema_version",
    "upsert_oidc_user",
    "get_user",
    "list_users",
    "find_user_by_email",
    "claim_first_local_owner",
    "set_local_password_verifier",
    "get_local_password_verifier",
    "delete_local_password_verifier",
    "replace_user_identity_claims",
    "list_user_identity_claims",
    "create_service_account",
    "ensure_service_account",
    "get_service_account",
    "PERSON_ASSIGNMENT",
    "access_plan_users",
    "create_access_plan",
    "delete_access_plan",
    "get_access_plan",
    "list_access_plans",
    "set_app_access_plan",
    "person_plan_id",
    "INVITE_ACCESS_PLAN_SETTING_KEY",
    "invite_access_plan",
    "update_access_plan",
    "runtime_access_row",
    "create_app",
    "ensure_personal_app_for_user",
    "ensure_session_app_for_user",
    "get_session_app_for_user",
    "is_session_app",
    "get_app",
    "disable_app",
    "list_apps",
    "list_apps_for_user",
    "create_credential_hash",
    "revoke_credential",
    "get_credential",
    "find_credential_by_hash",
    "find_runtime_credential_by_hash",
    "list_credentials",
    "list_active_credentials",
    "assign_admin_role",
    "reconcile_oidc_admin_roles",
    "revoke_admin_role",
    "list_admin_roles",
    "owner_display_name",
    "put_operator_setting",
    "put_operator_setting_with_audit",
    "get_service_registry_with_generation",
    "write_service_registry",
    "write_service_registry_with_audit",
    "write_service_registry_in_transaction",
    "OperatorSettingsGenerationConflict",
    "get_operator_setting",
    "list_operator_settings",
    "get_prompt_memory_preference",
    "ensure_prompt_memory_preference",
    "put_prompt_memory_preference",
    "record_audit",
    "list_audit",
    "get_audit_event",
]
