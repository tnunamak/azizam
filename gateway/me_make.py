"""Session make jobs: durable media work for the signed-in person.

Task pages call these routes with the browser session, never with an API key.
Each job is a durable media job attributed to the person's keyless "Azizam"
App, so receipts, the Library, and usage see the person's own work.

This module owns the job lifecycle (record, receipt, ownership, idempotency,
running, finishing, cancel). Producing media is the runtime's job: the gateway
supplies a ``MakeRuntime`` that runs the existing image routes, and it keeps
execution payloads in its existing prompt-aware spool. Nothing here knows any
engine.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from gateway import admin_store, media_jobs, ownership, prompt_memory, receipt_store
from gateway.admin_oidc import AdminSession
from gateway.receipts import RouteReceipt, new_request_id

APP_NAME = "Azizam"
JOB_MODE = "session_make"
# Shown while a canceled job's work settles; it then becomes ``canceled``.
CANCEL_REQUESTED = "cancel_requested"
_CANCELED = "Canceled by requester"
ROUTE_PATH = "/me/api/make/jobs"
SUPPORTED_INTENTS = frozenset({"image.generate", "image.edit"})
_SOURCE_FIELDS = ("source_asset_id", "source_job_id")
# Request fields passed to the backend unchanged. Anything else is refused,
# so a browser cannot smuggle routing or auth fields into the job.
_BACKEND_FIELDS = frozenset({
    "model", "prompt", "size", "n", "quality", "background", "output_format", "extensions",
})
_LIST_DEFAULT_LIMIT = 50
_LIST_MAX_LIMIT = 200

PathProvider = Path | Callable[[], Path]
SessionProvider = Callable[[Request], AdminSession | None]
MutationValidator = Callable[[Request, AdminSession], JSONResponse | None]
# (request, person id, asset id) -> the person's Library item, or None.
OwnedAssetFinder = Callable[[Request, str, str], Mapping[str, Any] | None]


class MakeCanceled(Exception):
    """The runtime stopped a job because its person canceled it."""


class MakeBackendError(Exception):
    """A backend failure whose ``public_message`` is safe to store and show."""

    def __init__(self, public_message: str, *, http_status: int | None = None) -> None:
        super().__init__(public_message)
        self.public_message = public_message
        self.http_status = http_status


@dataclass(frozen=True)
class MakeLimits:
    """What one make request may ask for, and how much work may wait.

    ``max_images`` matches the image routes' ``n`` range. Outstanding jobs
    are queued or running make jobs, per person and for the whole household.
    """

    max_prompt_chars: int = 32_000
    max_images: int = 10
    per_person: int = 3
    household: int = 12


class MakeRuntime(Protocol):
    """How the gateway runs a make job. Payloads hold the prompt."""

    limits: MakeLimits

    def keep_payload(self, job_id: str, payload: Mapping[str, Any]) -> None: ...

    def load_payload(self, job_id: str) -> Mapping[str, Any] | None: ...

    def drop_payload(self, job_id: str) -> None: ...

    def schedule(self, job_id: str, run: Callable[[], Awaitable[None]]) -> None: ...

    def admission(self, intent: str) -> Response | None:
        """Return the operator's refusal for new work of ``intent``, or None.

        It is the same response regular media jobs get, and submit returns it
        before storing anything.
        """
        ...

    def request_cancel(self, job_id: str) -> bool:
        """Stop a job's work. Return False only if its output is already published.

        After True, the job publishes nothing and keeps no prompt, and
        ``execute`` for it ends with ``MakeCanceled``.
        """
        ...

    async def execute(self, payload: Mapping[str, Any]) -> list[Mapping[str, Any]]: ...


class _MakeError(Exception):
    def __init__(self, code: str, message: str, status_code: int, *, retry_after: int | None = None) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after


class _AdmissionRefused(Exception):
    def __init__(self, response: Response) -> None:
        self.response = response


def add_make_routes(
    router: APIRouter,
    *,
    session_provider: SessionProvider,
    mutation_validator: MutationValidator | None,
    find_owned_asset: OwnedAssetFinder,
    admin_db_path: PathProvider,
    media_jobs_path: PathProvider,
    receipts_path: PathProvider,
    runtime: MakeRuntime | None,
) -> None:
    """Add the make routes to the person API router (prefix ``/me/api``)."""

    def person(request: Request, *, mutation: bool) -> tuple[dict[str, Any], Path] | JSONResponse:
        session = session_provider(request)
        if session is not None and mutation and mutation_validator is not None:
            action_error = mutation_validator(request, session)
            if action_error is not None:
                return action_error
        if session is None:
            raise _MakeError("not_authenticated", "Sign in to make things", 401)
        admin_path = _resolve(admin_db_path)
        user = admin_store.get_user(session.subject, path=admin_path)
        if user is None:
            raise _MakeError("session_user_not_found", "Signed-in user was not found", 401)
        return user, admin_path

    @router.post("/make/jobs", include_in_schema=False)
    async def submit(request: Request) -> JSONResponse:
        try:
            resolved = person(request, mutation=True)
            if isinstance(resolved, JSONResponse):
                return resolved
            user, admin_path = resolved
            if runtime is None:
                raise _MakeError("make_unavailable", "Making is not available on this gateway", 503)
            try:
                body = await request.json()
            except Exception:
                raise _MakeError("invalid_json", "Send a JSON object", 400) from None
            status_code, job = _submit(
                request, body, user=user, runtime=runtime,
                find_owned_asset=find_owned_asset,
                idempotency_key=request.headers.get("idempotency-key"),
                admin_path=admin_path,
                jobs_path=_resolve(media_jobs_path),
                receipts_path=_resolve(receipts_path),
            )
            return _json({"job": job}, status_code)
        except _AdmissionRefused as refused:
            return refused.response
        except _MakeError as exc:
            return _error(exc)

    @router.get("/make/jobs", include_in_schema=False)
    def list_jobs(request: Request) -> JSONResponse:
        try:
            resolved = person(request, mutation=False)
            if isinstance(resolved, JSONResponse):
                return resolved
            user, admin_path = resolved
            status = request.query_params.get("status") or None
            if status is not None and status not in media_jobs.ALL_STATUSES:
                raise _MakeError("invalid_status", "Unknown job status", 400)
            limit = _bounded_limit(request.query_params.get("limit"))
            app = admin_store.get_session_app_for_user(str(user["id"]), path=admin_path)
            jobs = [] if app is None else media_jobs.recent(
                limit, app_ids=[str(app["id"])], status=status, path=_resolve(media_jobs_path),
            )
            return _json({"jobs": [_client_view(job) for job in jobs]})
        except _MakeError as exc:
            return _error(exc)

    @router.get("/make/jobs/{job_id}", include_in_schema=False)
    def get_job(job_id: str, request: Request) -> JSONResponse:
        try:
            resolved = person(request, mutation=False)
            if isinstance(resolved, JSONResponse):
                return resolved
            user, admin_path = resolved
            jobs_path = _resolve(media_jobs_path)
            return _json({"job": _client_view(_own_make_job(job_id, str(user["id"]),
                                                            admin_path=admin_path, jobs_path=jobs_path))})
        except _MakeError as exc:
            return _error(exc)

    @router.post("/make/jobs/{job_id}/cancel", include_in_schema=False)
    def cancel_job(job_id: str, request: Request) -> JSONResponse:
        try:
            resolved = person(request, mutation=True)
            if isinstance(resolved, JSONResponse):
                return resolved
            user, admin_path = resolved
            jobs_path = _resolve(media_jobs_path)
            job = _own_make_job(job_id, str(user["id"]), admin_path=admin_path, jobs_path=jobs_path)
            finished = _MakeError("job_finished", "This job has already finished", 409)
            if job.get("status") in media_jobs.TERMINAL_STATUSES:
                raise finished
            try:  # A job that has not started stops here.
                media_jobs.update_status(job_id, media_jobs.STATUS_CANCELED, error=_CANCELED,
                                         expected_status=media_jobs.STATUS_QUEUED, path=jobs_path)
                _event(job_id, "canceled", media_jobs.STATUS_CANCELED, jobs_path)
                if runtime is not None:  # It also gives up its place in the queue.
                    runtime.request_cancel(job_id)
            except ValueError:
                if runtime is not None and not runtime.request_cancel(job_id):
                    raise _MakeError("job_finishing", "This job has already made its picture", 409) from None
                try:  # A running job stops in the background and then settles as canceled.
                    media_jobs.update_status(
                        job_id, media_jobs.STATUS_RUNNING,
                        metadata_patch={"cancel_requested_at": int(time.time())},
                        expected_status=media_jobs.STATUS_RUNNING, path=jobs_path,
                    )
                    _event(job_id, "cancel_requested", media_jobs.STATUS_RUNNING, jobs_path)
                except ValueError:
                    pass
            # Only after the status moves: a runner that is just starting must
            # see the cancel, not a missing payload.
            if runtime is not None:
                runtime.drop_payload(job_id)
            current = _client_job(job_id, jobs_path)
            if current.get("status") == media_jobs.STATUS_CANCELED:
                return _json({"job": current})
            if current.get("status") != CANCEL_REQUESTED:
                raise finished
            return _json({"job": current}, 202)
        except _MakeError as exc:
            return _error(exc)


def run_job(job_id: str, *, runtime: MakeRuntime, jobs_path: Path) -> Callable[[], Awaitable[None]]:
    """Return the coroutine function that takes one job to a terminal state."""

    async def run() -> None:
        try:
            await _run(job_id, runtime=runtime, jobs_path=jobs_path)
        finally:
            # A job interrupted by shutdown keeps its payload so it can resume.
            job = media_jobs.get(job_id, path=jobs_path)
            if job is None or job.get("status") in media_jobs.TERMINAL_STATUSES:
                runtime.drop_payload(job_id)

    return run


def resume_jobs(*, runtime: MakeRuntime, jobs_path: Path) -> int:
    """Restart unfinished make jobs after a gateway restart."""
    resumed = 0
    for status in (media_jobs.STATUS_RUNNING, media_jobs.STATUS_QUEUED):
        for job in media_jobs.recent(500, status=status, path=jobs_path):
            if (job.get("metadata") or {}).get("mode") != JOB_MODE:
                continue
            job_id = str(job["job_id"])
            if (job.get("metadata") or {}).get("cancel_requested_at"):
                _settle_canceled(job_id, jobs_path)
                continue
            if runtime.load_payload(job_id) is None:
                # A forgotten prompt lived only in memory; it cannot be resumed.
                _fail(job_id, "The gateway restarted before this job could finish", jobs_path)
                continue
            runtime.schedule(job_id, run_job(job_id, runtime=runtime, jobs_path=jobs_path))
            resumed += 1
    return resumed


def _submit(
    request: Request,
    body: Any,
    *,
    user: Mapping[str, Any],
    runtime: MakeRuntime,
    find_owned_asset: OwnedAssetFinder,
    idempotency_key: str | None,
    admin_path: Path,
    jobs_path: Path,
    receipts_path: Path,
) -> tuple[int, dict[str, Any]]:
    user_id = str(user["id"])
    intent, model, backend_body = _parse(body, runtime.limits)
    refusal = runtime.admission(intent)
    if refusal is not None:
        evidence = getattr(refusal, "resource_decision", None)
        if evidence is not None:
            app = admin_store.get_session_app_for_user(user_id, path=admin_path)
            app_id = str(app["id"]) if app else None
            refused = RouteReceipt(protocol="azizam.make", intent=intent, path=ROUTE_PATH,
                                   app_id=app_id, owner_key_hash=(ownership.principal_key("app", app_id)
                                       if app_id else ownership.principal_key("user", user_id)),
                                   resource_decision=evidence)
            refused.finish("rejected")
            try:
                receipt_store.record(refused, path=receipts_path)
            except Exception:
                pass  # Receipt storage must not replace the admission response.
        raise _AdmissionRefused(refusal)
    forget = backend_body.get("extensions", {}).get(prompt_memory.NAMESPACE) is not None
    app = admin_store.ensure_session_app_for_user(user_id, name=APP_NAME, path=admin_path)
    if app.get("disabled_at"):
        raise _MakeError("make_disabled", "Making is turned off for this account", 403)
    app_id = str(app["id"])
    key = (idempotency_key or "").strip()
    request_id = _idempotent_request_id(app_id, key, forget=forget) if key else new_request_id()
    source_asset_id = _owned_source(request, body, user_id=user_id, find_owned_asset=find_owned_asset,
                                    admin_path=admin_path, jobs_path=jobs_path)
    if (intent == "image.edit") != (source_asset_id is not None):
        raise _MakeError(
            "invalid_source",
            "image.edit needs one source picture; image.generate takes none", 400,
        )
    if source_asset_id is not None:
        backend_body["source_asset_id"] = source_asset_id

    owner = ownership.principal_key("app", app_id)
    job = media_jobs.MediaJob(
        intent=intent,
        workflow=f"{intent}.{model}"[:200],
        request_id=request_id,
        app_id=app_id,
        owner_key_hash=owner,
        api_key_name=APP_NAME,
        metadata={"mode": JOB_MODE, "requested_model": model[:160],
                  **({"source_asset_id": source_asset_id} if source_asset_id else {})},
    )
    try:
        created = media_jobs.create(job, outstanding_limits=_outstanding_limits(app_id, runtime.limits),
                                    input_data=backend_body, path=jobs_path)
    except media_jobs.IdempotencyConflict as exc:
        raise _MakeError(exc.code, str(exc), 409) from None
    except media_jobs.OutstandingLimitReached as exc:
        if exc.limit.scope == "person":
            raise _MakeError(
                "too_many_jobs",
                f"You already have {exc.limit.maximum} things being made. "
                "Wait for one to finish, then try again.", 429,
            ) from None
        raise _MakeError("make_busy", "Azizam is busy making other things. Try again in a minute.",
                         503, retry_after=60) from None
    if created.job_id != job.job_id:  # A concurrent retry won the same key.
        return _replay_status(created.to_dict()), _client_job(created.job_id, jobs_path)
    receipt = RouteReceipt(
        protocol="azizam.make", intent=intent, path=ROUTE_PATH, request_id=request_id,
        app_id=app_id, api_key_name=APP_NAME, requested_model=model, job_id=job.job_id,
        status="accepted", owner_key_hash=owner,
    )
    receipt_store.record(receipt, path=receipts_path)
    payload = {
        "schema_version": 1,
        "job_id": job.job_id,
        "intent": intent,
        "body": backend_body,
        "auth": {"app_id": app_id, "person_id": user_id},
        "created_at": job.created_at,
    }
    try:
        runtime.keep_payload(job.job_id, payload)
    except Exception as exc:  # noqa: BLE001
        _fail(job.job_id, f"Could not queue this job ({type(exc).__name__})", jobs_path)
        raise _MakeError("make_queue_failed", "Could not queue this job", 500) from None
    _event(job.job_id, "queued", media_jobs.STATUS_QUEUED, jobs_path)
    runtime.schedule(job.job_id, run_job(job.job_id, runtime=runtime, jobs_path=jobs_path))
    return 202, _client_job(job.job_id, jobs_path)


def _outstanding_limits(app_id: str, limits: MakeLimits) -> list[media_jobs.OutstandingLimit]:
    return [
        media_jobs.OutstandingLimit("person", limits.per_person, JOB_MODE, app_id=app_id),
        media_jobs.OutstandingLimit("household", limits.household, JOB_MODE),
    ]


def _parse(body: Any, limits: MakeLimits) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(body, Mapping):
        raise _MakeError("invalid_json", "Send a JSON object", 400)
    unknown = sorted(set(body) - _BACKEND_FIELDS - {"intent", *_SOURCE_FIELDS})
    if unknown:
        raise _MakeError("unsupported_field", f"Unsupported field: {unknown[0]}", 400)
    intent = body.get("intent")
    if intent not in SUPPORTED_INTENTS:
        raise _MakeError("unsupported_intent", "intent must be image.generate or image.edit", 400)
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise _MakeError("model_required", "Choose a model", 400)
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _MakeError("prompt_required", "Describe what to make", 400)
    if len(prompt) > limits.max_prompt_chars:
        raise _MakeError("prompt_too_long",
                         f"Describe it in at most {limits.max_prompt_chars:,} characters", 400)
    count = body.get("n", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= limits.max_images:
        raise _MakeError("invalid_n", f"n must be a whole number from 1 to {limits.max_images}", 400)
    try:
        prompt_memory.requested_mode(body)
    except prompt_memory.PromptModeError as exc:
        raise _MakeError("invalid_prompt_mode", str(exc), 400) from None
    backend_body = {key: body[key] for key in _BACKEND_FIELDS if key in body}
    backend_body["model"] = model.strip()
    return str(intent), model.strip(), backend_body


def _owned_source(
    request: Request,
    body: Mapping[str, Any],
    *,
    user_id: str,
    find_owned_asset: OwnedAssetFinder,
    admin_path: Path,
    jobs_path: Path,
) -> str | None:
    """Resolve the source picture, which must be the person's own.

    Both denials use one generic 404 so a probe learns nothing about another
    person's jobs or pictures.
    """
    asset_id, job_id = (body.get(field) for field in _SOURCE_FIELDS)
    if asset_id is None and job_id is None:
        return None
    if asset_id is not None and job_id is not None:
        raise _MakeError("invalid_source", "Send source_asset_id or source_job_id, not both", 400)
    not_found = _MakeError("source_not_found", "Source picture not found in your Library", 404)
    if job_id is not None:
        if not isinstance(job_id, str):
            raise not_found
        job = media_jobs.get(job_id, path=jobs_path)
        owned_apps = {str(app["id"]) for app in admin_store.list_apps_for_user(user_id, path=admin_path)}
        if job is None or str(job.get("app_id") or "") not in owned_apps:
            raise not_found
        asset_id = next((asset.get("asset_id") for asset in job.get("assets") or []
                         if str(asset.get("asset_id") or "").startswith("img_")), None)
        if asset_id is None:
            raise _MakeError("source_not_ready", "That job has no finished picture yet", 409)
    if not isinstance(asset_id, str) or not asset_id.startswith("img_"):
        raise not_found
    item = find_owned_asset(request, user_id, asset_id)
    if item is None or item.get("available") is False:
        raise not_found
    return asset_id


async def _run(job_id: str, *, runtime: MakeRuntime, jobs_path: Path) -> None:
    job = media_jobs.get(job_id, path=jobs_path)
    if job is None or job.get("status") in media_jobs.TERMINAL_STATUSES:
        return
    payload = runtime.load_payload(job_id)
    if payload is None:
        # Cancel marks the job before it drops the payload, so read it again.
        current = media_jobs.get(job_id, path=jobs_path) or {}
        if (current.get("metadata") or {}).get("cancel_requested_at"):
            _settle_canceled(job_id, jobs_path)
        else:
            _fail(job_id, "This job's input is no longer available", jobs_path)
        return
    try:
        media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, progress=5,
                                 metadata_patch={"started_at": int(time.time())}, path=jobs_path)
        if job.get("status") == media_jobs.STATUS_RUNNING:
            media_jobs.begin_attempt(job_id, path=jobs_path)
    except ValueError:  # Canceled before it started.
        return
    _event(job_id, "started", media_jobs.STATUS_RUNNING, jobs_path, progress=5)
    prompt = str((payload.get("body") or {}).get("prompt") or "")
    try:
        produced = await runtime.execute(payload)
    except MakeCanceled:
        _settle_canceled(job_id, jobs_path)
        return
    except MakeBackendError as exc:
        # Even a public backend message must not carry the prompt into storage.
        message = exc.public_message.replace(prompt, "[prompt]") if prompt else exc.public_message
        _fail(job_id, message, jobs_path, http_status=exc.http_status)
        return
    except Exception as exc:  # noqa: BLE001
        # Arbitrary exception text may hold the prompt or a backend secret.
        _fail(job_id, f"Making failed ({type(exc).__name__})", jobs_path)
        return
    assets = [
        {"asset_id": str(asset["asset_id"]), "media_type": str(asset.get("media_type") or "")}
        for asset in produced or []
        if isinstance(asset, Mapping) and isinstance(asset.get("asset_id"), str)
    ]
    if not assets:
        _fail(job_id, "The backend finished without a picture", jobs_path)
        return
    try:
        media_jobs.update_status(job_id, media_jobs.STATUS_SUCCEEDED, add_assets=assets,
                                 metadata_patch={"completed_at": int(time.time())}, path=jobs_path)
    except ValueError:  # Canceled while running: the late result is dropped.
        return
    _event(job_id, "succeeded", media_jobs.STATUS_SUCCEEDED, jobs_path, progress=100)


def _settle_canceled(job_id: str, jobs_path: Path) -> None:
    try:
        media_jobs.update_status(job_id, media_jobs.STATUS_CANCELED, error=_CANCELED, path=jobs_path)
    except ValueError:  # Already terminal.
        return
    _event(job_id, "canceled", media_jobs.STATUS_CANCELED, jobs_path, progress=100)


def _fail(job_id: str, message: str, jobs_path: Path, *, http_status: int | None = None) -> None:
    try:
        media_jobs.update_status(
            job_id, media_jobs.STATUS_FAILED, error=message[:500],
            metadata_patch={"last_http_status": int(http_status)} if http_status else None,
            path=jobs_path,
        )
    except ValueError:  # Already terminal (for example, canceled).
        return
    _event(job_id, "failed", media_jobs.STATUS_FAILED, jobs_path, progress=100)


def _event(job_id: str, kind: str, status: str, jobs_path: Path, *, progress: int = 0) -> None:
    try:
        media_jobs.append_event(job_id, kind, {"phase": kind, "status": status, "progress": progress},
                                path=jobs_path)
    except Exception:  # noqa: BLE001 - events are progress hints, not the record.
        pass


def _own_make_job(job_id: str, user_id: str, *, admin_path: Path, jobs_path: Path) -> dict[str, Any]:
    app = admin_store.get_session_app_for_user(user_id, path=admin_path)
    job = media_jobs.get(job_id, path=jobs_path) if app is not None else None
    if job is None or job.get("app_id") != app["id"]:
        raise _MakeError("job_not_found", "Job not found for this account", 404)
    return job


def _idempotent_request_id(app_id: str, key: str, *, forget: bool) -> str:
    # A forget request must never return an earlier remembered job.
    identity = f"{app_id}\0{key}\0{'forget' if forget else 'keep'}".encode()
    return "make_idem_" + hashlib.sha256(identity).hexdigest()[:40]


def _replay_status(job: Mapping[str, Any]) -> int:
    return 200 if job.get("status") in media_jobs.TERMINAL_STATUSES else 202


def _client_job(job_id: str, jobs_path: Path) -> dict[str, Any]:
    job = media_jobs.get(job_id, path=jobs_path)
    return _client_view(job) if job is not None else {}


def _client_view(job: Mapping[str, Any]) -> dict[str, Any]:
    """The client's view of a job. A running job whose cancel has not settled
    shows ``cancel_requested``: its work may still be running, but it will
    publish nothing."""
    view = media_jobs.for_audience(job, "client")
    if (job.get("status") == media_jobs.STATUS_RUNNING
            and (job.get("metadata") or {}).get("cancel_requested_at")):
        view["status"] = CANCEL_REQUESTED
    return view


def _bounded_limit(raw: str | None) -> int:
    try:
        value = int(raw) if raw else _LIST_DEFAULT_LIMIT
    except ValueError:
        raise _MakeError("invalid_limit", "limit must be a whole number", 400) from None
    return max(1, min(value, _LIST_MAX_LIMIT))


def _resolve(provider: PathProvider) -> Path:
    return provider() if callable(provider) else provider


def _json(content: Mapping[str, Any], status_code: int = 200,
          headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content,
                        headers={"cache-control": "no-store", **(headers or {})})


def _error(exc: _MakeError) -> JSONResponse:
    headers = {"retry-after": str(exc.retry_after)} if exc.retry_after else None
    return _json({"error": {"code": exc.code, "message": exc.message}}, exc.status_code, headers)
