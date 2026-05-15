"""Read-only ``/gateway/v1`` gateway-native router.

Thin FastAPI layer over the gateway's pure discovery, capability, and receipt
modules. The router does not import :mod:`proxy`, does not touch live auth, and
does not mutate any state. Wiring into the live app — auth, profile resolution,
audience policy, capability assembly, receipt persistence — is the caller's
job. Tests can construct the router with fixture providers.

Errors are protocol-shaped (``{"error": {"code": ..., "message": ...}}``) and,
for the ``client`` audience, redacted to avoid leaking local filesystem paths
or other gateway-internal details.

Endpoints
---------

* ``GET /gateway/v1/workflows``               — list workflows (audience-scoped)
* ``GET /gateway/v1/workflows/{id}``          — single workflow summary
* ``GET /gateway/v1/capabilities``            — list capabilities
* ``GET /gateway/v1/receipts``                — recent route receipts
* ``GET /gateway/v1/receipts/{request_id}``   — one route receipt

Capability and receipt endpoints return empty results (or a protocol-shaped
404) when their optional providers are not wired. Mounting the router without
a capability provider, for example, yields an empty capability catalog rather
than a crash.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse

from gateway.capabilities import Capability, summarize_capabilities
from gateway.discovery import VALID_AUDIENCES, WorkflowRegistry

AudienceResolver = Callable[[Request], str]
CapabilitiesProvider = Callable[[Request], Sequence[Capability] | Iterable[Capability]]
ReceiptGetter = Callable[..., dict[str, Any] | None]
ReceiptLister = Callable[..., list[dict[str, Any]]]

DEFAULT_AUDIENCE = "client"

# Cap recent-receipt listings to keep the response small and predictable.
RECEIPTS_DEFAULT_LIMIT = 50
RECEIPTS_MAX_LIMIT = 200


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


def _internal_error(audience: str, exc: BaseException) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_error_payload(
            "gateway_internal_error",
            _client_safe_message(str(exc), audience),
        ),
    )


def _build_receipt_providers(
    *,
    receipt_getter: ReceiptGetter | None,
    receipt_lister: ReceiptLister | None,
    receipt_store_path: str | Path | None,
) -> tuple[ReceiptGetter | None, ReceiptLister | None]:
    """Wire receipt providers, defaulting to the SQLite-backed store when a
    path is supplied. Explicit callables always win over the path shortcut so
    tests can inject pure in-memory providers.
    """
    if receipt_store_path is None:
        return receipt_getter, receipt_lister
    if receipt_getter is not None and receipt_lister is not None:
        return receipt_getter, receipt_lister

    from gateway import receipt_store as _receipt_store

    store_path = Path(receipt_store_path)

    def store_getter(
        request_id: str, *, audience: str
    ) -> dict[str, Any] | None:
        return _receipt_store.get(
            request_id, audience=audience, path=store_path
        )

    def store_lister(
        *,
        audience: str,
        limit: int,
        app_profile: str | None = None,
        since_s: int | None = None,
    ) -> list[dict[str, Any]]:
        return _receipt_store.recent(
            limit=limit,
            audience=audience,
            app_profile=app_profile,
            since_s=since_s,
            path=store_path,
        )

    return (
        receipt_getter or store_getter,
        receipt_lister or store_lister,
    )


def create_gateway_router(
    registry: WorkflowRegistry,
    *,
    audience_resolver: AudienceResolver | None = None,
    capabilities_provider: CapabilitiesProvider | None = None,
    receipt_getter: ReceiptGetter | None = None,
    receipt_lister: ReceiptLister | None = None,
    receipt_store_path: str | Path | None = None,
    prefix: str = "/gateway/v1",
) -> APIRouter:
    """Build a read-only gateway router.

    ``audience_resolver`` is invoked per-request to pick the discovery
    audience (``client``/``admin``/``internal``). The default resolver always
    returns ``client`` so this router is safe to mount without wiring auth.

    Optional dependency injection:

    * ``capabilities_provider`` — callable taking the request and returning an
      iterable of :class:`Capability`. When omitted, the capabilities endpoint
      returns an empty catalog.
    * ``receipt_getter`` / ``receipt_lister`` — fully custom callables. When a
      ``receipt_store_path`` is supplied instead, the router wires the durable
      :mod:`gateway.receipt_store` backend automatically. When neither is
      provided, the receipt endpoints return empty lists / protocol-shaped
      404s.

    Callers wiring real auth should pass a resolver that consults the
    authenticated profile and policy decision.
    """
    resolve_audience = audience_resolver or _default_audience_resolver
    receipt_getter, receipt_lister = _build_receipt_providers(
        receipt_getter=receipt_getter,
        receipt_lister=receipt_lister,
        receipt_store_path=receipt_store_path,
    )

    router = APIRouter(prefix=prefix, tags=["gateway-discovery"])

    @router.get("/workflows")
    def list_workflows(request: Request) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            catalog = registry.summarize_catalog(audience=audience)
        except Exception as exc:  # noqa: BLE001 — translate to protocol error
            return _internal_error(audience, exc)
        return JSONResponse(status_code=status.HTTP_200_OK, content=catalog)

    @router.get("/workflows/{workflow_id}")
    def get_workflow(workflow_id: str, request: Request) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            summary = registry.summarize_workflow(workflow_id, audience=audience)
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        if summary is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=_error_payload(
                    "workflow_not_found",
                    f"workflow {workflow_id!r} not found",
                ),
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=summary)

    @router.get("/capabilities")
    def list_capabilities(request: Request) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            if capabilities_provider is None:
                capabilities: Iterable[Capability] = ()
            else:
                capabilities = capabilities_provider(request) or ()
            catalog = summarize_capabilities(capabilities, audience=audience)
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        return JSONResponse(status_code=status.HTTP_200_OK, content=catalog)

    @router.get("/receipts")
    def list_receipts(
        request: Request,
        limit: int = Query(
            RECEIPTS_DEFAULT_LIMIT, ge=1, le=RECEIPTS_MAX_LIMIT
        ),
        app_profile: str | None = Query(None),
        since_s: int | None = Query(None, ge=0),
    ) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            if receipt_lister is None:
                rows: list[dict[str, Any]] = []
            else:
                rows = list(
                    receipt_lister(
                        audience=audience,
                        limit=limit,
                        app_profile=app_profile,
                        since_s=since_s,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "schema_version": 1,
                "audience": audience,
                "receipts": rows,
            },
        )

    @router.get("/receipts/{request_id}")
    def get_receipt(request_id: str, request: Request) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            row = (
                None
                if receipt_getter is None
                else receipt_getter(request_id, audience=audience)
            )
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        if row is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=_error_payload(
                    "receipt_not_found",
                    f"receipt {request_id!r} not found",
                ),
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=row)

    return router
