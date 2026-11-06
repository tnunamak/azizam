"""Owner-only HTTP surface of the service transition gate (design D2 and D4, step T1.2).

Mounted on the admin engine router (``/admin/api``):

    POST /engines/domains/{domain}/transitions               body {kind, target?, idempotency_key, compensates?}
    GET  /engines/domains/{domain}/transitions/{op_id}       the operation
    GET  /engines/domains/{domain}/gate                      {gate, op_id, parked, active_leases, last_lease_at}
    POST /engines/domains/{domain}/transitions/{op_id}/resolve   body {step, outcome, evidence_ref?}  (decision G3)

202 a new operation, or the same one under the same key; 409 another unfinished operation holds the domain, or the
key names another request (decision G4); 404 an unknown domain or one with no transition profile; 422 a request that
cannot start; 503 + Retry-After when the executor cannot take it now. All owner only.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, Callable, Mapping, Optional

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from gateway import client_errors, egress
from gateway.engine_admin import EngineRouteError, _is_owner, _owner_only
from gateway.transition_executor import InvalidTransition, UnknownDomain
from gateway.transition_gate import TransitionUnavailable
from gateway.transition_ops import Conflict, Operation, STEP_ORDER
from gateway.transition_runtime import NoProfile, TransitionRuntime
from gateway.transition_store import StaleOutcome

KINDS = ("select", "park", "wake")
OUTCOMES = ("success", "no_change", "partial", "unknown")
_KEY_LIMIT = 128


def _operation_json(operation: Operation) -> dict[str, Any]:
    return dataclasses.asdict(operation)


def _body_text(body: Mapping[str, Any], key: str, *, required: bool = False) -> Optional[str]:
    value = body.get(key)
    if value is None:
        if required:
            raise EngineRouteError(422, "invalid_transition", f"{key} is required", field=key)
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _KEY_LIMIT:
        raise EngineRouteError(422, "invalid_transition",
                               f"{key} is a text of 1 to {_KEY_LIMIT} characters", field=key)
    return value.strip()


def register_transition_routes(router: APIRouter, *, runtime: Callable[[], TransitionRuntime], guard: Callable[[Request], Any],
                               read_json: Callable[[Request], Any]) -> None:
    """Mount the transition routes. ``runtime`` is read per request (the gateway builds it once)."""

    async def _checked(request: Request, domain: str) -> tuple[Any, TransitionRuntime]:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            raise _Early(ctx)
        if not _is_owner(ctx):
            raise _Early(_owner_only("Only the owner can run a transition.").as_response())
        wired = runtime()
        if not await asyncio.to_thread(lambda: wired.is_domain(domain) and wired.enabled(domain)):
            raise _Early(EngineRouteError(status.HTTP_404_NOT_FOUND, "unknown_domain",
                                          "No such residency domain with a transition profile.").as_response())
        return ctx, wired

    def _answer(content: Mapping[str, Any], code: int = 200, headers: Optional[Mapping[str, str]] = None) -> JSONResponse:
        return egress.plain_json_response(dict(content), response_class="transition", status_code=code, headers=headers)

    def _unavailable(exc: TransitionUnavailable) -> JSONResponse:
        seconds = client_errors.retry_after(str(exc.retry_after_s))
        return EngineRouteError(status.HTTP_503_SERVICE_UNAVAILABLE, "transition_unavailable",
                                f"The domain cannot take this now: {exc.reason}.",
                                headers={"Retry-After": seconds} if seconds is not None else None).as_response()

    @router.post("/engines/domains/{domain}/transitions")
    async def start_transition(domain: str, request: Request) -> JSONResponse:
        try:
            _ctx, wired = await _checked(request, domain)
            body = await read_json(request)
            if not isinstance(body, Mapping):
                raise EngineRouteError(422, "invalid_transition", "The body is an object.")
            kind = body.get("kind")
            if kind not in KINDS:
                raise EngineRouteError(422, "invalid_transition",
                                       f"kind is one of {', '.join(KINDS)}", field="kind")
            key = _body_text(body, "idempotency_key", required=True)
            target = _body_text(body, "target")
            compensates = _body_text(body, "compensates")
            began = await wired.executor().submit(domain=domain, kind=kind, idempotency_key=key or "",
                                                  target=target, compensates=compensates)
        except _Early as early:
            return early.response
        except EngineRouteError as exc:
            return exc.as_response()
        except UnknownDomain:
            return EngineRouteError(status.HTTP_404_NOT_FOUND, "unknown_domain", "No such residency domain.").as_response()
        except InvalidTransition as exc:
            return EngineRouteError(422, "invalid_transition", str(exc)).as_response()
        except NoProfile:
            return EngineRouteError(status.HTTP_404_NOT_FOUND, "unknown_domain", "No such residency domain.").as_response()
        except TransitionUnavailable as exc:
            return _unavailable(exc)
        operation = began.operation
        if isinstance(began, Conflict):
            return _answer({"op_id": operation.op_id, "state": operation.state}, status.HTTP_409_CONFLICT)
        gate = wired.executor().store.gates.state(domain)  # type: ignore[attr-defined]
        return _answer({"op_id": operation.op_id, "state": operation.state, "gate": gate}, status.HTTP_202_ACCEPTED)

    @router.get("/engines/domains/{domain}/transitions/{op_id}")
    async def get_transition(domain: str, op_id: str, request: Request) -> JSONResponse:
        try:
            _ctx, wired = await _checked(request, domain)
        except _Early as early:
            return early.response
        operation = await asyncio.to_thread(wired.executor().store.get, op_id)
        if operation is None or operation.domain != domain:
            return EngineRouteError(status.HTTP_404_NOT_FOUND, "unknown_transition", "No such transition.").as_response()
        return _answer({"operation": _operation_json(operation)})

    @router.get("/engines/domains/{domain}/gate")
    async def get_gate(domain: str, request: Request) -> JSONResponse:
        try:
            _ctx, wired = await _checked(request, domain)
        except _Early as early:
            return early.response
        store = wired.executor().store
        current = await asyncio.to_thread(store.current, domain)
        gates = store.gates  # type: ignore[attr-defined]
        active, last = wired.leases(domain)
        return _answer({"gate": gates.state(domain), "op_id": current.op_id if current is not None else None,
                        "parked": gates.parked(domain), "active_leases": active, "last_lease_at": last})

    @router.post("/engines/domains/{domain}/transitions/{op_id}/resolve")
    async def resolve_transition(domain: str, op_id: str, request: Request) -> JSONResponse:
        try:
            _ctx, wired = await _checked(request, domain)
            body = await read_json(request)
            if not isinstance(body, Mapping):
                raise EngineRouteError(422, "invalid_transition", "The body is an object.")
            step = body.get("step")
            outcome = body.get("outcome")
            if step not in STEP_ORDER:
                raise EngineRouteError(422, "invalid_transition", "step names a step.", field="step")
            if outcome not in OUTCOMES:
                raise EngineRouteError(422, "invalid_transition",
                                       f"outcome is one of {', '.join(OUTCOMES)}", field="outcome")
            evidence_ref = _body_text(body, "evidence_ref")
            executor = wired.executor()
            held = await asyncio.to_thread(executor.store.get, op_id)
            if held is None or held.domain != domain:
                raise EngineRouteError(status.HTTP_404_NOT_FOUND, "unknown_transition", "No such transition.")
            operation = await executor.resolve(op_id, step=step, outcome=outcome, evidence_ref=evidence_ref)
        except _Early as early:
            return early.response
        except EngineRouteError as exc:
            return exc.as_response()
        except StaleOutcome as exc:
            return EngineRouteError(status.HTTP_409_CONFLICT, "nothing_to_resolve", str(exc)).as_response()
        return _answer({"op_id": operation.op_id, "state": operation.state,
                        "gate": wired.executor().store.gates.state(domain)}, status.HTTP_200_OK)  # type: ignore[attr-defined]


class _Early(Exception):
    """A response decided before the route body ran (not signed in, not the owner, no such domain)."""

    def __init__(self, response: JSONResponse) -> None:
        super().__init__("early response")
        self.response = response
