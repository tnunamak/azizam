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
from collections.abc import Sequence
from typing import Any, AsyncContextManager, Awaitable, Callable, ContextManager, Iterable, Mapping

from fastapi import FastAPI, Request
from starlette.responses import Response

from gateway import operation_security, request_history_capture
from gateway.access_plans import capability_for_intent
from gateway.frontend_shapes import _wire, build_shape, bundled_rows
from gateway.frontend_shapes._base import LegacyRoute
from gateway.workflow_run import RUN_INTENT
from gateway.workflow_runtime_types import (
    FrontendRow, JobService, RegistrySnapshot, RequestSpool, RunJobView, RunRefused)

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
    # name_receipt(owner, workflow_id, record_revision, contract_digest): the receipt names the record the request ran
    # (the one the row chose once the shape has read the body; the job's pinned one for a replay). A fact the caller does
    # not hold is None.
    name_receipt: Callable[[Any, str, int | None, str | None], None]
    # pinned(job_id) -> the definitions the job pinned, or None when it holds none.
    pinned: Callable[[str], Any]
    # record_inputs(owner, notes): the receipt lists the inputs the shape accepted, ignored or refused, as
    # ``(name, "accepted" | "ignored" | "rejected", reason)`` in the order the shape met them. None: the row's receipt lists none.
    record_inputs: Callable[[Any, Sequence[tuple[str, str, str | None]]], None] | None = None
    # record_facts(owner, facts): what the shape adjusted, each as (name, value, warning): the receipt keeps the value under
    # ``metadata[name]`` and raises the warning.
    record_facts: Callable[[Any, Sequence[tuple[str, Any, str | None]]], None] | None = None
    # legacy: the handlers a shape may hand a request to (``LegacyRoute.name``), each ``handler(request, caller, body)``, which enforces the route policy and owns its receipt itself ->
    # the answer. They exist for requests the data path does not serve yet; ``tests/test_legacy_video_ratchet.py`` counts them.
    # openapi: the OpenAPI operation documents of rows, by row id (the request and answer schemas the generic route does not know).
    openapi: Mapping[str, Mapping[str, Any]] = dataclasses.field(default_factory=dict)
    legacy: Mapping[str, Callable[[Request, Any, Mapping[str, Any]], Awaitable[Response]]] = dataclasses.field(default_factory=dict)


def row_paths(row_ids: Iterable[str]) -> tuple[str, ...]:
    """The canonical paths the named bundled rows mount: the paths their request receipts record."""
    rows = {row.id: row for row in bundled_rows()}
    return tuple(route.canonical for row_id in row_ids for route in rows[row_id].routes)


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
            for path in (route.canonical, *route.aliases):      # an alias answers as the canonical path: same policy and receipt
                app.add_api_route(path, _handler(deps, row, shape, route.canonical), methods=list(route.methods),
                                  include_in_schema=True, name=f"row:{row.id}:{path}",
                                  openapi_extra={**deps.openapi.get(row.id, {}), **operation_security.openapi_extra(operation_security.APP_KEY)})


def run_facts(workflow_id: str, contract: Mapping[str, Any]) -> dict[str, str]:
    """What the kept request says it ran, from one workflow's contract alone."""
    facts = {"workflow": workflow_id, "title": str(contract.get("display_name") or workflow_id)}
    capability = capability_for_intent(str(contract.get("intent") or ""))
    if capability:
        facts["capability"] = capability
    return facts


def _outcome(view: RunJobView | None, *, waited: bool = True) -> tuple[str, str]:
    """The receipt outcome of a job as the answer left it. ``waited`` is False when the answer was the job itself with no
    wait: a job still running is then accepted, and ``wait_timeout`` is only for a wait that ran out."""
    if view is None:
        return "failed", "job_unavailable"
    if not waited and view.status in ("queued", "running"):
        return "succeeded", "accepted"
    if view.status == "succeeded":
        return "succeeded", "succeeded"
    if view.status == "failed":
        return "failed", view.failure.code if view.failure is not None else "failed"
    if view.status == "canceled":
        return "cancelled", "cancelled"
    return "failed", "wait_timeout"


def route_backend(row: FrontendRow, path: str) -> str | None:
    """The service the matched route declares its receipt files under (``runtime_policy.backend``); None when it declares none."""
    route = next((r for r in row.routes if path == r.canonical or path in r.aliases), None)
    return (route.runtime_policy if route else {}).get("backend") or None


def route_intent(row: FrontendRow, path: str) -> str:
    """The intent the matched route declares (``runtime_policy.intent``); the generic run intent when it declares none.
    The request receipt and the job row both file under it."""
    route = next((r for r in row.routes if path == r.canonical or path in r.aliases), None)
    return str((route.runtime_policy if route else {}).get("intent") or RUN_INTENT)


def _handler(deps: RowRouteDeps, row: FrontendRow, shape: Any, canonical: str) -> Callable[[Request], Awaitable[Response]]:
    async def serve(request: Request) -> Response:
        caller = await asyncio.to_thread(deps.authenticate, request, canonical)
        if isinstance(caller, Response):
            return caller
        request.state.caller = caller            # the shapes that look a stored source up read it here
        handoff = await shape.legacy_route(request, row, deps.snapshot()) if deps.core() is not None else None
        if handoff is not None:
            # The old handler alone enforces the policy and owns the one receipt and request id: no row receipt exists yet.
            handler = deps.legacy.get(handoff.name)
            if handler is not None:
                return await handler(request, caller, handoff.body)
        request_id = deps.new_request_id()
        if row.receipt_opens == "before_policy":
            # The receipt is open when the policy decides, so a denial leaves a closed receipt behind it.
            async with deps.open_receipt(caller, request_id, canonical, row) as owner:
                denied = await asyncio.to_thread(deps.enforce_policy, caller, canonical)
                if denied is not None:
                    owner.report(status="failed", reason="policy_denied")
                    denied.headers[REQUEST_ID_HEADER] = request_id
                    return denied
                response = await answer(request, caller, owner, request_id)
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        denied = await asyncio.to_thread(deps.enforce_policy, caller, canonical)
        if denied is not None:
            return denied
        async with deps.open_receipt(caller, request_id, canonical, row) as owner:
            response = await answer(request, caller, owner, request_id)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    async def answer(request: Request, caller: Any, owner: Any, request_id: str) -> Response:
        core = deps.core()
        if core is None:
            response = deps.refused(RunRefused(503, "workflow_runtime_unavailable", "Workflow runs are not configured."))
            owner.report(status="failed", reason="workflow_runtime_unavailable")
            return response
        snapshot = deps.snapshot()
        spool = deps.new_spool()
        wants_async = prefers_async(request.headers.get("prefer"))
        answered = dataclasses.replace(row, mode="job") if wants_async else row
        return await _serve_open(deps, row, answered, shape, core, request, caller, snapshot, spool, owner,
                                 request_id, wants_async, route_intent(row, canonical), route_backend(row, canonical))

    return serve


async def _serve_open(deps: RowRouteDeps, row: FrontendRow, answered: FrontendRow, shape: Any, core: Any,
                      request: Request, caller: Any, snapshot: RegistrySnapshot, spool: RequestSpool, owner: Any,
                      request_id: str, wants_async: bool, intent: str, backend: str | None) -> Response:
    """The steps after the receipt opens. Each exit reports the outcome, or hands the receipt to the core."""
    if row.unconfigured is not None and not _wire.candidates(snapshot, row):
        # The deployment configures no workflow for this route: the row says what that is told.
        owner.report(status="failed", reason=str(row.unconfigured["code"]))
        return _wire.error_response(int(row.unconfigured["status"]), str(row.unconfigured["message"]),
                                    code=str(row.unconfigured["code"]))
    with deps.scope(request, spool):
        run_request = await shape.to_run_request(request, row, snapshot, spool=spool)
        record_inputs(deps, owner, request)
        if isinstance(run_request, LegacyRoute):         # the shape must have said so in ``legacy_route``; never a second owner
            owner.report(status="failed", reason="legacy_handoff_late")
            return _wire.error_response(500, "This request is not served", kind="api_error")
        if isinstance(run_request, Response):
            owner.report(status="failed", reason=f"http_{run_request.status_code}")
            return run_request
        entry = snapshot.get(run_request.workflow_id)
        facts: dict[str, str] = {}
        if entry is not None and row.receipt_closes == "terminal":
            deps.name_receipt(owner, entry.id, entry.record_revision, entry.contract_digest)
        if entry is not None and row.history_facts:
            facts = run_facts(entry.id, entry.contract)
            setattr(request.state, request_history_capture.RUN_STATE_KEY, facts)
        run_request = dataclasses.replace(run_request, request_id=request_id, intent=intent)
        # No separate gate call: ``submit_run`` gates a new job after it resolves the idempotency claim, so a replay of a
        # finished job still answers that job when its backend has become unavailable (S7 parity 6.2).
        outcome = await core.submit_run(run_request, caller, snapshot=snapshot)
    if isinstance(outcome, RunRefused):
        refusal = await shape.to_response(outcome, answered, jobs=deps.jobs(), caller=caller, request=request)
        owner.report(status="failed", reason=outcome.code)
        return refusal
    if row.backend_at_run:                                # the run exists: the service it runs on is now the receipt's
        owner.dispatching(backend)
    await shape.after_submit(request, outcome, caller)
    if outcome.disposition == "replay":
        facts = await asyncio.to_thread(_name_pinned, deps, owner, request, outcome, row)
    if row.receipt_closes == "submit" and outcome.disposition == "new":
        # The receipt is a submit receipt: it closes now, as the queued answer goes out, and the job never touches it.
        response = await shape.to_response(outcome, answered, jobs=deps.jobs(), caller=caller, request=request)
        owner.report(status="succeeded", reason=f"http_{response.status_code}")
        return response
    if answered.mode == "job" and outcome.disposition == "new":
        # The core closes the receipt when the job ends; nothing may await between this call and leaving the receipt.
        owner.handoff()
        return await shape.to_response(outcome, answered, jobs=deps.jobs(), caller=caller, request=request)
    response = await shape.to_response(outcome, answered, jobs=deps.jobs(), caller=caller, request=request)
    view = await asyncio.to_thread(_job_view, deps, outcome.job_id, caller)
    status, reason = _outcome(view, waited=answered.mode != "job")
    if view is not None and view.status == "succeeded" and row.result_receipt and deps.record_facts is not None:
        kept = [(name, view.result_metadata[key], None) for key, name in row.result_receipt.items() if key in view.result_metadata]
        deps.record_facts(owner, kept)
    owner.report(status=status, reason=reason)
    if view is not None and view.status == "succeeded" and answered.mode != "job":
        asset = next((ref.asset_id for ref in (view.results or ()) if ref.asset_id), None)
        if asset and row.history_facts:
            setattr(request.state, request_history_capture.RUN_STATE_KEY, {**facts, "asset_id": asset})
    return response


def record_inputs(deps: RowRouteDeps, owner: Any, request: Request) -> None:
    """The inputs the shape accepted, ignored or refused while it read the request, written to the receipt."""
    notes = getattr(request.state, "input_notes", None)
    if notes and deps.record_inputs is not None:
        deps.record_inputs(owner, notes)
    facts = getattr(request.state, "receipt_facts", None)
    if facts and deps.record_facts is not None:
        deps.record_facts(owner, facts)


def _name_pinned(deps: RowRouteDeps, owner: Any, request: Request, view: RunJobView, row: FrontendRow) -> dict[str, str]:
    """A replay answers the job that was made, not the record as it is now: the receipt and the kept request name what the
    job pinned. A fact the job does not hold stays unknown."""
    try:
        pin = deps.pinned(view.job_id)
    except Exception:  # noqa: BLE001 - an unreadable pin leaves the facts unknown
        pin = None
    named = row.receipt_closes == "terminal"
    if pin is None:
        if named:
            deps.name_receipt(owner, view.workflow_id, view.record_revision, None)
        facts = {"workflow": view.workflow_id}
    else:
        envelope = pin.envelope
        if named:
            deps.name_receipt(owner, envelope.source_id, envelope.record_revision, envelope.contract)
        facts = run_facts(envelope.source_id, pin.contract)
    if row.history_facts:
        setattr(request.state, request_history_capture.RUN_STATE_KEY, facts)
    return facts


def _job_view(deps: RowRouteDeps, job_id: str, caller: Any) -> RunJobView | None:
    try:
        return deps.jobs().get(job_id, caller)
    except Exception:  # noqa: BLE001 - an unreadable job is reported as unavailable, never as a failed answer
        return None
