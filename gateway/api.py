"""Gateway-native ``/gateway/v1`` router.

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
* ``GET /gateway/v1/aliases``                 — stable + OpenAI model aliases and what they resolve to
* ``GET /gateway/v1/aliases/resolve``         — dry-run one alias (``operation``, ``model``)
* ``GET /gateway/v1/extensions``              — list extension namespaces
* ``GET /gateway/v1/models/{model}/capabilities`` — model capability view
* ``GET /gateway/v1/receipts``                — recent route receipts
* ``GET /gateway/v1/receipts/{request_id}``   — one route receipt
* ``GET /gateway/v1/resources``               — client-visible backend resource/queue status
* ``POST /gateway/v1/media/jobs``             — create/attach or delegate executable media job submit
* ``GET /gateway/v1/media/jobs``              — recent media jobs
* ``GET /gateway/v1/media/jobs/{job_id}``     — single media job
* ``GET /gateway/v1/media/jobs/{job_id}/events`` — list job events
* ``POST /gateway/v1/media/jobs/{job_id}/events`` — append a job event
* ``POST /gateway/v1/media/jobs/{job_id}/cancel`` — cancel a non-terminal job

Capability, receipt, and media-job endpoints return empty results (or a
protocol-shaped 404) when their optional providers are not wired. Mounting the
router without a capability provider, for example, yields an empty capability
catalog rather than a crash.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from fastapi import APIRouter, Body, Query, Request, status
from fastapi.responses import JSONResponse

from gateway import egress
from gateway import output_availability
from gateway import media_jobs as _media_jobs
from gateway import receipts as _receipts
from gateway.capabilities import Capability, summarize_capabilities
from gateway.discovery import VALID_AUDIENCES, WorkflowRegistry
from gateway.extension_catalog import (
    summarize_extensions,
    summarize_model_capabilities,
)

AudienceResolver = Callable[[Request], str]
CapabilitiesProvider = Callable[[Request], Iterable[Capability] | Awaitable[Iterable[Capability]]]
ReceiptGetter = Callable[..., dict[str, Any] | None]
ReceiptLister = Callable[..., list[dict[str, Any]]]
ReceiptViewerResolver = Callable[[Request], str | None]
ResourceLister = Callable[..., list[dict[str, Any]] | Awaitable[list[dict[str, Any]]]]
MediaJobCreator = Callable[..., dict[str, Any]]
MediaJobGetter = Callable[..., dict[str, Any] | None]
MediaJobLister = Callable[..., list[dict[str, Any]]]
MediaJobEventLister = Callable[..., list[dict[str, Any]]]
MediaJobEventAppender = Callable[..., int | Mapping[str, Any]]
MediaJobCanceler = Callable[..., dict[str, Any] | None]
MediaJobSubmitter = Callable[..., Any]
RecordAuthorizer = Callable[[Request, str, Mapping[str, Any]], bool]
LedgerProvider = Callable[[Request, str | None], dict[str, Any] | None]

DEFAULT_AUDIENCE = "client"

# Cap recent-receipt listings to keep the response small and predictable.
RECEIPTS_DEFAULT_LIMIT = 50
RECEIPTS_MAX_LIMIT = 200

# Same caps for media-job listings.
MEDIA_JOBS_DEFAULT_LIMIT = 50
MEDIA_JOBS_MAX_LIMIT = 200


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


# One response serializer per resource. ``internal`` is the operator tier: it adds
# the raw ``metadata`` bag to the admin shape, nothing else. Owner and input
# identifiers are never in an API body, whatever the audience.
def _job_body(row: Mapping[str, Any], resolver: output_availability.AssetPathResolver | None = None) -> dict[str, Any]:
    return output_availability.project(
        {k: v for k, v in row.items() if k in _media_jobs.ADMIN_VISIBLE_FIELDS | {"metadata"}}, resolver)


def _receipt_body(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k in _receipts.OPERATOR_VISIBLE_FIELDS}


def _record_body(kind: str, row: Mapping[str, Any],
                 resolver: output_availability.AssetPathResolver | None = None) -> dict[str, Any]:
    return _receipt_body(row) if kind == "receipt" else _job_body(row, resolver)


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
        request_id: str, *, audience: str, viewer_person_id: str | None = None,
    ) -> dict[str, Any] | None:
        return _receipt_store.get(
            request_id, audience=audience, viewer_person_id=viewer_person_id, path=store_path
        )

    def store_many(ids: Iterable[str], *, audience: str, viewer_person_id: str | None = None) -> dict[str, dict[str, Any]]:
        return _receipt_store.get_many(
            ids, audience=audience, viewer_person_id=viewer_person_id, path=store_path,
        )

    store_getter.many = store_many  # type: ignore[attr-defined]

    def store_lister(
        *,
        audience: str,
        viewer_person_id: str | None = None,
        limit: int,
        app_profile: str | None = None,
        app_ids: list[str] | None = None,
        owner_key_hashes: list[str] | None = None,
        since_s: int | None = None,
    ) -> list[dict[str, Any]]:
        return _receipt_store.recent(
            limit=limit,
            audience=audience,
            viewer_person_id=viewer_person_id,
            app_profile=app_profile,
            app_ids=app_ids,
            owner_key_hashes=owner_key_hashes,
            since_s=since_s,
            path=store_path,
        )

    return (
        receipt_getter or store_getter,
        receipt_lister or store_lister,
    )


def _build_media_job_providers(
    *,
    media_job_creator: MediaJobCreator | None,
    media_job_getter: MediaJobGetter | None,
    media_job_lister: MediaJobLister | None,
    media_job_event_lister: MediaJobEventLister | None,
    media_job_event_appender: MediaJobEventAppender | None,
    media_job_canceler: MediaJobCanceler | None,
    media_job_store_path: str | Path | None,
) -> tuple[
    MediaJobCreator | None,
    MediaJobGetter | None,
    MediaJobLister | None,
    MediaJobEventLister | None,
    MediaJobEventAppender | None,
    MediaJobCanceler | None,
]:
    """Wire media-job providers, defaulting to the SQLite-backed store when a
    path is supplied. Explicit callables always win over the path shortcut.
    """
    if media_job_store_path is None:
        return (
            media_job_creator,
            media_job_getter,
            media_job_lister,
            media_job_event_lister,
            media_job_event_appender,
            media_job_canceler,
        )


    store_path = Path(media_job_store_path)

    def store_creator(
        payload: Mapping[str, Any], *, audience: str
    ) -> dict[str, Any]:
        # Raw clients submit work; only trusted adapters can report output or
        # lifecycle state. Keep legacy creation metadata separate from phases.
        submission = {key: payload[key] for key in (
            "intent", "workflow", "backend", "request_id", "job_id", "created_at",
            "app_profile", "app_id", "owner_key_hash", "api_key_name", "parent_request_id", "metadata",
        ) if key in payload}
        record = _media_jobs.create(submission, path=store_path)
        return record.to_dict(audience)

    def store_getter(job_id: str, *, audience: str) -> dict[str, Any] | None:
        return _media_jobs.get(job_id, audience=audience, path=store_path)

    def store_many(ids: Iterable[str], *, audience: str) -> dict[str, dict[str, Any]]:
        return _media_jobs.get_many(ids, audience=audience, path=store_path)

    store_getter.many = store_many  # type: ignore[attr-defined]

    def store_lister(
        *,
        audience: str,
        limit: int,
        app_profile: str | None = None,
        app_ids: list[str] | None = None,
        owner_key_hashes: list[str] | None = None,
        status: str | None = None,
        since_s: int | None = None,
    ) -> list[dict[str, Any]]:
        return _media_jobs.recent(
            limit=limit,
            audience=audience,
            app_profile=app_profile,
            app_ids=app_ids,
            owner_key_hashes=owner_key_hashes,
            status=status,
            since_s=since_s,
            path=store_path,
        )

    def store_event_appender(
        job_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None,
        *,
        audience: str,
    ) -> dict[str, int]:
        sequence = _media_jobs.append_client_event(
            job_id, event_type, payload, path=store_path
        )
        event = _media_jobs.events(job_id, after_sequence=sequence - 1, limit=1, path=store_path)[0]
        return {"id": event["id"], "sequence": event["sequence"]}

    def store_event_lister(
        job_id: str, *, audience: str, limit: int, after_id: int | None = None,
        after_sequence: int | None = None,
    ) -> list[dict[str, Any]]:
        return _media_jobs.events(
            job_id,
            audience=audience,
            limit=limit,
            after_id=after_id,
            after_sequence=after_sequence,
            path=store_path,
        )

    def store_canceler(
        job_id: str, *, audience: str, reason: str | None = None
    ) -> dict[str, Any] | None:
        updated = _media_jobs.request_cancel(job_id, reason=reason, path=store_path)
        if updated is None:
            return None
        return _media_jobs.get(job_id, audience=audience, path=store_path)

    return (
        media_job_creator or store_creator,
        media_job_getter or store_getter,
        media_job_lister or store_lister,
        media_job_event_lister or store_event_lister,
        media_job_event_appender or store_event_appender,
        media_job_canceler or store_canceler,
    )


def create_gateway_router(
    registry: WorkflowRegistry,
    *,
    audience_resolver: AudienceResolver | None = None,
    capabilities_provider: CapabilitiesProvider | None = None,
    receipt_getter: ReceiptGetter | None = None,
    receipt_lister: ReceiptLister | None = None,
    receipt_store_path: str | Path | None = None,
    receipt_viewer_resolver: ReceiptViewerResolver | None = None,
    resource_lister: ResourceLister | None = None,
    asset_url_ttl_s: int | None = None,
    asset_path_resolver: output_availability.AssetPathResolver | None = None,
    media_job_creator: MediaJobCreator | None = None,
    media_job_getter: MediaJobGetter | None = None,
    media_job_lister: MediaJobLister | None = None,
    media_job_event_lister: MediaJobEventLister | None = None,
    media_job_event_appender: MediaJobEventAppender | None = None,
    media_job_canceler: MediaJobCanceler | None = None,
    media_job_submitter: MediaJobSubmitter | None = None,
    media_job_store_path: str | Path | None = None,
    record_authorizer: RecordAuthorizer | None = None,
    record_scope_provider: Callable[[Request], dict[str, Any]] | None = None,
    aliases_provider: Callable[[str | None], dict[str, Any]] | None = None,
    alias_resolver: Callable[[str, str], dict[str, Any]] | None = None,
    entry_visible: Callable[[Request, Mapping[str, Any]], bool] | None = None,
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
    * ``receipt_viewer_resolver`` — authenticated person behind the caller's
      credential or session. Without it, non-internal receipt reads hide
      retention reasons and deadlines.

    Callers wiring real auth must also pass ``record_authorizer``. Audience
    filtering controls field disclosure, not ownership. The authorizer sees
    internal records and the request, and must fail closed for unattributed
    records. It is invoked before reads and mutations of receipts/media jobs.
    ``record_scope_provider`` passes trusted App/key allow-lists to list
    providers so ownership is applied before their limit. Custom list providers
    must accept these scope fields when this callback is configured.
    """
    resolve_audience = audience_resolver or _default_audience_resolver
    receipt_getter, receipt_lister = _build_receipt_providers(
        receipt_getter=receipt_getter,
        receipt_lister=receipt_lister,
        receipt_store_path=receipt_store_path,
    )
    (
        media_job_creator,
        media_job_getter,
        media_job_lister,
        media_job_event_lister,
        media_job_event_appender,
        media_job_canceler,
    ) = _build_media_job_providers(
        media_job_creator=media_job_creator,
        media_job_getter=media_job_getter,
        media_job_lister=media_job_lister,
        media_job_event_lister=media_job_event_lister,
        media_job_event_appender=media_job_event_appender,
        media_job_canceler=media_job_canceler,
        media_job_store_path=media_job_store_path,
    )

    router = APIRouter(prefix=prefix, tags=["gateway-discovery"])

    def scoped_record(
        getter: ReceiptGetter | None,
        record_id: str,
        request: Request,
        kind: str,
        audience: str,
    ) -> dict[str, Any] | None:
        if getter is None:
            return None
        if record_authorizer is not None:
            internal = getter(record_id, audience="internal")
            if internal is None or not record_authorizer(request, kind, internal):
                return None
        viewer = receipt_viewer_resolver(request) if kind == "receipt" and receipt_viewer_resolver else None
        row = getter(record_id, audience=audience, viewer_person_id=viewer) if kind == "receipt" else getter(record_id, audience=audience)
        return None if row is None else _record_body(kind, row, asset_path_resolver)

    def scoped_rows(
        rows: list[dict[str, Any]],
        getter: ReceiptGetter | None,
        request: Request,
        kind: str,
        audience: str,
    ) -> list[dict[str, Any]]:
        if record_authorizer is None:
            return [_record_body(kind, row, asset_path_resolver) for row in rows]
        id_field = "request_id" if kind == "receipt" else "job_id"
        many = getattr(getter, "many", None)
        if many is not None:
            # The store reads every authorized row at the caller's audience in one
            # query. The rows were listed at the internal audience and checked here,
            # so the per-record re-check of an unchanged record is not repeated.
            authorized = [str(row[id_field]) for row in rows if record_authorizer(request, kind, row)]
            viewer = receipt_viewer_resolver(request) if kind == "receipt" and receipt_viewer_resolver else None
            visible_rows = many(authorized, audience=audience, viewer_person_id=viewer) if kind == "receipt" else many(authorized, audience=audience)
            return [_record_body(kind, visible_rows[record_id], asset_path_resolver) for record_id in authorized if record_id in visible_rows]
        allowed: list[dict[str, Any]] = []
        for row in rows:
            if not record_authorizer(request, kind, row):
                continue
            # Re-read at the caller's audience: ownership needs internal
            # attribution, which must never appear in a client response.
            visible = scoped_record(getter, str(row[id_field]), request, kind, audience)
            if visible is not None:
                allowed.append(visible)
        return allowed

    def job_not_found(job_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_error_payload("media_job_not_found", f"media job {job_id!r} not found"),
        )

    @router.get("/workflows")
    def list_workflows(request: Request) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            catalog = registry.summarize_catalog(audience=audience)
            if entry_visible is not None:
                catalog["workflows"] = [row for row in catalog["workflows"] if entry_visible(request, row)]
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
        # A workflow the caller cannot see is as absent as one that does not exist.
        if summary is None or (entry_visible is not None and not entry_visible(request, summary)):
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=_error_payload(
                    "workflow_not_found",
                    f"workflow {workflow_id!r} not found",
                ),
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=summary)

    @router.get("/capabilities")
    async def list_capabilities(request: Request) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            if capabilities_provider is None:
                capabilities: Iterable[Capability] = ()
            else:
                capabilities = capabilities_provider(request) or ()
                if inspect.isawaitable(capabilities):
                    capabilities = await capabilities
            catalog = summarize_capabilities(capabilities, audience=audience)
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        return JSONResponse(status_code=status.HTTP_200_OK, content=catalog)

    @router.get("/aliases")
    def list_aliases(request: Request, operation: str | None = Query(None)) -> JSONResponse:
        if aliases_provider is None:
            return JSONResponse(status_code=status.HTTP_200_OK, content={"object": "aliases", "operations": {}})
        operations = aliases_provider(operation)
        if entry_visible is not None:
            original_operations = operations
            def visible_rows(op: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
                kept = []
                for row in rows:
                    model = row.get("alias") or row.get("engine") or row.get("to") or row.get("match") or ""
                    if not entry_visible(request, {"operation": op, "id": model}):
                        continue
                    row = dict(row)
                    for field in ("candidates", "skipped_engines"):
                        if field in row:
                            row[field] = [target for target in row[field]
                                          if entry_visible(request, {"operation": op, "id": target})]
                    kept.append(row)
                return kept

            operations = {op: {key: visible_rows(op, rows) for key, rows in view.items()}
                          for op, view in operations.items()}
            operations = {op: view for op, view in operations.items()
                          if any(view.values()) or view == original_operations[op]}
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"object": "aliases", "operations": operations},
        )

    @router.get("/aliases/resolve")
    def resolve_alias_dry_run(operation: str = Query(...), model: str = Query(...)) -> JSONResponse:
        if alias_resolver is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=_error_payload("aliases_unavailable", "alias resolution is not configured"),
            )
        result = alias_resolver(operation, model)
        code = status.HTTP_200_OK if result.get("resolved") or result.get("alias") is False else status.HTTP_409_CONFLICT
        return JSONResponse(status_code=code, content=result)

    @router.get("/extensions")
    def list_extensions(request: Request) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            catalog = summarize_extensions(registry, audience=audience)
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        return JSONResponse(status_code=status.HTTP_200_OK, content=catalog)

    @router.get("/models/{model}/capabilities")
    async def get_model_capabilities(model: str, request: Request) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            if capabilities_provider is None:
                capabilities: Iterable[Capability] = ()
            else:
                capabilities = capabilities_provider(request) or ()
                if inspect.isawaitable(capabilities):
                    capabilities = await capabilities
            catalog = summarize_model_capabilities(
                capabilities, model=model, audience=audience
            )
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
                        audience="internal" if record_authorizer else audience,
                        viewer_person_id=receipt_viewer_resolver(request) if receipt_viewer_resolver else None,
                        limit=limit,
                        app_profile=app_profile,
                        since_s=since_s,
                        **(record_scope_provider(request) if record_scope_provider else {}),
                    )
                )
                rows = scoped_rows(rows, receipt_getter, request, "receipt", audience)
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
            row = scoped_record(receipt_getter, request_id, request, "receipt", audience)
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

    @router.get("/resources")
    async def list_resources(request: Request) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            if resource_lister is None:
                resources: list[dict[str, Any]] = []
            else:
                listed = resource_lister(audience=audience)
                if inspect.isawaitable(listed):
                    listed = await listed
                resources = list(listed)
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "schema_version": 1,
                "audience": audience,
                "resources": resources,
            },
        )

    @router.post("/media/jobs")
    async def create_media_job(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        if media_job_submitter is not None:
            try:
                submitted = media_job_submitter(
                    request=request,
                    payload=payload,
                    audience=audience,
                )
                if inspect.isawaitable(submitted):
                    submitted = await submitted
            except Exception as exc:  # noqa: BLE001
                return _internal_error(audience, exc)
            if submitted is not None:
                return submitted
        if record_authorizer is not None and not record_authorizer(request, "media_job", {}):
            # The executable submitter derives ownership from authentication.
            # A raw store write would let a scoped caller forge attribution.
            # Explicit auth-disabled operation retains its unscoped store API.
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=_error_payload("unsupported_media_job", "Media job intent is not supported"),
            )
        if media_job_creator is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=_error_payload(
                    "media_jobs_unavailable",
                    "media job store is not configured",
                ),
            )
        if not isinstance(payload, Mapping) or not payload.get("intent"):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=_error_payload(
                    "invalid_media_job",
                    "media job payload must include 'intent'",
                ),
            )
        try:
            created = media_job_creator(payload, audience=audience)
        except ValueError as exc:
            from gateway.media_jobs import IdempotencyConflict
            if isinstance(exc, IdempotencyConflict):
                return JSONResponse(status_code=409, content=_error_payload(exc.code, str(exc)))
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=_error_payload(
                    "invalid_media_job",
                    _client_safe_message(str(exc), audience),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=_job_body(created, asset_path_resolver))

    @router.get("/media/jobs")
    def list_media_jobs(
        request: Request,
        limit: int = Query(
            MEDIA_JOBS_DEFAULT_LIMIT, ge=1, le=MEDIA_JOBS_MAX_LIMIT
        ),
        app_profile: str | None = Query(None),
        job_status: str | None = Query(None, alias="status"),
        since_s: int | None = Query(None, ge=0),
    ) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            if media_job_lister is None:
                rows: list[dict[str, Any]] = []
            else:
                rows = list(
                    media_job_lister(
                        audience="internal" if record_authorizer else audience,
                        limit=limit,
                        app_profile=app_profile,
                        status=job_status,
                        since_s=since_s,
                        **(record_scope_provider(request) if record_scope_provider else {}),
                    )
                )
                rows = scoped_rows(rows, media_job_getter, request, "media_job", audience)
        except ValueError as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=_error_payload(
                    "invalid_media_job_query",
                    _client_safe_message(str(exc), audience),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "schema_version": 1,
                "audience": audience,
                "asset_url_ttl_s": asset_url_ttl_s,
                "jobs": rows,
            },
        )

    @router.get("/media/jobs/{job_id}")
    def get_media_job(job_id: str, request: Request) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            row = scoped_record(media_job_getter, job_id, request, "media_job", audience)
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        if row is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=_error_payload(
                    "media_job_not_found",
                    f"media job {job_id!r} not found",
                ),
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={**row, "asset_url_ttl_s": asset_url_ttl_s},
        )

    @router.get("/media/jobs/{job_id}/events")
    def list_media_job_events(
        job_id: str,
        request: Request,
        limit: int = Query(
            MEDIA_JOBS_DEFAULT_LIMIT, ge=1, le=MEDIA_JOBS_MAX_LIMIT
        ),
        after_id: int | None = Query(None, ge=0),
        after_sequence: int | None = Query(None, ge=0),
    ) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        try:
            if after_id is not None and after_sequence is not None:
                raise ValueError("use either after_id or after_sequence, not both")
            row = scoped_record(media_job_getter, job_id, request, "media_job", audience)
            if row is None:
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content=_error_payload(
                        "media_job_not_found",
                        f"media job {job_id!r} not found",
                    ),
                )
            events = (
                []
                if media_job_event_lister is None
                else list(
                    media_job_event_lister(
                        job_id,
                        audience=audience,
                        limit=limit,
                        after_id=after_id,
                        **({"after_sequence": after_sequence} if after_sequence is not None else {}),
                    )
                )
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=_error_payload(
                    "invalid_media_job_event_query",
                    _client_safe_message(str(exc), audience),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "schema_version": 1,
                "audience": audience,
                "job_id": job_id,
                "events": events,
            },
        )

    @router.post("/media/jobs/{job_id}/events")
    def append_media_job_event(
        job_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        if media_job_event_appender is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=_error_payload(
                    "media_jobs_unavailable",
                    "media job store is not configured",
                ),
            )
        event_type = payload.get("event_type") if isinstance(payload, Mapping) else None
        event_payload = payload.get("payload") if isinstance(payload, Mapping) else None
        if not isinstance(event_type, str) or not event_type:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=_error_payload(
                    "invalid_media_job_event",
                    "event payload must include a non-empty 'event_type' string",
                ),
            )
        if event_payload is not None and not isinstance(event_payload, Mapping):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=_error_payload(
                    "invalid_media_job_event",
                    "'payload' must be an object when provided",
                ),
            )
        try:
            if record_authorizer is not None and scoped_record(
                media_job_getter, job_id, request, "media_job", audience
            ) is None:
                return job_not_found(job_id)
            from gateway.media_jobs import validate_client_event
            validate_client_event(event_type, event_payload)
            event_id = media_job_event_appender(
                job_id, event_type, event_payload, audience=audience
            )
        except LookupError:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=_error_payload(
                    "media_job_not_found",
                    f"media job {job_id!r} not found",
                ),
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=_error_payload(
                    "invalid_media_job_event",
                    _client_safe_message(str(exc), audience),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        cursor = dict(event_id) if isinstance(event_id, Mapping) else {"id": event_id}
        if audience not in {"admin", "internal"}:
            # Callers page by per-job sequence; the global id reveals others' activity.
            cursor.pop("id", None)
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={**cursor, "job_id": job_id, "event_type": event_type},
        )

    @router.post("/media/jobs/{job_id}/cancel")
    def cancel_media_job(
        job_id: str,
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> JSONResponse:
        audience = _resolve_request_audience(resolve_audience, request)
        if media_job_canceler is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=_error_payload(
                    "media_jobs_unavailable",
                    "media job store is not configured",
                ),
            )
        reason: str | None = None
        if isinstance(payload, Mapping):
            raw_reason = payload.get("reason")
            if raw_reason is not None and not isinstance(raw_reason, str):
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=_error_payload(
                        "invalid_media_job_cancel",
                        "'reason' must be a string when provided",
                    ),
                )
            reason = raw_reason
        try:
            if record_authorizer is not None and scoped_record(
                media_job_getter, job_id, request, "media_job", audience
            ) is None:
                return job_not_found(job_id)
            updated = media_job_canceler(job_id, audience=audience, reason=reason)
        except ValueError as exc:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=_error_payload(
                    "media_job_cancel_conflict",
                    _client_safe_message(str(exc), audience),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return _internal_error(audience, exc)
        if updated is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=_error_payload(
                    "media_job_not_found",
                    f"media job {job_id!r} not found",
                ),
            )
        return JSONResponse(status_code=202 if updated.get("cancel_requested") else 200, content=_job_body(updated, asset_path_resolver))

    return router


def create_request_retention_router(*, ledger_provider: LedgerProvider) -> APIRouter:
    """Expose dated facts through management auth, outside runtime App-key auth.

    The injected provider uses the same owner/admin guard as other ledger reads
    and masks receipt retention fields for the authenticated viewer.
    """
    router = APIRouter(prefix="/admin/api", route_class=egress.ManagementRoute)

    @router.get("/me/retention/ledger")
    def current_retention_ledger(request: Request) -> JSONResponse:
        try:
            view = ledger_provider(request, None)
        except PermissionError:
            return egress.safe_error_response(403, "admin_forbidden", "An owner or administrator is required")
        if view is None:
            return egress.safe_error_response(404, "ledger_not_found", "Ledger not found")
        return egress.plain_json_response({"ledger": view}, response_class="retention_ledger",
                                          headers={"Cache-Control": "no-store"})

    @router.get("/requests/{request_id}/retention/ledger")
    def request_retention_ledger(request_id: str, request: Request) -> JSONResponse:
        try:
            view = ledger_provider(request, request_id)
        except PermissionError:
            return egress.safe_error_response(403, "admin_forbidden", "An owner or administrator is required")
        if view is None:
            return egress.safe_error_response(404, "ledger_not_found", "Ledger not found")
        return egress.plain_json_response({"ledger": view}, response_class="retention_ledger",
                                          headers={"Cache-Control": "no-store"})

    return router
