"""``/admin/api/subscriptions``: the owner connects, checks and disconnects the gateway's subscription plans.

Owner only. A connection belongs to the instance; who may use its models is the access plans' decision, never this
router's. A response never carries a token.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import egress
from gateway.admin_api import AdminAuth, AdminContext, _require_admin
from gateway.session_guard import SessionEnded
from gateway.reasoning_efforts import EFFORTS
from gateway.subscription_oauth import SubscriptionError, SubscriptionService

_HEADERS = {"Cache-Control": "no-store"}
_MAX_RECORD_BYTES = 64 * 1024


def read_record_file(path: str) -> Mapping[str, Any]:
    """The sign-in record at a path the owner names. Never echoed back; a bad file is one named refusal.

    The file is opened once (a link is not followed, and nothing waits on a pipe), checked on that open handle, and
    read up to a fixed size, so nothing can change between the check and the read."""
    refusal = SubscriptionError(400, "import_unreadable", "That file could not be read as a sign-in record.")
    try:
        descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError):
        raise refusal from None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise refusal
            raw = handle.read(_MAX_RECORD_BYTES + 1)
        if len(raw) > _MAX_RECORD_BYTES:
            raise refusal
        record = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        raise refusal from None
    if not isinstance(record, Mapping):
        raise refusal
    return record


def create_subscription_router(
    *, service: SubscriptionService | Callable[[], SubscriptionService], auth: AdminAuth,
    prefix: str = "/admin/api/subscriptions",
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["admin-subscriptions"], route_class=egress.ManagementRoute)
    listeners: set[Any] = set()

    def svc() -> SubscriptionService:
        return service() if callable(service) else service

    async def owner_only(request: Request) -> AdminContext | JSONResponse:
        # The sign-in check reads SQLite, so it runs off the event loop.
        ctx = await asyncio.to_thread(_require_admin, auth, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if "owner" not in ctx.roles:
            return egress.safe_error_response(403, "owner_required", "Only the owner can connect a plan.")
        return ctx

    def refusal(error: SubscriptionError) -> JSONResponse:
        return egress.safe_error_response(error.status, error.code, error.message)

    def plain(content: Mapping[str, Any]) -> JSONResponse:
        return egress.plain_json_response(content, response_class="subscriptions", headers=_HEADERS)

    def owner_id(ctx: AdminContext) -> str:
        return str(ctx.actor_id or ctx.subject)

    @router.get("", include_in_schema=False)
    async def list_subscriptions(request: Request) -> JSONResponse:
        ctx = await owner_only(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return plain({"subscriptions": await svc().status()})

    @router.post("/{kind}/connect", include_in_schema=False)
    async def connect(kind: str, request: Request) -> JSONResponse:
        ctx = await owner_only(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await request.json()
        except ValueError:
            body = {}
        try:
            started = await svc().start(owner_id(ctx), kind, session=ctx.browser_session)
        except SubscriptionError as error:
            return refusal(error)
        except SessionEnded as ended:
            return egress.safe_error_response(401, ended.code, ended.message)
        listening = False
        if isinstance(body, Mapping) and body.get("listen") is True:
            listener = await svc().listen_for_callback(kind_id=kind)
            if listener is not None:
                listeners.add(listener)
                listening = True
        return plain({"subscription": {**started, "listening": listening}})

    @router.post("/{kind}/finish", include_in_schema=False)
    async def finish(kind: str, request: Request) -> JSONResponse:
        ctx = await owner_only(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, Mapping) or not isinstance(body.get("attempt_id"), str) or not isinstance(body.get("callback"), str):
            return egress.safe_error_response(400, "invalid_request", "Send attempt_id and callback.")
        try:
            view = await svc().finish(owner_id(ctx), body["attempt_id"], body["callback"], session=ctx.browser_session)
        except SubscriptionError as error:
            return refusal(error)
        except SessionEnded as ended:
            return egress.safe_error_response(401, ended.code, ended.message)
        return plain({"subscription": view})

    @router.post("/{kind}/import", include_in_schema=False)
    async def import_record(kind: str, request: Request) -> JSONResponse:
        ctx = await owner_only(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, Mapping) or not isinstance(body.get("path"), str) or not body["path"]:
            return egress.safe_error_response(400, "invalid_request", "Send the path of the record file.")
        try:
            record = read_record_file(body["path"])
            view = await svc().import_record(owner_id(ctx), kind, record, session=ctx.browser_session)
        except SubscriptionError as error:
            return refusal(error)
        except SessionEnded as ended:
            return egress.safe_error_response(401, ended.code, ended.message)
        if body.get("remove_source") is True:
            try:
                Path(body["path"]).expanduser().unlink()
            except OSError:
                view = {**view, "source_removed": False}
            else:
                view = {**view, "source_removed": True}
        return plain({"subscription": view})

    @router.patch("/{kind}/reasoning", include_in_schema=False)
    async def set_reasoning(kind: str, request: Request) -> JSONResponse:
        ctx = await owner_only(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await request.json()
        except ValueError:
            body = None
        if (not isinstance(body, Mapping) or "top_effort" not in body
                or (body["top_effort"] is not None
                    and (not isinstance(body["top_effort"], str) or body["top_effort"] not in EFFORTS))):
            return egress.safe_error_response(400, "invalid_top_effort", "Choose a known reasoning level or null for no limit.")
        try:
            await asyncio.to_thread(svc().kind, kind)
            await asyncio.to_thread(svc().store.set_top_effort, kind, body["top_effort"])
            view = await svc().status_one(kind)
        except SubscriptionError as error:
            return refusal(error)
        except ValueError as error:
            return egress.safe_error_response(400, "plan_not_connected", str(error))
        return plain({"subscription": view})

    @router.post("/{kind}/install-runner", include_in_schema=False)
    async def install_runner(kind: str, request: Request) -> JSONResponse:
        ctx = await owner_only(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            view = await svc().install_runner(kind)
        except SubscriptionError as error:
            return refusal(error)
        return plain({"subscription": view})

    @router.post("/{kind}/refresh-models", include_in_schema=False)
    async def refresh_models(kind: str, request: Request) -> JSONResponse:
        ctx = await owner_only(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            await svc().refresh_models(kind)
            view = await svc().status_one(kind)
        except SubscriptionError as error:
            return refusal(error)
        return plain({"subscription": view})

    @router.delete("/{kind}", include_in_schema=False)
    async def disconnect(kind: str, request: Request) -> JSONResponse:
        ctx = await owner_only(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            view = await svc().disconnect(kind, session=ctx.browser_session)
        except SubscriptionError as error:
            return refusal(error)
        except SessionEnded as ended:
            return egress.safe_error_response(401, ended.code, ended.message)
        return plain({"subscription": view})

    return router
