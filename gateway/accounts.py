"""Built-in identities, single-use links and immediate access revocation.

Account product state shares admin.db with Apps and Profiles. Link consumption,
account creation, default App/key creation and auditing commit together. Password
work runs outside the write transaction; persistent throttles limit that work.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from gateway import admin_store
from gateway.admin_runtime import generate_app_api_key, is_human_user_record
from gateway.local_owner import verify_password as verify_legacy_password
from gateway.runtime_auth import hash_api_key

_SCRYPT_N = 2**17
_PASSWORD_WORK = threading.BoundedSemaphore(2)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_access (
 user_id TEXT PRIMARY KEY REFERENCES users(id), profile_id TEXT REFERENCES profiles(id) ON DELETE SET NULL,
 disabled_at INTEGER, removed_at INTEGER, require_registered_session INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS account_sessions (
 session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS account_links (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 profile_id TEXT REFERENCES profiles(id) ON DELETE SET NULL, user_id TEXT REFERENCES users(id), email TEXT,
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


class AccountError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@contextmanager
def connection(path: Path):
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            yield conn
    finally:
        conn.close()


def initialize(path: Path) -> None:
    with connection(path) as conn:
        conn.executescript(_SCHEMA)


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise AccountError("invalid_email", "Enter a valid email address.")
    return email


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise AccountError("invalid_password", "Use a password between 12 and 1,024 characters.")
    salt = secrets.token_bytes(16)
    with _PASSWORD_WORK:
        derived = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=8, p=1,
                                 maxmem=160 * 1024 * 1024, dklen=32)
    return f"scrypt$v1${salt.hex()}${derived.hex()}"


def verify_password(password: str, verifier: str) -> bool:
    if not isinstance(password, str) or len(password) > 1024:
        return False
    if verifier.startswith("pbkdf2_sha256$"):
        with _PASSWORD_WORK:
            return verify_legacy_password(password, verifier)
    try:
        scheme, version, salt_hex, digest_hex = verifier.split("$")
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        if (scheme, version) != ("scrypt", "v1") or len(salt) != 16 or len(expected) != 32:
            return False
    except (ValueError, AttributeError):
        return False
    with _PASSWORD_WORK:
        derived = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=8, p=1,
                                 maxmem=160 * 1024 * 1024, dklen=32)
    return hmac.compare_digest(derived, expected)


# An unknown account performs exactly the same scrypt operation as a known one.
_DUMMY_VERIFIER = "scrypt$v1$" + "00" * 16 + "$" + "00" * 32


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _audit(conn, action: str, actor: str, target: str, now: int, metadata=None):
    conn.execute("INSERT INTO audit_events(id,created_at,actor_kind,actor_id,action,"
                 "target_kind,target_id,metadata_json) VALUES(?,?,?,?,?,?,?,?)",
                 (_id("evt"), now, "user", actor, action, "user" if action.startswith("people.") else "account_link",
                  target, json.dumps(metadata or {})))


def _valid_profile(conn, profile_id: str):
    if not conn.execute("SELECT id FROM profiles WHERE id=?", (profile_id,)).fetchone():
        raise AccountError("profile_not_found", "Access plan not found.", 404)


def access(user_id: str, *, path: Path) -> dict[str, Any] | None:
    with connection(path) as conn:
        row = conn.execute("SELECT * FROM account_access WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def profile_for_user(user_id: str, *, path: Path) -> str | None:
    with connection(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='account_access'").fetchone():
            return None
        row = conn.execute("SELECT profile_id FROM account_access WHERE user_id=? AND removed_at IS NULL", (user_id,)).fetchone()
        return row[0] if row else None


def session_allowed(user_id: str, session_id: str, *, path: Path) -> bool:
    with connection(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='account_access'").fetchone():
            return True
        row = conn.execute("SELECT * FROM account_access WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return True  # Existing local-owner and OIDC sessions stay compatible.
        if row["disabled_at"] is not None or row["removed_at"] is not None:
            return False
        if not row["require_registered_session"]:
            return True
        return conn.execute("SELECT 1 FROM account_sessions WHERE user_id=? AND session_id=? AND expires_at>?",
                            (user_id, session_id, int(time.time()))).fetchone() is not None


def register_session(user_id: str, session_id: str, expires_at: int, *, path: Path):
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute("SELECT * FROM account_access WHERE user_id=?", (user_id,)).fetchone()
        if state and (state["disabled_at"] is not None or state["removed_at"] is not None):
            raise AccountError("account_disabled", "This account is disabled.", 403)
        conn.execute("DELETE FROM account_sessions WHERE expires_at<=?", (int(time.time()),))
        conn.execute("INSERT INTO account_sessions VALUES(?,?,?)", (session_id, user_id, expires_at))


def throttle(ip: str, email: str, *, path: Path, now: int | None = None):
    ts = int(time.time()) if now is None else now
    exceeded = False
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM account_login_limits WHERE started_at<=?", (ts - 900,))
        for label, value, maximum in (("ip", ip, 60), ("email", email.strip().lower(), 10)):
            bucket = hashlib.sha256(f"{label}:{value}".encode()).hexdigest()
            conn.execute("INSERT INTO account_login_limits VALUES(?,?,1) ON CONFLICT(bucket) "
                         "DO UPDATE SET attempts=attempts+1", (bucket, ts))
            attempts = conn.execute("SELECT attempts FROM account_login_limits WHERE bucket=?", (bucket,)).fetchone()[0]
            exceeded |= attempts > maximum
    if exceeded:
        raise AccountError("login_rate_limited", "Too many attempts. Try again in 15 minutes.", 429)


def login(email: str, password: str, ip: str, *, path: Path) -> dict:
    throttle(ip, email, path=path)
    email = email.strip().lower()
    with connection(path) as conn:
        row = conn.execute("SELECT u.*,p.verifier FROM users u JOIN local_password_credentials p "
                           "ON p.user_id=u.id WHERE LOWER(u.email)=? AND u.issuer='local'", (email,)).fetchone()
    valid = verify_password(password, row["verifier"] if row else _DUMMY_VERIFIER)
    state = access(row["id"], path=path) if row else None
    if not valid or not row or (state and (state["disabled_at"] is not None or state["removed_at"] is not None)):
        raise AccountError("invalid_login", "Email or password is incorrect.", 401)
    ts = int(time.time())
    with connection(path) as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (ts, row["id"]))
        _audit(conn, "account.login", row["id"], row["id"], ts)
    return {k: v for k, v in dict(row).items() if k != "verifier"}


def _public_link(row) -> dict:
    return {key: value for key, value in dict(row).items() if key != "token_hash"}


def create_link(kind: str, actor: str, *, path: Path, profile_id: str | None = None,
                user_id: str | None = None, email: str | None = None, expires_in: int = 604800, token_factory: Callable[[str], str] | None = None) -> tuple[dict, str]:
    if kind not in {"invite", "reset"}:
        raise AccountError("invalid_link", "Invalid link type.")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or not 60 <= expires_in <= 30 * 86400:
        raise AccountError("invalid_expiry", "Link expiry must be between one minute and 30 days.")
    email = normalize_email(email) if email else None
    link_id, ts = _id("link"), int(time.time())
    token = token_factory(link_id) if token_factory else secrets.token_urlsafe(32)
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if kind == "invite":
            _valid_profile(conn, profile_id)
        else:
            row = conn.execute("SELECT u.id FROM users u JOIN local_password_credentials p ON p.user_id=u.id "
                               "WHERE u.id=?", (user_id,)).fetchone()
            if not row:
                raise AccountError("account_not_found", "Built-in account not found.", 404)
            state = conn.execute("SELECT * FROM account_access WHERE user_id=?", (user_id,)).fetchone()
            if state and (state["disabled_at"] is not None or state["removed_at"] is not None):
                raise AccountError("account_disabled", "Enable this account before resetting its password.", 409)
            conn.execute("UPDATE account_links SET revoked_at=? WHERE user_id=? AND kind='reset' AND used_at IS NULL", (ts, user_id))
        conn.execute("INSERT INTO account_links VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)",
                     (link_id, kind, hashlib.sha256(token.encode()).hexdigest(), profile_id, user_id, email, actor, ts, ts + expires_in,
                      ))
        _audit(conn, f"account.{kind}.create", actor, link_id, ts)
        row = conn.execute("SELECT * FROM account_links WHERE id=?", (link_id,)).fetchone()
        return _public_link(row), token


def _usable_link(conn, token: str, kind: str, now: int):
    if not isinstance(token, str) or not 20 <= len(token) <= 128:
        raise AccountError("link_unavailable", "This link has expired or is no longer available.", 404)
    row = conn.execute("SELECT * FROM account_links WHERE token_hash=? AND kind=?",
                       (hashlib.sha256(token.encode()).hexdigest(), kind)).fetchone()
    if not row or row["used_at"] is not None or row["revoked_at"] is not None or row["expires_at"] <= now:
        raise AccountError("link_unavailable", "This link has expired or is no longer available.", 404)
    return row


def inspect_invite(token: str, *, path: Path) -> dict:
    with connection(path) as conn:
        row = _usable_link(conn, token, "invite", int(time.time()))
        _valid_profile(conn, row["profile_id"])
        return _public_link(row)


def list_invites(*, path: Path) -> list[dict]:
    with connection(path) as conn:
        rows = conn.execute("SELECT l.*,p.name plan_name FROM account_links l LEFT JOIN profiles p "
                            "ON l.profile_id=p.id WHERE kind='invite' ORDER BY created_at DESC").fetchall()
        return [_public_link(row) for row in rows]


def revoke_invite(link_id: str, actor: str, *, path: Path):
    with connection(path) as conn:
        ts = int(time.time())
        cur = conn.execute("UPDATE account_links SET revoked_at=? WHERE id=? AND kind='invite' AND used_at IS NULL",
                           (ts, link_id))
        if not cur.rowcount:
            raise AccountError("invite_not_found", "Pending invite not found.", 404)
        _audit(conn, "account.invite.revoke", actor, link_id, ts)


def accept_invite(token: str, email: str, name: str, password: str | None, *, path: Path,
                  oidc_user_id: str | None = None) -> tuple[dict, dict, dict]:
    email = normalize_email(email)
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise AccountError("invalid_name", "Enter your name (up to 120 characters).")
    inspect_invite(token, path=path)
    verifier = hash_password(password) if oidc_user_id is None else None
    api_key, ts = generate_app_api_key(), int(time.time())
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        link = _usable_link(conn, token, "invite", ts)
        _valid_profile(conn, link["profile_id"])
        if link["email"] and not hmac.compare_digest(link["email"].encode(), email.encode()):
            raise AccountError("invite_email_mismatch", "Use the email address on this invitation.", 403)
        if oidc_user_id:
            user = conn.execute("SELECT * FROM users WHERE id=? AND issuer!='local'", (oidc_user_id,)).fetchone()
            if not user or (user["email"] or "").lower() != email:
                raise AccountError("identity_mismatch", "Sign in with the invited account.", 403)
            state = conn.execute("SELECT * FROM account_access WHERE user_id=?", (oidc_user_id,)).fetchone()
            if state:
                raise AccountError("account_exists", "This account already has an access plan.", 409)
            user_id = oidc_user_id
        else:
            if conn.execute("SELECT 1 FROM users WHERE LOWER(email)=?", (email,)).fetchone():
                raise AccountError("account_exists", "An account already uses this email. Sign in or ask the owner for help.", 409)
            user_id = _id("usr")
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (user_id, "local", email, email, name.strip(), ts, ts))
            conn.execute("INSERT INTO local_password_credentials VALUES(?,?,?,?)", (user_id, verifier, ts, ts))
        # OIDC may have provisioned Apps before invitation acceptance. The
        # invite plan must replace every explicit old App/key override.
        conn.execute("UPDATE apps SET default_profile_id=? WHERE owner_user_id=?", (link["profile_id"], user_id))
        conn.execute("UPDATE credentials SET profile_id=? WHERE app_id IN (SELECT id FROM apps WHERE owner_user_id=?)", (link["profile_id"], user_id))
        conn.execute("INSERT INTO account_access(user_id,profile_id) VALUES(?,?)", (user_id, link["profile_id"]))
        app_id, credential_id = _id("app"), _id("cred")
        conn.execute("INSERT INTO apps(id,kind,name,owner_user_id,default_profile_id,created_at) VALUES(?,?,?,?,?,?)",
                     (app_id, "personal", "My first app", user_id, link["profile_id"], ts))
        conn.execute("INSERT INTO credentials(id,subject_kind,subject_id,app_id,profile_id,hash,prefix,display_name,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (credential_id, "app", app_id, app_id, link["profile_id"], hash_api_key(api_key), api_key[:18], "First app key", ts))
        conn.execute("UPDATE account_links SET used_at=?,user_id=? WHERE id=?", (ts, user_id, link["id"]))
        _audit(conn, "account.invite.accept", user_id, link["id"], ts, {"profile_id": link["profile_id"], "app_id": app_id})
        user = dict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
        app = dict(conn.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone())
    return user, app, {"id": credential_id, "app_id": app_id, "api_key": api_key, "prefix": api_key[:18]}


def reset_password(token: str, password: str, *, path: Path):
    with connection(path) as conn:
        _usable_link(conn, token, "reset", int(time.time()))
    verifier, ts = hash_password(password), int(time.time())
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        link = _usable_link(conn, token, "reset", ts)
        user_id = link["user_id"]
        state = conn.execute("SELECT * FROM account_access WHERE user_id=?", (user_id,)).fetchone()
        if state and (state["disabled_at"] is not None or state["removed_at"] is not None):
            raise AccountError("account_disabled", "This account is disabled.", 403)
        conn.execute("UPDATE local_password_credentials SET verifier=?,updated_at=? WHERE user_id=?", (verifier, ts, user_id))
        conn.execute("INSERT INTO account_access(user_id,require_registered_session) VALUES(?,1) ON CONFLICT(user_id) "
                     "DO UPDATE SET require_registered_session=1", (user_id,))
        conn.execute("DELETE FROM account_sessions WHERE user_id=?", (user_id,))
        conn.execute("UPDATE account_links SET used_at=? WHERE id=?", (ts, link["id"]))
        _audit(conn, "account.password.reset", user_id, link["id"], ts)


def people(*, path: Path) -> list[dict]:
    with connection(path) as conn:
        rows = conn.execute("SELECT u.id,u.issuer,u.subject,u.display_name name,u.email,a.profile_id,p.name plan_name,"
                            "a.disabled_at,MAX(u.last_login_at,(SELECT COALESCE(MAX(c.last_used_at),0) FROM credentials c "
                            "JOIN apps app ON app.id=c.app_id WHERE app.owner_user_id=u.id)) last_active "
                            "FROM users u LEFT JOIN account_access a ON a.user_id=u.id LEFT JOIN profiles p ON p.id=a.profile_id "
                            "WHERE a.removed_at IS NULL ORDER BY u.created_at").fetchall()
        people = []
        for row in rows:
            if not is_human_user_record(dict(row)):
                continue
            roles = conn.execute("SELECT role FROM admin_role_assignments WHERE subject_kind='user' AND subject_id=? AND revoked_at IS NULL", (row["id"],)).fetchall()
            person = {key: value for key, value in dict(row).items() if key not in {"issuer", "subject"}}
            person.update(disabled=row["disabled_at"] is not None, requests_this_month=None,
                          builtin=conn.execute("SELECT 1 FROM local_password_credentials WHERE user_id=?", (row["id"],)).fetchone() is not None,
                          protected=any(role[0] in {"owner", "admin", "superadmin"} for role in roles),
                          role="owner" if any(role[0] == "owner" for role in roles) else "admin" if any(role[0] in {"admin", "superadmin"} for role in roles) else "member")
            people.append(person)
        return people


def change_person(user_id: str, actor: str, *, path: Path, profile_id: str | None = None,
                  disabled: bool | None = None, remove: bool = False):
    ts = int(time.time())
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone():
            raise AccountError("person_not_found", "Person not found.", 404)
        # Owners/admins are protected: People manages members, never break-glass access.
        if conn.execute("SELECT 1 FROM admin_role_assignments WHERE subject_kind='user' AND subject_id=? "
                        "AND revoked_at IS NULL AND role IN ('owner','admin','superadmin')", (user_id,)).fetchone():
            raise AccountError("protected_person", "Manage owner and administrator access through role settings.", 409)
        state = conn.execute("SELECT * FROM account_access WHERE user_id=?", (user_id,)).fetchone()
        if state and state["removed_at"] is not None:
            raise AccountError("person_not_found", "Person not found.", 404)
        conn.execute("INSERT OR IGNORE INTO account_access(user_id) VALUES(?)", (user_id,))
        if profile_id is not None:
            _valid_profile(conn, profile_id)
            conn.execute("UPDATE account_access SET profile_id=? WHERE user_id=?", (profile_id, user_id))
            conn.execute("UPDATE apps SET default_profile_id=? WHERE owner_user_id=?", (profile_id, user_id))
            conn.execute("UPDATE credentials SET profile_id=? WHERE app_id IN (SELECT id FROM apps WHERE owner_user_id=?)", (profile_id, user_id))
        if disabled is not None or remove:
            conn.execute("UPDATE account_access SET disabled_at=?,removed_at=?,require_registered_session=1 WHERE user_id=?",
                         (ts if disabled or remove else None, ts if remove else None, user_id))
            conn.execute("DELETE FROM account_sessions WHERE user_id=?", (user_id,))
            if disabled or remove:
                conn.execute("UPDATE apps SET disabled_at=? WHERE owner_user_id=?", (ts, user_id))
                conn.execute("UPDATE credentials SET revoked_at=? WHERE (app_id IN (SELECT id FROM apps WHERE owner_user_id=?) "
                             "OR (subject_kind='user' AND subject_id=?)) AND revoked_at IS NULL", (ts, user_id, user_id))
                conn.execute("UPDATE account_links SET revoked_at=? WHERE user_id=? AND used_at IS NULL", (ts, user_id))
                conn.execute("UPDATE admin_role_assignments SET revoked_at=? WHERE subject_kind='user' AND subject_id=?", (ts, user_id))
            else:
                conn.execute("UPDATE apps SET disabled_at=NULL WHERE owner_user_id=?", (user_id,))
            if remove:
                conn.execute("DELETE FROM local_password_credentials WHERE user_id=?", (user_id,))
        _audit(conn, "people.remove" if remove else "people.update", actor, user_id, ts,
               {"profile_id": profile_id, "disabled": disabled})


def record_oidc_verification(user_id: str, verified: bool, *, path: Path):
    initialize(path)
    with connection(path) as conn:
        conn.execute("INSERT INTO account_oidc_verification VALUES(?,?) ON CONFLICT(user_id) "
                     "DO UPDATE SET email_verified=excluded.email_verified", (user_id, int(verified)))


def oidc_email_verified(user_id: str, *, path: Path) -> bool:
    with connection(path) as conn:
        row = conn.execute("SELECT email_verified FROM account_oidc_verification WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row[0])


def invite_token(link_id: str, signing_key: bytes) -> str:
    """Recover an owner's copyable invite without storing its bearer token.

    The existing persistent signing secret is domain-separated from cookie
    signatures. Random link IDs ensure distinct tokens; the DB retains only
    their hashes. Rotation of that secret makes old copy operations unavailable.
    """
    if not signing_key or len(signing_key) < 32:
        raise AccountError("sessions_unavailable", "Browser sessions are unavailable.", 503)
    digest = hmac.new(signing_key, b"gateway.invite.v1:" + link_id.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def copy_invite(link_id: str, actor: str, signing_key: bytes, *, path: Path) -> str:
    token = invite_token(link_id, signing_key)
    ts = int(time.time())
    with connection(path) as conn:
        link = _usable_link(conn, token, "invite", ts)
        if link["id"] != link_id:
            raise AccountError("link_unavailable", "This link is no longer available.", 404)
        _audit(conn, "account.invite.copy", actor, link_id, ts)
    return token


def profile_reference_count(conn, profile_id: str) -> int:
    """Active product references that an owner must clear before deletion."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='account_access'").fetchone():
        return 0
    return conn.execute("SELECT COUNT(*) FROM account_access WHERE profile_id=? AND removed_at IS NULL", (profile_id,)).fetchone()[0] + conn.execute("SELECT COUNT(*) FROM account_links WHERE profile_id=? AND kind='invite' AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?", (profile_id, int(time.time()))).fetchone()[0]
