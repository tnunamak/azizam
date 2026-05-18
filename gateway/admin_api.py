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
``proxy.py``, and never returns raw key material outside the one-time
``POST /admin/api/apps/{app_id}/keys`` response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from gateway.policy import (
    AppProfile,
    PolicyDecision,
    PolicyRequest,
    evaluate,
)
from gateway.profiles import ProfileLoadError, load_profile

__all__ = [
    "AdminAuthError",
    "AdminContext",
    "AdminStore",
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
    JSON-able dicts; raw key material must only ever appear in
    :meth:`create_credential`'s return value under the key ``"api_key"``.
    """

    def list_users(self) -> list[dict[str, Any]]: ...

    def list_apps(self) -> list[dict[str, Any]]: ...

    def get_app(self, app_id: str) -> dict[str, Any] | None: ...

    def create_app(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def list_profiles(self) -> list[dict[str, Any]]: ...

    def create_profile(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def list_credentials(self) -> list[dict[str, Any]]: ...

    def create_credential(
        self, *, app_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def revoke_credential(
        self, *, credential_id: str, actor: AdminContext
    ) -> dict[str, Any] | None: ...

    def recent_audit(self, *, limit: int) -> list[dict[str, Any]]: ...


AdminAuth = Callable[[Request], AdminContext]
ProfileResolver = Callable[[str], AppProfile | None]


_AUDIT_DEFAULT_LIMIT = 50
_AUDIT_MAX_LIMIT = 200


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
        and request.headers.get("x-ai-gateway-admin-action") != "1"
    ):
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
    return {k: v for k, v in record.items() if k not in {"api_key", "key", "secret"}}


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
    return PolicyRequest(**{k: payload[k] for k in payload})


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
    prefix: str = "/admin/api",
) -> APIRouter:
    """Build an admin-plane router.

    ``store`` provides persistence. ``auth`` is invoked on every request and
    must raise :class:`AdminAuthError` for unauthenticated or forbidden
    callers. ``profile_resolver`` lets the policy simulator accept a stored
    profile id (string) rather than an inline profile dict.
    """

    router = APIRouter(prefix=prefix, tags=["admin"])

    def _guard(request: Request) -> AdminContext | JSONResponse:
        return _require_admin(auth, request)

    @router.get("/overview")
    def overview(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        users = store.list_users()
        apps = store.list_apps()
        profiles = store.list_profiles()
        credentials = _scrub_credentials(store.list_credentials())
        active_credentials = [
            c for c in credentials if not c.get("revoked_at")
        ]
        payload = {
            "actor": {"subject": ctx.subject, "email": ctx.email},
            "counts": {
                "users": len(users),
                "apps": len(apps),
                "profiles": len(profiles),
                "credentials": len(credentials),
                "active_credentials": len(active_credentials),
            },
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
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_profile",
                str(exc),
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_201_CREATED, content={"profile": record}
        )

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
