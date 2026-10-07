"""SQLite-backed durable store for admin / control-plane state.

This module separates *durable facts* (who exists, what they have, what was
done) from *policy decisions* (what they're allowed to do right now). The
policy evaluator lives in :mod:`gateway.policy`; this store just persists the
profile JSON that the evaluator later loads.

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
  ``app_``, ``prof_``, ``cred_``, ``aud_``).
* OIDC users are unique by ``(issuer, subject)`` — re-logins update profile
  data but preserve the row ID, so anything pointing at the user (apps,
  credentials, role assignments) stays stable.

The HTTP surface (OIDC callback, dashboard, admin API) is intentionally out
of scope here — this module is the durable substrate those routes call into.
"""

from __future__ import annotations

from gateway.compat import get_env

import json
import os
import secrets
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

_default_db = Path(__file__).resolve().parent.parent / "data" / "admin.db"
DB_PATH = Path(get_env("AZIZAM_ADMIN_DB")) if get_env("AZIZAM_ADMIN_DB") else _default_db

SCHEMA_VERSION = 4

SUBJECT_KINDS = ("user", "service_account", "app")
APP_KINDS = ("personal", "service")
IDENTITY_CLAIM_KINDS = ("oidc_group", "oidc_role")
RUNTIME_PROFILE_MAPPING_SOURCE_SPECS = (
    {"id": "oidc_group", "label": "OIDC group", "description": "Match a configured OIDC group.", "value_mode": "required", "priority": 20},
    {"id": "oidc_role", "label": "OIDC role", "description": "Match a configured OIDC role.", "value_mode": "required", "priority": 30},
    {"id": "email", "label": "Email", "description": "Match one normalized email address.", "value_mode": "required", "priority": 10},
    {"id": "domain", "label": "Domain", "description": "Match one normalized email domain.", "value_mode": "required", "priority": 40},
    {"id": "gateway_role", "label": "Gateway role", "description": "Match one gateway admin role.", "value_mode": "required", "priority": 50},
    {"id": "all_users", "label": "All signed-in users", "description": "Match every signed-in user.", "value_mode": "fixed", "fixed_value": "*", "priority": 100},
)
RUNTIME_PROFILE_MAPPING_SOURCE_KINDS = tuple(spec["id"] for spec in RUNTIME_PROFILE_MAPPING_SOURCE_SPECS)
_RUNTIME_PROFILE_MAPPING_SOURCE_SPEC_BY_ID = {spec["id"]: spec for spec in RUNTIME_PROFILE_MAPPING_SOURCE_SPECS}

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

-- Fixed manual access preserved during the v4 authorization migration. Old
-- claim aliases cannot safely reconstruct an immutable group path.
CREATE TABLE IF NOT EXISTS legacy_identity_profile_grants (
  user_id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS service_accounts (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  created_at INTEGER NOT NULL,
  disabled_at INTEGER
);

CREATE TABLE IF NOT EXISTS apps (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  owner_user_id TEXT,
  owner_service_account_id TEXT,
  default_profile_id TEXT,
  created_at INTEGER NOT NULL,
  disabled_at INTEGER,
  FOREIGN KEY(owner_user_id) REFERENCES users(id),
  FOREIGN KEY(owner_service_account_id) REFERENCES service_accounts(id),
  FOREIGN KEY(default_profile_id) REFERENCES profiles(id)
);
CREATE INDEX IF NOT EXISTS apps_owner_user ON apps(owner_user_id);
CREATE INDEX IF NOT EXISTS apps_owner_service ON apps(owner_service_account_id);

CREATE TABLE IF NOT EXISTS profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  profile_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
  id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  app_id TEXT,
  profile_id TEXT,
  hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  display_name TEXT,
  scopes_json TEXT,
  created_at INTEGER NOT NULL,
  expires_at INTEGER,
  revoked_at INTEGER,
  last_used_at INTEGER,
  FOREIGN KEY(app_id) REFERENCES apps(id),
  FOREIGN KEY(profile_id) REFERENCES profiles(id)
);
CREATE INDEX IF NOT EXISTS credentials_subject ON credentials(subject_kind, subject_id);
CREATE INDEX IF NOT EXISTS credentials_app ON credentials(app_id);
CREATE INDEX IF NOT EXISTS credentials_active ON credentials(revoked_at);

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

CREATE TABLE IF NOT EXISTS runtime_profile_mappings (
  id TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  source_value TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  priority INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  created_by TEXT,
  disabled_at INTEGER,
  UNIQUE(source_kind, source_value, profile_id)
);
CREATE INDEX IF NOT EXISTS runtime_profile_mappings_lookup
  ON runtime_profile_mappings(source_kind, source_value, disabled_at);

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


# ---------------------------------------------------------------------------
# connection plumbing
# ---------------------------------------------------------------------------


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")


def _exec_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    role_columns = {column[1] for column in conn.execute("PRAGMA table_info(admin_role_assignments)")}
    migrate_legacy_claims = (row is not None and int(row[0]) < 4) or "provenance" not in role_columns
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
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    # Grants made before provenance tracking stay manual. This migration is
    # intentionally idempotent even for databases with an inconsistent version.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(admin_role_assignments)")}
    if "provenance" not in columns:
        conn.execute(
            "ALTER TABLE admin_role_assignments "
            "ADD COLUMN provenance TEXT NOT NULL DEFAULT 'manual'"
        )
    if migrate_legacy_claims:
        _migrate_legacy_identity_profiles(conn)
    # Seed existing documents once. INSERT OR IGNORE preserves every revision
    # across restarts, including the registry's existing CAS generation.
    conn.execute(
        "INSERT OR IGNORE INTO operator_settings_generations("
        "setting_key, operator_settings_generation) "
        "SELECT key, 1 FROM operator_settings"
    )
    conn.commit()


def _migrate_legacy_identity_profiles(conn: sqlite3.Connection) -> None:
    """Freeze existing claim-derived access, then discard unrecoverable aliases.

    The v3 matcher folded case and path separators. Preserve only its current
    winning profile, never its ability to authorize future profile mappings.
    This runs once inside the schema migration's write transaction.
    """
    mappings = conn.execute(
        "SELECT source_kind,source_value,profile_id FROM runtime_profile_mappings "
        "WHERE disabled_at IS NULL ORDER BY priority ASC,created_at ASC"
    ).fetchall()
    has_account_access = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_access'"
    ).fetchone() is not None
    users = conn.execute(
        "SELECT id,email FROM users WHERE EXISTS "
        "(SELECT 1 FROM user_identity_claims WHERE user_id=users.id)"
    ).fetchall()
    for user_id, email in users:
        # A manual People access plan already took precedence over mappings.
        if has_account_access and conn.execute(
            "SELECT 1 FROM account_access WHERE user_id=? "
            "AND profile_id IS NOT NULL AND removed_at IS NULL", (user_id,)
        ).fetchone():
            continue
        email_value = _normalize_access_value(email)
        _, _, domain = email_value.partition("@")
        candidates = {("all_users", "*")}
        if email_value:
            candidates.add(("email", email_value))
        if domain:
            candidates.add(("domain", domain))
        roles = {row[0] for row in conn.execute(
            "SELECT role FROM admin_role_assignments WHERE subject_kind='user' "
            "AND subject_id=? AND revoked_at IS NULL", (user_id,)
        )}
        role = next((role for role in ("owner", "superadmin", "admin") if role in roles), "none")
        candidates.add(("gateway_role", role))
        for kind, value in conn.execute(
            "SELECT claim_kind,claim_value FROM user_identity_claims WHERE user_id=?", (user_id,)
        ):
            normalized = _normalize_access_value(value)
            if normalized:
                candidates.add((kind, normalized))
        for kind, value, profile_id in mappings:
            if (kind, value) not in candidates:
                continue
            if kind in IDENTITY_CLAIM_KINDS:
                # Match the gateway's profile-reference semantics. Default
                # policy sentinels remain fixed defaults rather than row IDs.
                if profile_id not in {"default", "default-local"} and not conn.execute(
                    "SELECT 1 FROM profiles WHERE id=?", (profile_id,)
                ).fetchone():
                    for stored_id, name, document in conn.execute(
                        "SELECT id,name,profile_json FROM profiles ORDER BY created_at ASC"
                    ):
                        try:
                            policy = json.loads(document)
                        except (TypeError, ValueError):
                            policy = {}
                        policy_id = policy.get("id") if isinstance(policy, Mapping) else None
                        if profile_id in {stored_id, name, policy_id}:
                            profile_id = stored_id
                            break
                conn.execute(
                    "INSERT OR IGNORE INTO legacy_identity_profile_grants(user_id,profile_id) "
                    "VALUES (?,?)", (user_id, profile_id),
                )
            break
    conn.execute("DELETE FROM user_identity_claims")


def secure_state_path(path: Path) -> None:
    """Create private SQLite state and tighten existing database sidecars.

    SQLite inherits database permissions for new WAL/SHM files. Precreating the
    database avoids a process-wide umask change in this multithreaded gateway.
    """
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
    """Create the data dir, run the schema, integrity-check, replace if broken.

    Idempotent: running it twice on the same file is a no-op.
    """
    secure_state_path(path)

    if path.exists():
        try:
            test = sqlite3.connect(str(path), timeout=5.0)
            try:
                _apply_pragmas(test)
                row = test.execute("PRAGMA integrity_check").fetchone()
                if not row or row[0] != "ok":
                    raise sqlite3.DatabaseError(
                        f"integrity_check returned: {row[0] if row else 'empty'}"
                    )
            finally:
                test.close()
        except sqlite3.DatabaseError as e:
            broken = path.with_name(f"{path.name}.broken-{int(time.time())}")
            print(
                f"[admin_store] DB corrupt ({e}); moving aside to {broken}",
                flush=True,
            )
            try:
                shutil.move(str(path), str(broken))
                broken.chmod(0o600)
            except Exception as mv_err:  # noqa: BLE001
                print(
                    f"[admin_store] failed to move broken db: {mv_err}",
                    flush=True,
                )
                try:
                    path.unlink()
                except Exception:
                    pass

    secure_state_path(path)
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        _apply_pragmas(conn)
        _exec_schema(conn)
    finally:
        conn.close()


@contextmanager
def _conn(path: Path = DB_PATH):
    secure_state_path(path)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()


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
        else:
            user_id = row["id"]
            conn.execute(
                "UPDATE users SET email = COALESCE(?, email), "
                "display_name = COALESCE(?, display_name), last_login_at = ? "
                "WHERE id = ?",
                (email, display_name, ts, user_id),
            )
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
        else:
            user_id = row["id"]
            conn.execute(
                "UPDATE users SET email = COALESCE(?, email), "
                "display_name = COALESCE(?, display_name), last_login_at = ? "
                "WHERE id = ?",
                (email, display_name, ts, user_id),
            )

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
# profiles
# ---------------------------------------------------------------------------


def create_profile(
    name: str,
    profile_doc: Mapping[str, Any],
    *,
    description: str | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Store a profile JSON document.

    The doc shape matches what :func:`gateway.profiles.load_profile` accepts,
    so the dashboard can hand it straight to the policy evaluator. We don't
    parse it here — keeping the durable fact (the JSON) separate from the
    policy interpretation lets the evaluator evolve without migrations.
    """
    if not name:
        raise ValueError("profile name is required")
    ts = _now() if now is None else int(now)
    prof_id = _gen_id("prof")
    payload = json.dumps(dict(profile_doc), ensure_ascii=False, sort_keys=True)
    try:
        with _conn(path) as conn:
            conn.execute(
                "INSERT INTO profiles(id, name, description, profile_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (prof_id, name, description, payload, ts),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM profiles WHERE id = ?", (prof_id,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"profile name {name!r} is already in use") from exc
    return _profile_row_to_dict(row)


def get_profile(
    profile_id: str, *, path: Path = DB_PATH
) -> dict[str, Any] | None:
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    return _profile_row_to_dict(row) if row else None


def update_profile(
    profile_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    profile_doc: Mapping[str, Any] | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    assignments: list[str] = []
    params: list[Any] = []
    if name is not None:
        if not name:
            raise ValueError("profile name is required")
        assignments.append("name = ?")
        params.append(name)
    if description is not None:
        assignments.append("description = ?")
        params.append(description)
    if profile_doc is not None:
        assignments.append("profile_json = ?")
        params.append(json.dumps(dict(profile_doc), ensure_ascii=False, sort_keys=True))
    if not assignments:
        return get_profile(profile_id, path=path)

    params.append(profile_id)
    try:
        with _conn(path) as conn:
            cur = conn.execute(
                f"UPDATE profiles SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            if cur.rowcount == 0:
                return None
            conn.commit()
            row = conn.execute(
                "SELECT * FROM profiles WHERE id = ?", (profile_id,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"profile name is already in use") from exc
    return _profile_row_to_dict(row) if row else None


def list_profiles(*, path: Path = DB_PATH) -> list[dict[str, Any]]:
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM profiles ORDER BY created_at ASC"
        ).fetchall()
    return [_profile_row_to_dict(r) for r in rows]


def delete_profile(
    profile_id: str, *, path: Path = DB_PATH
) -> dict[str, Any] | None:
    """Delete a profile row. Returns the deleted record or None if not found.

    Raises ValueError if any app or runtime-profile-mapping still references
    this profile, to prevent dangling foreign-key references.
    """
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            return None
        app_count = conn.execute(
            "SELECT COUNT(*) FROM apps WHERE default_profile_id = ?", (profile_id,)
        ).fetchone()[0]
        if app_count:
            raise ValueError(
                f"profile {profile_id!r} is assigned to {app_count} app(s); "
                "reassign or clear those apps before deleting"
            )
        mapping_count = conn.execute(
            "SELECT COUNT(*) FROM runtime_profile_mappings "
            "WHERE profile_id = ? AND disabled_at IS NULL",
            (profile_id,),
        ).fetchone()[0]
        if mapping_count:
            raise ValueError(
                f"profile {profile_id!r} is used by {mapping_count} active runtime "
                "mapping(s); disable those mappings before deleting"
            )
        from gateway.accounts import profile_reference_count
        if profile_reference_count(conn, profile_id):
            raise ValueError("access plan is assigned to people or pending invitations; "
                             "change their plan or revoke the invitations before deleting")
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()
    return _profile_row_to_dict(row)


def _profile_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys()}
    raw = data.pop("profile_json", None)
    if raw:
        try:
            data["profile"] = json.loads(raw)
        except json.JSONDecodeError:
            data["profile"] = None
    else:
        data["profile"] = None
    return data


# ---------------------------------------------------------------------------
# apps
# ---------------------------------------------------------------------------


def create_app(
    name: str,
    *,
    kind: str,
    owner_user_id: str | None = None,
    owner_service_account_id: str | None = None,
    default_profile_id: str | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    if kind not in APP_KINDS:
        raise ValueError(f"app kind must be one of {APP_KINDS}, got {kind!r}")
    if kind == "personal" and not owner_user_id:
        raise ValueError("personal apps require owner_user_id")
    if kind == "service" and not owner_service_account_id:
        raise ValueError("service apps require owner_service_account_id")
    if owner_user_id and owner_service_account_id:
        raise ValueError("an app cannot be owned by both a user and a service account")

    ts = _now() if now is None else int(now)
    app_id = _gen_id("app")
    with _conn(path) as conn:
        conn.execute(
            "INSERT INTO apps(id, kind, name, owner_user_id, "
            "owner_service_account_id, default_profile_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                app_id,
                kind,
                name,
                owner_user_id,
                owner_service_account_id,
                default_profile_id,
                ts,
            ),
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
    default_profile_id: str | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Return the user's personal app, creating one if missing.

    A user has at most one *personal* app — it's the home for their own
    data-plane keys ("the key in your editor"). Service apps are separate
    and created explicitly.
    """
    if not user_id:
        raise ValueError("user_id is required")
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM apps WHERE kind = 'personal' AND owner_user_id = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is not None and default_profile_id and not row["default_profile_id"]:
            conn.execute(
                "UPDATE apps SET default_profile_id = ? WHERE id = ?",
                (default_profile_id, row["id"]),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM apps WHERE id = ?", (row["id"],)
            ).fetchone()
    if row is not None:
        return _row_to_dict(row)  # type: ignore[return-value]
    return create_app(
        name or f"personal-{user_id}",
        kind="personal",
        owner_user_id=user_id,
        default_profile_id=default_profile_id,
        now=now,
        path=path,
    )


def get_app(app_id: str, *, path: Path = DB_PATH) -> dict[str, Any] | None:
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM apps WHERE id = ?", (app_id,)
        ).fetchone()
    return _row_to_dict(row)


def update_app(
    app_id: str,
    *,
    name: str | None = None,
    default_profile_id: str | None = None,
    clear_default_profile: bool = False,
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    assignments: list[str] = []
    params: list[Any] = []
    if name is not None:
        if not name:
            raise ValueError("app name is required")
        assignments.append("name = ?")
        params.append(name)
    if clear_default_profile or default_profile_id is not None:
        assignments.append("default_profile_id = ?")
        params.append(None if clear_default_profile else default_profile_id)
    if not assignments:
        return get_app(app_id, path=path)

    params.append(app_id)
    with _conn(path) as conn:
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
    profile_id: str | None = None,
    display_name: str | None = None,
    scopes: Iterable[str] | None = None,
    expires_at: int | None = None,
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
        conn.execute(
            "INSERT INTO credentials(id, subject_kind, subject_id, app_id, "
            "profile_id, hash, prefix, display_name, scopes_json, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cred_id,
                subject_kind,
                subject_id,
                app_id,
                profile_id,
                hash,
                prefix,
                display_name,
                scopes_json,
                ts,
                expires_at,
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
) -> bool:
    """Record that a credential was just used (debounced).

    Updates ``last_used_at`` only when it is null or older than ``min_interval``
    seconds, so a busy key doesn't issue a write on every request. Returns True
    when a row was actually updated. Best-effort: callers on the auth hot path
    should not fail a request if this raises.
    """
    ts = _now() if now is None else int(now)
    cutoff = ts - max(0, int(min_interval))
    with _conn(path) as conn:
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
    hash: str, *, path: Path = DB_PATH
) -> dict[str, Any] | None:
    """Look up a credential by stored hash. Caller hashes the secret first."""
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM credentials WHERE hash = ?", (hash,)
        ).fetchone()
    return _credential_row_to_dict(row) if row else None


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


# ---------------------------------------------------------------------------
# runtime profile mappings
# ---------------------------------------------------------------------------


def _normalize_runtime_mapping(
    source_kind: str,
    source_value: str | None,
) -> tuple[str, str]:
    kind = str(source_kind or "").strip().lower()
    spec = _RUNTIME_PROFILE_MAPPING_SOURCE_SPEC_BY_ID.get(kind)
    if spec is None:
        raise ValueError(
            "source_kind must be one of "
            f"{RUNTIME_PROFILE_MAPPING_SOURCE_KINDS}"
        )
    if spec["value_mode"] == "fixed":
        return kind, str(spec["fixed_value"])
    value = (
        _normalize_identity_value(kind, source_value)
        if kind in IDENTITY_CLAIM_KINDS else _normalize_access_value(source_value)
    )
    if not value:
        raise ValueError("source_value is required")
    return kind, value


def _default_runtime_mapping_priority(source_kind: str) -> int:
    return int(_RUNTIME_PROFILE_MAPPING_SOURCE_SPEC_BY_ID[source_kind]["priority"])


def create_runtime_profile_mapping(
    *,
    source_kind: str,
    source_value: str | None,
    profile_id: str,
    priority: int | None = None,
    created_by: str | None = None,
    now: int | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    kind, value = _normalize_runtime_mapping(source_kind, source_value)
    if not profile_id:
        raise ValueError("profile_id is required")
    ts = _now() if now is None else int(now)
    mapping_id = _gen_id("map")
    prio = (
        _default_runtime_mapping_priority(kind)
        if priority is None
        else int(priority)
    )
    with _conn(path) as conn:
        existing = conn.execute(
            "SELECT * FROM runtime_profile_mappings "
            "WHERE source_kind = ? AND source_value = ? AND profile_id = ?",
            (kind, value, profile_id),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE runtime_profile_mappings SET disabled_at = NULL, "
                "priority = ?, created_at = ?, created_by = ? WHERE id = ?",
                (prio, ts, created_by, existing["id"]),
            )
            mapping_id = existing["id"]
        else:
            conn.execute(
                "INSERT INTO runtime_profile_mappings"
                "(id, source_kind, source_value, profile_id, priority, "
                "created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (mapping_id, kind, value, profile_id, prio, ts, created_by),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM runtime_profile_mappings WHERE id = ?",
            (mapping_id,),
        ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def disable_runtime_profile_mapping(
    mapping_id: str,
    *,
    now: int | None = None,
    path: Path = DB_PATH,
) -> bool:
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        cur = conn.execute(
            "UPDATE runtime_profile_mappings SET disabled_at = ? "
            "WHERE id = ? AND disabled_at IS NULL",
            (ts, mapping_id),
        )
        conn.commit()
        return cur.rowcount > 0


def list_runtime_profile_mappings(
    *,
    include_disabled: bool = False,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    where = "" if include_disabled else "WHERE disabled_at IS NULL"
    with _conn(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM runtime_profile_mappings {where} "
            "ORDER BY priority ASC, created_at ASC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def replace_default_runtime_profile_mapping(
    *, profile_id: str | None, actor: Any, path: Path = DB_PATH,
) -> None:
    """Atomically replace the universal grant, preserving specific identity mappings."""
    ts = _now()
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if profile_id not in {None, "default", "default-local"} and conn.execute(
            "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone() is None:
            raise ValueError("access plan not found")
        conn.execute(
            "UPDATE runtime_profile_mappings SET disabled_at = ? WHERE source_kind = 'all_users' AND disabled_at IS NULL",
            (ts,),
        )
        if profile_id is not None:
            conn.execute(
                "INSERT INTO runtime_profile_mappings "
                "(id, source_kind, source_value, profile_id, priority, created_at, created_by) "
                "VALUES (?, 'all_users', '*', ?, 100, ?, ?) "
                "ON CONFLICT(source_kind, source_value, profile_id) DO UPDATE SET "
                "disabled_at = NULL, priority = 100, created_at = excluded.created_at, created_by = excluded.created_by",
                (_gen_id("map"), profile_id, ts, actor.subject),
            )
        _record_audit_in_transaction(
            conn, "operator.default_access_plan.update", actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id, actor_label=actor.actor_label,
            target_kind="runtime_profile_mapping", target_id="all_users",
            metadata={"profile_id": profile_id}, now=ts,
        )
        conn.commit()


def resolve_runtime_profile_mapping(
    *,
    email: str,
    gateway_role: str,
    oidc_groups: Iterable[str] = (),
    oidc_roles: Iterable[str] = (),
    path: Path = DB_PATH,
) -> str | None:
    email_value = _normalize_access_value(email)
    _, _, domain = email_value.partition("@")
    candidates: set[tuple[str, str]] = {("all_users", "*")}
    if email_value:
        candidates.add(("email", email_value))
    if domain:
        candidates.add(("domain", domain))
    role_value = _normalize_access_value(gateway_role or "none")
    if role_value:
        candidates.add(("gateway_role", role_value))
    for group in oidc_groups:
        value = _normalize_identity_value("oidc_group", group)
        if value:
            candidates.add(("oidc_group", value))
    for role in oidc_roles:
        value = _normalize_identity_value("oidc_role", role)
        if value:
            candidates.add(("oidc_role", value))

    for row in list_runtime_profile_mappings(path=path):
        kind = str(row.get("source_kind") or "")
        value = str(row.get("source_value") or "")
        # Old short-name mappings remain exact root paths after migration.
        if kind in IDENTITY_CLAIM_KINDS:
            value = _normalize_identity_value(kind, value)
        pair = (kind, value)
        if pair in candidates:
            return str(row.get("profile_id") or "") or None
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
    value = json.loads(row["value_json"])
    generation = row["operator_settings_generation"]
    return {
        "key": "service_registry", "value": value, "updated_at": row["updated_at"],
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
    "RUNTIME_PROFILE_MAPPING_SOURCE_KINDS",
    "RUNTIME_PROFILE_MAPPING_SOURCE_SPECS",
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
    "create_profile",
    "get_profile",
    "list_profiles",
    "create_app",
    "ensure_personal_app_for_user",
    "get_app",
    "disable_app",
    "list_apps",
    "list_apps_for_user",
    "create_credential_hash",
    "revoke_credential",
    "get_credential",
    "find_credential_by_hash",
    "list_credentials",
    "list_active_credentials",
    "assign_admin_role",
    "reconcile_oidc_admin_roles",
    "revoke_admin_role",
    "list_admin_roles",
    "create_runtime_profile_mapping",
    "disable_runtime_profile_mapping",
    "list_runtime_profile_mappings",
    "resolve_runtime_profile_mapping",
    "put_operator_setting",
    "put_operator_setting_with_audit",
    "get_service_registry_with_generation",
    "write_service_registry",
    "write_service_registry_with_audit",
    "write_service_registry_in_transaction",
    "OperatorSettingsGenerationConflict",
    "get_operator_setting",
    "list_operator_settings",
    "record_audit",
    "list_audit",
    "get_audit_event",
]
