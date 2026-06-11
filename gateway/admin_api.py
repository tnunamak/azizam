"""Admin plane API router.

Builds a FastAPI router that exposes JSON endpoints under ``/admin/api`` for
managing apps (the user-facing data-plane noun), users, app profiles,
credentials, and for inspecting audit records and policy decisions.

This module is intentionally decoupled from persistent storage and live auth:

* Auth is injected via an :class:`AdminAuth` callable that returns an
  :class:`AdminContext` for authorised requests, and raises
  :class:`AdminAuthError` for unauthenticated / forbidden ones. Errors are
  rendered as JSON.
* Persistence is injected as an :class:`AdminStore` — any object exposing the
  duck-typed methods listed below. The router treats it as a black box.
* Policy simulation calls :func:`gateway.policy.evaluate` directly so the
  admin plane stays consistent with the live runtime decision logic.

The router never reads files, never reads ``os.environ``, never touches
``proxy.py``, and never returns raw key material outside one-time key creation
responses.
"""

from __future__ import annotations

import json
import hmac
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from gateway.policy import (
    AppProfile,
    PolicyDecision,
    PolicyRequest,
    evaluate,
)
from gateway.litellm_substrate import compile_litellm_substrate_config
from gateway.profiles import ProfileLoadError, load_profile
from gateway import extension_packages, service_registry

__all__ = [
    "AdminAuthError",
    "AdminContext",
    "AdminStore",
    "access_model_summary",
    "derive_admin_attention",
    "create_admin_router",
]


@dataclass(frozen=True)
class AdminContext:
    """Authenticated admin caller.

    ``roles`` should contain at least ``"admin"`` for write actions. Callers
    decide their own role taxonomy; this module only checks ``is_admin``.
    """

    subject: str
    email: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)
    auth_method: str | None = None
    csrf_token: str | None = None

    @property
    def is_admin(self) -> bool:
        return bool({"owner", "admin", "superadmin"} & set(self.roles))


class AdminAuthError(Exception):
    """Raised by an injected auth callable to deny a request.

    ``status_code`` should be 401 for missing credentials and 403 for an
    authenticated caller without admin role. ``code`` and ``message`` populate
    the JSON error envelope.
    """

    def __init__(
        self,
        message: str = "admin authentication required",
        *,
        status_code: int = status.HTTP_401_UNAUTHORIZED,
        code: str = "admin_unauthenticated",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class AdminStore(Protocol):
    """Duck-typed persistence surface.

    Implementations may be in-memory, SQLite, etc. All methods return plain
    JSON-able dicts; raw key material must only ever appear in one-time key
    creation return values under the key ``"api_key"``.
    """

    def list_users(self) -> list[dict[str, Any]]: ...

    def list_apps(self) -> list[dict[str, Any]]: ...

    def get_app(self, app_id: str) -> dict[str, Any] | None: ...

    def create_app(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def update_app(
        self, *, app_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None: ...

    def disable_app(
        self, *, app_id: str, actor: AdminContext
    ) -> dict[str, Any] | None: ...

    def list_profiles(self) -> list[dict[str, Any]]: ...

    def get_profile(self, profile_id: str) -> dict[str, Any] | None: ...

    def create_profile(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def update_profile(
        self, *, profile_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None: ...

    def delete_profile(
        self, *, profile_id: str, actor: AdminContext
    ) -> dict[str, Any] | None: ...

    def list_credentials(self) -> list[dict[str, Any]]: ...

    def create_credential(
        self, *, app_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def create_admin_automation_key(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def revoke_credential(
        self, *, credential_id: str, actor: AdminContext
    ) -> dict[str, Any] | None: ...

    def list_runtime_profile_mappings(self) -> list[dict[str, Any]]: ...

    def create_runtime_profile_mapping(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def disable_runtime_profile_mapping(
        self, *, mapping_id: str, actor: AdminContext
    ) -> dict[str, Any] | None: ...

    def operator_overrides(self) -> dict[str, Any]: ...

    def update_user_access_defaults(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def update_service_availability_overrides(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def update_media_job_admission_overrides(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def get_service_registry(self) -> dict[str, Any]: ...

    def update_service_registry(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def add_service(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def update_service(
        self, *, service_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def remove_service(
        self, *, service_id: str, actor: AdminContext
    ) -> dict[str, Any]: ...

    def set_service_enabled(
        self, *, service_id: str, enabled: bool, actor: AdminContext
    ) -> dict[str, Any]: ...

    def get_extension_package_index(self) -> dict[str, Any]: ...

    def validate_extension_package_source(
        self, *, source: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def enable_extension_package(
        self, *, source: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def get_oidc_config_setting(self) -> dict[str, Any] | None: ...

    def update_oidc_config_setting(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def recent_audit(self, *, limit: int) -> list[dict[str, Any]]: ...


AdminAuth = Callable[[Request], AdminContext]
ProfileResolver = Callable[[str], AppProfile | None]
# async (host, port, path) -> {ok, status, error, url}
ServiceProbe = Callable[[str, "int | None", str], "Awaitable[Mapping[str, Any]]"]


_AUDIT_DEFAULT_LIMIT = 50
_AUDIT_MAX_LIMIT = 200
_SERVICE_AVAILABILITY_STATES = frozenset(
    {"available", "degraded", "draining", "maintenance", "disabled"}
)
_SERVICE_OVERRIDE_FIELDS = frozenset(
    {"state", "status", "reason", "retry_after_seconds"}
)
_MEDIA_ADMISSION_FIELDS = frozenset(
    {
        "disabled_intents",
        "disabled_resource_classes",
        "reserved_tokens",
        "max_queue_depth",
        "retry_after_seconds",
    }
)


def _attention_app_profile_id(record: Mapping[str, Any]) -> str:
    """Resolve an App's effective profile id (mirrors admin_render._app_profile_id)."""
    profile_id = (
        record.get("default_profile_id")
        or record.get("profile_id")
        or record.get("profile")
    )
    return str(profile_id) if profile_id else "default-local"


def derive_admin_attention(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return "things that want a look" — the operator's actual landing screen.

    Pure function over a dashboard snapshot, so the same list backs both the
    admin HTML overview and the ``/admin/api/overview`` JSON (the migration-safe
    seam). Each item is ``{kind, severity, title, detail, section}`` where
    ``severity`` is ``"problem"`` (broken — needs fixing) or ``"attention"``
    (works but incomplete). An EMPTY list means everything's healthy — and the
    overview says so, rather than padding the screen with green stat cards.

    Signals mirror the per-row statuses the detail tables already show, so the
    overview never disagrees with the sections:
      * Apps: disabled, no profile, profile missing, or no active key.
      * Services: ``down`` (problem) or ``degraded`` (attention).
    """
    items: list[dict[str, Any]] = []

    apps = [a for a in snapshot.get("apps", []) if isinstance(a, Mapping)]
    profiles = [p for p in snapshot.get("profiles", []) if isinstance(p, Mapping)]
    profile_ids = {str(p.get("id")) for p in profiles if p.get("id")}
    credentials = [c for c in snapshot.get("credentials", []) if isinstance(c, Mapping)]
    active_keys_by_app: dict[str, int] = {}
    for cred in credentials:
        if cred.get("revoked_at"):
            continue
        app_ref = str(cred.get("app_id") or cred.get("subject_id") or "")
        if app_ref:
            active_keys_by_app[app_ref] = active_keys_by_app.get(app_ref, 0) + 1

    for app in apps:
        app_id = str(app.get("id") or "")
        name = str(app.get("name") or app_id or "App")
        profile_id = _attention_app_profile_id(app)
        if app.get("disabled_at"):
            items.append({
                "kind": "app", "severity": "problem", "section": "apps-keys",
                "title": f"{name} is disabled",
                "detail": "Re-enable it or remove it.",
            })
        elif not profile_id:
            items.append({
                "kind": "app", "severity": "problem", "section": "apps-keys",
                "title": f"{name} has no permission profile",
                "detail": "Assign a profile so its keys can be used.",
            })
        elif profile_id != "default-local" and profile_id not in profile_ids:
            items.append({
                "kind": "app", "severity": "problem", "section": "apps-keys",
                "title": f"{name}'s profile is missing",
                "detail": "Its profile was deleted; assign a new one.",
            })
        elif active_keys_by_app.get(app_id, 0) < 1:
            items.append({
                "kind": "app", "severity": "attention", "section": "apps-keys",
                "title": f"{name} has no active key",
                "detail": "Mint a key so it can reach the gateway.",
            })

    for svc in snapshot.get("services", []):
        if not isinstance(svc, Mapping):
            continue
        state = str(svc.get("status") or "").lower()
        label = str(svc.get("name") or svc.get("key") or "A service")
        if state == "down":
            items.append({
                "kind": "service", "severity": "problem", "section": "services",
                "title": f"{label} is down",
                "detail": "Requests that need it will fail until it's back.",
            })
        elif state == "degraded":
            items.append({
                "kind": "service", "severity": "attention", "section": "services",
                "title": f"{label} is degraded",
                "detail": "It's reachable but not fully healthy.",
            })

    order = {"problem": 0, "attention": 1}
    items.sort(key=lambda i: order.get(str(i["severity"]), 2))
    return items


def access_model_summary() -> dict[str, Any]:
    return {
        "nouns": ["user", "app", "profile", "key", "runtime_mapping", "admin_role"],
        "users": {
            "identity": "OIDC identity",
            "relationship": "users are not apps",
        },
        "apps": {
            "relationship": "apps inherit/default to profiles",
        },
        "profiles": {
            "relationship": "profiles are reusable runtime access policies",
        },
        "keys": {
            "relationship": "keys authenticate apps or admin automation without exposing raw secrets",
        },
        "runtime_mappings": {
            "source_kinds": [
                "oidc_group",
                "oidc_role",
                "email",
                "domain",
                "gateway_role",
                "all_users",
            ],
            "relationship": (
                "OIDC groups, OIDC roles, emails, domains, gateway roles, "
                "and all signed-in users can map identities to runtime profiles"
            ),
        },
        "admin_roles": {
            "relationship": "admin roles control the admin plane separately from runtime access",
        },
    }


class _RouteError(Exception):
    """Internal flow-control exception for endpoint validation errors."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def as_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content=_error_payload(self.code, self.message),
        )


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _auth_error_response(exc: AdminAuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(exc.code, exc.message),
    )


def _require_admin(auth: AdminAuth, request: Request) -> AdminContext | JSONResponse:
    """Resolve admin context, returning a JSONResponse on failure."""
    try:
        ctx = auth(request)
    except AdminAuthError as exc:
        return _auth_error_response(exc)
    if not isinstance(ctx, AdminContext):
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_payload(
                "admin_auth_invalid",
                "admin auth callable must return AdminContext",
            ),
        )
    if not ctx.is_admin:
        return _auth_error_response(
            AdminAuthError(
                "admin role required",
                status_code=status.HTTP_403_FORBIDDEN,
                code="admin_forbidden",
            )
        )
    if (
        ctx.auth_method == "session"
        and request.method not in {"GET", "HEAD", "OPTIONS"}
    ):
        if ctx.csrf_token:
            supplied = request.headers.get("x-ai-gateway-csrf") or ""
            if not hmac.compare_digest(supplied, ctx.csrf_token):
                return _auth_error_response(
                    AdminAuthError(
                        "admin session mutation requires a valid CSRF token",
                        status_code=status.HTTP_403_FORBIDDEN,
                        code="admin_csrf_required",
                    )
                )
        elif request.headers.get("x-ai-gateway-admin-action") != "1":
            return _auth_error_response(
                AdminAuthError(
                    "admin session mutation requires same-origin admin action header",
                    status_code=status.HTTP_403_FORBIDDEN,
                    code="admin_action_header_required",
                )
            )
    return ctx


async def _read_json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_json",
            f"request body is not valid JSON: {exc.msg}",
        ) from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_json",
            "request body must be a JSON object",
        )
    return payload


def _scrub_credential(record: Mapping[str, Any]) -> dict[str, Any]:
    """Strip raw key material from a credential record before returning it.

    Stores must never embed raw keys in list/read records, but this is a
    defence-in-depth pass so a buggy store cannot leak through the admin API.
    """
    blocked = {"api_key", "key", "secret", "hash", "key_hash"}
    return {k: v for k, v in record.items() if k not in blocked}


def _scrub_credentials(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [_scrub_credential(r) for r in records]


def _coerce_limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, maximum)


def _validate_retry_after(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_operator_override",
            f"{field} must be a non-negative integer",
        )
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_operator_override",
            f"{field} must be a non-negative integer",
        ) from exc
    if seconds < 0:
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_operator_override",
            f"{field} must be a non-negative integer",
        )
    return seconds


def _validate_string_list(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_operator_override",
            f"{field} must be a list of strings",
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_operator_override",
                f"{field} must be a list of non-empty strings",
            )
        token = item.strip()
        if token not in out:
            out.append(token)
    return out


def _validate_service_availability_overrides(
    payload: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    overrides = payload.get("overrides", payload)
    if not isinstance(overrides, Mapping):
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_operator_override",
            "service availability overrides must be a mapping",
        )
    normalized: dict[str, dict[str, Any]] = {}
    for service_id, raw in overrides.items():
        service_key = str(service_id or "").strip()
        if not service_key:
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_operator_override",
                "service id is required",
            )
        if not isinstance(raw, Mapping):
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_operator_override",
                f"override for {service_key!r} must be a mapping",
            )
        unknown = set(raw.keys()) - _SERVICE_OVERRIDE_FIELDS
        if unknown:
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_operator_override",
                f"unknown service override fields: {sorted(unknown)}",
            )
        state = raw.get("state", raw.get("status"))
        if state not in _SERVICE_AVAILABILITY_STATES:
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_operator_override",
                "service state must be one of: "
                + ", ".join(sorted(_SERVICE_AVAILABILITY_STATES)),
            )
        reason = raw.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_operator_override",
                "service reason must be a string",
            )
        normalized[service_key] = {
            "state": state,
            "reason": (reason or "").strip() or None,
            "retry_after_seconds": _validate_retry_after(
                raw.get("retry_after_seconds"),
                field="retry_after_seconds",
            ),
        }
    return normalized


def _validate_media_job_admission_overrides(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    unknown = set(payload.keys()) - _MEDIA_ADMISSION_FIELDS
    if unknown:
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_operator_override",
            f"unknown media admission override fields: {sorted(unknown)}",
        )
    max_queue_depth = payload.get("max_queue_depth")
    if max_queue_depth is not None:
        max_queue_depth = _validate_retry_after(
            max_queue_depth,
            field="max_queue_depth",
        )
    return {
        "disabled_intents": _validate_string_list(
            payload.get("disabled_intents"),
            field="disabled_intents",
        ),
        "disabled_resource_classes": _validate_string_list(
            payload.get("disabled_resource_classes"),
            field="disabled_resource_classes",
        ),
        "reserved_tokens": _validate_string_list(
            payload.get("reserved_tokens"),
            field="reserved_tokens",
        ),
        "max_queue_depth": max_queue_depth,
        "retry_after_seconds": _validate_retry_after(
            payload.get("retry_after_seconds"),
            field="retry_after_seconds",
        ),
    }


def _validate_service_registry(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return service_registry.normalize_service_registry(payload)
    except Exception as exc:  # noqa: BLE001
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_service_registry",
            f"invalid service registry: {exc}",
        ) from exc


def _validate_service_form(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an add/edit backend form and return the cleaned form fields.

    Requires a name and a way to reach the backend (a base URL or host). The
    store maps the form to a backend config and normalizes it; this gate just
    surfaces operator-facing 400s for the obvious omissions.
    """
    if not isinstance(payload, Mapping):
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST, "invalid_service", "body must be an object"
        )
    name = str(payload.get("name") or "").strip()
    if not name:
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST, "invalid_service", "a name is required"
        )
    has_host = bool(str(payload.get("host") or "").strip())
    has_url = bool(str(payload.get("base_url") or "").strip())
    if not (has_host or has_url):
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_service",
            "a base URL (or host) is required",
        )
    # An inline plaintext key is never accepted — secrets are referenced by env
    # var name only (api_key_env).
    if any(str(k).lower() == "api_key" for k in payload):
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_service",
            "paste an env var name in 'api_key_env', not the key itself",
        )
    return dict(payload)


def _validate_extension_package_source(payload: Mapping[str, Any]) -> dict[str, Any]:
    source = payload.get("source") if isinstance(payload, Mapping) else None
    if not isinstance(source, Mapping):
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_extension_package_source",
            "extension package source must be an object",
        )
    return {"source": dict(source)}


_USER_ACCESS_DEFAULT_FIELDS = frozenset(
    {"self_service_apps", "auto_create_personal_app"}
)

_OIDC_CONFIG_FIELDS = frozenset(
    {
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
    }
)
_OIDC_STRING_FIELDS = frozenset(
    {
        "issuer",
        "client_id",
        "client_secret",
        "redirect_uri",
        "authorization_endpoint",
        "token_endpoint",
        "jwks_uri",
    }
)
_OIDC_LIST_FIELDS = frozenset(
    {
        "scopes",
        "allowed_email_domains",
        "admin_emails",
        "admin_groups",
        "admin_roles",
    }
)


def _validate_oidc_config_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(payload.keys()) - _OIDC_CONFIG_FIELDS
    if unknown:
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_oidc_config",
            f"unknown oidc fields: {sorted(unknown)}",
        )
    out: dict[str, Any] = {}
    if "enabled" in payload:
        out["enabled"] = _validate_bool_field(payload["enabled"], field="enabled")
    for key in _OIDC_STRING_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            out[key] = ""
            continue
        if not isinstance(value, str):
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_oidc_config",
                f"{key} must be a string",
            )
        out[key] = value.strip()
    for key in _OIDC_LIST_FIELDS:
        if key not in payload:
            continue
        out[key] = _validate_string_list(payload[key], field=key)
    if "clock_skew_seconds" in payload:
        value = payload["clock_skew_seconds"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_oidc_config",
                "clock_skew_seconds must be a non-negative integer",
            )
        seconds = int(value)
        if seconds < 0:
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_oidc_config",
                "clock_skew_seconds must be a non-negative integer",
            )
        out["clock_skew_seconds"] = seconds
    if out.get("enabled"):
        for required in ("issuer", "client_id", "redirect_uri"):
            if required in out and not out[required]:
                raise _RouteError(
                    status.HTTP_400_BAD_REQUEST,
                    "invalid_oidc_config",
                    f"{required} is required when enabled=true",
                )
    return out


def _validate_bool_field(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled"}:
            return False
    raise _RouteError(
        status.HTTP_400_BAD_REQUEST,
        "invalid_operator_override",
        f"{field} must be true or false",
    )


def _validate_user_access_defaults(payload: Mapping[str, Any]) -> dict[str, bool]:
    unknown = set(payload.keys()) - _USER_ACCESS_DEFAULT_FIELDS
    if unknown:
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_operator_override",
            f"unknown user access fields: {sorted(unknown)}",
        )
    return {
        field: _validate_bool_field(payload[field], field=field)
        for field in sorted(_USER_ACCESS_DEFAULT_FIELDS)
        if field in payload
    }


def _resolve_profile(
    profile_payload: Any,
    *,
    profile_resolver: ProfileResolver | None,
) -> AppProfile:
    """Build a profile from either an inline dict or a stored profile id."""
    if isinstance(profile_payload, str):
        if profile_resolver is None:
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "profile_resolver_unavailable",
                "no profile resolver configured; pass an inline profile dict",
            )
        profile = profile_resolver(profile_payload)
        if profile is None:
            raise _RouteError(
                status.HTTP_404_NOT_FOUND,
                "profile_not_found",
                f"profile {profile_payload!r} not found",
            )
        return profile
    try:
        return load_profile(profile_payload)
    except ProfileLoadError as exc:
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST, "invalid_profile", str(exc)
        ) from exc


_POLICY_REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "intent",
        "workflow",
        "model",
        "backend",
        "capability_exposure",
        "capability_trust_tier",
        "capability_resource_tier",
        "capability_tags",
        "requires_cloud_fallback",
        "native_passthrough",
        "native_operation",
        "native_backend",
    }
)


def _build_policy_request(payload: Any) -> PolicyRequest:
    if payload is None:
        return PolicyRequest()
    if not isinstance(payload, Mapping):
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_request",
            "request must be a mapping",
        )
    unknown = set(payload.keys()) - _POLICY_REQUEST_FIELDS
    if unknown:
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_request",
            f"unknown request fields: {sorted(unknown)}",
        )
    data = dict(payload)
    if "capability_tags" in data and data["capability_tags"] is not None:
        tags = data["capability_tags"]
        if (
            isinstance(tags, (str, bytes, Mapping))
            or not isinstance(tags, Iterable)
        ):
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_request",
                "capability_tags must be a list of strings",
            )
        tag_list = list(tags)
        if not all(isinstance(tag, str) for tag in tag_list):
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_request",
                "capability_tags must be a list of strings",
            )
        data["capability_tags"] = frozenset(tag_list)
    return PolicyRequest(**data)


def _decision_payload(decision: PolicyDecision) -> dict[str, Any]:
    return {
        "decision": decision.decision,
        "allowed": decision.allowed,
        "sources": list(decision.sources),
        "reasons": list(decision.reasons),
        "visibility_level": decision.visibility_level,
    }


def create_admin_router(
    store: AdminStore,
    *,
    auth: AdminAuth,
    profile_resolver: ProfileResolver | None = None,
    service_probe: ServiceProbe | None = None,
    prefix: str = "/admin/api",
) -> APIRouter:
    """Build an admin-plane router.

    ``store`` provides persistence. ``auth`` is invoked on every request and
    must raise :class:`AdminAuthError` for unauthenticated or forbidden
    callers. ``profile_resolver`` lets the policy simulator accept a stored
    profile id (string) rather than an inline profile dict. ``service_probe`` is
    an optional ``async (host, port, path) -> {ok, status, error, url}`` used by
    the backend "Test connection" control; when absent that route reports the
    capability is unavailable rather than failing.
    """

    router = APIRouter(prefix=prefix, tags=["admin"])

    def _guard(request: Request) -> AdminContext | JSONResponse:
        return _require_admin(auth, request)

    @router.get("/session")
    def session(request: Request) -> JSONResponse:
        """Actor identity + CSRF token for browser (cookie-session) clients.

        The server-rendered admin embeds the CSRF token in its HTML; a JS
        client (the ``/ui/`` React app) bootstraps the same data from this
        endpoint using its session cookie. ``csrf_token`` is empty for
        bearer-key callers, whose mutations don't require CSRF headers.
        """
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "actor": {
                    "subject": ctx.subject,
                    "email": ctx.email,
                    "roles": sorted(ctx.roles),
                    "csrf_token": ctx.csrf_token or "",
                }
            },
        )

    @router.get("/overview")
    def overview(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        users = store.list_users()
        apps = store.list_apps()
        profiles = store.list_profiles()
        runtime_mappings = store.list_runtime_profile_mappings()
        credentials = _scrub_credentials(store.list_credentials())
        active_credentials = [
            c for c in credentials if not c.get("revoked_at")
        ]
        # "Things that want a look" — the operator's real landing list, derived
        # from the same data the detail tables show. Service health needs a live
        # status fetch this read-only endpoint doesn't do, so the JSON exposes
        # the App-level signals; the HTML overview adds service signals on top.
        attention = derive_admin_attention({
            "apps": apps,
            "profiles": profiles,
            "credentials": credentials,
        })
        payload = {
            "actor": {
                "subject": ctx.subject,
                "email": ctx.email,
                "roles": sorted(ctx.roles),
            },
            "access_model": access_model_summary(),
            "counts": {
                "users": len(users),
                "apps": len(apps),
                "profiles": len(profiles),
                "runtime_profile_mappings": len(runtime_mappings),
                "credentials": len(credentials),
                "active_credentials": len(active_credentials),
            },
            "attention": attention,
            "runtime_profile_mappings": runtime_mappings,
        }
        return JSONResponse(status_code=status.HTTP_200_OK, content=payload)

    @router.get("/users")
    def list_users(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"users": list(store.list_users())},
        )

    @router.get("/apps")
    def list_apps(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"apps": list(store.list_apps())},
        )

    @router.post("/apps")
    async def create_app(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
        except _RouteError as exc:
            return exc.as_response()
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_app",
                "app name is required",
            ).as_response()
        try:
            record = store.create_app(data=body, actor=ctx)
        except ValueError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_app",
                str(exc),
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_201_CREATED, content={"app": record}
        )

    @router.patch("/apps/{app_id}")
    async def update_app(app_id: str, request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
        except _RouteError as exc:
            return exc.as_response()
        if "name" in body:
            name = body.get("name")
            if not isinstance(name, str) or not name.strip():
                return _RouteError(
                    status.HTTP_400_BAD_REQUEST,
                    "invalid_app",
                    "app name is required",
                ).as_response()
        profile_id = body.get("default_profile_id", body.get("profile_id"))
        if profile_id:
            profile_key = str(profile_id).strip()
            if (
                profile_key != "default-local"
                and store.get_profile(profile_key) is None
            ):
                return _RouteError(
                    status.HTTP_400_BAD_REQUEST,
                    "invalid_app",
                    f"profile {profile_key!r} not found",
                ).as_response()
        try:
            record = store.update_app(app_id=app_id, data=body, actor=ctx)
        except ValueError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_app",
                str(exc),
            ).as_response()
        if record is None:
            return _RouteError(
                status.HTTP_404_NOT_FOUND,
                "app_not_found",
                f"app {app_id!r} not found",
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK, content={"app": record}
        )

    @router.delete("/apps/{app_id}")
    def disable_app(app_id: str, request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        record = store.disable_app(app_id=app_id, actor=ctx)
        if record is None:
            return _RouteError(
                status.HTTP_404_NOT_FOUND,
                "app_not_found",
                f"app {app_id!r} not found",
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK, content={"app": record}
        )

    @router.get("/profiles")
    def list_profiles(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"profiles": list(store.list_profiles())},
        )

    @router.post("/profiles")
    async def create_profile(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
        except _RouteError as exc:
            return exc.as_response()
        profile_block = body.get("profile") if "profile" in body else body
        try:
            load_profile(profile_block)
        except ProfileLoadError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_profile",
                str(exc),
            ).as_response()
        try:
            record = store.create_profile(data=body, actor=ctx)
        except ValueError as exc:
            msg = str(exc)
            if "already in use" in msg:
                return _RouteError(
                    status.HTTP_409_CONFLICT,
                    "profile_name_conflict",
                    msg,
                ).as_response()
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_profile",
                msg,
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_201_CREATED, content={"profile": record}
        )

    @router.get("/profiles/{profile_id}")
    def get_profile(profile_id: str, request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        record = store.get_profile(profile_id)
        if record is None:
            return _RouteError(
                status.HTTP_404_NOT_FOUND,
                "profile_not_found",
                f"profile {profile_id!r} not found",
            ).as_response()
        return JSONResponse(status_code=status.HTTP_200_OK, content={"profile": record})

    @router.patch("/profiles/{profile_id}")
    async def update_profile(profile_id: str, request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
        except _RouteError as exc:
            return exc.as_response()
        if "profile" in body:
            try:
                load_profile(body.get("profile"))
            except ProfileLoadError as exc:
                return _RouteError(
                    status.HTTP_400_BAD_REQUEST,
                    "invalid_profile",
                    str(exc),
                ).as_response()
        try:
            record = store.update_profile(
                profile_id=profile_id, data=body, actor=ctx
            )
        except ValueError as exc:
            msg = str(exc)
            if "already in use" in msg:
                return _RouteError(
                    status.HTTP_409_CONFLICT,
                    "profile_name_conflict",
                    msg,
                ).as_response()
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_profile",
                msg,
            ).as_response()
        if record is None:
            return _RouteError(
                status.HTTP_404_NOT_FOUND,
                "profile_not_found",
                f"profile {profile_id!r} not found",
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK, content={"profile": record}
        )

    @router.delete("/profiles/{profile_id}")
    def delete_profile(profile_id: str, request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            record = store.delete_profile(profile_id=profile_id, actor=ctx)
        except ValueError as exc:
            return _RouteError(
                status.HTTP_409_CONFLICT,
                "profile_in_use",
                str(exc),
            ).as_response()
        if record is None:
            return _RouteError(
                status.HTTP_404_NOT_FOUND,
                "profile_not_found",
                f"profile {profile_id!r} not found",
            ).as_response()
        return JSONResponse(status_code=status.HTTP_200_OK, content={"profile": record})

    @router.get("/runtime-profile-mappings")
    def list_runtime_profile_mappings(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "mappings": list(store.list_runtime_profile_mappings()),
            },
        )

    @router.post("/runtime-profile-mappings")
    async def create_runtime_profile_mapping(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
        except _RouteError as exc:
            return exc.as_response()
        profile_id = str(body.get("profile_id") or "").strip()
        if profile_id and profile_id != "default-local":
            if store.get_profile(profile_id) is None:
                return _RouteError(
                    status.HTTP_400_BAD_REQUEST,
                    "invalid_runtime_mapping",
                    f"profile {profile_id!r} not found",
                ).as_response()
        try:
            record = store.create_runtime_profile_mapping(data=body, actor=ctx)
        except ValueError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_runtime_mapping",
                str(exc),
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={"mapping": record},
        )

    @router.delete("/runtime-profile-mappings/{mapping_id}")
    def disable_runtime_profile_mapping(
        mapping_id: str,
        request: Request,
    ) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        record = store.disable_runtime_profile_mapping(
            mapping_id=mapping_id,
            actor=ctx,
        )
        if record is None:
            return _RouteError(
                status.HTTP_404_NOT_FOUND,
                "runtime_mapping_not_found",
                f"runtime Profile mapping {mapping_id!r} not found",
            ).as_response()
        return JSONResponse(status_code=status.HTTP_200_OK, content={"mapping": record})

    @router.post("/apps/{app_id}/keys")
    async def create_key(app_id: str, request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
        except _RouteError as exc:
            return exc.as_response()
        if store.get_app(app_id) is None:
            return _RouteError(
                status.HTTP_404_NOT_FOUND,
                "app_not_found",
                f"app {app_id!r} not found",
            ).as_response()
        try:
            record = store.create_credential(app_id=app_id, data=body, actor=ctx)
        except ValueError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_credential",
                str(exc),
            ).as_response()
        # The store is allowed (and expected) to return a one-time ``api_key``
        # here. List/read endpoints scrub it.
        return JSONResponse(
            status_code=status.HTTP_201_CREATED, content={"credential": record}
        )

    @router.post("/admin-keys")
    async def create_admin_key(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
        except _RouteError as exc:
            return exc.as_response()
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_admin_key",
                "admin automation key name is required",
            ).as_response()
        role = str(body.get("role") or "admin").strip()
        if role not in {"admin", "owner"}:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_admin_key",
                "role must be one of: admin, owner",
            ).as_response()
        if role == "owner" and "owner" not in set(ctx.roles):
            return _RouteError(
                status.HTTP_403_FORBIDDEN,
                "admin_role_forbidden",
                "owner role required to mint owner keys",
            ).as_response()
        try:
            record = store.create_admin_automation_key(
                data={**body, "role": role}, actor=ctx
            )
        except ValueError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_admin_key",
                str(exc),
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_201_CREATED, content={"credential": record}
        )

    @router.get("/credentials")
    def list_credentials(request: Request) -> JSONResponse:
        """Flat list of every credential (app keys + admin automation keys).

        Records are scrubbed of key material. Clients group by ``app_id`` /
        ``subject_kind`` to render per-app key rows and active-key counts.
        """
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "credentials": _scrub_credentials(store.list_credentials())
            },
        )

    @router.delete("/credentials/{credential_id}")
    def delete_credential(
        credential_id: str, request: Request
    ) -> JSONResponse:
        """DELETE alias for POST …/revoke — idiomatic REST revocation."""
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        record = store.revoke_credential(
            credential_id=credential_id, actor=ctx
        )
        if record is None:
            return _RouteError(
                status.HTTP_404_NOT_FOUND,
                "credential_not_found",
                f"credential {credential_id!r} not found",
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"credential": _scrub_credential(record)},
        )

    @router.post("/credentials/{credential_id}/revoke")
    def revoke_credential(
        credential_id: str, request: Request
    ) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        record = store.revoke_credential(
            credential_id=credential_id, actor=ctx
        )
        if record is None:
            return _RouteError(
                status.HTTP_404_NOT_FOUND,
                "credential_not_found",
                f"credential {credential_id!r} not found",
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"credential": _scrub_credential(record)},
        )

    @router.get("/operator/overrides")
    def list_operator_overrides(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"overrides": store.operator_overrides()},
        )

    @router.put("/operator/user-access-defaults")
    async def update_user_access_defaults(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            defaults = _validate_user_access_defaults(body)
            record = store.update_user_access_defaults(data=defaults, actor=ctx)
        except _RouteError as exc:
            return exc.as_response()
        except ValueError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_operator_override",
                str(exc),
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"user_access_defaults": record},
        )

    @router.put("/operator/service-availability")
    async def update_service_availability_overrides(
        request: Request,
    ) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            overrides = _validate_service_availability_overrides(body)
            current = store.operator_overrides().get("service_availability", {})
            merged = dict(current) if isinstance(current, Mapping) else {}
            for service_id, override in overrides.items():
                clears_default = (
                    override.get("state") == "available"
                    and override.get("reason") is None
                    and override.get("retry_after_seconds") is None
                )
                if clears_default:
                    merged.pop(service_id, None)
                else:
                    merged[service_id] = override
            record = store.update_service_availability_overrides(
                data=merged,
                actor=ctx,
            )
        except _RouteError as exc:
            return exc.as_response()
        except ValueError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_operator_override",
                str(exc),
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"service_availability": record},
        )

    @router.get("/operator/service-registry")
    def get_service_registry(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"service_registry": store.get_service_registry()},
        )

    @router.put("/operator/service-registry")
    async def update_service_registry(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            registry = _validate_service_registry(body)
            record = store.update_service_registry(data=registry, actor=ctx)
        except _RouteError as exc:
            return exc.as_response()
        except ValueError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_service_registry",
                str(exc),
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"service_registry": record},
        )

    def _service_op_error(exc: ValueError) -> JSONResponse:
        """Map a registry-op ValueError to the right operator-facing status."""
        msg = str(exc)
        if "not found" in msg:
            code, http = "service_not_found", status.HTTP_404_NOT_FOUND
        elif "already exists" in msg:
            code, http = "service_conflict", status.HTTP_409_CONFLICT
        elif "not editable" in msg or "provisioned" in msg:
            code, http = "service_not_editable", status.HTTP_403_FORBIDDEN
        else:
            code, http = "invalid_service", status.HTTP_400_BAD_REQUEST
        return _RouteError(http, code, msg).as_response()

    @router.post("/operator/services")
    async def add_service(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            form = _validate_service_form(body)
            record = store.add_service(data=form, actor=ctx)
        except _RouteError as exc:
            return exc.as_response()
        except ValueError as exc:
            return _service_op_error(exc)
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={"service_registry": record},
        )

    @router.patch("/operator/services/{service_id}")
    async def update_service(service_id: str, request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            form = _validate_service_form({**body, "name": body.get("name") or service_id})
            record = store.update_service(service_id=service_id, data=form, actor=ctx)
        except _RouteError as exc:
            return exc.as_response()
        except ValueError as exc:
            return _service_op_error(exc)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"service_registry": record},
        )

    @router.delete("/operator/services/{service_id}")
    def remove_service(service_id: str, request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            record = store.remove_service(service_id=service_id, actor=ctx)
        except ValueError as exc:
            return _service_op_error(exc)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"service_registry": record},
        )

    @router.post("/operator/services/{service_id}/enabled")
    async def set_service_enabled(service_id: str, request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            enabled = bool(body.get("enabled"))
            record = store.set_service_enabled(
                service_id=service_id, enabled=enabled, actor=ctx
            )
        except _RouteError as exc:
            return exc.as_response()
        except ValueError as exc:
            return _service_op_error(exc)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"service_registry": record},
        )

    @router.post("/operator/services/test")
    async def test_service_connection(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if service_probe is None:
            return _RouteError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "probe_unavailable",
                "connection testing is not available in this process",
            ).as_response()
        try:
            body = await _read_json_body(request)
        except _RouteError as exc:
            return exc.as_response()
        # Accept the live form draft (base_url or host/port) without persisting.
        raw = service_registry.service_form_to_raw({**body, "name": body.get("name") or "probe"})
        host = str(raw.get("host") or "").strip()
        if not host:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST, "invalid_service", "a host or base URL is required"
            ).as_response()
        port = raw.get("port")
        path = str(raw.get("health_endpoint") or (str(raw.get("base_path") or "/v1").rstrip("/") + "/models"))
        result = await service_probe(host, port, path)
        return JSONResponse(status_code=status.HTTP_200_OK, content={"probe": dict(result)})

    @router.post("/operator/services/detect-models")
    async def detect_service_models(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if service_probe is None:
            return _RouteError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "probe_unavailable",
                "model detection is not available in this process",
            ).as_response()
        try:
            body = await _read_json_body(request)
        except _RouteError as exc:
            return exc.as_response()
        raw = service_registry.service_form_to_raw({**body, "name": body.get("name") or "probe"})
        host = str(raw.get("host") or "").strip()
        if not host:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST, "invalid_service", "a host or base URL is required"
            ).as_response()
        port = raw.get("port")
        base = str(raw.get("base_path") or "/v1").rstrip("/")
        models_path = base + "/models"
        try:
            result = await service_probe(host, port, models_path)
            if not result.get("ok"):
                return JSONResponse(
                    status_code=status.HTTP_200_OK,
                    content={
                        "detected": {
                            "models": [],
                            "ok": False,
                            "error": result.get("error") or f"HTTP {result.get('status')}",
                        }
                    },
                )
            resp_body = result.get("body")
            models: list[str] = []
            if isinstance(resp_body, dict):
                data = resp_body.get("data") or resp_body.get("models") or []
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            mid = item.get("id")
                            if isinstance(mid, str):
                                models.append(mid)
                        elif isinstance(item, str):
                            models.append(item)
            elif isinstance(resp_body, list):
                for item in resp_body:
                    if isinstance(item, dict):
                        mid = item.get("id")
                        if isinstance(mid, str):
                            models.append(mid)
                    elif isinstance(item, str):
                        models.append(item)
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"detected": {"models": models, "ok": True, "error": ""}},
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "detected": {
                        "models": [],
                        "ok": False,
                        "error": str(exc) or type(exc).__name__,
                    }
                },
            )

    @router.get("/operator/litellm-config")
    def get_litellm_config(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        registry = store.get_service_registry()
        compiled = compile_litellm_substrate_config(
            (registry.get("services") or {}),
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"litellm_config": compiled},
        )

    @router.get("/extensions")
    def list_extension_packages(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"extension_packages": store.get_extension_package_index()},
        )

    @router.post("/extensions/validate")
    async def validate_extension_package(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            validated = _validate_extension_package_source(body)
            review = store.validate_extension_package_source(
                source=validated["source"],
                actor=ctx,
            )
        except _RouteError as exc:
            return exc.as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"review": review},
        )

    @router.post("/extensions/enable")
    async def enable_extension_package(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            validated = _validate_extension_package_source(body)
            source = dict(validated["source"])
            reviewed = body.get("review") if isinstance(body, Mapping) else None
            resolved_source = (
                reviewed.get("resolved_source")
                if isinstance(reviewed, Mapping)
                else body.get("resolved_source")
            )
            if isinstance(resolved_source, Mapping):
                resolved_ref = resolved_source.get("resolved_ref")
                if resolved_ref:
                    source["resolved_ref"] = str(resolved_ref)
            result = store.enable_extension_package(
                source=source,
                actor=ctx,
            )
            review = result.get("review") if isinstance(result, Mapping) else None
            if isinstance(review, Mapping) and not review.get("ok"):
                return _RouteError(
                    status.HTTP_400_BAD_REQUEST,
                    "invalid_extension_package",
                    "; ".join(str(item) for item in review.get("errors") or [])
                    or "invalid extension package",
                ).as_response()
        except _RouteError as exc:
            return exc.as_response()
        except service_registry.ServiceRegistryConflict as exc:
            return _RouteError(
                status.HTTP_409_CONFLICT,
                "extension_package_conflict",
                str(exc),
            ).as_response()
        except ValueError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_extension_package",
                str(exc),
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=result,
        )

    @router.put("/operator/media-job-admission")
    async def update_media_job_admission_overrides(
        request: Request,
    ) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            overrides = _validate_media_job_admission_overrides(body)
            record = store.update_media_job_admission_overrides(
                data=overrides,
                actor=ctx,
            )
        except _RouteError as exc:
            return exc.as_response()
        except ValueError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_operator_override",
                str(exc),
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"media_job_admission": record},
        )

    @router.get("/oidc-config")
    def get_oidc_config(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        record = store.get_oidc_config_setting()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"oidc_config": record},
        )

    @router.put("/oidc-config")
    async def update_oidc_config(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            payload = _validate_oidc_config_payload(body)
            record = store.update_oidc_config_setting(data=payload, actor=ctx)
        except _RouteError as exc:
            return exc.as_response()
        except ValueError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_oidc_config",
                str(exc),
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"oidc_config": record},
        )

    @router.get("/audit")
    def audit(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        limit = _coerce_limit(
            request.query_params.get("limit"),
            default=_AUDIT_DEFAULT_LIMIT,
            maximum=_AUDIT_MAX_LIMIT,
        )
        entries = store.recent_audit(limit=limit)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"audit": list(entries), "limit": limit},
        )

    @router.get("/audit-events")
    def audit_events(request: Request) -> JSONResponse:
        """Canonical noun-based alias for GET /audit."""
        return audit(request)

    @router.post("/policy/simulate")
    async def simulate(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            profile = _resolve_profile(
                body.get("profile"), profile_resolver=profile_resolver
            )
            policy_request = _build_policy_request(body.get("request"))
        except _RouteError as exc:
            return exc.as_response()
        decision = evaluate(profile, policy_request)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "profile_id": profile.id,
                "decision": _decision_payload(decision),
            },
        )

    return router
