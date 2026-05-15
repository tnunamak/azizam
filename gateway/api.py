"""Read-only ``/gateway/v1`` discovery router.

Thin FastAPI layer over :class:`gateway.discovery.WorkflowRegistry`. The router
is pure in the sense that it does not import :mod:`proxy`, does not touch live
auth, and does not mutate any state. Wiring into the live app — auth, profile
resolution, audience policy — is the caller's job. Tests can construct the
router with a fixture registry and a stub audience resolver.

Errors are protocol-shaped (``{"error": {"code": ..., "message": ...}}``) and,
for the ``client`` audience, redacted to avoid leaking local filesystem paths
or other gateway-internal details.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from gateway.discovery import VALID_AUDIENCES, WorkflowRegistry

AudienceResolver = Callable[[Request], str]

DEFAULT_AUDIENCE = "client"


def _default_audience_resolver(_: Request) -> str:
    return DEFAULT_AUDIENCE


def _normalize_audience(value: str | None) -> str:
    if value is None:
        return DEFAULT_AUDIENCE
    if value not in VALID_AUDIENCES:
        return DEFAULT_AUDIENCE
    return value


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _client_safe_message(message: str, audience: str) -> str:
    if audience == "client":
        return "internal gateway error"
    return message


def _resolve_request_audience(
    resolver: AudienceResolver,
    request: Request,
) -> str:
    try:
        return _normalize_audience(resolver(request))
    except Exception:
        return DEFAULT_AUDIENCE


def create_gateway_router(
    registry: WorkflowRegistry,
    *,
    audience_resolver: AudienceResolver | None = None,
    prefix: str = "/gateway/v1",
) -> APIRouter:
    """Build a read-only discovery router backed by ``registry``.

    ``audience_resolver`` is invoked per-request to pick the discovery
    audience (``client``/``admin``/``internal``). The default resolver always
    returns ``client`` so this router is safe to mount without wiring auth.
    Callers wiring real auth should pass a resolver that consults the
    authenticated profile and policy decision.
    """
    resolve_audience = audience_resolver or _default_audience_resolver
    router = APIRouter(prefix=prefix, tags=["gateway-discovery"])

    @router.get("/workflows")
    def list_workflows(request: Request) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            catalog = registry.summarize_catalog(audience=audience)
        except Exception as exc:  # noqa: BLE001 — translate to protocol error
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content=_error_payload(
                    "gateway_internal_error",
                    _client_safe_message(str(exc), audience),
                ),
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=catalog)

    @router.get("/workflows/{workflow_id}")
    def get_workflow(workflow_id: str, request: Request) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            summary = registry.summarize_workflow(workflow_id, audience=audience)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content=_error_payload(
                    "gateway_internal_error",
                    _client_safe_message(str(exc), audience),
                ),
            )
        if summary is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=_error_payload(
                    "workflow_not_found",
                    f"workflow {workflow_id!r} not found",
                ),
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=summary)

    return router
