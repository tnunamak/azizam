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

import copy
import ipaddress
from dataclasses import replace
import functools
import secrets
import json
import time
import sqlite3
from pathlib import Path
from collections.abc import Callable, Collection
from typing import Any, Iterable, Mapping

from fastapi import Request, status

from gateway import admin_store, browser_sessions, engine_check, step_up, extension_packages, projection, projection_types, record_carriers, record_integrity, route_registry, secret_commands, secret_refs, secret_store, service_registry, trusted_addresses
from gateway.admin_store import OIDC_CLIENT_SECRET_REF
from gateway.admin_api import ActorOutranked, AdminAuthError, AdminContext, AdminKeyNameTaken, access_model_summary
from gateway.admin_oidc import (
    SERVICE_ACCOUNT_SESSION_PREFIX,
    CookieConfig,
    OIDCConfigError,
    SessionError,
    csrf_token_for_session,
    load_oidc_config,
    validate_oidc_display_name,
)
from gateway.access_plans import PERSON, PlanError, plan_from_document, plan_summary
from gateway.policy import EffectiveAccess
from gateway.litellm_runtime import write_litellm_config_artifact  # a privileged write to a restricted file, never a response
from gateway.provider_admin import (
    provider_admin_setting_from_store,
    provider_admin_setting_to_store,
)
from gateway.runtime_auth import RuntimeIdentity, hash_api_key

APP_KEY_PREFIX = "opk_"
ADMIN_KEY_PREFIX = "oak_"
ADMIN_CONTROL_ROLES = frozenset({"owner", "admin", "superadmin"})
ADMIN_KEY_CREATABLE_ROLES = frozenset({"owner", "admin"})
_ROLE_RANK = {"admin": 1, "superadmin": 2, "owner": 3}
LOCAL_ADMIN_KEY_ISSUER = "local-admin-key"
SERVICE_AVAILABILITY_OVERRIDES_KEY = "service_availability_overrides"
MEDIA_JOB_ADMISSION_OVERRIDES_KEY = "media_job_admission_overrides"
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
    "authorization_endpoint",
    "token_endpoint",
    "jwks_uri",
    "clock_skew_seconds",
    "groups_claim",
    "display_name",
)

OIDC_SECRET_FIELDS: frozenset[str] = frozenset({"client_secret"})
OIDC_LIST_FIELDS: frozenset[str] = frozenset({"scopes"})
_OIDC_SECRET_REDACTED = "<redacted>"
# The actor of an audit row that names no account: a change made by a tool,
# not a person (Activity → Changes says "Automatic", never "Unknown user").
AUTOMATIC_ACTOR_LABEL = "Automatic"


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
    blocked = {"hash", "api_key", "key", "secret", "session_id"}
    out = {k: v for k, v in record.items() if k not in blocked}
    if "session_id" in record:
        # Name the binding, never the sign-in session it is bound to.
        out["session_bound"] = record["session_id"] is not None
    return out


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


def _plan_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """An access plan as the admin API returns it: document plus summary."""
    out = dict(record)
    try:
        out["summary"] = plan_summary(plan_from_document(record["plan"], plan_id=str(record["id"])))
    except PlanError:
        out["summary"] = None
    return out


def _redact_oidc_setting(
    value: Mapping[str, Any], *, updated_at: Any = None
) -> dict[str, Any]:
    """Describe a stored setting; the secret is only ever "set" or not."""
    out: dict[str, Any] = {}
    for key in OIDC_PROVIDER_FIELDS:
        if key == "client_secret":
            if OIDC_CLIENT_SECRET_REF not in value and key not in value:
                continue
            secret_set = bool(value.get(OIDC_CLIENT_SECRET_REF))
            out[key] = _OIDC_SECRET_REDACTED if secret_set else ""
            out[f"{key}_set"] = secret_set
        elif key in value:
            out[key] = value[key]
    if updated_at is not None:
        out["updated_at"] = updated_at
    return out


_OIDC_LIST_FIELDS = frozenset({"scopes"})


def _merge_oidc_setting(
    current: Mapping[str, Any], update: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge the non-secret fields; the stored secret reference is kept.

    The caller stores a new secret and replaces the reference itself. A stored
    empty ``client_secret`` is an explicit "no secret" override and is kept.
    """
    merged: dict[str, Any] = {}
    if current.get(OIDC_CLIENT_SECRET_REF):
        merged[OIDC_CLIENT_SECRET_REF] = current[OIDC_CLIENT_SECRET_REF]
    elif current.get("client_secret") == "":
        merged["client_secret"] = ""
    for key in OIDC_PROVIDER_FIELDS:
        if key in OIDC_SECRET_FIELDS:
            continue
        if key in update:
            new_value = update[key]
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


def _validate_merged_oidc_setting(value: Mapping[str, Any]) -> None:
    if "display_name" in value:
        validate_oidc_display_name(value["display_name"])
    if not value.get("enabled"):
        return
    for key in ("issuer", "client_id", "client_secret", "redirect_uri"):
        if not str(value.get(key) or "").strip():
            raise ValueError(f"{key} is required when enabled=true")


class RegistryNotApplied(RuntimeError):
    """The registry change is saved, but the running gateway could not apply it (a failed hot reload)."""

    def __init__(self, message: str, *, record: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.record = record


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
        self.on_credential_revoked: "Callable[[Mapping[str, Any]], bool | None] | None" = None
        # The live host supplies this so a candidate DB override is accepted
        # only when the same parser used by login accepts its effective merge.
        self.oidc_config_validator: "Callable[[Mapping[str, Any]], bool | None] | None" = None

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

    def _enrich_app(self, record: dict[str, Any], listing: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """Annotate an App with its assignment and accountable person.

        ``listing`` holds the owners, person plans and plans of a whole
        listing, read in one query each.
        """
        assignment = record.get("access_plan")
        owner_id = str(record.get("owner_user_id") or "")
        if listing is None:
            owner = admin_store.get_user(owner_id, path=self.path) if owner_id else None
        else:
            owner = listing["users"].get(owner_id)
        record["accountable_person"] = (
            {"id": owner_id, "name": owner.get("display_name") or owner.get("email")} if owner else None
        )
        if assignment != PERSON:
            plan_id = assignment
        elif listing is None:
            plan_id = admin_store.person_plan_id(owner_id, path=self.path)
        else:
            plan_id = listing["person_plans"].get(owner_id)
        if not plan_id:
            plan = None
        elif listing is None:
            plan = admin_store.get_access_plan(str(plan_id), path=self.path)
        else:
            plan = listing["plans"].get(str(plan_id))
        record["same_as_person"] = assignment == PERSON
        record["access_plan_name"] = plan.get("name") if plan else None
        record["effective_access_plan_id"] = plan.get("id") if plan else None
        return record

    def _visible_admin_app(self, record: Mapping[str, Any], owners: Mapping[str, Any] | None = None) -> bool:
        """True when an App belongs on the operator-facing Apps surface.

        ``owners`` holds the owners of a whole listing, read in one query.
        """
        if record.get("kind") != "personal":
            return True
        owner_user_id = str(record.get("owner_user_id") or "")
        if not owner_user_id:
            return True
        owner = admin_store.get_user(owner_user_id, path=self.path) if owners is None else owners.get(owner_user_id)
        return owner is None or is_human_user_record(owner)

    def list_apps(self) -> list[dict[str, Any]]:
        records = admin_store.list_apps(path=self.path)
        owner_ids = [str(r["owner_user_id"]) for r in records if r.get("owner_user_id")]
        listing = {
            "users": admin_store.get_users(owner_ids, path=self.path),
            "person_plans": admin_store.person_plan_ids(owner_ids, path=self.path),
            "plans": {str(plan["id"]): plan for plan in admin_store.list_access_plans(path=self.path)},
        }
        return [
            self._enrich_app(r, listing)
            for r in records
            if self._visible_admin_app(r, listing["users"])
        ]

    def get_app(self, app_id: str) -> dict[str, Any] | None:
        record = admin_store.get_app(app_id, path=self.path)
        return self._enrich_app(record) if record is not None else None

    @staticmethod
    def _require_owner_or_accountable(actor: AdminContext, accountable_user_id: str) -> None:
        """Only the owner may act for an App that someone else answers for.

        An administrator is not the owner: an App accountable to another person
        spends as that person, so an administrator may only create, re-plan or
        key an App that answers to themselves. A key has no person of its own
        and therefore gets no exception.
        """
        if "owner" in actor.roles:
            return
        if actor.actor_kind == "user" and accountable_user_id and actor.audit_actor_id == accountable_user_id:
            return
        raise PermissionError("Only the owner can do this for an App that someone else answers for.")

    def create_app(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        """Create an App with its accountable person and its access plan.

        ``access_plan`` is required: a plan id, or ``"person"`` for "Same as
        its person". The accountable person defaults to the signed-in person,
        or, for an owner key, to the gateway owner. Only the owner may name
        someone else; an administrator's App always answers to the
        administrator, so its spend stays inside the administrator's own plan.
        """
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("app name is required")
        kind = str(data.get("kind") or "service")
        owner_user_id = str(data.get("owner_user_id") or "").strip()
        if not owner_user_id and actor.actor_kind == "user":
            owner_user_id = actor.audit_actor_id
        if not owner_user_id:
            owner_user_id = admin_store.instance_owner_id(path=self.path) or ""
        if not owner_user_id:
            raise ValueError(
                "an app needs a person who answers for it: pass owner_user_id, or create the owner account first"
            )
        self._require_owner_or_accountable(actor, owner_user_id)
        if admin_store.get_user(owner_user_id, path=self.path) is None:
            raise ValueError(f"user {owner_user_id!r} not found")
        access_plan = str(data.get("access_plan") or "").strip()
        if not access_plan:
            raise ValueError("access_plan is required: an access plan id or \"person\"")
        record = admin_store.create_app(
            name,
            kind=kind,
            owner_user_id=owner_user_id,
            access_plan=access_plan,
            path=self.path,
        )
        admin_store.record_audit(
            "app.create",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="app",
            target_id=record["id"],
            metadata={"name": name, "kind": record.get("kind"), "access_plan": access_plan},
            path=self.path,
        )
        return self._enrich_app(record)

    def update_app(
        self, *, app_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None:
        before = admin_store.get_app(app_id, path=self.path)
        if before is None:
            return None
        if "access_plan" in data or "owner_user_id" in data:
            self._require_owner_or_accountable(actor, str(before.get("owner_user_id") or ""))
        self._require_actor_not_outranked(
            actor, self._app_holder_roles(before),
            "This App answers to the owner or to someone with more access than you, "
            "so only a person with at least their access can change it.")

        name = None
        if "name" in data:
            name = str(data.get("name") or "").strip()
            if not name:
                raise ValueError("app name is required")
        access_plan = None
        if "access_plan" in data:
            access_plan = str(data.get("access_plan") or "").strip()
            if not access_plan:
                raise ValueError("an app's access plan cannot be cleared; turn the app off instead")

        record = admin_store.update_app(
            app_id,
            name=name,
            access_plan=access_plan,
            path=self.path,
        )
        admin_store.record_audit(
            "app.update",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="app",
            target_id=app_id,
            metadata={"name": name, "access_plan": access_plan},
            path=self.path,
        )
        return self._enrich_app(record) if record is not None else None

    def disable_app(
        self, *, app_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        before = admin_store.get_app(app_id, path=self.path)
        if before is None:
            return None
        self._require_actor_not_outranked(
            actor, self._app_holder_roles(before),
            "This App answers to the owner or to someone with more access than you, "
            "so only a person with at least their access can disable it.")
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
        self._require_actor_not_outranked(
            actor, self._app_holder_roles(before),
            "This App answers to the owner or to someone with more access than you, "
            "so only a person with at least their access can turn it back on.")
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

    def list_access_plans(self) -> list[dict[str, Any]]:
        out = []
        for record in admin_store.list_access_plans(path=self.path):
            plan = _plan_record(record)
            plan["used_by"] = self.access_plan_users(record["id"])
            out.append(plan)
        return out

    def access_plan_users(self, plan_id: str) -> dict[str, Any]:
        with admin_store._conn(self.path) as conn:
            return admin_store.access_plan_users(conn, plan_id)

    def get_access_plan(self, plan_id: str) -> dict[str, Any] | None:
        record = admin_store.get_access_plan(plan_id, path=self.path)
        return _plan_record(record) if record is not None else None

    def _audit_plan(self, action: str, actor: AdminContext, plan_id: str, name: Any) -> None:
        admin_store.record_audit(
            action,
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="access_plan",
            target_id=plan_id,
            metadata={"name": name},
            path=self.path,
        )

    def create_access_plan(self, *, data: Mapping[str, Any], actor: AdminContext) -> dict[str, Any]:
        try:
            record = admin_store.create_access_plan(data, path=self.path)
        except PlanError as exc:
            raise ValueError(str(exc)) from exc
        self._audit_plan("access_plan.create", actor, record["id"], record["name"])
        return _plan_record(record)

    def update_access_plan(
        self, *, plan_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None:
        try:
            record = admin_store.update_access_plan(plan_id, data, path=self.path)
        except PlanError as exc:
            raise ValueError(str(exc)) from exc
        if record is None:
            return None
        self._audit_plan("access_plan.update", actor, plan_id, record["name"])
        return _plan_record(record)

    def delete_access_plan(self, *, plan_id: str, actor: AdminContext) -> dict[str, Any] | None:
        record = admin_store.delete_access_plan(plan_id, path=self.path)
        if record is None:
            return None
        self._audit_plan("access_plan.delete", actor, plan_id, record.get("name"))
        return _plan_record(record)

    def list_trusted_addresses(self, *, app_id: str) -> list[dict[str, Any]] | None:
        if admin_store.get_app(app_id, path=self.path) is None:
            return None
        return admin_store.list_trusted_addresses(app_id, path=self.path)

    def add_trusted_address(
        self, *, app_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None:
        """Trust a client address or range for one App. ValueError carries a sentence for the owner."""
        if admin_store.get_app(app_id, path=self.path) is None:
            return None
        try:
            address = trusted_addresses.normalize_entry(data.get("address"))
            record = admin_store.add_trusted_address(
                app_id, address, note=str(data.get("note") or "")[:200],
                created_by=actor.audit_actor_id, max_entries=trusted_addresses.MAX_ENTRIES_PER_APP,
                path=self.path,
            )
        except LookupError:
            return None
        admin_store.record_audit(
            "app.trusted_address.add",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="app",
            target_id=app_id,
            metadata={"address": address, "entry_id": record["id"]},
            path=self.path,
        )
        return record

    def delete_trusted_address(
        self, *, app_id: str, entry_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        app = admin_store.get_app(app_id, path=self.path)
        if app is not None:
            self._require_actor_not_outranked(
                actor, self._app_holder_roles(app),
                "This App answers to the owner or to someone with more access than you, "
                "so only a person with at least their access can remove its trusted addresses.")
        record = admin_store.delete_trusted_address(app_id, entry_id, path=self.path)
        if record is None:
            return None
        admin_store.record_audit(
            "app.trusted_address.remove",
            actor_kind=actor.actor_kind,
            actor_id=actor.audit_actor_id,
            actor_label=actor.actor_label,
            target_kind="app",
            target_id=app_id,
            metadata={"address": record["address"], "entry_id": entry_id},
            path=self.path,
        )
        return record

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
        self._require_owner_or_accountable(actor, str(app.get("owner_user_id") or ""))
        api_key = generate_app_api_key()
        record = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=app_id,
            hash=hash_api_key(api_key),
            prefix=_key_prefix(api_key),
            display_name=str(data.get("label") or data.get("name") or "API key"),
            scopes=data.get("scopes") if isinstance(data.get("scopes"), list) else None,
            created_by=actor.audit_actor_id if actor.actor_kind == "user" else "",
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
        out["prompt_memory"] = app_key_prompt_memory(app, record)
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
        if "owner" not in actor.roles and role not in actor.roles:
            raise PermissionError("owner role required to mint owner keys")
        # A key always gets a service account of its own. Reusing one by name
        # would let a caller take over a key that someone else minted.
        try:
            service_account = admin_store.create_service_account(
                name, description="Owner for an admin automation key created from the dashboard.",
                path=self.path,
            )
        except sqlite3.IntegrityError:
            raise AdminKeyNameTaken(f"a key named {name!r} already exists; choose another name") from None
        minter = f"{actor.actor_kind}:{actor.audit_actor_id}"
        assignment = admin_store.assign_admin_role(
            role=role,
            subject_kind="service_account",
            subject_id=service_account["id"],
            granted_by=actor.audit_actor_id,
            actor=actor,
            path=self.path,
        )
        api_key = generate_admin_api_key()
        credential = admin_store.create_credential_hash(
            subject_kind="service_account",
            subject_id=service_account["id"],
            hash=hash_api_key(api_key),
            prefix=_key_prefix(api_key),
            display_name=f"{name} admin automation key",
            created_by=minter,
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

    def setting_revision(self, key: str) -> str:
        return admin_store.operator_setting_revision(
            admin_store.get_operator_setting(key, path=self.path)
        )

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
            "service_availability": (
                service_setting["value"] if service_setting is not None else {}
            ),
            "media_job_admission": (
                media_setting["value"] if media_setting is not None else {}
            ),
            "extension_package_index": self.get_extension_package_index(),
            "revisions": {MEDIA_JOB_ADMISSION_OVERRIDES_KEY: admin_store.operator_setting_revision(media_setting)},
        }

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
        self, *, data: Mapping[str, Any], actor: AdminContext,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        value = _copy_override_value(data)
        record = admin_store.put_operator_setting_with_audit(
            MEDIA_JOB_ADMISSION_OVERRIDES_KEY, value,
            updated_by=actor.subject, expected_revision=expected_revision,
            audit_action="operator.media_job_admission.update",
            audit_actor_kind=actor.actor_kind, audit_actor_id=actor.audit_actor_id,
            audit_actor_label=actor.actor_label,
            audit_target_kind="operator_setting",
            audit_target_id=MEDIA_JOB_ADMISSION_OVERRIDES_KEY,
            audit_metadata={"fields": sorted(value)}, path=self.path,
        )
        return {**record["value"], "revision": admin_store.operator_setting_revision(record)}

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

    def get_service_registry_snapshot(self) -> tuple[dict[str, Any], int]:
        """One registry read binds the engine view and its compare-and-swap revision."""
        snapshot = admin_store.get_service_registry_with_generation(path=self.path)
        if snapshot is None:
            return service_registry.read_registry({})[0], 0
        registry, _excluded = service_registry.read_registry(snapshot["value"])
        return registry, int(snapshot["operator_settings_generation"])

    def get_service_registry_exclusions(self) -> list[service_registry.ExcludedRecord]:
        """The stored records ``get_service_registry`` leaves out because they do not validate, each with why."""
        return service_registry.load_service_registry_exclusions(path=self.path)

    def _excluded_service(self, service_id: str) -> service_registry.ExcludedRecord | None:
        wanted = service_registry.normalize_id(service_id)
        return next((item for item in self.get_service_registry_exclusions()
                     if item.section == "services" and item.id == wanted), None)

    def _excluded_record(self, section: str, record_id: str) -> service_registry.ExcludedRecord | None:
        wanted = service_registry.normalize_id(record_id)
        return next((item for item in self.get_service_registry_exclusions()
                     if item.section == section and item.id == wanted), None)

    def replace_excluded_record(
        self, *, section: str, record_id: str, record: Any, actor: AdminContext, expected_generation: int,
    ) -> dict[str, Any]:
        """Fix a record the registry leaves out by saving an edited copy of what a read shows.

        A carrier of the stored record that holds secret entries must come back as read (or be left out) and is
        kept; inline input becomes new entries, which only the owner may add. A record that could not be parsed
        is replaced whole, and the entries it held are deleted. The result must validate with the rest of the
        registry, or the save is refused with the reason and nothing is stored.
        """
        held = self._excluded_record(section, record_id)
        if held is None:
            raise ValueError(f"{dict(service_registry._SECTIONS).get(section, section)} {record_id!r} not found among the records left out")
        created: list[str] = []
        if section == "services" and isinstance(record, Mapping):
            # A record that could not be parsed holds no entries a write must keep: it is replaced whole.
            stored = held.record if projection_types.record_parses(section, held.record) else None
            try:
                record = copy.deepcopy(dict(record))
                record_carriers.restore_echoed_record(record, stored)
            except record_carriers.EchoRefused as error:
                raise ValueError(str(error)) from None
            probe = lambda value, origin=None: secret_refs.new_ref()  # noqa: E731 - a dry run: nothing is stored
            try:
                _, moved = record_carriers.ingest_service(record, stored, probe)
            except record_carriers.CarrierLocked as error:
                raise ValueError(str(error)) from None
            if moved and "owner" not in actor.roles:
                raise PermissionError("Only the owner can add a secret.")

            def put(value: str, origin: str | None = None) -> str:
                ref = secret_refs.store(value, db_path=self.path, origin=origin)
                created.append(ref)
                return ref

            record, _ = record_carriers.ingest_service(record, stored, put)
        try:
            if section == "services" and isinstance(record, Mapping) and service_registry.is_litellm_service_record(held.id, record):
                raise ValueError(f"service {held.id!r} is a managed substrate; remove it and the gateway restores its own")
            candidate = copy.deepcopy(self.get_service_registry())
            candidate[section] = {**(candidate.get(section) or {}), held.key: record}
            try:
                registry = service_registry.normalize_service_registry(candidate)
            except ValueError as error:
                raise ValueError(projection.scrub_text(str(error)[:600], record_carriers.legacy_secret_texts(record))) from None
            saved = self._persist_service_registry(
                registry,
                action="operator.service.updated",
                actor=actor,
                service_id=held.id,
                expected_generation=expected_generation,
                extra={"left_out_fixed": section},
                removing={(section, held.id)},
                ingest=False,
            )
        except RegistryNotApplied:
            self._discard_secrets(sorted(set(record_carriers.refs_in(held.record)) - set(record_carriers.refs_in(record))))
            raise
        except BaseException:
            self._discard_secrets(created)
            raise
        self._discard_secrets(sorted(set(record_carriers.refs_in(held.record)) - set(record_carriers.refs_in(record))))
        return saved

    def remove_excluded_record(
        self, *, section: str, record_id: str, actor: AdminContext, expected_generation: int,
    ) -> dict[str, Any]:
        """Drop a record the registry leaves out, and the secret entries it held. Nothing else changes."""
        held = self._excluded_record(section, record_id)
        if held is None:
            raise ValueError(f"{dict(service_registry._SECTIONS).get(section, section)} {record_id!r} not found among the records left out")
        try:
            saved = self._persist_service_registry(
                self.get_service_registry(),
                action="operator.service.removed",
                actor=actor,
                service_id=held.id,
                expected_generation=expected_generation,
                extra={"left_out_removed": section},
                removing={(section, held.id)},
                ingest=False,
            )
        except RegistryNotApplied:
            self._discard_secrets(record_carriers.refs_in(held.record))
            raise
        self._discard_secrets(record_carriers.refs_in(held.record))
        return saved

    def _with_excluded_records(
        self, value: Mapping[str, Any], *, removing: Collection[tuple[str, str]] = (),
    ) -> tuple[dict[str, Any], list[service_registry.ExcludedRecord]]:
        """The document to store for ``value``, and the excluded records it carries along.

        ``value`` holds the records that validate. A stored record that does not is neither fixed nor
        removed by a save that does not name it, so it goes back into the document exactly as it was
        stored. It is dropped only when the caller removes it (``removing``: section and id) or replaces
        it with a record of the same id.
        """
        document = dict(value)
        kept: list[service_registry.ExcludedRecord] = []
        for item in self.get_service_registry_exclusions():
            if (item.section, item.id) in removing:
                continue
            section = dict(document.get(item.section) or {})
            if any(service_registry.normalize_id((row.get("id") if isinstance(row, Mapping) else None) or key) == item.id
                   for key, row in section.items()):
                continue  # the caller replaced it with a record that validates
            if item.key in section:
                raise ValueError(
                    f"{dict(service_registry._SECTIONS)[item.section]} {item.id!r} is left out of the registry and stored under "
                    f"the key {item.key!r}, which this save uses; fix or remove {item.id!r} first")
            section[item.key] = item.record
            document[item.section] = section
            kept.append(item)
        return document, kept

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

    def _settle_credential_bindings(
        self, registry: Mapping[str, Any], actor: AdminContext, *, _trusted_engine_evidence: bool = False
    ) -> dict[str, Any]:
        """Normalize a registry about to be written; keep credential changes for the owner.

        Administrators may add keyless external services and edit metadata.
        Existing connection, offering and lifecycle settings belong to the
        owner, whether or not a key is attached. The owner also binds each key
        to the origin that may receive it.
        """
        before = self.get_service_registry()
        service_registry.validate_protocol_dispatch_registry(registry, before=before)
        value = service_registry.normalize_service_registry(registry)
        if "owner" not in actor.roles:
            old_services = before.get("services") or {}
            for service_id, record in (value.get("services") or {}).items():
                previous = old_services.get(service_id) or {}
                if not _trusted_engine_evidence:
                    # Verification facts come from EngineService, never raw caller data.
                    for field in ("engine_check", "engine_tests"):
                        old_evidence = (previous.get(field) or None, (previous.get("config") or {}).get(field) or None)
                        new_evidence = (record.get(field) or None, (record.get("config") or {}).get(field) or None)
                        if old_evidence != new_evidence:
                            raise PermissionError("Only the engine verification routes can change saved check or test evidence.")
                if previous:
                    old_config, new_config = dict(previous.get("config") or {}), dict(record.get("config") or {})
                    # The legacy PATCH form fills model_group from id. That
                    # representation change grants no new offering or route.
                    for config_values, service in ((old_config, previous), (new_config, record)):
                        if isinstance(config_values.get("models"), (list, tuple)):
                            config_values["models"] = service_registry._form_models_to_config(config_values["models"])
                        config_values["lifecycle"] = service_registry.legacy_lifecycle_from_service(service)
                    defaults = {"base_path": "/v1", "scheme": "http", "timeout": 600,
                                "headers": [], "models": [], "transforms": [], "role": "required", "managed": False}
                    # Compare settings that grant connection, routing or host control.
                    # Metadata may be written by administrators; evidence needs the trusted route.
                    keys = {
                        "host", "port", "scheme", "base_path", "base_url", "api_base", "default_base_url",
                        "base_url_env", "api_base_env", "address", "path_prefix", "protocol", "native_passthrough",
                        "timeout", "headers", "models", "transforms", "lifecycle", "managed", "role",
                        "gateway_lifecycle", "gateway_lifecycle_base_url", "probe_with_inference_connection",
                        "health_endpoint", "api_key", "api_key_env", "api_key_ref", "api_key_secret_id", "credential_origin",
                        "litellm_provider", "provider_adapter",
                    }
                    missing_timeout = object()
                    if (old_config.get("timeout", missing_timeout) != new_config.get("timeout", missing_timeout)
                            or any((old_config.get(key) or defaults.get(key)) != (new_config.get(key) or defaults.get(key))
                                   for key in keys if key != "timeout")):
                        raise PermissionError("Only the owner can change service settings.")
                    for field in ("capabilities", "priority", "role", "kind", "resources", "secret_refs"):
                        if previous.get(field) != record.get(field):
                            raise PermissionError(f"Only the owner can change service {field}.")
                for field in ("connection", "process", "protocol_operations", "control_file_roots",
                              "offerings", "residency", "observations", "credentials"):
                    # Migration writes empty blocks; an empty block grants nothing.
                    old_value = (previous.get(field) or None, (previous.get("config") or {}).get(field) or None)
                    new_value = (record.get(field) or None, (record.get("config") or {}).get(field) or None)
                    if field == "process" and not previous:
                        # Migration turns an external lifecycle into a process that
                        # runs nothing; lifecycle_driver_changed below judges whether
                        # a top-level process controls the host.
                        old_value, new_value = old_value[1], new_value[1]
                    if old_value != new_value:
                        raise PermissionError(f"Only the owner can change service {field}.")
        route_registry.refuse_unmountable_frontends(before, value)
        changed = service_registry.credential_binding_changes(before, value)
        if changed:
            if "owner" not in actor.roles:
                raise PermissionError(
                    "Only the owner can attach a key to a service, move a service that has one, "
                    "or set a lifecycle driver on a service.")
            # Where a stored key is sent is the thing a stolen session must not move (decision D36).
            step_up.require_recent(
                authenticated_at=actor.authenticated_at, subject=actor.subject,
                window_seconds=browser_sessions.policy(self.path).step_up_seconds,
            )
            value = service_registry.bind_credential_origins(value, changed)
        self._refuse_blocked_native_hosts(before, value)
        return value

    @staticmethod
    def _refuse_blocked_native_hosts(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
        """Look up the host of each engine that opens or moves its native lane, and refuse one that resolves somewhere forbidden.

        The address as written was checked when the record was read (service_registry). A name is
        looked up here, once at save; the lane checks it again on every call.
        """
        old_services = before.get("services") if isinstance(before.get("services"), Mapping) else {}
        for service_id, record in (after.get("services") or {}).items():
            config = record.get("config") if isinstance(record, Mapping) else None
            if not isinstance(config, Mapping) or not service_registry.bool_from_config(config.get("native_passthrough"), False):
                continue
            url = service_registry.native_host_url(config)
            old = (old_services.get(service_id) or {}).get("config") or {}
            if (
                service_registry.bool_from_config(old.get("native_passthrough"), False)
                and service_registry.native_host_url(old) == url
            ):
                continue
            reason = engine_check.blocked_reason_blocking(url) if url else None
            if reason:
                raise ValueError(f"service {service_id!r}: native pass-through cannot reach its host. {reason}")

    def _ingest_carriers(self, registry: Mapping[str, Any], actor: AdminContext) -> tuple[dict[str, Any], list[str], list[str]]:
        """The registry to store for a write, the references it created, and the ones it no longer holds (decision D27).

        A carrier that holds secret entries is kept exactly (a write must send it back as read or leave it out);
        inline input (a header value, an environment value, a command argument after a credential flag, URL user
        info) becomes a new secret entry, and only the owner may add one. The caller deletes ``created`` when the
        write fails and ``retired`` once it has committed.
        """
        stored = self.get_service_registry()
        stored_services = stored.get("services") or {}
        out = copy.deepcopy(dict(registry))
        services = out.get("services") if isinstance(out.get("services"), dict) else {}
        probe = lambda value, origin=None: secret_refs.new_ref()  # noqa: E731 - a dry run: nothing is stored
        for service_id, record in services.items():
            try:
                _, moved = record_carriers.ingest_service(record, stored_services.get(service_id), probe)
            except record_carriers.CarrierLocked as error:
                raise ValueError(str(error)) from None
            if moved and "owner" not in actor.roles:
                raise PermissionError("Only the owner can add a secret.")
        created: list[str] = []

        def put(value: str, origin: str | None = None) -> str:
            ref = secret_refs.store(value, db_path=self.path, origin=origin)
            created.append(ref)
            return ref

        try:
            for service_id, record in list(services.items()):
                services[service_id], _ = record_carriers.ingest_service(record, stored_services.get(service_id), put)
        except BaseException:
            self._discard_secrets(created)
            raise
        retired = sorted(set(record_carriers.refs_in(stored_services)) - set(record_carriers.refs_in(services)))
        return out, created, retired

    def _discard_secrets(self, refs: Iterable[str]) -> None:
        for ref in refs:
            try:
                secret_refs.delete(ref, db_path=self.path)
            except Exception:  # noqa: BLE001 - an orphan is unreadable and harmless; the write's own result stands
                pass

    def update_service_registry(
        self, *, data: Mapping[str, Any], actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        data, created, retired = self._ingest_carriers(data, actor)
        try:
            value = self._settle_credential_bindings(data, actor)
        except BaseException:
            self._discard_secrets(created)
            raise
        document, excluded = self._with_excluded_records(value)
        try:
            record = self._write_registry_for_update(document, value, actor, expected_generation)
        except BaseException:
            self._discard_secrets(created)
            raise
        self._discard_secrets(retired)
        record = {**record, "value": value, "excluded": excluded}
        self.write_litellm_config(value)
        self._fire_registry_changed(record)
        return record

    def _write_registry_for_update(self, document: Mapping[str, Any], value: Mapping[str, Any], actor: AdminContext,
                                   expected_generation: int) -> dict[str, Any]:
        record = admin_store.write_service_registry_with_audit(
            document,
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
        return record

    # -- single-service CRUD -------------------------------------------------
    # Granular operator edits on one backend service. Each persists the new
    # registry, writes an audit row, and rewrites the LiteLLM substrate config
    # (so the execution layer stays in sync) — the same durable effects as the
    # full-registry update, scoped to one service. Changes take effect
    # immediately via the on_service_registry_changed callback (hot reload);
    # the LiteLLM subprocess still requires a separate restart.

    def _fire_registry_changed(self, record: Mapping[str, Any]) -> None:
        """Invoke the registered hot-reload callback, if any. A reload that fails is reported to the caller.

        The registry is already saved by now, so the error says so: the change is stored, and the
        running gateway does not have it yet.
        """
        cb = self.on_service_registry_changed
        if cb is not None:
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[admin_runtime] WARNING: on_service_registry_changed callback "
                    f"raised {type(exc).__name__} (hot-reload did not take effect)",
                    flush=True,
                )
                raise RegistryNotApplied(str(exc) or type(exc).__name__, record=record) from exc

    def _persist_service_registry(
        self,
        registry: Mapping[str, Any],
        *,
        action: str,
        actor: AdminContext,
        service_id: str,
        expected_generation: int,
        extra: Mapping[str, Any] | None = None,
        removing: Collection[tuple[str, str]] = (),
        ingest: bool = True,
        _trusted_engine_evidence: bool = False,
    ) -> dict[str, Any]:
        created: list[str] = []
        retired: list[str] = []
        if ingest:
            # Carriers first: what a read showed is put back as stored before the rules that compare with the stored record.
            registry, created, retired = self._ingest_carriers(registry, actor)
        try:
            value = self._settle_credential_bindings(registry, actor, _trusted_engine_evidence=_trusted_engine_evidence)
        except BaseException:
            self._discard_secrets(created)
            raise
        document, excluded = self._with_excluded_records(value, removing=removing)
        try:
            record = self._write_registry_for_persist(document, value, action, actor, service_id, expected_generation, extra)
        except BaseException:
            self._discard_secrets(created)
            raise
        self._discard_secrets(retired)
        # The returned record is what the gateway uses: the records that validate. The excluded ones are listed beside it.
        record = {**record, "value": value, "excluded": excluded}
        self.write_litellm_config(value)
        self._fire_registry_changed(record)
        fresh = admin_store.get_service_registry_with_generation(path=self.path)
        if fresh and fresh["operator_settings_generation"] != record["operator_settings_generation"]:
            fresh_registry, fresh_excluded = service_registry.read_registry(fresh["value"])
            record = {**fresh, "value": fresh_registry, "excluded": fresh_excluded}
        return record

    def _write_registry_for_persist(self, document: Mapping[str, Any], value: Mapping[str, Any], action: str, actor: AdminContext,
                                    service_id: str, expected_generation: int, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        record = admin_store.write_service_registry_with_audit(
            document,
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
        return record

    def add_service(
        self, *, data: Mapping[str, Any], actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        service_id = service_registry.normalize_id(
            data.get("id") or data.get("name")
        )
        raw = service_registry.service_form_to_raw(data)
        self._refuse_excluded_id(service_id)
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

    def _refuse_excluded_id(self, service_id: str) -> None:
        """Adding a service under the id of one that is left out would replace it without the operator saying so."""
        held = self._excluded_service(service_id)
        if held is not None:
            raise ValueError(
                f"service {held.id!r} already exists but is left out of the registry ({held.error}); "
                "fix it or remove it first")

    def write_engine(
        self, *, service_id: str, raw: Mapping[str, Any], actor: AdminContext,
        expected_generation: int, replace: bool, existing: Mapping[str, Any] | None = None,
        drop_inference_secret_ref: bool = False,
    ) -> dict[str, Any]:
        """Create or replace one service from an engine definition's record (gateway/engine_admin.py).

        ``raw`` is the whole record, already built and checked; the credential and
        lifecycle rules for administrators are applied here as for every write.
        """
        if not replace:
            self._refuse_excluded_id(service_id)
        registry, record = service_registry.upsert_service_in_registry(
            self.get_service_registry(), service_id=service_id, raw=raw, source="ui", replace=replace,
        )
        if isinstance(raw.get("lifecycle"), Mapping):
            record["process"] = service_registry._v2_process(raw["lifecycle"])
        if existing is not None:
            # An edit through the engine page names the engine's own settings. Everything else the record holds (its
            # process and observations, its residency and resources, its secret references, its kind and provenance)
            # stays as stored, secret entries included.
            named = {key: True for key in raw if key in ("process", "observations", "residency", "resources")}
            if "lifecycle" in raw and raw["lifecycle"] != dict(service_registry.legacy_lifecycle_from_service(existing)):
                named["lifecycle"] = True  # a lifecycle the page changed; one it sent back as stored is not a change
            service_registry.carry_unnamed_record_fields(existing, record, named=named)
            if drop_inference_secret_ref:
                record.get("secret_refs", {}).pop("api_key", None)
        return self._persist_service_registry(
            registry,
            action="operator.service.updated" if replace else "operator.service.added",
            actor=actor,
            service_id=service_id,
            expected_generation=expected_generation,
            extra={"definition": str(raw.get("definition") or "")},
            _trusted_engine_evidence=True,
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
        left_out = self._excluded_service(sid) if existing is None else None
        if left_out is not None and isinstance(left_out.record, Mapping):
            # The way to fix a record that is left out: edit it, starting from what is stored.
            existing = left_out.record
        metadata_fields = {"name", "enabled", "disabled", "note", "acknowledge_warnings"}
        if existing is not None and "owner" not in actor.roles and set(data) - metadata_fields:
            raise PermissionError("Only the owner can change service settings.")
        if existing is not None and set(data) <= metadata_fields:
            # A metadata save must not rebuild a connection through a legacy
            # form, which can discard URL aliases or custom protocol paths.
            registry = copy.deepcopy(current_registry)
            record = registry["services"][sid]
            for field, value in data.items():
                if field == "acknowledge_warnings":
                    continue
                if field == "disabled":
                    disabled = service_registry.bool_from_config(value, False)
                    record["enabled"] = not disabled
                    if "enabled" in record.get("config", {}):
                        record["config"]["enabled"] = not disabled
                    if "disabled" in record.get("config", {}):
                        record["config"]["disabled"] = disabled
                    continue
                if field == "enabled":
                    value = service_registry.bool_from_config(value, True)
                else:
                    value = str(value).strip()
                if field != "note":
                    record[field] = value
                if field == "note" or field in record.get("config", {}):
                    record.setdefault("config", {})[field] = value
            return self._persist_service_registry(
                registry, action="operator.service.updated", actor=actor,
                service_id=sid, expected_generation=expected_generation,
            )
        if existing is not None and not service_registry.is_litellm_service_record(
            sid, existing
        ):
            existing_config = existing.get("config") if isinstance(existing.get("config"), Mapping) else {}
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
            # A lifecycle driver kept only on the record (not in its config) must
            # survive a partial edit: dropping it would count as removing a driver,
            # which only the owner may do.
            existing_lifecycle = service_registry.legacy_lifecycle_from_service(existing)
            if "lifecycle" not in form_base and existing_lifecycle:
                kept_lifecycle = copy.deepcopy(dict(existing_lifecycle))
                for form_key, block_key in service_registry._LIFECYCLE_FORM_KEYS.items():
                    if form_key in data:
                        kept_lifecycle[block_key] = data[form_key]
                form_base["lifecycle"] = kept_lifecycle
            # Drop None values so they don't override defaults downstream, then
            # overlay the caller-supplied partial data.
            merged: dict[str, Any] = {
                k: v for k, v in form_base.items() if v is not None
            }
            merged.update(data)
        else:
            merged = dict(data)
        raw = service_registry.service_form_to_raw(merged)
        registry, record = service_registry.upsert_service_in_registry(
            current_registry,
            service_id=service_id,
            raw=raw,
            source="ui",
            replace=left_out is None,  # a record that is left out is not in the registry yet: this edit adds it back
        )
        if existing is not None and not service_registry.is_litellm_service_record(sid, existing):
            service_registry.carry_unnamed_record_fields(existing, record, named=data)
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
        sid = service_registry.normalize_id(service_id)
        current = self.get_service_registry()
        if sid not in (current.get("services") or {}) and self._excluded_service(sid) is not None:
            # A service that is left out has nothing in the registry to remove; the save drops the stored record.
            registry, removing = current, {("services", sid)}
        else:
            registry, removing = service_registry.remove_service_from_registry(current, service_id), set()
        return self._persist_service_registry(
            registry,
            action="operator.service.removed",
            actor=actor,
            service_id=sid,
            expected_generation=expected_generation,
            removing=removing,
        )

    def apply_secret_commands(
        self, *, service_id: str, commands: list[dict[str, Any]], actor: AdminContext, expected_generation: int,
    ) -> dict[str, Any]:
        """Change the secret entries of one service by explicit commands (decision D27).

        Only the owner changes a secret. The new secrets are stored first, the record is saved with the registry
        generation check, and the secrets this call replaced or cleared are deleted after the save; a failure
        deletes the ones this call stored. ``retain`` commands only check that the entry exists.
        """
        sid = service_registry.normalize_id(service_id)
        if any(command["operation"] != "retain" for command in commands) and "owner" not in actor.roles:
            raise PermissionError("Only the owner can change a secret.")
        with secret_store.provider_mutation_lock(self.path):
            snapshot = admin_store.get_service_registry_with_generation(path=self.path)
            generation = int(snapshot["operator_settings_generation"]) if snapshot else 0
            if generation != expected_generation:
                raise admin_store.OperatorSettingsGenerationConflict("the service registry changed")
            registry = service_registry.read_registry(snapshot["value"])[0] if snapshot else self.get_service_registry()
            record = (registry.get("services") or {}).get(sid)
            if record is None:
                raise ValueError(f"service {sid!r} not found")
            put = secret_refs.putter(self.path)
            try:
                new_record, outcome = secret_commands.apply_commands(record, commands, put)
            except secret_commands.CommandError as error:
                for ref in getattr(error, "created", ()):
                    secret_refs.delete(ref, db_path=self.path)
                raise
            if not outcome.changed:
                return {**(snapshot or {}), "value": registry,
                        "excluded": self.get_service_registry_exclusions()}
            registry["services"][sid] = new_record
            try:
                saved = self._persist_service_registry(
                    registry, action="operator.service.secret_entries", actor=actor, service_id=sid,
                    expected_generation=expected_generation, ingest=False,
                    extra={"entries_added": len(outcome.created), "entries_retired": len(outcome.retired)})
            except BaseException as error:
                if not isinstance(error, RegistryNotApplied):
                    for ref in outcome.created:
                        secret_refs.delete(ref, db_path=self.path)
                    raise
                saved = None
                pending = error
            for ref in outcome.retired:
                secret_refs.delete(ref, db_path=self.path)
            if saved is None:
                raise pending
            return saved

    def set_service_enabled(
        self, *, service_id: str, enabled: bool, actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        if service_registry.normalize_id(service_id) not in (self.get_service_registry().get("services") or {}):
            held = self._excluded_service(service_id)
            if held is not None:
                raise ValueError(f"service {held.id!r} is left out of the registry ({held.error}); fix it or remove it first")
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

    @staticmethod
    def _split_package_source(source: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """A package source with the credentials of its URLs taken out: the public source, and the credential parts by key.

        The parts live in memory for the one clone that needs them; ``enable`` stores them as entries. Any field that is
        not one of the source's own (a token, a password, a header) is dropped from the public source here; ``enable``
        keeps each as an entry of its own (``record_carriers.source_extras``), and nothing reads it back.
        """
        try:
            record_carriers.source_extras(source)
        except ValueError as error:
            raise ValueError(str(error)) from None
        public = {key: value for key, value in source.items() if key in record_carriers.PACKAGE_SOURCE_PUBLIC_KEYS}
        credentials: dict[str, dict[str, Any]] = {}
        for key in record_carriers.PACKAGE_SOURCE_URL_KEYS:
            value = public.get(key)
            if isinstance(value, str) and "://" in value:
                try:
                    stripped, parts = record_carriers.split_url(value.strip())
                except record_carriers.AmbiguousUrl:
                    raise ValueError("that package source URL cannot be read") from None
                if parts:
                    public[key], credentials[key] = stripped, parts
        return public, credentials

    def validate_extension_package_source(
        self, *, source: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        public, credentials = self._split_package_source(source)
        self._require_owner_for_host_source(public, actor)
        return extension_packages.validate_extension_package_source(public, credentials=credentials)

    @staticmethod
    def _require_owner_for_host_source(source: Mapping[str, Any], actor: AdminContext) -> None:
        """A package source that reads files on the gateway host is for the owner only."""
        if "owner" not in actor.roles and extension_packages.source_reads_gateway_host(source):
            raise PermissionError(
                "Only the owner can name a directory or a non-https git url as a package source.")

    def enable_extension_package(
        self, *, source: Mapping[str, Any], actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        extras = record_carriers.source_extras(source)
        source, credentials = self._split_package_source(source)
        self._require_owner_for_host_source(source, actor)
        review = extension_packages.validate_extension_package_source(
            source,
            install_root=self.path.parent / "extensions" / "installed",
            credentials=credentials,
        )
        if not review.get("ok"):
            return {
                "extension_package": None,
                "review": review,
                "service_registry": self.get_service_registry(),
                "extension_packages": self.get_extension_package_index(),
            }

        package_id = str((review.get("package") or {}).get("id") or "")
        existing = self.get_extension_package_index()["packages"].get(package_id)
        if (existing and not existing.get("enabled", True)
                and existing.get("package_hash") == review.get("package_hash")):
            result = self._change_extension_package(
                package_id=package_id, actor=actor, mode="enable",
                expected_generation=expected_generation,
            )
            result["review"] = review
            return result

        imported = extension_packages.imported_registry_records(review)
        registry = service_registry.merge_imported_records(
            self.get_service_registry(),
            services=imported["services"],
            protocol_frontends=imported["protocol_frontends"],
            workflows=imported["workflows"],
            capabilities=imported["capabilities"],
        )
        # A package is data the administrator picked, not the owner: it gets the
        # same credential and lifecycle checks as a registry edit.
        registry, ingested, retired = self._ingest_carriers(registry, actor)
        try:
            registry = self._settle_credential_bindings(registry, actor)
        except BaseException:
            self._discard_secrets(ingested)
            raise
        package_index = extension_packages.merge_package_index(
            self.get_extension_package_index(),
            review,
            source=source,
        )
        created: list[str] = []

        def put(value: str, origin: str | None = None) -> str:
            ref = secret_refs.store(value, db_path=self.path, origin=origin)
            created.append(ref)
            return ref

        try:
            for key, parts in credentials.items():
                entries = record_carriers.url_entries(str(source[key]), parts, put)
                for held in record_carriers.package_sources(package_index):
                    if held.get(key) == source[key] and not held.get(key + record_carriers.SECRETS_SUFFIX):
                        held[key + record_carriers.SECRETS_SUFFIX] = copy.deepcopy(entries)
            if extras:
                held_extras = {key: secret_refs.entry(put(text, None)) for key, text in extras.items()}
                for held in record_carriers.package_sources(package_index):
                    if all(held.get(key) == source[key] for key in ("url", "repo_url", "path", "directory", "repo", "repository") if key in source):
                        held[record_carriers.SOURCE_EXTRA_ENTRIES] = copy.deepcopy(held_extras)
        except BaseException:
            self._discard_secrets([*created, *ingested])
            raise
        package_id = str((review.get("package") or {}).get("id") or "")
        # The snapshot keeps a digest of each normalized record the package wrote, not a copy.
        # They are digested in the form the registry stores them (references, not secrets), under the gateway's own key.
        package_index["packages"][package_id].update(extension_packages.installed_records_proof({
            kind: {record_id: registry[kind][record_id] for record_id in imported[kind]}
            for kind in sorted(extension_packages.DATA_OBJECT_KINDS)
        }, key=record_integrity.install_key(self.path)))
        # Registry, package index and both audit facts commit together: a
        # failure at any step leaves the generation and every row unchanged.
        document, excluded = self._with_excluded_records(registry)
        try:
            registry_record, index_record = admin_store.write_service_registry_and_setting_with_audits(
                document,
                expected_generation=expected_generation,
                updated_by=actor.subject,
                setting_key=EXTENSION_PACKAGE_INDEX_SETTING_KEY,
                setting_value=package_index,
                audits=[
                    {
                        "action": "operator.service_registry.update",
                        "actor_kind": actor.actor_kind,
                        "actor_id": actor.audit_actor_id,
                        "actor_label": actor.actor_label,
                        "target_kind": "operator_setting",
                        "target_id": SERVICE_REGISTRY_SETTING_KEY,
                        "metadata": {
                            "service_count": len(registry.get("services") or {}),
                            "protocol_frontend_count": len(registry.get("protocol_frontends") or {}),
                            "workflow_count": len(registry.get("workflows") or {}),
                            "capability_count": len(registry.get("capabilities") or {}),
                        },
                    },
                    {
                        "action": "operator.extension_package.enable",
                        "actor_kind": actor.actor_kind,
                        "actor_id": actor.audit_actor_id,
                        "actor_label": actor.actor_label,
                        "target_kind": "extension_package",
                        "target_id": package_id,
                        "metadata": {
                            "service_count": len(imported["services"]),
                            "protocol_frontend_count": len(imported["protocol_frontends"]),
                            "workflow_count": len(imported["workflows"]),
                            "capability_count": len(imported["capabilities"]),
                            "package_hash": review.get("package_hash"),
                        },
                    },
                ],
                path=self.path,
            )
        except BaseException:
            self._discard_secrets([*created, *ingested])
            raise
        self._discard_secrets(retired)
        # The artifact and reload observe the committed registry: the records that validate.
        registry_record = {**registry_record, "value": registry, "excluded": excluded}
        self.write_litellm_config(registry)
        self._fire_registry_changed(registry_record)
        return {
            "extension_package": (index_record["value"].get("packages") or {}).get(package_id),
            "review": review,
            "service_registry": registry,
            "operator_settings_generation": registry_record[
                "operator_settings_generation"
            ],
            "extension_packages": index_record["value"],
        }

    def remove_extension_package(
        self, *, package_id: str, actor: AdminContext, mode: str = "remove",
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """Remove or disable package-owned records and the index atomically."""
        if mode not in {"remove", "disable"}:
            raise ValueError("package mode must be remove or disable")
        return self._change_extension_package(
            package_id=package_id, actor=actor, mode=mode,
            expected_generation=expected_generation,
        )

    def _change_extension_package(
        self, *, package_id: str, actor: AdminContext, mode: str,
        expected_generation: int | None,
    ) -> dict[str, Any]:
        # Preserve raw fields when deciding whether an operator edited a row.
        record = admin_store.get_service_registry_with_generation(path=self.path)
        registry = record["value"] if record else {}
        generation = int(record["operator_settings_generation"]) if record else 0
        if expected_generation is None:
            expected_generation = generation
        index = self.get_extension_package_index()
        try:
            package_index, next_registry, affected = extension_packages.change_package_lifecycle(
                index, registry, package_id, mode=mode, key=self._install_key(index),
            )
        except KeyError as exc:
            raise ValueError(str(exc)) from exc

        def refuse_if_a_plan_names_the_package(conn: sqlite3.Connection) -> None:
            # Runs after the write lock is held, so a plan saved after this read waits and is
            # either checked here or saved after the removal commits; no plan is read stale.
            if mode != "remove":
                return
            references = {
                f"access plan {plan['name']}": plan["plan"]
                for plan in admin_store.list_access_plans_in(conn)
            }
            extension_packages.change_package_lifecycle(
                index, registry, package_id, mode=mode, references=references, key=self._install_key(index))

        registry_record, index_record = admin_store.write_service_registry_and_setting_with_audits(
            next_registry,
            expected_generation=expected_generation,
            precheck=refuse_if_a_plan_names_the_package,
            updated_by=actor.subject,
            setting_key=EXTENSION_PACKAGE_INDEX_SETTING_KEY,
            setting_value=package_index,
            audits=[{
                "action": f"operator.extension_package.{mode}",
                "actor_kind": actor.actor_kind,
                "actor_id": actor.audit_actor_id,
                "actor_label": actor.actor_label,
                "target_kind": "extension_package",
                "target_id": package_id,
                "metadata": {"affected_records": affected},
            }],
            path=self.path,
        )
        # What a removal took out of the registry and the index no longer needs its secrets: the entries of the removed
        # records and of the package's own source. A record the package left (detached, kept) keeps its entries.
        gone = [registry[item["kind"]][item["id"]] for item in affected
                if item["action"] == "removed" and isinstance((registry.get(item["kind"]) or {}).get(item["id"]), Mapping)]
        gone_refs = {ref for record in gone for ref in record_carriers.refs_in(record)}
        gone_refs |= set(record_carriers.refs_in((index["packages"].get(package_id) or {}).get("source")))
        still_used = set(record_carriers.refs_in(next_registry)) | set(record_carriers.refs_in(index_record["value"]))
        self._discard_secrets(sorted(gone_refs - still_used))
        self.write_litellm_config(registry_record["value"])
        self._fire_registry_changed(registry_record)
        return {
            "extension_package": (index_record["value"].get("packages") or {}).get(package_id),
            "extension_packages": index_record["value"],
            "service_registry": registry_record["value"],
            "operator_settings_generation": registry_record["operator_settings_generation"],
            "affected_records": affected,
        }

    def _install_key(self, index: Mapping[str, Any]) -> bytes | None:
        """The key of the install proofs, when the index holds a keyed proof (a store with none is not given a key file)."""
        keyed = any(
            isinstance(entry, Mapping) and "mac" in entry
            for package in (index.get("packages") or {}).values() if isinstance(package, Mapping)
            for records in (package.get("installed_records") or {}).values() if isinstance(records, Mapping)
            for entry in records.values()
        )
        return record_integrity.install_key(self.path) if keyed else None

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
        """Return the DB-stored OIDC provider config with its secret resolved.

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
        return self._with_resolved_oidc_secret(value)

    def _with_resolved_oidc_secret(self, stored: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(stored)
        ref = value.pop(OIDC_CLIENT_SECRET_REF, None)
        if value.get("client_secret"):
            # Startup moves stored literals into the secret store; one seen
            # here was written behind this process and is not trusted.
            print("[admin_runtime] ignoring a plaintext OIDC client secret in settings", flush=True)
            del value["client_secret"]
        if ref:
            try:
                value["client_secret"] = secret_store.get_provider_secret(str(ref), db_path=self.path)
            except Exception as exc:  # noqa: BLE001
                print(f"[admin_runtime] OIDC client secret unreadable: {type(exc).__name__}", flush=True)
        return value

    def update_oidc_config_setting(
        self, *, data: Mapping[str, Any], actor: AdminContext,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """Persist the DB-stored OIDC provider config, in a transaction of its own."""
        secret_store.ensure_provider_secret_schema(self.path)
        with admin_store.transaction(self.path) as tx:
            return self.update_oidc_config_setting_in(
                tx, data=data, actor=actor, expected_revision=expected_revision)

    def update_oidc_config_setting_in(
        self, tx: admin_store.Transaction, *, data: Mapping[str, Any], actor: AdminContext,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """Persist the DB-stored OIDC provider config inside a transaction the caller commits.

        Preserves the existing ``client_secret`` when the update omits it or
        passes an empty/redacted value. Unknown fields are rejected so a
        misspelled key never silently disables auth. A new secret is stored
        under a fresh id and the old one deleted, both in ``tx``: a rollback
        leaves the old secret in place and no stray new one. The caller must
        have run ``secret_store.ensure_provider_secret_schema`` before ``tx``
        began.
        """
        if "owner" not in actor.roles:
            raise PermissionError("owner role required to change OIDC trust configuration")
        unknown = set(data.keys()) - set(OIDC_PROVIDER_FIELDS)
        if unknown:
            raise ValueError(
                f"unknown oidc_provider_config fields: {sorted(unknown)}"
            )
        conn = tx.conn
        current = admin_store.get_operator_setting_in(conn, OIDC_PROVIDER_SETTING_KEY)
        current_revision = admin_store.operator_setting_revision(current)
        if expected_revision is not None and expected_revision != current_revision:
            raise admin_store.SettingsConflictError()
        current_raw = current["value"] if current else {}
        merged = _merge_oidc_setting(current_raw, data)
        # Validate with the secret the login flow would use; store a reference.
        candidate = self._with_resolved_oidc_secret(merged)
        supplied = data.get("client_secret")
        new_secret = None if _is_blank_or_redacted(supplied) else str(supplied)
        if new_secret is not None and new_secret == candidate.get("client_secret"):
            new_secret = None
        if new_secret is not None:
            candidate["client_secret"] = new_secret
        _validate_merged_oidc_setting(candidate)
        candidate_enabled = candidate.get("enabled", True)
        if self.oidc_config_validator is not None:
            validated_enabled = self.oidc_config_validator(candidate)
            if validated_enabled is not None:
                candidate_enabled = validated_enabled
        old_ref = merged.get(OIDC_CLIENT_SECRET_REF)
        new_ref = None
        if new_secret is not None:
            # A fresh id per write: a rejected settings write leaves the
            # current reference pointing at the current secret.
            new_ref = f"oidc-client-secret-{secrets.token_hex(8)}"
            secret_store.put_provider_secret(new_ref, new_secret, db_path=self.path, conn=conn)
            merged.pop("client_secret", None)
            merged[OIDC_CLIENT_SECRET_REF] = new_ref

        def validate_sign_in(conn):
            row = conn.execute("SELECT value_json FROM operator_settings WHERE key = 'sign_in'").fetchone()
            mode = json.loads(row["value_json"]).get("mode", "both") if row else "both"
            if mode == "oidc" and not candidate_enabled:
                raise ValueError("Enable built-in sign-in before disabling the identity provider; at least one working sign-in method is required.")

        record = admin_store.put_operator_setting_with_audit_in(
            conn,
            OIDC_PROVIDER_SETTING_KEY,
            merged,
            updated_by=actor.subject,
            expected_revision=current_revision,
            validate_in_transaction=validate_sign_in,
            audit_action="operator.oidc_provider_config.update",
            audit_actor_kind=actor.actor_kind,
            audit_actor_id=actor.audit_actor_id,
            audit_actor_label=actor.actor_label,
            audit_target_kind="operator_setting",
            audit_target_id=OIDC_PROVIDER_SETTING_KEY,
            audit_metadata={
                "fields": sorted(str(k) for k in data.keys()),
                "secret_changed": new_ref is not None,
            },
        )
        if new_ref is not None and old_ref:
            secret_store.delete_provider_secret(str(old_ref), db_path=self.path, conn=conn)
        return {
            **_redact_oidc_setting(record["value"], updated_at=record.get("updated_at")),
            "revision": admin_store.operator_setting_revision(record),
        }

    @staticmethod
    def _role_rank(roles: Iterable[str]) -> int:
        return max((_ROLE_RANK.get(str(role), 0) for role in roles), default=0)

    def _require_actor_not_outranked(
        self, actor: AdminContext, holder_roles: Iterable[str], message: str,
    ) -> None:
        """Refuse when whoever holds this has more control-plane power than the actor.

        An administrator may cut off keys and Apps of members and of other
        administrators. They may not cut off the owner, or a holder of a role
        above theirs: that would let them lock out the person who granted
        their own power.
        """
        if self._role_rank(holder_roles) > self._role_rank(actor.roles):
            raise ActorOutranked(message)

    def _app_holder_roles(self, app: Mapping[str, Any] | None) -> frozenset[str]:
        """Control-plane roles of the person an App answers to."""
        if app is None:
            return frozenset()
        if app.get("owner_user_id"):
            return admin_store.effective_admin_roles(
                "user", str(app["owner_user_id"]), path=self.path)
        return frozenset()

    def revoke_credential(
        self, *, credential_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        before = admin_store.get_credential(credential_id, path=self.path)
        if before is None:
            return None
        if before.get("subject_kind") in {"user", "service_account"}:
            holder = admin_store.effective_admin_roles(
                str(before["subject_kind"]), str(before["subject_id"]),
                minted_by=str(before.get("created_by") or "") or None, path=self.path)
        else:
            holder = self._app_holder_roles(admin_store.get_app(
                str(before.get("app_id") or before.get("subject_id") or ""), path=self.path))
        self._require_actor_not_outranked(
            actor, holder,
            "This key belongs to the owner or to someone with more access than you, "
            "so only a person with at least their access can revoke it.")
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
                print(f"[admin_runtime] WARNING: on_credential_revoked callback raised: {type(exc).__name__}", flush=True)
        return _sanitize_credential(record or before)

    def recent_audit(
        self, *, limit: int, include_internal: bool = False
    ) -> list[dict[str, Any]]:
        return [
            self._present_audit_event(row)
            for row in admin_store.list_audit(
                limit=limit,
                exclude_internal_activity=not include_internal,
                path=self.path,
            )
        ]

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
        # Accounts are never hard-deleted, so an id no account has was never a
        # person or automation account: a tool on the gateway's host recorded
        # the change under its own name. That is an automatic change, and the
        # page says so rather than "Unknown user" (no inference from the id).
        if kind == "user":
            principal = admin_store.get_user(actor_id, path=self.path) or {}
            out["actor_label"] = principal.get("display_name") or principal.get("email") or AUTOMATIC_ACTOR_LABEL
        elif kind == "service_account":
            principal = admin_store.get_service_account(actor_id, path=self.path) or {}
            out["actor_label"] = principal.get("name") or AUTOMATIC_ACTOR_LABEL
        elif kind in {"system", "cli"}:
            out["actor_label"] = "System"
        else:
            # Legacy rows can retain their historical kind but are not
            # reclassified from their ID; this keeps the boundary truthful.
            out["actor_label"] = AUTOMATIC_ACTOR_LABEL
        return out

    def list_audit_page(
        self,
        *,
        limit: int = 50,
        actor_id: str | None = None,
        action_prefix: str | None = None,
        exclude_access_audit: bool = False,
        include_internal: bool = False,
        include_automated: bool = False,
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
            exclude_internal_activity=not include_internal,
            exclude_service_account_activity=not include_automated,
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
            "automated_count": admin_store.count_service_account_audit(path=self.path),
        }

    def dashboard_snapshot(self, actor: AdminContext) -> dict[str, Any]:
        users = self.list_users()
        apps = self.list_apps()
        plans = self.list_access_plans()
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
                "access_plans": len(plans),
                "credentials": len(credentials),
                "active_credentials": sum(1 for c in credentials if not c.get("revoked_at")),
            },
            "access_model": access_model_summary(),
            "users": users,
            "apps": apps,
            "access_plans": plans,
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
                {"label": "Access plans JSON", "href": "/admin/api/access-plans"},
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
                    # A revoked or ended session is no session: a bearer key
                    # sent beside a stale cookie still authenticates.
                    session = browser_sessions.read(
                        token, signing_key=self.signing_key, path=self.store.path,
                    )
                    subject_kind, subject_id = session_admin_role_subject(
                        session.subject
                    )
                    role_names = admin_store.effective_admin_roles(
                        subject_kind, subject_id, path=self.store.path,
                        oidc_max_age=self.cookie.max_age_seconds,
                    )
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
                            session_id=session.session_id,
                            session_expires_at=int(session.expires_at),
                            authenticated_at=int(session.last_authenticated),
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

        # An admin key works only while the person who minted it still holds a
        # role that permits it (see admin_store.effective_admin_roles).
        role_names = admin_store.effective_admin_roles(
            str(credential["subject_kind"]), str(credential["subject_id"]),
            minted_by=str(credential.get("created_by") or "") or None,
            path=self.store.path, oidc_max_age=self.cookie.max_age_seconds,
        )
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


def person_for_app_credential(app: Mapping[str, Any], credential: Mapping[str, Any]) -> str | None:
    """Personal keys represent their own App owner; shared/service keys do not.

    An unresolved creator cannot prove ownership. Such keys keep App
    access but do not inherit a person or their saved prompts.
    """
    owner = app.get("owner_user_id")
    if app.get("kind") != "personal" or not owner:
        return None
    creator = credential.get("created_by")
    return str(owner) if creator == owner else None


def app_key_prompt_memory(app: Mapping[str, Any], credential: Mapping[str, Any]) -> dict[str, Any]:
    """Describe prompt access at mint time using the same rule as authentication."""
    person_id = person_for_app_credential(app, credential)
    return {
        "person_id": person_id,
        "description": (
            "Prompts sent with this key follow your prompt-memory settings. "
            "Anyone holding it can access your saved prompts."
            if person_id else
            "Prompts sent with this key are not remembered. Generated items belong to the App. "
            "Requests to /v1/responses can explicitly ask to save a conversation with store:true until it is explicitly deleted."
        ),
    }


@functools.lru_cache(maxsize=256)
def _parsed_plan(plan_json: str, plan_id: str):
    """Parse a stored plan once per distinct document (it is immutable text)."""
    try:
        return plan_from_document(json.loads(plan_json), plan_id=plan_id)
    except (PlanError, ValueError, TypeError):
        return None


def _plan_from_row(plan_json: Any, plan_id: Any):
    if not plan_json or not plan_id:
        return None
    return _parsed_plan(str(plan_json), str(plan_id))


def identity_from_access_row(
    row: Mapping[str, Any], *, key_hash: str, name: str, person_id: str | None,
) -> RuntimeIdentity:
    """Build the identity of design §2.5 step 1 from one runtime_access_row.

    A disabled App or a disabled, removed or missing accountable person yields
    an identity whose ``denial`` names the reason; plans come from the same
    row, so the whole decision reads one snapshot.
    """
    denial = None
    if row.get("app_disabled_at") is not None:
        denial = "app_disabled"
    elif (row.get("person_row") is None or row.get("access_row") is None
          or row.get("person_disabled_at") is not None or row.get("person_removed_at") is not None):
        denial = "person_disabled"
    elif row.get("person_signin_refused"):
        # Reversible: a successful sign-in deletes the refusal record.
        denial = "person_signin_refused"
    person_plan = _plan_from_row(row.get("person_plan_json"), row.get("person_plan_id"))
    assignment = row.get("access_plan")
    if assignment == PERSON:
        app_plan = person_plan
    else:
        app_plan = _plan_from_row(row.get("app_plan_json"), assignment)
    return RuntimeIdentity(
        name=name,
        key_hash=key_hash,
        downstream={},
        access=EffectiveAccess(person_plan=person_plan, app_plan=app_plan),
        app_id=str(row["app_id"]),
        person_id=person_id,
        accountable_person_id=str(row.get("owner_user_id") or "") or None,
        denial=denial,
    )


def access_for_person(person_id: str, *, path: Path = admin_store.DB_PATH) -> EffectiveAccess:
    """What a new "Same as my access plan" App of this person could do."""
    plan_id = admin_store.person_plan_id(person_id, path=path)
    record = admin_store.get_access_plan(plan_id, path=path) if plan_id else None
    plan = _parsed_plan(json.dumps(record["plan"], sort_keys=True), plan_id) if record else None
    return EffectiveAccess(person_plan=plan, app_plan=plan)


def runtime_identity_for_app_credential_hash(
    key_hash: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> RuntimeIdentity | None:
    """Return a data-plane identity for an App key, or None if the key is unknown.

    Only active App credentials authenticate; user and service-account keys
    stay control-plane-only, and a session App acts only through its person's
    browser session. A disabled App or person returns an identity with
    ``denial`` set, so the caller can say which (design §3.3).
    """
    with admin_store.runtime_auth_connection(path) as connection:
        row = admin_store.runtime_access_row(connection, key_hash=key_hash)
        if row is None or row["cred_subject_kind"] != "app" or row["on_session_app"]:
            return None
        if not _credential_active({"revoked_at": row["cred_revoked_at"], "expires_at": row["cred_expires_at"]}):
            return None
        # Record that this key was just used so the portal can show real "used N
        # ago" recency. Debounced in the store (at most one write per key per
        # minute) and best-effort: a recency write must never fail a live request.
        try:
            admin_store.touch_credential_last_used(str(row["cred_id"]), path=path, connection=connection)
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
    app = {"kind": row["app_kind"], "owner_user_id": row["owner_user_id"]}
    return identity_from_access_row(
        row,
        key_hash=key_hash,
        name=str(row.get("app_name") or row.get("cred_display_name") or row["app_id"]),
        person_id=person_for_app_credential(app, {"created_by": row["cred_created_by"]}),
    )


def runtime_identity_for_session_app(
    app_id: str,
    person_id: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> RuntimeIdentity | None:
    """Return the keyless identity a signed-in person acts through.

    It passes the same App, person and plan checks as an App key, but only
    for the person's own session App, which never holds a credential. Its
    ``key_hash`` is the App's ownership principal, not a credential hash.
    """
    with admin_store.runtime_auth_connection(path) as connection:
        row = admin_store.runtime_access_row(connection, app_id=app_id)
    if row is None or row.get("owner_user_id") != person_id or not row["on_session_app"]:
        return None
    return identity_from_access_row(
        row, key_hash=f"app:{app_id}", name=str(row.get("app_name") or app_id), person_id=person_id,
    )


def runtime_identity_for_trusted_address(
    client: Any,
    *,
    path: Path = admin_store.DB_PATH,
) -> RuntimeIdentity | None:
    """Return the identity of the App an owner trusted this client address for.

    ``client`` is the direct client address (never a forwarded one the caller
    has not checked). The narrowest matching entry wins. The App passes the
    same checks as with a key: a disabled App or person gives an identity
    whose ``denial`` names the reason. The identity has no data person, so
    saved prompts and the Library stay closed to it. Returns None when no
    entry covers the address.
    """
    with admin_store.runtime_auth_connection(path) as connection:
        entries = admin_store.list_trusted_addresses(connection=connection)
        entry = trusted_addresses.best_match(client, entries) if entries else None
        if entry is None:
            return None
        row = admin_store.runtime_access_row(connection, app_id=str(entry["app_id"]))
    if row is None or row["on_session_app"]:
        return None
    identity = identity_from_access_row(
        row, key_hash=f"app:{row['app_id']}", name=str(row.get("app_name") or row["app_id"]), person_id=None,
    )
    return replace(identity, trusted_address=str(client))


def runtime_identity_for_trusted_app(
    app_id: str,
    address: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> RuntimeIdentity | None:
    """Return the identity of an App a trusted address ran as, for its queued work.

    A media job accepted from a trusted address runs later in a worker, which
    has no client address of its own that may count. The job keeps its App
    only while ``address``, the one the request came from, still matches this
    App, by the same narrowest-entry rule as ``runtime_identity_for_trusted_address``.
    Another address the App is trusted for does not count. Returns None when
    the address no longer maps to this App.
    """
    try:
        client = ipaddress.ip_address(address)
    except ValueError:
        return None
    identity = runtime_identity_for_trusted_address(client, path=path)
    return identity if identity is not None and identity.app_id == app_id else None


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
    if any(row.get("granted_by") not in (None, admin_store.OPERATOR_GRANTOR)
           for row in admin_store.list_admin_roles(
               subject_kind="service_account", subject_id=service_account["id"],
               include_revoked=True, path=path)):
        raise PermissionError(f"{name!r} was created from the dashboard; choose another name")
    assignment = admin_store.assign_admin_role(
        role=role,
        subject_kind="service_account",
        subject_id=service_account["id"],
        granted_by=admin_store.OPERATOR_GRANTOR,
        actor_kind="cli",
        path=path,
    )
    api_key = generate_admin_api_key()
    credential = admin_store.create_credential_hash(
        subject_kind="service_account",
        subject_id=service_account["id"],
        hash=hash_api_key(api_key),
        prefix=_key_prefix(api_key),
        display_name=f"{name} admin key",
        created_by=admin_store.OPERATOR_GRANTOR,
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
]
