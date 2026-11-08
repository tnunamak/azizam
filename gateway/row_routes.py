"""Mount frontend rows as routes over the run core (CODE_TO_DATA_S4_S8, section 8).

A row (``registry/protocol_frontends.yml``, ``frontends:``) names a reviewed wire shape and the routes it owns. This module
mounts the routes of the rows it is given. Each request runs the same steps, whatever the row:

1. authenticate, then the runtime route policy for the route's canonical path;
2. the shape reads its wire request and chooses the workflow the row selects (``Shape.to_run_request``);
3. the run core admits and creates the job (``submit_run``);
4. the shape renders the job: for a ``sync`` row it waits and answers in the wire's own form; with ``Prefer:
   respond-async`` (RFC 7240) the row answers with the job at once instead.

This module names no workflow, shape, intent or service. The main module supplies the effects it owns (``RowRouteDeps``).
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, ContextManager, Iterable

from fastapi import FastAPI, Request
from starlette.responses import Response

from gateway import operation_security
from gateway.frontend_shapes import build_shape, bundled_rows
from gateway.workflow_runtime_types import FrontendRow, JobService, RegistrySnapshot, RequestSpool, RunRefused

PREFER_ASYNC = "respond-async"


@dataclass(frozen=True)
class RowRouteDeps:
    """What a mounted row reads from the gateway."""

    # authenticate(request, canonical_path) -> the caller's context, or the refusal to send.
    authenticate: Callable[[Request, str], Any]
    # enforce_policy(caller, canonical_path) -> a refusal to send, or None. Runs off the event loop.
    enforce_policy: Callable[[Any, str], Response | None]
    # The run core now, or None until it is configured.
    core: Callable[[], Any]
    snapshot: Callable[[], RegistrySnapshot]
    jobs: Callable[[], JobService]
    # new_spool() -> a request spool; scope(request, spool) -> a context manager that cleans staged files up.
    new_spool: Callable[[], RequestSpool]
    scope: Callable[[Request, RequestSpool], ContextManager[Any]]
    refused: Callable[[RunRefused], Response]


def prefers_async(value: str | None) -> bool:
    """Whether a ``Prefer`` header asks for ``respond-async`` (RFC 7240: comma-separated tokens, optional parameters)."""
    for preference in (value or "").split(","):
        if preference.split(";", 1)[0].split("=", 1)[0].strip().lower() == PREFER_ASYNC:
            return True
    return False


def mount_rows(app: FastAPI, deps: RowRouteDeps, row_ids: Iterable[str]) -> None:
    """Add a route for every method and canonical path of each named bundled row."""
    rows = {row.id: row for row in bundled_rows()}
    for row_id in row_ids:
        row = rows[row_id]
        shape = build_shape(row)
        for route in row.routes:
            app.add_api_route(route.canonical, _handler(deps, row, shape, route.canonical), methods=list(route.methods),
                              include_in_schema=True, name=f"row:{row.id}:{route.canonical}",
                              openapi_extra=operation_security.openapi_extra(operation_security.APP_KEY))


def _handler(deps: RowRouteDeps, row: FrontendRow, shape: Any, canonical: str) -> Callable[[Request], Awaitable[Response]]:
    async def serve(request: Request) -> Response:
        caller = await asyncio.to_thread(deps.authenticate, request, canonical)
        if isinstance(caller, Response):
            return caller
        denied = await asyncio.to_thread(deps.enforce_policy, caller, canonical)
        if denied is not None:
            return denied
        core = deps.core()
        if core is None:
            return deps.refused(RunRefused(503, "workflow_runtime_unavailable", "Workflow runs are not configured."))
        snapshot = deps.snapshot()
        spool = deps.new_spool()
        answered = dataclasses.replace(row, mode="job") if prefers_async(request.headers.get("prefer")) else row
        with deps.scope(request, spool):
            run_request = await shape.to_run_request(request, row, snapshot, spool=spool)
            if isinstance(run_request, Response):
                return run_request
            refused = await asyncio.to_thread(core.target_gate, run_request.workflow_id, caller, snapshot=snapshot)
            outcome = refused if refused is not None else await core.submit_run(run_request, caller, snapshot=snapshot)
        return await shape.to_response(outcome, answered, jobs=deps.jobs(), caller=caller, request=request)

    return serve

