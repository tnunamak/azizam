"""Stable media ownership, independent of the credential used to authenticate.

Credential hashes remain read aliases until old data is migrated. Revocation
removes authentication, not the principal's data or its historical aliases.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any


def principal_key(kind: str, subject_id: str | None) -> str | None:
    if kind not in {"app", "user", "service_account"} or not isinstance(subject_id, str) or not subject_id:
        return None
    return f"{kind}:{subject_id}"


def principal_key_for_credential(credential: Mapping[str, Any]) -> str | None:
    kind = credential.get("subject_kind")
    subject_id = credential.get("subject_id")
    if kind == "app":
        subject_id = credential.get("app_id") or subject_id
    return principal_key(kind, subject_id)


def _credentials(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        from gateway import admin_store
        path = admin_store.DB_PATH
    path = Path(path)
    if not path.exists():
        return []
    # No store initialization, schema changes, last-used updates or secret reads.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='credentials'").fetchone():
            return []
        return [dict(row) for row in conn.execute("SELECT hash, subject_kind, subject_id, app_id FROM credentials ORDER BY created_at, id")]


def stored_owner_key(owner: str | None, *, app_id: str | None = None, path: Path | None = None) -> str | None:
    """Normalize trusted write attribution; unmapped legacy owners stay opaque."""
    if app_id:
        return principal_key("app", app_id)
    if not owner or owner.startswith(("app:", "user:", "service_account:")):
        return owner
    for credential in _credentials(path):
        if credential["hash"] == owner:
            return principal_key_for_credential(credential) or owner
    return owner


def owner_key(auth_context: Any, *, path: Path | None = None) -> str | None:
    identity = getattr(auth_context, "identity", None)
    if identity is None:
        # Signed session/admin contexts already expose a trusted subject.
        kind = getattr(auth_context, "actor_kind", None)
        subject = getattr(auth_context, "actor_id", None) or getattr(auth_context, "subject", None)
        if kind and subject:
            return principal_key(kind, subject)
        kind = getattr(auth_context, "subject_kind", None)
        subject = getattr(auth_context, "subject_id", None)
        if kind and subject:
            return principal_key(kind, subject)
        subject = getattr(auth_context, "subject", None)
        if subject:
            from gateway.admin_runtime import session_admin_role_subject
            return principal_key(*session_admin_role_subject(subject))
        return None
    app_id = getattr(identity, "app_id", None)
    if app_id:
        return principal_key("app", app_id)
    kind = getattr(identity, "subject_kind", None)
    subject = getattr(identity, "subject_id", None)
    if kind and subject:
        return principal_key(kind, subject)
    return stored_owner_key(getattr(identity, "key_hash", None), path=path)


def credential_owner_keys(owner: str | None, *, path: Path | None = None,
                          current_key_hash: str | None = None) -> tuple[str, ...]:
    """Principal plus every credential belonging to it, including revoked keys."""
    if owner is None:
        return ()
    credentials = _credentials(path)
    principal = next((principal_key_for_credential(c) for c in credentials if c["hash"] == owner), None) or owner
    return tuple(dict.fromkeys([principal, owner, *([current_key_hash] if current_key_hash else []), *[
        c["hash"] for c in credentials if principal_key_for_credential(c) == principal
    ]]))


def readable_owner_keys(auth_context: Any, *, path: Path | None = None) -> tuple[str, ...]:
    identity = getattr(auth_context, "identity", None)
    return credential_owner_keys(
        owner_key(auth_context, path=path), path=path,
        current_key_hash=getattr(identity, "key_hash", None),
    )
