"""Mount frontend rows as routes over the run core (CODE_TO_DATA_S4_S8, section 8).

A row (``registry/protocol_frontends.yml``, ``frontends:``) names a reviewed wire shape and the routes it owns. This module
mounts the routes of the rows it is given. Each request runs the same steps, whatever the row:

1. authenticate, then the runtime route policy for the route's canonical path (a denial opens no receipt);
2. the request receipt opens; every later outcome closes it once;
3. the shape reads its wire request and chooses the workflow the row selects (``Shape.to_run_request``);
4. the run core admits and creates the job (``submit_run``);
5. the shape renders the job: for a ``sync`` row it waits and answers in the wire's own form; with ``Prefer:
   respond-async`` (RFC 7240) the row answers with the job at once instead, and hands the receipt to the run core,
   which closes it when the job ends.

The kept request (History) learns what the request ran from ``request.state``: the workflow, its title and capability,
and, for a sync answer, the Library asset of the first stored result.

This module names no workflow, shape, intent or service. The main module supplies the effects it owns (``RowRouteDeps``).
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
from typing import Any, AsyncContextManager, Awaitable, Callable, ContextManager, Iterable

from fastapi import FastAPI, Request
from starlette.responses import Response

from gateway import operation_security, request_history_capture
from gateway.access_plans import capability_for_intent
from gateway.frontend_shapes import build_shape, bundled_rows
from gateway.workflow_runtime_types import (
    FrontendRow, JobService, RegistrySnapshot, RequestSpool, RunJobView, RunRefused, WorkflowEntry)

PREFER_ASYNC = "respond-async"
REQUEST_ID_HEADER = "x-request-id"


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
    new_request_id: Callable[[], str]
    # open_receipt(caller, request_id, canonical_path, row) -> an async context manager whose owner has
    # ``report(status=, reason=)`` and ``handoff()``: leaving it closes the receipt with the reported outcome (cancelled
    # for a cancelled request). ``handoff()`` leaves the closing to the run core, which calls the run port's
    # ``close_receipt`` when the job ends.
    open_receipt: Callable[[Any, str, str, FrontendRow], AsyncContextManager[Any]]
    # name_receipt(owner, entry): the receipt names the record the row chose (known once the shape has read the body).
    name_receipt: Callable[[Any, WorkflowEntry], None]


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


def run_facts(entry: WorkflowEntry) -> dict[str, str]:
    """What the kept request says it ran, from the chosen record alone."""
    facts = {"workflow": entry.id, "title": str(entry.contract.get("display_name") or entry.id)}
    capability = capability_for_intent(entry.intent)
    if capability:
        facts["capability"] = capability
    return facts


def _outcome(view: RunJobView | None) -> tuple[str, str]:
    """The receipt outcome of a job as the sync wait left it."""
    if view is None:
        return "failed", "job_unavailable"
    if view.status == "succeeded":
        return "succeeded", "succeeded"
    if view.status == "failed":
        return "failed", view.failure.code if view.failure is not None else "failed"
    if view.status == "canceled":
        return "cancelled", "cancelled"
    return "failed", "wait_timeout"


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
        wants_async = prefers_async(request.headers.get("prefer"))
        answered = dataclasses.replace(row, mode="job") if wants_async else row
        request_id = deps.new_request_id()
        async with deps.open_receipt(caller, request_id, canonical, row) as owner:
            response = await _serve_open(deps, row, answered, shape, core, request, caller, snapshot, spool, owner,
                                         request_id, wants_async)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    return serve


async def _serve_open(deps: RowRouteDeps, row: FrontendRow, answered: FrontendRow, shape: Any, core: Any,
                      request: Request, caller: Any, snapshot: RegistrySnapshot, spool: RequestSpool, owner: Any,
                      request_id: str, wants_async: bool) -> Response:
    """The steps after the receipt opens. Each exit reports the outcome, or hands the receipt to the core."""
    with deps.scope(request, spool):
        run_request = await shape.to_run_request(request, row, snapshot, spool=spool)
        if isinstance(run_request, Response):
            owner.report(status="failed", reason=f"http_{run_request.status_code}")
            return run_request
        entry = snapshot.get(run_request.workflow_id)
        facts: dict[str, str] = {}
        if entry is not None:
            deps.name_receipt(owner, entry)
            facts = run_facts(entry)
            setattr(request.state, request_history_capture.RUN_STATE_KEY, facts)
        run_request = dataclasses.replace(run_request, request_id=request_id)
        refused = await asyncio.to_thread(core.target_gate, run_request.workflow_id, caller, snapshot=snapshot)
        outcome = refused if refused is not None else await core.submit_run(run_request, caller, snapshot=snapshot)
    if isinstance(outcome, RunRefused):
        owner.report(status="failed", reason=outcome.code)
        return await shape.to_response(outcome, answered, jobs=deps.jobs(), caller=caller, request=request)
    if wants_async and outcome.disposition == "new":
        # The core closes the receipt when the job ends; nothing may await between this call and leaving the receipt.
        owner.handoff()
        return await shape.to_response(outcome, answered, jobs=deps.jobs(), caller=caller, request=request)
    response = await shape.to_response(outcome, answered, jobs=deps.jobs(), caller=caller, request=request)
    view = await asyncio.to_thread(_job_view, deps, outcome.job_id, caller)
    status, reason = _outcome(view)
    owner.report(status=status, reason=reason)
    if view is not None and view.status == "succeeded" and not wants_async:
        asset = next((ref.asset_id for ref in (view.results or ()) if ref.asset_id), None)
        if asset:
            setattr(request.state, request_history_capture.RUN_STATE_KEY, {**facts, "asset_id": asset})
    return response


def _job_view(deps: RowRouteDeps, job_id: str, caller: Any) -> RunJobView | None:
    try:
        return deps.jobs().get(job_id, caller)
    except Exception:  # noqa: BLE001 - an unreadable job is reported as unavailable, never as a failed answer
        return None
