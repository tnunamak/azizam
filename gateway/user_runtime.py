"""Normal-user account view over the durable admin store.

The user portal is intentionally narrow. It lets a signed-in OIDC user create
user-owned Apps, see non-secret API-key records, create keys for Apps they own,
inspect owned-key access, and delete their own keys or Apps. It never grants
Profiles or changes App ownership; those remain admin-plane actions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from gateway import admin_store
from gateway.admin_oidc import AdminSession
from gateway.admin_runtime import generate_app_api_key
from gateway.runtime_auth import hash_api_key

__all__ = [
    "UserPortalError",
    "build_user_portal_snapshot",
    "create_user_app",
    "delete_user_app",
    "get_user_app_credential",
    "mint_user_app_key",
    "request_user_access",
    "revoke_user_app_key",
]


class UserPortalError(ValueError):
    """Raised when a user-plane account action is not allowed."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _sanitize_credential(record: Mapping[str, Any]) -> dict[str, Any]:
    blocked = {"hash", "api_key", "key", "secret"}
    return {k: v for k, v in record.items() if k not in blocked}


def _key_prefix(api_key: str) -> str:
    return api_key[:18]


def _credential_active(record: Mapping[str, Any]) -> bool:
    return record.get("revoked_at") is None


def _profile_summary(profile_id: str | None, *, path: Path) -> dict[str, Any] | None:
    if not profile_id:
        return None
    record = admin_store.get_profile(profile_id, path=path)
    if record is None:
        return {
            "id": profile_id,
            "name": "Missing Profile",
            "missing": True,
        }
    profile = record.get("profile") if isinstance(record, Mapping) else None
    policy_id = None
    protocols: list[str] = []
    allow: dict[str, Any] = {}
    status_visibility = "basic"
    if isinstance(profile, Mapping):
        policy_id = profile.get("id")
        protocols = list(profile.get("protocols") or [])
        raw_allow = profile.get("allow")
        allow = dict(raw_allow) if isinstance(raw_allow, Mapping) else {}
        raw_visibility = profile.get("status_visibility")
        if isinstance(raw_visibility, Mapping):
            status_visibility = str(raw_visibility.get("level") or "basic")
    return {
        "id": record.get("id") or profile_id,
        "name": record.get("name") or policy_id or profile_id,
        "policy_id": policy_id,
        "protocols": protocols,
        "allow": allow,
        "status_visibility": status_visibility,
        "missing": False,
    }


def _default_profile_summary(profile_id: str | None, *, path: Path) -> dict[str, Any]:
    if profile_id:
        profile = _profile_summary(profile_id, path=path)
        if profile is not None:
            profile["inherited"] = True
            return profile
    return {
        "id": "default-local",
        "name": "Default local",
        "policy_id": "default-local",
        "protocols": [],
        "allow": {},
        "status_visibility": "basic",
        "missing": False,
        "inherited": True,
    }


def _credentials_for_app(app_id: str, *, path: Path) -> list[dict[str, Any]]:
    credentials = []
    for record in admin_store.list_active_credentials(
        subject_kind="app",
        app_id=app_id,
        path=path,
    ):
        item = _sanitize_credential(record)
        item["active"] = True
        credentials.append(item)
    return credentials


def _app_belongs_to_user(app_id: str, user_id: str, *, path: Path) -> dict[str, Any]:
    app = admin_store.get_app(app_id, path=path)
    if app is None or app.get("owner_user_id") != user_id:
        raise UserPortalError(
            "app_not_found",
            "App not found for this account",
            status_code=404,
        )
    if app.get("disabled_at"):
        raise UserPortalError(
            "app_disabled",
            "App is disabled",
            status_code=403,
        )
    return app


def _effective_profile_id(
    app: Mapping[str, Any], *, default_profile_id: str | None = None
) -> str | None:
    profile_id = app.get("default_profile_id") or default_profile_id
    return str(profile_id) if profile_id else None


def _app_profile_for_self_service(
    app: Mapping[str, Any],
    *,
    default_profile_id: str | None = None,
    path: Path,
) -> str | None:
    profile_id = _effective_profile_id(app, default_profile_id=default_profile_id)
    if not profile_id:
        return None
    if admin_store.get_profile(str(profile_id), path=path) is None:
        raise UserPortalError(
            "profile_missing",
            "This App's inherited permissions are unavailable; ask the operator to repair access",
            status_code=403,
        )
    return str(profile_id)


def _user_from_session(session: AdminSession, *, path: Path) -> dict[str, Any]:
    user = admin_store.get_user(session.subject, path=path)
    if user is None:
        raise UserPortalError(
            "session_user_not_found",
            "Signed-in user was not found in the admin store",
            status_code=401,
        )
    return user


def _app_payload(
    app: Mapping[str, Any],
    *,
    default_profile_id: str | None,
    path: Path,
) -> dict[str, Any]:
    profile_id = app.get("default_profile_id")
    effective_profile_id = _effective_profile_id(
        app,
        default_profile_id=default_profile_id,
    )
    credentials = _credentials_for_app(str(app["id"]), path=path)
    profile = (
        _profile_summary(str(profile_id), path=path)
        if profile_id
        else _default_profile_summary(default_profile_id, path=path)
    )
    return {
        "id": app.get("id"),
        "kind": app.get("kind"),
        "name": app.get("name"),
        "disabled_at": app.get("disabled_at"),
        "default_profile_id": profile_id,
        "profile_id": profile_id,
        "effective_profile_id": effective_profile_id or "default-local",
        "profile": profile,
        "can_mint_keys": (
            not app.get("disabled_at")
            and not (profile or {}).get("missing")
        ),
        "can_delete": not bool(app.get("disabled_at")),
        "inherits_profile": not bool(profile_id),
        "profile_required": False,
        "credentials": credentials,
        "active_credentials": len(credentials),
        "created_at": app.get("created_at"),
    }


def build_user_portal_snapshot(
    session: AdminSession | None,
    *,
    auto_create_personal_app: bool = False,
    default_profile_id: str | None = None,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Return the account data safe to embed in ``/me``.

    Missing or invalid sessions are represented as an unauthenticated snapshot
    so the HTML page can still serve the paste-a-key flow.
    """
    if session is None:
        return {"authenticated": False, "apps": []}
    try:
        user = _user_from_session(session, path=path)
    except UserPortalError as exc:
        return {
            "authenticated": False,
            "apps": [],
            "error": {"code": exc.code, "message": exc.message},
        }

    if auto_create_personal_app:
        label = user.get("email") or user.get("display_name") or user["id"]
        admin_store.ensure_personal_app_for_user(
            str(user["id"]),
            name=f"{label} personal access",
            path=path,
        )

    apps: list[dict[str, Any]] = []
    for app in admin_store.list_apps_for_user(user["id"], path=path):
        if app.get("disabled_at"):
            continue
        apps.append(_app_payload(
            app,
            default_profile_id=default_profile_id,
            path=path,
        ))

    return {
        "authenticated": True,
        "user": {
            "id": user.get("id"),
            "email": user.get("email") or session.email,
            "name": user.get("display_name") or user.get("email") or session.email,
            "role": session.role,
        },
        "apps": apps,
    }


def mint_user_app_key(
    session: AdminSession,
    app_id: str,
    *,
    label: str | None = None,
    default_profile_id: str | None = None,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Create an API key for an App already owned by ``session.subject``."""
    user = _user_from_session(session, path=path)
    app = _app_belongs_to_user(app_id, str(user["id"]), path=path)
    _app_profile_for_self_service(
        app,
        default_profile_id=default_profile_id,
        path=path,
    )
    api_key = generate_app_api_key()
    record = admin_store.create_credential_hash(
        subject_kind="app",
        subject_id=str(app["id"]),
        hash=hash_api_key(api_key),
        prefix=_key_prefix(api_key),
        display_name=label or "Self-service key",
        path=path,
    )
    admin_store.record_audit(
        "credential.self_create",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="credential",
        target_id=record["id"],
        metadata={"app_id": app["id"], "prefix": record["prefix"]},
        path=path,
    )
    out = _sanitize_credential(record)
    out["active"] = True
    out["api_key"] = api_key
    return out


def create_user_app(
    session: AdminSession,
    *,
    name: str,
    default_profile_id: str | None = None,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Create a new user-owned App that inherits the user's default policy."""
    user = _user_from_session(session, path=path)
    cleaned_name = " ".join(str(name or "").split())
    if not cleaned_name:
        raise UserPortalError(
            "app_name_required",
            "Name the App before creating it.",
            status_code=400,
        )
    if len(cleaned_name) > 80:
        raise UserPortalError(
            "app_name_too_long",
            "App names must be 80 characters or fewer.",
            status_code=400,
        )
    app = admin_store.create_app(
        cleaned_name,
        kind="personal",
        owner_user_id=str(user["id"]),
        default_profile_id=default_profile_id,
        path=path,
    )
    admin_store.record_audit(
        "app.self_create",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="app",
        target_id=str(app["id"]),
        metadata={"email": user.get("email"), "name": cleaned_name},
        path=path,
    )
    return {"app": _app_payload(app, default_profile_id=default_profile_id, path=path)}


def delete_user_app(
    session: AdminSession,
    app_id: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Remove a user-owned App from self-service use and revoke its active keys."""
    user = _user_from_session(session, path=path)
    app = _app_belongs_to_user(app_id, str(user["id"]), path=path)
    revoked = 0
    for credential in admin_store.list_active_credentials(
        subject_kind="app",
        app_id=str(app["id"]),
        path=path,
    ):
        if admin_store.revoke_credential(str(credential["id"]), path=path):
            revoked += 1
    disabled = admin_store.disable_app(str(app["id"]), path=path) or app
    admin_store.record_audit(
        "app.self_delete",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="app",
        target_id=str(app["id"]),
        metadata={"email": user.get("email"), "revoked_credentials": revoked},
        path=path,
    )
    return {
        "app": {
            "id": disabled.get("id"),
            "name": disabled.get("name"),
            "disabled_at": disabled.get("disabled_at"),
            "revoked_credentials": revoked,
        }
    }


def request_user_access(
    session: AdminSession,
    *,
    default_profile_id: str | None = None,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Create or return the signed-in user's self-service Personal App."""
    user = _user_from_session(session, path=path)
    label = user.get("email") or user.get("display_name") or user["id"]
    app = admin_store.ensure_personal_app_for_user(
        str(user["id"]),
        name=f"{label} personal access",
        default_profile_id=default_profile_id,
        path=path,
    )
    admin_store.record_audit(
        "app.self_create",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="app",
        target_id=app["id"],
        metadata={
            "email": user.get("email"),
            "effective_profile_id": (
                app.get("default_profile_id") or default_profile_id or "default-local"
            ),
        },
        path=path,
    )
    return {"app": _app_payload(app, default_profile_id=default_profile_id, path=path)}


def get_user_app_credential(
    session: AdminSession,
    credential_id: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Return an active App credential owned by ``session.subject``."""
    user = _user_from_session(session, path=path)
    credential = admin_store.get_credential(credential_id, path=path)
    if (
        credential is None
        or credential.get("subject_kind") != "app"
        or not _credential_active(credential)
    ):
        raise UserPortalError(
            "credential_not_found",
            "API key not found for this account",
            status_code=404,
        )
    app_id = str(credential.get("app_id") or credential.get("subject_id") or "")
    _app_belongs_to_user(app_id, str(user["id"]), path=path)
    return credential


def revoke_user_app_key(
    session: AdminSession,
    credential_id: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Delete an API key for an App owned by ``session.subject``."""
    user = _user_from_session(session, path=path)
    before = get_user_app_credential(session, credential_id, path=path)
    app_id = str(before.get("app_id") or before.get("subject_id") or "")
    admin_store.revoke_credential(credential_id, path=path)
    after = admin_store.get_credential(credential_id, path=path) or before
    admin_store.record_audit(
        "credential.self_revoke",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="credential",
        target_id=credential_id,
        metadata={"app_id": app_id, "prefix": before.get("prefix")},
        path=path,
    )
    out = _sanitize_credential(after)
    out["active"] = False
    return out
