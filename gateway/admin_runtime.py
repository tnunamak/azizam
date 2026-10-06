"""Runtime glue for the Azizam admin control plane.

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

from gateway import admin_store, extension_packages, service_registry
from gateway.admin_api import AdminAuthError, AdminContext, access_model_summary
from gateway.admin_oidc import (
    CookieConfig,
    OIDCConfigError,
    SessionError,
    csrf_token_for_session,
    decode_session_cookie,
    load_oidc_config,
)
from gateway.policy import DEFAULT_PROFILE, default_profile_id, is_default_profile_id
from gateway.profiles import ProfileLoadError, load_profile
from gateway.litellm_substrate import write_litellm_config_artifact
from gateway.provider_admin import (
    provider_admin_setting_from_store,
    provider_admin_setting_to_store,
)
from gateway.runtime_auth import RuntimeIdentity, hash_api_key

APP_KEY_PREFIX = "opk_"
ADMIN_KEY_PREFIX = "oak_"
DEFAULT_SERVICE_ACCOUNT = "gateway-admin"
ADMIN_CONTROL_ROLES = frozenset({"owner", "admin", "superadmin"})
ADMIN_KEY_CREATABLE_ROLES = frozenset({"owner", "admin"})
SERVICE_ACCOUNT_SESSION_PREFIX = "service_account:"
LOCAL_ADMIN_KEY_ISSUER = "local-admin-key"
SERVICE_AVAILABILITY_OVERRIDES_KEY = "service_availability_overrides"
MEDIA_JOB_ADMISSION_OVERRIDES_KEY = "media_job_admission_overrides"
USER_ACCESS_DEFAULTS_SETTING_KEY = "user_access_defaults"
OIDC_PROVIDER_SETTING_KEY = "oidc_provider_config"
SERVICE_REGISTRY_SETTING_KEY = service_registry.SERVICE_REGISTRY_SETTING_KEY
EXTENSION_PACKAGE_INDEX_SETTING_KEY = extension_packages.EXTENSION_PACKAGE_INDEX_SETTING_KEY

OIDC_PROVIDER_FIELDS: tuple[str, ...] = (
    "enabled",
    "issuer",
    "client_id",
    "client_secret",
    "redirect_uri",
    "scopes",
    "allowed_email_domains",
    "admin_emails",
    "admin_groups",
    "admin_roles",
    "authorization_endpoint",
    "token_endpoint",
    "jwks_uri",
    "clock_skew_seconds",
    "groups_claim",
)

OIDC_SECRET_FIELDS: frozenset[str] = frozenset({"client_secret"})
OIDC_LIST_FIELDS: frozenset[str] = frozenset({
    "scopes", "allowed_email_domains", "admin_emails", "admin_groups", "admin_roles",
})
_OIDC_SECRET_REDACTED = "<redacted>"


def service_account_session_subject(service_account_id: str) -> str:
    """Encode a service-account browser admin session subject.

    Signed browser admin sessions existed before they could carry an explicit
    subject kind. Keep OIDC/local users as raw ``usr_*`` ids for compatibility,
    and encode only service-account sessions with an unambiguous prefix.
    """
    return f"{SERVICE_ACCOUNT_SESSION_PREFIX}{service_account_id}"


def session_admin_role_subject(subject: str) -> tuple[str, str]:
    """Return the durable admin-role subject represented by a session subject."""
    if subject.startswith(SERVICE_ACCOUNT_SESSION_PREFIX):
        service_account_id = subject[len(SERVICE_ACCOUNT_SESSION_PREFIX):]
        if service_account_id:
            return "service_account", service_account_id
    return "user", subject


def is_human_user_record(user: Mapping[str, Any]) -> bool:
    """True for People rows, false for legacy admin-key claim placeholders."""
    return str(user.get("issuer") or "") != LOCAL_ADMIN_KEY_ISSUER


def _bool_setting(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def generate_app_api_key() -> str:
    return f"{APP_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def generate_admin_api_key() -> str:
    return f"{ADMIN_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def _key_prefix(api_key: str) -> str:
    return api_key[:18]


def _sanitize_credential(record: Mapping[str, Any]) -> dict[str, Any]:
    blocked = {"hash", "api_key", "key", "secret"}
    return {k: v for k, v in record.items() if k not in blocked}


def _copy_override_value(value: Mapping[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if isinstance(item, Mapping):
            copied[key_text] = _copy_override_value(item)
        elif isinstance(item, (list, tuple)):
            copied[key_text] = [
                _copy_override_value(v) if isinstance(v, Mapping) else v
                for v in item
            ]
        else:
            copied[key_text] = item
    return copied


def _stored_profile_id(value: Any) -> str | None:
    """Return a stored Profile id, treating the built-in default as inherited."""
    profile_id = str(value or "").strip()
    if not profile_id or is_default_profile_id(profile_id):
        return None
    return profile_id


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


def _redact_oidc_setting(
    value: Mapping[str, Any], *, updated_at: Any = None
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in OIDC_PROVIDER_FIELDS:
        if key not in value:
            continue
        if key in OIDC_SECRET_FIELDS:
            out[key] = _OIDC_SECRET_REDACTED if value.get(key) else ""
            out[f"{key}_set"] = bool(value.get(key))
        else:
            out[key] = value[key]
    if updated_at is not None:
        out["updated_at"] = updated_at
    return out


def _merge_oidc_setting(
    current: Mapping[str, Any], update: Mapping[str, Any]
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in OIDC_PROVIDER_FIELDS:
        if key in update:
            new_value = update[key]
            if key in OIDC_SECRET_FIELDS and _is_blank_or_redacted(new_value):
                # Preserve the existing secret when caller omits/clears it.
                if key in current:
                    merged[key] = current[key]
            else:
                # Canonical stored clears are "" for scalar fields and [] for
                # list fields. Normalize old direct-store nulls on their next
                # write; HTTP input never creates them.
                merged[key] = (
                    [] if new_value is None and key in _OIDC_LIST_FIELDS
                    else "" if new_value is None
                    else new_value
                )
        elif key in current:
            current_value = current[key]
            merged[key] = (
                [] if current_value is None and key in _OIDC_LIST_FIELDS
                else "" if current_value is None
                else current_value
            )
    return merged


def _is_blank_or_redacted(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return stripped == "" or stripped == _OIDC_SECRET_REDACTED
    return False


def _oidc_secret_changed(
    current: Mapping[str, Any], update: Mapping[str, Any]
) -> bool:
    for key in OIDC_SECRET_FIELDS:
        if key not in update:
            continue
        if _is_blank_or_redacted(update[key]):
            continue
        if str(current.get(key) or "") != str(update[key] or ""):
            return True
    return False


def _validate_merged_oidc_setting(value: Mapping[str, Any]) -> None:
    if not value.get("enabled"):
        return
    for key in ("issuer", "client_id", "client_secret", "redirect_uri"):
        if not str(value.get(key) or "").strip():
            raise ValueError(f"{key} is required when enabled=true")


class AdminSQLiteStore:
    """Adapter from ``admin_store`` functions to ``gateway.admin_api``."""

    def __init__(self, path: Path = admin_store.DB_PATH) -> None:
        self.path = path
        # Optional zero-argument callable invoked after every service-registry
        # mutation (add/update/remove/enable/toggle/bulk-replace).  Set this
        # from outside (e.g. proxy.py after ADMIN_STORE is created) to trigger
        # an in-process hot reload without a circular import.
        self.on_service_registry_changed: "Callable[[], None] | None" = None
        # Called with the credential record (including its stored hash) after a
        # credential is revoked, so data keyed by that key (custom voices) can be removed.
        self.on_credential_revoked: "Callable[[Mapping[str, Any]], None] | None" = None
        # The live host supplies this so a candidate DB override is accepted
        # only when the same parser used by login accepts its effective merge.
        self.oidc_config_validator: "Callable[[Mapping[str, Any]], None] | None" = None

    def init_db(self) -> None:
        admin_store.init_db(self.path)

    def list_users(self) -> list[dict[str, Any]]:
        users = [
            user for user in admin_store.list_users(path=self.path)
            if is_human_user_record(user)
        ]
        for user in users:
            roles = admin_store.list_admin_roles(
                subject_kind="user",
                subject_id=user["id"],
                path=self.path,
            )
            user["roles"] = [r["role"] for r in roles]
            user.setdefault("name", user.get("display_name"))
        return users

    def _enrich_app(self, record: dict[str, Any]) -> dict[str, Any]:
        """Annotate an app record with resolved effective profile fields."""
        pid = record.get("default_profile_id")
        if pid:
            profile = admin_store.get_profile(str(pid), path=self.path)
            record["effective_profile_id"] = str(pid)
            record["effective_profile_name"] = (
                str(profile.get("name") or "") if profile else None
            )
            record["effective_profile_source"] = "app_default"
        else:
            record["effective_profile_id"] = default_profile_id()
            record["effective_profile_name"] = default_profile_id()
            record["effective_profile_source"] = "fallback"
        return record

    def _visible_admin_app(self, record: Mapping[str, Any]) -> bool:
        """True when an App belongs on the operator-facing Apps surface."""
        if record.get("kind") != "personal":
            return True
        owner_user_id = str(record.get("owner_user_id") or "")
        if not owner_user_id:
            return True
        owner = admin_store.get_user(owner_user_id, path=self.path)
        return owner is None or is_human_user_record(owner)

    def list_apps(self) -> list[dict[str, Any]]:
        return [
            self._enrich_app(r)
            for r in admin_store.list_apps(path=self.path)
            if self._visible_admin_app(r)
        ]

    def get_app(self, app_id: str) -> dict[str, Any] | None:
        record = admin_store.get_app(app_id, path=self.path)
        return self._enrich_app(record) if record is not None else None

    def create_app(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("app name is required")
        kind = str(data.get("kind") or "service")
        profile_id = _stored_profile_id(data.get("profile_id"))
        owner_user_id = data.get("owner_user_id")
        if profile_id and admin_store.get_profile(profile_id, path=self.path) is None:
            raise ValueError(f"profile {profile_id!r} not found")
        if kind == "personal" or owner_user_id:
            if not owner_user_id:
                raise ValueError("personal apps require owner_user_id")
            if admin_store.get_user(str(owner_user_id), path=self.path) is None:
                raise ValueError(f"user {owner_user_id!r} not found")
            record = admin_store.create_app(
                name,
                kind="personal",
                owner_user_id=str(owner_user_id),
                default_profile_id=profile_id,
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
                default_profile_id=profile_id,
                path=self.path,
            )
        admin_store.record_audit(
            "app.create",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="app",
            target_id=record["id"],
            metadata={"name": name, "kind": record.get("kind")},
            path=self.path,
        )
        return self._enrich_app(record)

    def update_app(
        self, *, app_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None:
        if admin_store.get_app(app_id, path=self.path) is None:
            return None

        name = None
        if "name" in data:
            name = str(data.get("name") or "").strip()
            if not name:
                raise ValueError("app name is required")

        profile_key_present = "default_profile_id" in data or "profile_id" in data
        profile_value = data.get("default_profile_id", data.get("profile_id"))
        profile_id = _stored_profile_id(profile_value)
        clear_profile = profile_key_present and profile_id is None
        if profile_id and admin_store.get_profile(profile_id, path=self.path) is None:
            raise ValueError(f"profile {profile_id!r} not found")

        record = admin_store.update_app(
            app_id,
            name=name,
            default_profile_id=profile_id,
            clear_default_profile=clear_profile,
            path=self.path,
        )
        admin_store.record_audit(
            "app.update",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="app",
            target_id=app_id,
            metadata={"name": name, "default_profile_id": profile_id},
            path=self.path,
        )
        return self._enrich_app(record) if record is not None else None

    def disable_app(
        self, *, app_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        before = admin_store.get_app(app_id, path=self.path)
        if before is None:
            return None
        record = admin_store.disable_app(app_id, path=self.path)
        admin_store.record_audit(
            "app.disable",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="app",
            target_id=app_id,
            metadata={"name": before.get("name")},
            path=self.path,
        )
        return self._enrich_app(record) if record is not None else None

    def enable_app(
        self, *, app_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        before = admin_store.get_app(app_id, path=self.path)
        if before is None:
            return None
        record = admin_store.enable_app(app_id, path=self.path)
        admin_store.record_audit(
            "app.enable",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="app",
            target_id=app_id,
            metadata={"name": before.get("name")},
            path=self.path,
        )
        return self._enrich_app(record) if record is not None else None

    def list_profiles(self) -> list[dict[str, Any]]:
        return [
            _profile_summary(record)
            for record in admin_store.list_profiles(path=self.path)
        ]

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        return admin_store.get_profile(profile_id, path=self.path)

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
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="profile",
            target_id=record["id"],
            metadata={"name": name},
            path=self.path,
        )
        return _profile_summary(record)

    def update_profile(
        self, *, profile_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None:
        if admin_store.get_profile(profile_id, path=self.path) is None:
            return None

        profile_doc = None
        if "profile" in data:
            raw_profile = data.get("profile")
            if not isinstance(raw_profile, Mapping):
                raise ValueError("profile must be a mapping")
            try:
                load_profile(raw_profile)
            except ProfileLoadError as exc:
                raise ValueError(str(exc)) from exc
            profile_doc = raw_profile

        name = None
        if "name" in data or "label" in data:
            name = str(data.get("name") or data.get("label") or "").strip()
            if not name:
                raise ValueError("profile name is required")

        record = admin_store.update_profile(
            profile_id,
            name=name,
            description=(
                str(data.get("description"))
                if data.get("description") is not None
                else None
            ),
            profile_doc=profile_doc,
            path=self.path,
        )
        admin_store.record_audit(
            "profile.update",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="profile",
            target_id=profile_id,
            metadata={"name": name},
            path=self.path,
        )
        return _profile_summary(record) if record is not None else None

    def delete_profile(
        self, *, profile_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        record = admin_store.delete_profile(profile_id, path=self.path)
        if record is None:
            return None
        admin_store.record_audit(
            "profile.delete",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="profile",
            target_id=profile_id,
            metadata={"name": record.get("name")},
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
        profile_id = _stored_profile_id(data.get("profile_id"))
        if profile_id and admin_store.get_profile(profile_id, path=self.path) is None:
            raise ValueError(f"profile {profile_id!r} not found")
        record = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=app_id,
            hash=hash_api_key(api_key),
            prefix=_key_prefix(api_key),
            profile_id=profile_id,
            display_name=str(data.get("label") or data.get("name") or "API key"),
            scopes=data.get("scopes") if isinstance(data.get("scopes"), list) else None,
            path=self.path,
        )
        admin_store.record_audit(
            "credential.create",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
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
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
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

    def list_runtime_profile_mappings(self) -> list[dict[str, Any]]:
        return admin_store.list_runtime_profile_mappings(path=self.path)

    def operator_overrides(self) -> dict[str, Any]:
        service_setting = admin_store.get_operator_setting(
            SERVICE_AVAILABILITY_OVERRIDES_KEY,
            path=self.path,
        )
        media_setting = admin_store.get_operator_setting(
            MEDIA_JOB_ADMISSION_OVERRIDES_KEY,
            path=self.path,
        )
        return {
            "user_access_defaults": self.user_access_defaults(),
            "service_availability": (
                service_setting["value"] if service_setting is not None else {}
            ),
            "media_job_admission": (
                media_setting["value"] if media_setting is not None else {}
            ),
            "extension_package_index": self.get_extension_package_index(),
        }

    def user_access_defaults(self) -> dict[str, bool]:
        setting = admin_store.get_operator_setting(
            USER_ACCESS_DEFAULTS_SETTING_KEY,
            path=self.path,
        )
        value = setting["value"] if setting is not None else {}
        if not isinstance(value, Mapping):
            value = {}
        self_service = _bool_setting(value.get("self_service_apps"), True)
        return {
            "self_service_apps": self_service,
            "auto_create_personal_app": _bool_setting(
                value.get("auto_create_personal_app"),
                self_service,
            ),
        }

    def update_user_access_defaults(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, bool]:
        value = self.user_access_defaults()
        for key in ("self_service_apps", "auto_create_personal_app"):
            if key in data:
                value[key] = _bool_setting(data[key], value[key])
        record = admin_store.put_operator_setting(
            USER_ACCESS_DEFAULTS_SETTING_KEY,
            value,
            updated_by=actor.subject,
            path=self.path,
        )
        admin_store.record_audit(
            "operator.user_access_defaults.update",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="operator_setting",
            target_id=USER_ACCESS_DEFAULTS_SETTING_KEY,
            metadata={"fields": sorted(data.keys())},
            path=self.path,
        )
        return record["value"]

    def update_service_availability_overrides(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        value = _copy_override_value(data)
        record = admin_store.put_operator_setting(
            SERVICE_AVAILABILITY_OVERRIDES_KEY,
            value,
            updated_by=actor.subject,
            path=self.path,
        )
        admin_store.record_audit(
            "operator.service_availability.update",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="operator_setting",
            target_id=SERVICE_AVAILABILITY_OVERRIDES_KEY,
            metadata={"override_count": len(value)},
            path=self.path,
        )
        return record["value"]

    def update_media_job_admission_overrides(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        value = _copy_override_value(data)
        record = admin_store.put_operator_setting(
            MEDIA_JOB_ADMISSION_OVERRIDES_KEY,
            value,
            updated_by=actor.subject,
            path=self.path,
        )
        admin_store.record_audit(
            "operator.media_job_admission.update",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="operator_setting",
            target_id=MEDIA_JOB_ADMISSION_OVERRIDES_KEY,
            metadata={"fields": sorted(value.keys())},
            path=self.path,
        )
        return record["value"]

    def get_service_registry(self) -> dict[str, Any]:
        registry = service_registry.load_service_registry(path=self.path)
        if registry is None:
            return {
                "schema_version": service_registry.SERVICE_REGISTRY_SCHEMA_VERSION,
                "services": {},
                "protocol_frontends": {},
                "workflows": {},
                "capabilities": {},
            }
        return registry

    def get_provider_admin_state(self) -> dict[str, Any]:
        return provider_admin_setting_from_store(path=self.path)

    def update_provider_admin_state(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        return provider_admin_setting_to_store(data, actor=actor, path=self.path)

    def write_litellm_config(self, registry: Mapping[str, Any] | None = None) -> Path:
        registry = registry or self.get_service_registry()
        return write_litellm_config_artifact(
            registry,
            # Anchor the artifact to the gateway state dir (next to admin.db),
            # matching proxy.py's startup writer and the litellm record's
            # config_path contract (resolved relative to ADMIN_DB_PATH.parent).
            # Using the repo root instead landed operator edits at
            # <repo>/litellm.config.yaml while start.sh's watcher reloads
            # data/litellm.config.yaml — so service/model changes never reached
            # the running LiteLLM subprocess.
            base_dir=self.path.parent,
        )

    def update_service_registry(
        self, *, data: Mapping[str, Any], actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        value = service_registry.normalize_service_registry(data)
        record = admin_store.write_service_registry_with_audit(
            value,
            expected_generation=expected_generation,
            updated_by=actor.subject,
            audit_action="operator.service_registry.update",
            audit_actor_kind=actor.actor_kind,
            audit_actor_id=actor.audit_actor_id,
            audit_actor_label=actor.actor_label,
            audit_target_kind="operator_setting",
            audit_target_id=SERVICE_REGISTRY_SETTING_KEY,
            audit_metadata={
                "service_count": len(value.get("services") or {}),
                "protocol_frontend_count": len(value.get("protocol_frontends") or {}),
                "workflow_count": len(value.get("workflows") or {}),
                "capability_count": len(value.get("capabilities") or {}),
            },
            path=self.path,
        )
        self.write_litellm_config(record["value"])
        self._fire_registry_changed()
        return record

    # -- single-service CRUD -------------------------------------------------
    # Granular operator edits on one backend service. Each persists the new
    # registry, writes an audit row, and rewrites the LiteLLM substrate config
    # (so the execution layer stays in sync) — the same durable effects as the
    # full-registry update, scoped to one service. Changes take effect
    # immediately via the on_service_registry_changed callback (hot reload);
    # the LiteLLM subprocess still requires a separate restart.

    def _fire_registry_changed(self) -> None:
        """Invoke the registered hot-reload callback, if any."""
        cb = self.on_service_registry_changed
        if cb is not None:
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                print(
                    f"[admin_runtime] WARNING: on_service_registry_changed callback "
                    f"raised an exception (hot-reload may not have taken effect): {exc}",
                    flush=True,
                )

    def _persist_service_registry(
        self,
        registry: Mapping[str, Any],
        *,
        action: str,
        actor: AdminContext,
        service_id: str,
        expected_generation: int,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        value = service_registry.normalize_service_registry(registry)
        record = admin_store.write_service_registry_with_audit(
            value,
            expected_generation=expected_generation,
            updated_by=actor.subject,
            audit_action=action,
            audit_actor_kind=actor.actor_kind,
            audit_actor_id=actor.audit_actor_id,
            audit_actor_label=actor.actor_label,
            audit_target_kind="service",
            audit_target_id=service_id,
            audit_metadata={
                "service_count": len(value.get("services") or {}), **(extra or {})
            },
            path=self.path,
        )
        self.write_litellm_config(record["value"])
        self._fire_registry_changed()
        return record

    def add_service(
        self, *, data: Mapping[str, Any], actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        service_id = service_registry.normalize_id(
            data.get("id") or data.get("name")
        )
        raw = service_registry.service_form_to_raw(data)
        current_registry = self.get_service_registry()
        if service_id not in current_registry.get("services", {}) and raw.get("models"):
            # Newly authored model mappings use the current schema. Existing
            # collections still require an explicit migration before routing.
            raw.setdefault("model_policy_schema_version", service_registry.MODEL_POLICY_SCHEMA_VERSION)
        registry, _record = service_registry.upsert_service_in_registry(
            current_registry,
            service_id=service_id,
            raw=raw,
            source="ui",
        )
        return self._persist_service_registry(
            registry,
            action="operator.service.added",
            actor=actor,
            service_id=service_id,
            expected_generation=expected_generation,
        )

    def update_service(
        self, *, service_id: str, data: Mapping[str, Any], actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        # Merge PATCH data onto the existing record so a partial update (e.g.
        # models-only) doesn't lose existing name, base_url, capabilities, etc.
        # We flatten the stored record into a form-like dict, then overlay the
        # caller-supplied data so explicit values always win.
        sid = service_registry.normalize_id(service_id)
        current_registry = self.get_service_registry()
        existing = (current_registry.get("services") or {}).get(sid)
        if existing is not None and not service_registry.is_litellm_service_record(
            sid, existing
        ):
            existing_config = existing.get("config") or {}
            # H1 (honesty law): start from ALL top-level record fields and EVERY
            # config key so that unmodeled/unknown fields are never silently
            # dropped by a partial PATCH.  The explicit caller-supplied data
            # overlays last so it always wins.
            #
            # Top-level record fields that belong in the form namespace:
            form_base: dict[str, Any] = {
                "name": existing.get("name") or sid,
                "enabled": existing.get("enabled"),
                "capabilities": existing.get("capabilities"),
                "role": existing.get("role"),
                "priority": existing.get("priority"),
            }
            # Every config key passes through — this is the fix for the
            # ComfyUI data-loss bug (workflows_dir, video_workflows_dir,
            # video_timeout, default_model, and any future protocol keys).
            for cfg_key, cfg_val in existing_config.items():
                if cfg_key not in form_base:
                    form_base[cfg_key] = cfg_val
            # Drop None values so they don't override defaults downstream, then
            # overlay the caller-supplied partial data.
            merged: dict[str, Any] = {
                k: v for k, v in form_base.items() if v is not None
            }
            merged.update(data)
        else:
            merged = dict(data)
        raw = service_registry.service_form_to_raw(merged)
        registry, _record = service_registry.upsert_service_in_registry(
            current_registry,
            service_id=service_id,
            raw=raw,
            source="ui",
            replace=True,
        )
        return self._persist_service_registry(
            registry,
            action="operator.service.updated",
            actor=actor,
            service_id=service_registry.normalize_id(service_id),
            expected_generation=expected_generation,
        )

    def remove_service(
        self, *, service_id: str, actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        registry = service_registry.remove_service_from_registry(
            self.get_service_registry(), service_id
        )
        return self._persist_service_registry(
            registry,
            action="operator.service.removed",
            actor=actor,
            service_id=service_registry.normalize_id(service_id),
            expected_generation=expected_generation,
        )

    def set_service_enabled(
        self, *, service_id: str, enabled: bool, actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        registry = service_registry.set_service_enabled(
            self.get_service_registry(), service_id, enabled
        )
        return self._persist_service_registry(
            registry,
            action="operator.service.toggled",
            actor=actor,
            service_id=service_registry.normalize_id(service_id),
            expected_generation=expected_generation,
            extra={"enabled": bool(enabled)},
        )

    def get_extension_package_index(self) -> dict[str, Any]:
        record = admin_store.get_operator_setting(
            EXTENSION_PACKAGE_INDEX_SETTING_KEY,
            path=self.path,
        )
        value = record["value"] if record is not None else {}
        return extension_packages.normalize_extension_package_index(value)

    def validate_extension_package_source(
        self, *, source: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        del actor
        return extension_packages.validate_extension_package_source(source)

    def enable_extension_package(
        self, *, source: Mapping[str, Any], actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        review = extension_packages.validate_extension_package_source(
            source,
            install_root=self.path.parent / "extensions" / "installed",
        )
        if not review.get("ok"):
            return {
                "extension_package": None,
                "review": review,
                "service_registry": self.get_service_registry(),
                "extension_packages": self.get_extension_package_index(),
            }

        imported = extension_packages.imported_registry_records(review)
        registry = service_registry.merge_imported_records(
            self.get_service_registry(),
            services=imported["services"],
            protocol_frontends=imported["protocol_frontends"],
            workflows=imported["workflows"],
            capabilities=imported["capabilities"],
        )
        registry_record = self.update_service_registry(
            data=registry,
            actor=actor,
            expected_generation=expected_generation,
        )
        package_index = extension_packages.merge_package_index(
            self.get_extension_package_index(),
            review,
            source=source,
        )
        index_record = admin_store.put_operator_setting(
            EXTENSION_PACKAGE_INDEX_SETTING_KEY,
            package_index,
            updated_by=actor.subject,
            path=self.path,
        )
        package_id = str((review.get("package") or {}).get("id") or "")
        admin_store.record_audit(
            "operator.extension_package.enable",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="extension_package",
            target_id=package_id,
            metadata={
                "service_count": len(imported["services"]),
                "protocol_frontend_count": len(imported["protocol_frontends"]),
                "workflow_count": len(imported["workflows"]),
                "capability_count": len(imported["capabilities"]),
                "package_hash": review.get("package_hash"),
            },
            path=self.path,
        )
        return {
            "extension_package": (index_record["value"].get("packages") or {}).get(package_id),
            "review": review,
            "service_registry": registry_record["value"],
            "operator_settings_generation": registry_record[
                "operator_settings_generation"
            ],
            "extension_packages": index_record["value"],
        }

    def remove_extension_package(
        self, *, package_id: str, actor: AdminContext
    ) -> dict[str, Any]:
        """Remove an extension package from the index by id."""
        try:
            package_index = extension_packages.remove_extension_package(
                self.get_extension_package_index(), package_id
            )
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        index_record = admin_store.put_operator_setting(
            EXTENSION_PACKAGE_INDEX_SETTING_KEY,
            package_index,
            updated_by=actor.subject,
            path=self.path,
        )
        admin_store.record_audit(
            "operator.extension_package.remove",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="extension_package",
            target_id=package_id,
            metadata={},
            path=self.path,
        )
        return {"extension_packages": index_record["value"]}

    def get_oidc_config_setting(self) -> dict[str, Any] | None:
        """Return the DB-stored OIDC provider config with secrets redacted.

        Returns ``None`` when no operator has saved an OIDC config yet —
        callers should fall back to bootstrap YAML/env in that case.
        """
        record = admin_store.get_operator_setting(
            OIDC_PROVIDER_SETTING_KEY,
            path=self.path,
        )
        if record is None:
            return None
        value = record.get("value")
        if not isinstance(value, Mapping):
            return None
        return _redact_oidc_setting(value, updated_at=record.get("updated_at"))

    def get_oidc_config_setting_raw(self) -> Mapping[str, Any] | None:
        """Return the raw DB-stored OIDC provider config (secret intact).

        Internal callers (e.g. the OIDC login flow) need the secret to call
        the token endpoint. UI/API surfaces must use
        :meth:`get_oidc_config_setting`.
        """
        record = admin_store.get_operator_setting(
            OIDC_PROVIDER_SETTING_KEY,
            path=self.path,
        )
        if record is None:
            return None
        value = record.get("value")
        if not isinstance(value, Mapping):
            return None
        return dict(value)

    def update_oidc_config_setting(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        """Persist the DB-stored OIDC provider config.

        Preserves the existing ``client_secret`` when the update omits it or
        passes an empty/redacted value. Unknown fields are rejected so a
        misspelled key never silently disables auth.
        """
        unknown = set(data.keys()) - set(OIDC_PROVIDER_FIELDS)
        if unknown:
            raise ValueError(
                f"unknown oidc_provider_config fields: {sorted(unknown)}"
            )
        current_raw = self.get_oidc_config_setting_raw() or {}
        merged = _merge_oidc_setting(current_raw, data)
        _validate_merged_oidc_setting(merged)
        if self.oidc_config_validator is not None:
            self.oidc_config_validator(merged)
        record = admin_store.put_operator_setting_with_audit(
            OIDC_PROVIDER_SETTING_KEY,
            merged,
            updated_by=actor.subject,
            audit_action="operator.oidc_provider_config.update",
            audit_actor_kind=actor.actor_kind,
            audit_actor_id=actor.audit_actor_id,
            audit_actor_label=actor.actor_label,
            audit_target_kind="operator_setting",
            audit_target_id=OIDC_PROVIDER_SETTING_KEY,
            audit_metadata={
                "fields": sorted(str(k) for k in data.keys()),
                "secret_changed": _oidc_secret_changed(current_raw, data),
            },
            path=self.path,
        )
        return _redact_oidc_setting(
            record["value"],
            updated_at=record.get("updated_at"),
        )

    def create_runtime_profile_mapping(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        source_kind = str(data.get("source_kind") or "").strip()
        source_value = str(data.get("source_value") or "").strip()
        profile_id = str(data.get("profile_id") or "").strip()
        if profile_id and not is_default_profile_id(profile_id) and admin_store.get_profile(
            profile_id,
            path=self.path,
        ) is None:
            raise ValueError(f"profile {profile_id!r} not found")
        record = admin_store.create_runtime_profile_mapping(
            source_kind=source_kind,
            source_value=source_value,
            profile_id=profile_id or default_profile_id(),
            created_by=actor.subject,
            path=self.path,
        )
        admin_store.record_audit(
            "runtime_profile_mapping.create",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="runtime_profile_mapping",
            target_id=record["id"],
            metadata={
                "source_kind": record.get("source_kind"),
                "source_value": record.get("source_value"),
                "profile_id": record.get("profile_id"),
            },
            path=self.path,
        )
        return record

    def disable_runtime_profile_mapping(
        self, *, mapping_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        before = next(
            (
                row
                for row in admin_store.list_runtime_profile_mappings(
                    include_disabled=True,
                    path=self.path,
                )
                if str(row.get("id") or "") == mapping_id
            ),
            None,
        )
        if before is None:
            return None
        admin_store.disable_runtime_profile_mapping(mapping_id, path=self.path)
        record = next(
            (
                row
                for row in admin_store.list_runtime_profile_mappings(
                    include_disabled=True,
                    path=self.path,
                )
                if str(row.get("id") or "") == mapping_id
            ),
            before,
        )
        admin_store.record_audit(
            "runtime_profile_mapping.disable",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="runtime_profile_mapping",
            target_id=mapping_id,
            metadata={"profile_id": before.get("profile_id")},
            path=self.path,
        )
        return record

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
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="credential",
            target_id=credential_id,
            metadata={"prefix": before.get("prefix")},
            path=self.path,
        )
        callback = self.on_credential_revoked
        if callback is not None:
            try:
                callback(before)
            except Exception as exc:  # noqa: BLE001 - revoking must not fail because cleanup did
                print(f"[admin_runtime] WARNING: on_credential_revoked callback raised: {exc}", flush=True)
        return _sanitize_credential(record or before)

    def recent_audit(self, *, limit: int) -> list[dict[str, Any]]:
        return [self._present_audit_event(row) for row in admin_store.list_audit(limit=limit, path=self.path)]

    def get_audit_event(self, event_id: str) -> dict[str, Any] | None:
        row = admin_store.get_audit_event(event_id, path=self.path)
        return self._present_audit_event(row) if row is not None else None

    def _present_audit_event(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Return the event-time actor label, with a legacy-row fallback."""
        out = dict(row)
        recorded_label = out.get("actor_label")
        if isinstance(recorded_label, str) and recorded_label.strip():
            out["actor_label"] = recorded_label.strip()[:256]
            return out
        kind = str(out.get("actor_kind") or "")
        actor_id = str(out.get("actor_id") or "")
        if kind == "user":
            principal = admin_store.get_user(actor_id, path=self.path) or {}
            out["actor_label"] = principal.get("display_name") or principal.get("email") or "Unknown user"
        elif kind == "service_account":
            principal = admin_store.get_service_account(actor_id, path=self.path) or {}
            out["actor_label"] = principal.get("name") or "Unknown service account"
        elif kind in {"system", "cli"}:
            out["actor_label"] = "System"
        else:
            # Legacy rows can retain their historical kind but are not
            # reclassified from their ID; this keeps the boundary truthful.
            out["actor_label"] = "Unknown principal"
        return out

    def list_audit_page(
        self,
        *,
        limit: int = 50,
        actor_id: str | None = None,
        action_prefix: str | None = None,
        exclude_access_audit: bool = False,
        before_created_at: int | None = None,
        before_id: str | None = None,
        target_kind: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        """Return one page of audit events with cursor-based pagination.

        Requests ``limit + 1`` rows from the store to cheaply detect whether
        more rows exist.  Returns ``next_cursor`` as ``"<created_at>:<id>"``
        when more rows follow, or ``null`` when the result is exhausted for the
        given filter set.
        """
        fetch_limit = int(limit) + 1
        rows = admin_store.list_audit(
            target_kind=target_kind,
            target_id=target_id,
            actor_id=actor_id,
            action_prefix=action_prefix,
            exclude_access_audit=exclude_access_audit,
            before_created_at=before_created_at,
            before_id=before_id,
            limit=fetch_limit,
            path=self.path,
        )
        has_more = len(rows) == fetch_limit
        page = rows[:limit]
        next_cursor: str | None = None
        if has_more and page:
            last = page[-1]
            next_cursor = f"{last['created_at']}:{last['id']}"
        return {
            "records": [self._present_audit_event(row) for row in page],
            "next_cursor": next_cursor,
        }

    def profile_for_id(self, profile_id: str):
        record = admin_store.get_profile(profile_id, path=self.path)
        if not record:
            return None
        try:
            return load_profile(record.get("profile"), default_id=record.get("id"))
        except ProfileLoadError:
            return None

    def dashboard_snapshot(self, actor: AdminContext) -> dict[str, Any]:
        users = self.list_users()
        apps = self.list_apps()
        profiles = self.list_profiles()
        runtime_mappings = self.list_runtime_profile_mappings()
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
                "runtime_profile_mappings": len(runtime_mappings),
                "credentials": len(credentials),
                "active_credentials": sum(1 for c in credentials if not c.get("revoked_at")),
            },
            "access_model": access_model_summary(),
            "users": users,
            "apps": apps,
            "profiles": profiles,
            "runtime_profile_mappings": runtime_mappings,
            "credentials": credentials,
            "audit": audit,
            "discovery_links": [
                {"label": "Account", "href": "/"},
                {"label": "Gateway metadata", "href": "/.well-known/azizam"},
                {
                    "label": "Agent skill",
                    "href": "/.well-known/skills/azizam-gateway/SKILL.md",
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
                    subject_kind, subject_id = session_admin_role_subject(
                        session.subject
                    )
                    roles = admin_store.list_admin_roles(
                        subject_kind=subject_kind,
                        subject_id=subject_id,
                        path=self.store.path,
                    )
                    role_names = frozenset(str(role["role"]) for role in roles)
                    if ADMIN_CONTROL_ROLES & role_names:
                        return AdminContext(
                            subject=session.subject,
                            email=session.email,
                            actor_kind=subject_kind,
                            actor_label=self._principal_label(subject_kind, subject_id, session.email),
                            actor_id=subject_id,
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
            actor_kind=str(credential["subject_kind"]),
            actor_label=self._principal_label(
                str(credential["subject_kind"]), str(credential["subject_id"]), None
            ),
            roles=role_names,
            auth_method="bearer",
        )

    def _principal_label(self, kind: str, subject_id: str, email: str | None) -> str | None:
        if kind == "user":
            user = admin_store.get_user(subject_id, path=self.store.path) or {}
            return str(user.get("display_name") or user.get("email") or email or "").strip() or None
        if kind == "service_account":
            service_account = admin_store.get_service_account(subject_id, path=self.store.path) or {}
            return str(service_account.get("name") or "").strip() or None
        return None


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

    Runtime ``opk_...`` keys live in the durable admin store. Runtime auth must
    accept only active App credentials from that store; user/service-account
    admin keys remain control-plane-only.
    """
    credential = admin_store.find_credential_by_hash(key_hash, path=path)
    if credential is None or not _credential_active(credential):
        return None
    if credential.get("subject_kind") != "app":
        return None

    # Record that this key was just used so the portal can show real "used N
    # ago" recency. Debounced in the store (at most one write per key per
    # minute) and best-effort: a recency write must never fail a live request.
    cred_id = credential.get("id")
    if cred_id:
        try:
            admin_store.touch_credential_last_used(str(cred_id), path=path)
        except Exception:
            pass

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
        app_id=app_id,
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
        actor_label="CLI",
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
    "EXTENSION_PACKAGE_INDEX_SETTING_KEY",
    "generate_admin_api_key",
    "generate_app_api_key",
    "OIDC_PROVIDER_FIELDS",
    "OIDC_PROVIDER_SETTING_KEY",
    "OIDC_SECRET_FIELDS",
    "USER_ACCESS_DEFAULTS_SETTING_KEY",
]
