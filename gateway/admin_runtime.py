"""Runtime glue for the AI Gateway admin control plane.

This module adapts the durable :mod:`gateway.admin_store` functions to the
duck-typed store expected by :mod:`gateway.admin_api`, and provides a small
admin authenticator for bearer admin keys and signed dashboard sessions.

It intentionally keeps two planes separate:

* app credentials (`subject_kind == "app"`) authenticate data-plane runtime
  calls;
* user/service-account credentials authenticate admin/control-plane calls.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any, Mapping

from fastapi import Request, status

from gateway import admin_store
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.admin_oidc import (
    CookieConfig,
    SessionError,
    csrf_token_for_session,
    decode_session_cookie,
)
from gateway.policy import DEFAULT_PROFILE
from gateway.profiles import ProfileLoadError, load_profile
from gateway.runtime_auth import RuntimeIdentity, hash_api_key

APP_KEY_PREFIX = "opk_"
ADMIN_KEY_PREFIX = "oak_"
DEFAULT_SERVICE_ACCOUNT = "gateway-admin"
ADMIN_CONTROL_ROLES = frozenset({"owner", "admin", "superadmin"})
ADMIN_KEY_CREATABLE_ROLES = frozenset({"owner", "admin"})


def generate_app_api_key() -> str:
    return f"{APP_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def generate_admin_api_key() -> str:
    return f"{ADMIN_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def _key_prefix(api_key: str) -> str:
    return api_key[:18]


def _sanitize_credential(record: Mapping[str, Any]) -> dict[str, Any]:
    blocked = {"hash", "api_key", "key", "secret"}
    return {k: v for k, v in record.items() if k not in blocked}


def _profile_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(record)
    profile = out.get("profile")
    if isinstance(profile, Mapping):
        out["protocols"] = profile.get("protocols") or []
        out["workflows"] = profile.get("workflows") or []
        visibility = profile.get("status_visibility") or {}
        if isinstance(visibility, Mapping):
            out["status_visibility"] = visibility.get("level") or "basic"
        else:
            out["status_visibility"] = "basic"
    return out


class AdminSQLiteStore:
    """Adapter from ``admin_store`` functions to ``gateway.admin_api``."""

    def __init__(self, path: Path = admin_store.DB_PATH) -> None:
        self.path = path

    def init_db(self) -> None:
        admin_store.init_db(self.path)

    def list_users(self) -> list[dict[str, Any]]:
        users = admin_store.list_users(path=self.path)
        for user in users:
            roles = admin_store.list_admin_roles(
                subject_kind="user",
                subject_id=user["id"],
                path=self.path,
            )
            user["roles"] = [r["role"] for r in roles]
            user.setdefault("name", user.get("display_name"))
        return users

    def list_apps(self) -> list[dict[str, Any]]:
        return admin_store.list_apps(path=self.path)

    def get_app(self, app_id: str) -> dict[str, Any] | None:
        return admin_store.get_app(app_id, path=self.path)

    def create_app(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("app name is required")
        kind = str(data.get("kind") or "service")
        profile_id = data.get("profile_id")
        owner_user_id = data.get("owner_user_id")
        if profile_id and admin_store.get_profile(str(profile_id), path=self.path) is None:
            raise ValueError(f"profile {profile_id!r} not found")
        if kind == "personal" or owner_user_id:
            if not owner_user_id:
                raise ValueError("personal apps require owner_user_id")
            if admin_store.get_user(str(owner_user_id), path=self.path) is None:
                raise ValueError(f"user {owner_user_id!r} not found")
            record = admin_store.ensure_personal_app_for_user(
                str(owner_user_id),
                name=name,
                default_profile_id=str(profile_id) if profile_id else None,
                path=self.path,
            )
        else:
            svc = admin_store.ensure_service_account(
                str(data.get("owner_service_account_name") or DEFAULT_SERVICE_ACCOUNT),
                description="Default owner for shared Apps created from the admin dashboard.",
                path=self.path,
            )
            record = admin_store.create_app(
                name,
                kind="service",
                owner_service_account_id=svc["id"],
                default_profile_id=str(profile_id) if profile_id else None,
                path=self.path,
            )
        admin_store.record_audit(
            "app.create",
            actor_kind="admin",
            actor_id=actor.subject,
            target_kind="app",
            target_id=record["id"],
            metadata={"name": name, "kind": record.get("kind")},
            path=self.path,
        )
        return record

    def list_profiles(self) -> list[dict[str, Any]]:
        return [
            _profile_summary(record)
            for record in admin_store.list_profiles(path=self.path)
        ]

    def create_profile(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        profile_doc = data.get("profile") if "profile" in data else data
        if not isinstance(profile_doc, Mapping):
            raise ValueError("profile must be a mapping")
        profile_id = str(profile_doc.get("id") or data.get("id") or "").strip()
        name = str(data.get("name") or data.get("label") or profile_id).strip()
        if not name:
            raise ValueError("profile name is required")
        record = admin_store.create_profile(
            name,
            profile_doc,
            description=data.get("description"),
            path=self.path,
        )
        admin_store.record_audit(
            "profile.create",
            actor_kind="admin",
            actor_id=actor.subject,
            target_kind="profile",
            target_id=record["id"],
            metadata={"name": name},
            path=self.path,
        )
        return _profile_summary(record)

    def list_credentials(self) -> list[dict[str, Any]]:
        return [
            _sanitize_credential(record)
            for record in admin_store.list_credentials(path=self.path)
        ]

    def create_credential(
        self, *, app_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        app = admin_store.get_app(app_id, path=self.path)
        if app is None:
            raise ValueError(f"app {app_id!r} not found")
        api_key = generate_app_api_key()
        profile_id = data.get("profile_id")
        if profile_id and admin_store.get_profile(str(profile_id), path=self.path) is None:
            raise ValueError(f"profile {profile_id!r} not found")
        record = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=app_id,
            hash=hash_api_key(api_key),
            prefix=_key_prefix(api_key),
            profile_id=str(profile_id) if profile_id else None,
            display_name=str(data.get("label") or data.get("name") or "API key"),
            scopes=data.get("scopes") if isinstance(data.get("scopes"), list) else None,
            path=self.path,
        )
        admin_store.record_audit(
            "credential.create",
            actor_kind="admin",
            actor_id=actor.subject,
            target_kind="credential",
            target_id=record["id"],
            metadata={"app_id": app_id, "prefix": record["prefix"]},
            path=self.path,
        )
        out = _sanitize_credential(record)
        out["api_key"] = api_key
        return out

    def create_admin_automation_key(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("admin automation key name is required")
        role = str(data.get("role") or "admin").strip()
        if role not in ADMIN_KEY_CREATABLE_ROLES:
            raise ValueError("role must be one of: owner, admin")
        service_account = admin_store.ensure_service_account(
            name,
            description="Owner for an admin automation key created from the dashboard.",
            path=self.path,
        )
        api_key = generate_admin_api_key()
        credential = admin_store.create_credential_hash(
            subject_kind="service_account",
            subject_id=service_account["id"],
            hash=hash_api_key(api_key),
            prefix=_key_prefix(api_key),
            display_name=f"{name} admin automation key",
            path=self.path,
        )
        assignment = admin_store.assign_admin_role(
            role=role,
            subject_kind="service_account",
            subject_id=service_account["id"],
            granted_by=actor.subject,
            path=self.path,
        )
        admin_store.record_audit(
            "admin_key.create",
            actor_kind="admin",
            actor_id=actor.subject,
            target_kind="credential",
            target_id=credential["id"],
            metadata={
                "service_account": name,
                "role": role,
                "prefix": credential["prefix"],
            },
            path=self.path,
        )
        out = _sanitize_credential(credential)
        out["api_key"] = api_key
        out["role"] = assignment["role"]
        out["service_account"] = service_account["name"]
        return out

    def revoke_credential(
        self, *, credential_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        before = admin_store.get_credential(credential_id, path=self.path)
        if before is None:
            return None
        admin_store.revoke_credential(credential_id, path=self.path)
        record = admin_store.get_credential(credential_id, path=self.path)
        admin_store.record_audit(
            "credential.revoke",
            actor_kind="admin",
            actor_id=actor.subject,
            target_kind="credential",
            target_id=credential_id,
            metadata={"prefix": before.get("prefix")},
            path=self.path,
        )
        return _sanitize_credential(record or before)

    def recent_audit(self, *, limit: int) -> list[dict[str, Any]]:
        return admin_store.list_audit(limit=limit, path=self.path)

    def profile_for_id(self, profile_id: str):
        record = admin_store.get_profile(profile_id, path=self.path)
        if not record:
            return None
        try:
            return load_profile(record.get("profile"))
        except ProfileLoadError:
            return None

    def dashboard_snapshot(self, actor: AdminContext) -> dict[str, Any]:
        users = self.list_users()
        apps = self.list_apps()
        profiles = self.list_profiles()
        credentials = self.list_credentials()
        audit = self.recent_audit(limit=50)
        return {
            "actor": {
                "subject": actor.subject,
                "email": actor.email,
                "roles": sorted(actor.roles),
                "csrf_token": actor.csrf_token or "",
            },
            "counts": {
                "users": len(users),
                "apps": len(apps),
                "profiles": len(profiles),
                "credentials": len(credentials),
                "active_credentials": sum(1 for c in credentials if not c.get("revoked_at")),
            },
            "users": users,
            "apps": apps,
            "profiles": profiles,
            "credentials": credentials,
            "audit": audit,
            "discovery_links": [
                {"label": "Account", "href": "/"},
                {"label": "Gateway metadata", "href": "/.well-known/ai-gateway"},
                {
                    "label": "Agent skill",
                    "href": "/.well-known/skills/openai-proxy-gateway/SKILL.md",
                },
                {"label": "Admin overview", "href": "/admin/api/overview"},
                {"label": "Apps JSON", "href": "/admin/api/apps"},
                {"label": "Profiles JSON", "href": "/admin/api/profiles"},
                {"label": "Audit JSON", "href": "/admin/api/audit"},
            ],
        }


class AdminAuthenticator:
    """Authenticate admin API requests from signed sessions or admin keys."""

    def __init__(
        self,
        store: AdminSQLiteStore,
        *,
        signing_key: bytes | None = None,
        cookie: CookieConfig | None = None,
    ) -> None:
        self.store = store
        self.signing_key = signing_key
        self.cookie = cookie or CookieConfig()

    def __call__(self, request: Request) -> AdminContext:
        if self.signing_key:
            token = request.cookies.get(self.cookie.name)
            if token:
                try:
                    session = decode_session_cookie(
                        token,
                        signing_key=self.signing_key,
                    )
                    roles = admin_store.list_admin_roles(
                        subject_kind="user",
                        subject_id=session.subject,
                        path=self.store.path,
                    )
                    role_names = frozenset(str(role["role"]) for role in roles)
                    if ADMIN_CONTROL_ROLES & role_names:
                        return AdminContext(
                            subject=session.subject,
                            email=session.email,
                            roles=role_names,
                            auth_method="session",
                            csrf_token=csrf_token_for_session(
                                session,
                                signing_key=self.signing_key,
                            ),
                        )
                    raise AdminAuthError(
                        "admin role required",
                        status_code=status.HTTP_403_FORBIDDEN,
                        code="admin_forbidden",
                    )
                except SessionError:
                    pass

        api_key = _bearer_token(request)
        if not api_key:
            raise AdminAuthError()

        credential = admin_store.find_credential_by_hash(
            hash_api_key(api_key), path=self.store.path
        )
        if credential is None or not _credential_active(credential):
            raise AdminAuthError("invalid admin credential")
        if credential.get("subject_kind") not in {"user", "service_account"}:
            raise AdminAuthError(
                "app API keys cannot administer the gateway",
                status_code=status.HTTP_403_FORBIDDEN,
                code="admin_forbidden",
            )

        roles = admin_store.list_admin_roles(
            subject_kind=str(credential["subject_kind"]),
            subject_id=str(credential["subject_id"]),
            path=self.store.path,
        )
        role_names = frozenset(str(role["role"]) for role in roles)
        if not (ADMIN_CONTROL_ROLES & role_names):
            raise AdminAuthError(
                "admin role required",
                status_code=status.HTTP_403_FORBIDDEN,
                code="admin_forbidden",
            )
        return AdminContext(
            subject=str(credential["subject_id"]),
            roles=role_names,
            auth_method="bearer",
        )


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header.split(None, 1)[1].strip()
    return ""


def _credential_active(record: Mapping[str, Any]) -> bool:
    if record.get("revoked_at"):
        return False
    expires_at = record.get("expires_at")
    if expires_at is not None and int(expires_at) <= int(time.time()):
        return False
    return True


def runtime_identity_for_app_credential_hash(
    key_hash: str,
    *,
    inherited_profile_id_for_app: Callable[[Mapping[str, Any]], str | None] | None = None,
    path: Path = admin_store.DB_PATH,
) -> RuntimeIdentity | None:
    """Return a data-plane identity for an admin-store App credential.

    Dashboard-minted ``opk_...`` keys live in the durable admin store, not in
    ``proxy_api_keys.yml``. Runtime auth must accept only active App
    credentials from that store; user/service-account admin keys remain
    control-plane-only.
    """
    credential = admin_store.find_credential_by_hash(key_hash, path=path)
    if credential is None or not _credential_active(credential):
        return None
    if credential.get("subject_kind") != "app":
        return None

    app_id = str(credential.get("app_id") or credential.get("subject_id") or "")
    if not app_id:
        return None
    app = admin_store.get_app(app_id, path=path)
    if app is None or app.get("disabled_at"):
        return None

    profile = DEFAULT_PROFILE
    profile_id = credential.get("profile_id") or app.get("default_profile_id")
    if not profile_id and inherited_profile_id_for_app is not None:
        profile_id = inherited_profile_id_for_app(app)
    if profile_id:
        profile_record = admin_store.get_profile(str(profile_id), path=path)
        if profile_record is None:
            return None
        try:
            profile = load_profile(
                profile_record.get("profile"),
                default_id=str(profile_id),
            )
        except ProfileLoadError:
            return None

    return RuntimeIdentity(
        name=str(app.get("name") or credential.get("display_name") or app_id),
        key_hash=key_hash,
        downstream={},
        profile=profile,
    )


def create_admin_service_account_key(
    name: str,
    *,
    role: str = "owner",
    path: Path = admin_store.DB_PATH,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Create a control-plane service-account key and role assignment."""
    if role not in ADMIN_KEY_CREATABLE_ROLES:
        raise ValueError("role must be one of: owner, admin")
    service_account = admin_store.ensure_service_account(name, path=path)
    api_key = generate_admin_api_key()
    credential = admin_store.create_credential_hash(
        subject_kind="service_account",
        subject_id=service_account["id"],
        hash=hash_api_key(api_key),
        prefix=_key_prefix(api_key),
        display_name=f"{name} admin key",
        path=path,
    )
    assignment = admin_store.assign_admin_role(
        role=role,
        subject_kind="service_account",
        subject_id=service_account["id"],
        granted_by="cli",
        path=path,
    )
    admin_store.record_audit(
        "admin_key.create",
        actor_kind="cli",
        actor_id="cli",
        target_kind="credential",
        target_id=credential["id"],
        metadata={"service_account": name, "role": role, "prefix": credential["prefix"]},
        path=path,
    )
    return api_key, _sanitize_credential(credential), assignment


__all__ = [
    "ADMIN_KEY_PREFIX",
    "APP_KEY_PREFIX",
    "AdminAuthenticator",
    "AdminSQLiteStore",
    "create_admin_service_account_key",
    "generate_admin_api_key",
    "generate_app_api_key",
]
