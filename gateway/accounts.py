"""Built-in identities, single-use links and immediate access revocation.

Account product state shares admin.db with Apps and access plans. Link consumption,
account creation, default App/key creation and auditing commit together. Password
work runs outside the write transaction; persistent throttles limit that work.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from gateway import admin_store, request_connections, sqlite_startup
from gateway.admin_runtime import generate_app_api_key, is_human_user_record
from gateway.local_owner import verify_password as verify_legacy_password
from gateway.runtime_auth import hash_api_key

_SCRYPT_N = 2**17
_PASSWORD_WORK = threading.BoundedSemaphore(2)
_SCHEMA = admin_store.ACCOUNT_SCHEMA


class AccountError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@contextmanager
def connection(path: Path):
    if request_connections.active():
        # Share the request's admin-store connection: this is the same database.
        with admin_store._conn(path) as conn, conn:
            yield conn
        return
    conn = sqlite_startup.connect(path, timeout=10)
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


def _audit(conn, action: str, actor: str, target: str, now: int, metadata=None,
           *, actor_kind: str = "user", actor_label: str | None = None,
           target_kind: str | None = None):
    admin_store._record_audit_in_transaction(
        conn, action, actor_kind=actor_kind, actor_id=actor, actor_label=actor_label,
        target_kind=target_kind or ("user" if action.startswith("people.") else "account_link"),
        target_id=target, metadata=metadata, now=now,
    )


def _valid_plan(conn, plan_id: str | None):
    if not plan_id or not conn.execute("SELECT id FROM access_plans WHERE id=?", (plan_id,)).fetchone():
        raise AccountError("plan_not_found", "Access plan not found.", 404)


def access(user_id: str, *, path: Path) -> dict[str, Any] | None:
    with connection(path) as conn:
        row = conn.execute("SELECT * FROM account_access WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def plan_for_user(user_id: str, *, path: Path) -> str | None:
    """The person's access plan id, or None (no plan: cannot generate)."""
    return admin_store.person_plan_id(user_id, path=path)


def ensure_access_row(conn, user_id: str) -> None:
    """Every person has an account_access row; a missing row denies (design §2.13)."""
    conn.execute("INSERT OR IGNORE INTO account_access(user_id) VALUES(?)", (user_id,))


def session_allowed(user_id: str, session_id: str, *, path: Path) -> bool:
    with connection(path) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='account_session_revocations'").fetchone():
            if conn.execute("SELECT 1 FROM account_session_revocations WHERE session_id=? AND expires_at>?",
                            (session_id, int(time.time()))).fetchone():
                return False
        row = conn.execute("SELECT * FROM account_access WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            # Service-account sessions have no access row. A person without one
            # still cannot generate: the runtime treats them as disabled.
            return True
        if row["disabled_at"] is not None or row["removed_at"] is not None:
            return False
        if not row["require_registered_session"]:
            return True
        return conn.execute("SELECT 1 FROM account_sessions WHERE user_id=? AND session_id=? AND expires_at>?",
                            (user_id, session_id, int(time.time()))).fetchone() is not None


def revoke_session(session_id: str, expires_at: int, *, path: Path) -> None:
    """Deny replay of signed cookies, including pre-registration sessions."""
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM account_session_revocations WHERE expires_at<=?", (int(time.time()),))
        conn.execute("INSERT INTO account_session_revocations VALUES(?,?) "
                     "ON CONFLICT(session_id) DO UPDATE SET expires_at=MAX(expires_at, excluded.expires_at)",
                     (session_id, expires_at))
        conn.execute("DELETE FROM account_sessions WHERE session_id=?", (session_id,))
        # Keys bound to this sign-in (the Try page's) end with it.
        conn.execute("UPDATE credentials SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
                     (int(time.time()), session_id))


def revoke_person_sessions(user_id: str, *, path: Path) -> None:
    """End every signed-in session of a person, including ones never registered.

    From now on only registered sessions count for this person, and none is
    left, so each cookie they hold stops working. Signing in again registers a
    new session. Keys bound to their sign-ins end with them.
    """
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_access_row(conn, user_id)
        conn.execute("UPDATE account_access SET require_registered_session=1 WHERE user_id=?", (user_id,))
        session_ids = [row[0] for row in conn.execute(
            "SELECT session_id FROM account_sessions WHERE user_id=?", (user_id,))]
        conn.execute("DELETE FROM account_sessions WHERE user_id=?", (user_id,))
        now = int(time.time())
        for session_id in session_ids:
            conn.execute("UPDATE credentials SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
                         (now, session_id))


def record_signin_refusal(user_id: str, *, path: Path) -> None:
    """Remember that this person's latest sign-in was refused (their keys pause)."""
    with connection(path) as conn:
        conn.execute("INSERT INTO account_signin_refusals VALUES(?,?) ON CONFLICT(user_id) "
                     "DO UPDATE SET refused_at=excluded.refused_at", (user_id, int(time.time())))


def clear_signin_refusal(user_id: str, *, path: Path) -> None:
    """A successful sign-in lifts the pause on the person's keys."""
    with connection(path) as conn:
        conn.execute("DELETE FROM account_signin_refusals WHERE user_id=?", (user_id,))


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


def throttle_oidc(ip: str, *, path: Path, now: int | None = None) -> None:
    initialize(path)
    ts = int(time.time()) if now is None else now
    bucket = "oidc:" + hashlib.sha256(ip.encode()).hexdigest()
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM account_login_limits WHERE bucket LIKE 'oidc:%' AND started_at<=?", (ts - 60,))
        conn.execute("INSERT INTO account_login_limits VALUES(?,?,1) ON CONFLICT(bucket) DO UPDATE SET attempts=attempts+1",
                     (bucket, ts))
        attempts = conn.execute("SELECT attempts FROM account_login_limits WHERE bucket=?", (bucket,)).fetchone()[0]
    if attempts > 20:
        raise AccountError("login_rate_limited", "Too many sign-in attempts. Try again in a minute.", 429)


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


def create_link(kind: str, actor: str, *, path: Path, plan_id: str | None = None,
                user_id: str | None = None, email: str | None = None, expires_in: int = 604800, token_factory: Callable[[str], str] | None = None,
                actor_kind: str = "user", actor_label: str | None = None) -> tuple[dict, str]:
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
            _valid_plan(conn, plan_id)
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
                     (link_id, kind, hashlib.sha256(token.encode()).hexdigest(), plan_id, user_id, email, actor, ts, ts + expires_in,
                      ))
        _audit(conn, f"account.{kind}.create", actor, link_id, ts, actor_kind=actor_kind, actor_label=actor_label)
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
        _valid_plan(conn, row["plan_id"])
        return _public_link(row)


def list_invites(*, path: Path) -> list[dict]:
    with connection(path) as conn:
        rows = conn.execute("SELECT l.*,p.name plan_name FROM account_links l LEFT JOIN access_plans p "
                            "ON l.plan_id=p.id WHERE kind='invite' ORDER BY created_at DESC").fetchall()
        return [_public_link(row) for row in rows]


def revoke_invite(link_id: str, actor: str, *, path: Path, actor_kind: str = "user", actor_label: str | None = None):
    with connection(path) as conn:
        ts = int(time.time())
        cur = conn.execute("UPDATE account_links SET revoked_at=? WHERE id=? AND kind='invite' AND used_at IS NULL",
                           (ts, link_id))
        if not cur.rowcount:
            raise AccountError("invite_not_found", "Pending invite not found.", 404)
        _audit(conn, "account.invite.revoke", actor, link_id, ts, actor_kind=actor_kind, actor_label=actor_label)


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
        _valid_plan(conn, link["plan_id"])
        if link["email"] and not hmac.compare_digest(link["email"].encode(), email.encode()):
            raise AccountError("invite_email_mismatch", "Use the email address on this invitation.", 403)
        if oidc_user_id:
            user = conn.execute("SELECT * FROM users WHERE id=? AND issuer!='local'", (oidc_user_id,)).fetchone()
            if not user or (user["email"] or "").lower() != email:
                raise AccountError("identity_mismatch", "Sign in with the invited account.", 403)
            state = conn.execute("SELECT * FROM account_access WHERE user_id=?", (oidc_user_id,)).fetchone()
            if state and (state["plan_id"] or state["removed_at"] is not None):
                raise AccountError("account_exists", "This account already has an access plan.", 409)
            user_id = oidc_user_id
        else:
            if conn.execute("SELECT 1 FROM users WHERE LOWER(email)=?", (email,)).fetchone():
                raise AccountError("account_exists", "An account already uses this email. Sign in or ask the owner for help.", 409)
            user_id = _id("usr")
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (user_id, "local", email, email, name.strip(), ts, ts))
            conn.execute("INSERT INTO local_password_credentials VALUES(?,?,?,?)", (user_id, verifier, ts, ts))
        # The invitation's plan was previewed when it was sent (design §2.12).
        conn.execute("INSERT INTO account_access(user_id,plan_id) VALUES(?,?) ON CONFLICT(user_id) "
                     "DO UPDATE SET plan_id=excluded.plan_id", (user_id, link["plan_id"]))
        app_id, credential_id = _id("app"), _id("cred")
        conn.execute("INSERT INTO apps(id,kind,name,owner_user_id,access_plan,created_at) VALUES(?,?,?,?,'person',?)",
                     (app_id, "personal", "My first app", user_id, ts))
        conn.execute("INSERT INTO credentials(id,subject_kind,subject_id,app_id,hash,prefix,display_name,created_at,created_by) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (credential_id, "app", app_id, app_id, hash_api_key(api_key), api_key[:18], "First app key", ts, user_id))
        conn.execute("UPDATE account_links SET used_at=?,user_id=? WHERE id=?", (ts, user_id, link["id"]))
        _audit(conn, "account.invite.accept", user_id, link["id"], ts, {"plan_id": link["plan_id"], "app_id": app_id, "credential_id": credential_id})
        _audit(conn, "credential.create", user_id, credential_id, ts,
               {"app_id": app_id, "prefix": api_key[:18], "source": "invite"}, target_kind="credential")
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
        rows = conn.execute("SELECT u.id,u.issuer,u.subject,u.display_name name,u.email,a.plan_id,p.name plan_name,"
                            "a.disabled_at,MAX(u.last_login_at,(SELECT COALESCE(MAX(c.last_used_at),0) FROM credentials c "
                            "JOIN apps app ON app.id=c.app_id WHERE app.owner_user_id=u.id)) last_active "
                            "FROM users u LEFT JOIN account_access a ON a.user_id=u.id "
                            "LEFT JOIN access_plans p ON p.id=a.plan_id "
                            "WHERE a.removed_at IS NULL ORDER BY u.created_at").fetchall()
        people = []
        for row in rows:
            if not is_human_user_record(dict(row)):
                continue
            roles = conn.execute("SELECT role,provenance,scope_app_id FROM admin_role_assignments WHERE subject_kind='user' AND subject_id=? AND revoked_at IS NULL", (row["id"],)).fetchall()
            person = {key: value for key, value in dict(row).items() if key not in {"issuer", "subject"}}
            person.update(disabled=row["disabled_at"] is not None, requests_this_month=None,
                          builtin=conn.execute("SELECT 1 FROM local_password_credentials WHERE user_id=?", (row["id"],)).fetchone() is not None,
                          protected=any(role[0] in {"owner", "admin", "superadmin"} for role in roles),
                          role="owner" if any(role[0] == "owner" for role in roles) else "admin" if any(role[0] in {"admin", "superadmin"} for role in roles) else "member")
            person["grants"] = [{"role": role["role"], "source": "oidc-claim" if role["provenance"] == "oidc" else "manual",
                                 "scope_app_id": role["scope_app_id"]} for role in roles]
            people.append(person)
        return people


_UNCHANGED = object()


def change_person(user_id: str, actor: str, *, path: Path, plan_id: Any = _UNCHANGED,
                  disabled: bool | None = None, remove: bool = False,
                  actor_kind: str = "user", actor_label: str | None = None):
    """Assign a person's plan (an id, or None for no plan), disable or remove them.

    A plan change reaches every App that is "Same as its person" and bounds
    every other App the person answers for (design §2.6).
    """
    ts = int(time.time())
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone():
            raise AccountError("person_not_found", "Person not found.", 404)
        # Owners/admins are protected: People manages members, never break-glass access.
        if (disabled is not None or remove) and conn.execute(
                "SELECT 1 FROM admin_role_assignments WHERE subject_kind='user' AND subject_id=? "
                "AND revoked_at IS NULL AND role IN ('owner','admin','superadmin')", (user_id,)).fetchone():
            raise AccountError("protected_person", "Manage owner and administrator access through role settings.", 409)
        state = conn.execute("SELECT * FROM account_access WHERE user_id=?", (user_id,)).fetchone()
        if state and state["removed_at"] is not None:
            raise AccountError("person_not_found", "Person not found.", 404)
        ensure_access_row(conn, user_id)
        if plan_id is not _UNCHANGED:
            if plan_id is not None:
                _valid_plan(conn, plan_id)
            conn.execute("UPDATE account_access SET plan_id=? WHERE user_id=?", (plan_id, user_id))
        if disabled is not None or remove:
            conn.execute("UPDATE account_access SET disabled_at=?,removed_at=?,require_registered_session=1 WHERE user_id=?",
                         (ts if disabled or remove else None, ts if remove else None, user_id))
            conn.execute("DELETE FROM account_sessions WHERE user_id=?", (user_id,))
            if disabled or remove:
                # Account suspension is independent of per-App disablement.
                # Removal is permanent and also disables the owned Apps.
                if remove:
                    conn.execute("UPDATE apps SET disabled_at=? WHERE owner_user_id=?", (ts, user_id))
                conn.execute("UPDATE credentials SET revoked_at=? WHERE (app_id IN (SELECT id FROM apps WHERE owner_user_id=?) "
                             "OR (subject_kind='user' AND subject_id=?)) AND revoked_at IS NULL", (ts, user_id, user_id))
                conn.execute("UPDATE account_links SET revoked_at=? WHERE user_id=? AND used_at IS NULL", (ts, user_id))
                conn.execute("UPDATE admin_role_assignments SET revoked_at=? WHERE subject_kind='user' AND subject_id=?", (ts, user_id))
            if remove:
                conn.execute("DELETE FROM local_password_credentials WHERE user_id=?", (user_id,))
        metadata: dict[str, Any] = {"disabled": disabled}
        if plan_id is not _UNCHANGED:
            metadata["plan_id"] = plan_id
        _audit(conn, "people.remove" if remove else "people.update", actor, user_id, ts,
               metadata, actor_kind=actor_kind, actor_label=actor_label)

    if remove:
        from gateway import prompt_memory
        prompt_memory.delete_person(user_id)


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


def copy_invite(link_id: str, actor: str, signing_key: bytes, *, path: Path,
                actor_kind: str = "user", actor_label: str | None = None) -> str:
    token = invite_token(link_id, signing_key)
    ts = int(time.time())
    with connection(path) as conn:
        link = _usable_link(conn, token, "invite", ts)
        if link["id"] != link_id:
            raise AccountError("link_unavailable", "This link is no longer available.", 404)
        _audit(conn, "account.invite.copy", actor, link_id, ts, actor_kind=actor_kind, actor_label=actor_label)
    return token
