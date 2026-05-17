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

import json
import secrets
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "admin.db"

SCHEMA_VERSION = 1

SUBJECT_KINDS = ("user", "service_account", "app")
APP_KINDS = ("personal", "service")

# Credentials attached to an app are data-plane (a runtime client uses them
# to call /v1/chat/completions etc.). Credentials attached to a user or
# service account are control-plane (used to call admin APIs).
_DATA_PLANE_SUBJECTS = frozenset({"app"})
_CONTROL_PLANE_SUBJECTS = frozenset({"user", "service_account"})


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
  revoked_at INTEGER,
  UNIQUE(role, subject_kind, subject_id, scope_app_id),
  FOREIGN KEY(scope_app_id) REFERENCES apps(id)
);
CREATE INDEX IF NOT EXISTS admin_role_subject
  ON admin_role_assignments(subject_kind, subject_id);

CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  actor_kind TEXT,
  actor_id TEXT,
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
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
        )
    elif int(row[0]) != SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            f"admin_store schema version mismatch: db={row[0]} code={SCHEMA_VERSION}"
        )
    conn.commit()


def init_db(path: Path = DB_PATH) -> None:
    """Create the data dir, run the schema, integrity-check, replace if broken.

    Idempotent: running it twice on the same file is a no-op.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

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
            except Exception as mv_err:  # noqa: BLE001
                print(
                    f"[admin_store] failed to move broken db: {mv_err}",
                    flush=True,
                )
                try:
                    path.unlink()
                except Exception:
                    pass

    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        _apply_pragmas(conn)
        _exec_schema(conn)
    finally:
        conn.close()


@contextmanager
def _conn(path: Path = DB_PATH):
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
    return _profile_row_to_dict(row)


def get_profile(
    profile_id: str, *, path: Path = DB_PATH
) -> dict[str, Any] | None:
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    return _profile_row_to_dict(row) if row else None


def list_profiles(*, path: Path = DB_PATH) -> list[dict[str, Any]]:
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM profiles ORDER BY created_at ASC"
        ).fetchall()
    return [_profile_row_to_dict(r) for r in rows]


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
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT id FROM admin_role_assignments "
            "WHERE role = ? AND subject_kind = ? AND subject_id = ? "
            "AND COALESCE(scope_app_id, '') = COALESCE(?, '')",
            (role, subject_kind, subject_id, scope_app_id),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE admin_role_assignments SET revoked_at = NULL, "
                "granted_at = ?, granted_by = ? WHERE id = ?",
                (ts, granted_by, row["id"]),
            )
            assignment_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO admin_role_assignments(role, subject_kind, "
                "subject_id, scope_app_id, granted_at, granted_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (role, subject_kind, subject_id, scope_app_id, ts, granted_by),
            )
            assignment_id = cur.lastrowid
        conn.commit()
        out = conn.execute(
            "SELECT * FROM admin_role_assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
    return _row_to_dict(out)  # type: ignore[return-value]


def revoke_admin_role(
    assignment_id: int, *, now: int | None = None, path: Path = DB_PATH
) -> bool:
    ts = _now() if now is None else int(now)
    with _conn(path) as conn:
        cur = conn.execute(
            "UPDATE admin_role_assignments SET revoked_at = ? "
            "WHERE id = ? AND revoked_at IS NULL",
            (ts, assignment_id),
        )
        conn.commit()
        return cur.rowcount > 0


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
# audit
# ---------------------------------------------------------------------------


def record_audit(
    action: str,
    *,
    actor_kind: str | None = None,
    actor_id: str | None = None,
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
    aud_id = _gen_id("aud")
    payload = (
        json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True)
        if metadata
        else None
    )
    with _conn(path) as conn:
        conn.execute(
            "INSERT INTO audit_events(id, created_at, actor_kind, actor_id, "
            "action, target_kind, target_id, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                aud_id,
                ts,
                actor_kind,
                actor_id,
                action,
                target_kind,
                target_id,
                payload,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM audit_events WHERE id = ?", (aud_id,)
        ).fetchone()
    return _audit_row_to_dict(row)


def list_audit(
    *,
    target_kind: str | None = None,
    target_id: str | None = None,
    limit: int = 100,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if target_kind is not None:
        clauses.append("target_kind = ?")
        params.append(target_kind)
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(target_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with _conn(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM audit_events{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [_audit_row_to_dict(r) for r in rows]


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


__all__ = [
    "DB_PATH",
    "SCHEMA_VERSION",
    "SUBJECT_KINDS",
    "APP_KINDS",
    "init_db",
    "schema_version",
    "upsert_oidc_user",
    "get_user",
    "list_users",
    "create_service_account",
    "get_service_account",
    "create_profile",
    "get_profile",
    "list_profiles",
    "create_app",
    "ensure_personal_app_for_user",
    "get_app",
    "list_apps_for_user",
    "create_credential_hash",
    "revoke_credential",
    "get_credential",
    "find_credential_by_hash",
    "list_active_credentials",
    "assign_admin_role",
    "revoke_admin_role",
    "list_admin_roles",
    "record_audit",
    "list_audit",
]
