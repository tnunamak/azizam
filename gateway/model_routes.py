"""Compile exact service-local model references into immutable ordered route edges.

Availability and caller policy are deliberately absent. A registry revision owns
these values; a disconnected credential cannot change the graph's identity.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any


class ModelRouteError(ValueError):
    """Invalid route data, with a stable code and registry location."""

    def __init__(self, code: str, path: tuple[str | int, ...], message: str):
        self.code, self.path = code, path
        super().__init__(f"{code}: {'.'.join(map(str, path))}: {message}")


@dataclass(frozen=True)
class RouteEdge:
    id: str
    service_id: str
    model_id: str

    @property
    def target(self) -> tuple[str, str]:
        return self.service_id, self.model_id

    @property
    def deployment_id(self) -> str:
        """An injective private LiteLLM name, never a shared logical name."""
        return f"gateway-route:{len(self.service_id)}:{self.service_id}:{self.model_id}"


@dataclass(frozen=True)
class ModelRouteIndex:
    by_model: Mapping[tuple[str, str], tuple[RouteEdge, ...]]
    pins: Mapping[str, RouteEdge]
    pin_owners: Mapping[str, tuple[str, str]]


def model_records(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = config.get("models")
    if isinstance(raw, Mapping):
        return [{"id": key, **dict(row)} if isinstance(row, Mapping)
                else {"id": key, "upstream_model": row} for key, row in raw.items()]
    if isinstance(raw, (list, tuple)):
        return [row if isinstance(row, Mapping) else {"id": row, "upstream_model": row}
                for row in raw if isinstance(row, (Mapping, str))]
    return []


def validate_route_fields(record: Mapping[str, Any], path: tuple[str | int, ...] = ()) -> None:
    """Validate the closed route shape without resolving cross-service references."""
    if "execution_only" in record and type(record["execution_only"]) is not bool:
        raise ModelRouteError("invalid_execution_only", path, "execution_only must be a boolean")
    if "routes" not in record:
        return
    if record.get("base_model_id") not in (None, ""):
        raise ModelRouteError("route_on_alias", path, "only a base model may declare routes")
    routes = record["routes"]
    if not isinstance(routes, (list, tuple)):
        raise ModelRouteError("invalid_routes", path, "routes must be a list")
    if not routes:
        raise ModelRouteError("empty_routes", path, "routes must not be empty")
    for index, edge in enumerate(routes):
        at = (*path, "routes", index)
        if not isinstance(edge, Mapping):
            raise ModelRouteError("invalid_route", at, "a route must be an object")
        unknown = set(edge) - {"id", "service_id", "model_id"}
        if unknown:
            raise ModelRouteError("unknown_route_field", at, f"unknown fields: {sorted(unknown)}")
        for key in ("id", "service_id", "model_id"):
            if not isinstance(edge.get(key), str) or not edge[key]:
                raise ModelRouteError("invalid_route", at, f"{key} must be a nonempty string")


def compile_model_routes(services: Mapping[str, Any], *, reserved_names: Iterable[str] = ()) -> ModelRouteIndex:
    """Compile a snapshot's services. All target comparisons are exact, with no I/O.

    Legacy public-name sharing is unchanged for singleton records. A declared
    logical group must uniquely own its public names; execution-only rows make
    same-id execution targets unambiguous without publishing another name.
    """
    reserved = frozenset(reserved_names)
    records: dict[tuple[str, str], Mapping[str, Any]] = {}
    public: dict[str, list[tuple[str, str]]] = {}
    for sid, service in services.items():
        config = service.get("config", {}) if isinstance(service, Mapping) else {}
        if not isinstance(config, Mapping):
            continue
        for row in model_records(config):
            mid = row.get("id") or row.get("model_group") or row.get("upstream_model")
            if not isinstance(mid, str) or not mid:
                continue
            key = (sid, mid)
            path = ("services", sid, "config", "models", mid)
            validate_route_fields(row, path)
            if key in records:
                raise ModelRouteError("duplicate_model_id", path, "model id is not unique within its service")
            records[key] = row
            if row.get("execution_only") is not True:
                names = {mid, row.get("model_group") or mid}
                # Existing remote publication decorates names with the configured provider id.
                provider = config.get("provider_id")
                if provider:
                    names |= {f"{provider}/{name}" for name in tuple(names) if not name.startswith(f"{provider}/")}
                for name in names:
                    public.setdefault(name, []).append(key)

    by_model: dict[tuple[str, str], tuple[RouteEdge, ...]] = {}
    pins: dict[str, RouteEdge] = {}
    owners: dict[str, tuple[str, str]] = {}
    for key, record in records.items():
        if record.get("base_model_id") not in (None, ""):
            continue
        sid, mid = key
        path = ("services", sid, "config", "models", mid)
        raw = record.get("routes")
        if raw is None:
            by_model[key] = (RouteEdge(f"{sid}/{mid}", sid, mid),)
            continue
        for name, claimants in public.items():
            if key in claimants and len(claimants) > 1:
                raise ModelRouteError("duplicate_public_name", path, f"logical name {name!r} has multiple owners")
        edges: list[RouteEdge] = []
        targets: set[tuple[str, str]] = set()
        for i, item in enumerate(raw):
            edge = RouteEdge(item["id"], item["service_id"], item["model_id"])
            at = (*path, "routes", i)
            if edge.id in reserved:
                raise ModelRouteError("pin_public_name_collision", at, f"pin {edge.id!r} names another public entity")
            if edge.id in pins:
                raise ModelRouteError("duplicate_route_id", at, f"route/pin id {edge.id!r} is already declared")
            if edge.service_id not in services:
                raise ModelRouteError("missing_route_service", at, f"service {edge.service_id!r} does not exist")
            target = records.get(edge.target)
            if target is None:
                raise ModelRouteError("missing_route_model", at,
                    f"model {edge.model_id!r} does not exist in its service. "
                    "If the target is discovered from the connection, add the target row first "
                    f"in services.{edge.service_id}.config.models.")
            if target.get("base_model_id") not in (None, ""):
                raise ModelRouteError("route_target_alias", at, "a route must target a base record")
            if "routes" in target and edge.target != key:
                raise ModelRouteError("nested_routes", at, "a target has routes; only the group's own self edge is permitted")
            if edge.target in targets:
                raise ModelRouteError("duplicate_route_target", at, f"{edge.service_id}/{edge.model_id} is already a route in this group")
            targets.add(edge.target)
            for claimant in public.get(edge.id, ()):
                if claimant != edge.target:
                    raise ModelRouteError("pin_public_name_collision", at, f"pin {edge.id!r} names another public record")
            pins[edge.id], owners[edge.id] = edge, key
            edges.append(edge)
        by_model[key] = tuple(edges)
    # Singleton pins retain exact target identity. An explicit pin may already
    # name the same target's ordinary prefixed singleton publication.
    for key, edges in by_model.items():
        if "routes" in records[key] or records[key].get("execution_only") is True:
            continue
        edge = edges[0]
        if edge.id in pins:
            if pins[edge.id].target != edge.target:
                raise ModelRouteError("duplicate_pin_id", ("services", *key), f"pin {edge.id!r} names different targets")
            continue
        if any(claimant != edge.target for claimant in public.get(edge.id, ())):
            # Public-name sharing between singletons predates routes and stays legal here: the singleton keeps
            # its ordinary name resolution and gets no pin. The registry's own alias checks still refuse
            # the names that are truly ambiguous, with their own message.
            continue
        pins[edge.id], owners[edge.id] = edge, key
    return ModelRouteIndex(MappingProxyType(by_model), MappingProxyType(pins), MappingProxyType(owners))


def require_unreferenced_services(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    """A service cannot be deleted while the candidate document still names it."""
    removed = set(before) - set(after)
    if not removed:
        return
    for sid, service in after.items():
        config = service.get("config") if isinstance(service, Mapping) else None
        if not isinstance(config, Mapping):
            continue  # a left-out or malformed record names no routes
        for row in model_records(config):
            for edge in row.get("routes") or ():
                if isinstance(edge, Mapping) and edge.get("service_id") in removed:
                    raise ModelRouteError("service_referenced_by_route", ("services", sid),
                                          f"service {edge['service_id']!r} is still referenced by route {edge.get('id')!r}")


# Request dispatch deliberately lives below the compiler. Imports here are
# deferred so registry validation stays independent of transports and stores.
@dataclass(frozen=True)
class RouteStep:
    """One edge of a route chain: what serves it and the condition dispatch applies to it.

    ``kind`` is "plan" for a connected plan, "paid" for a paid provider and
    "local" otherwise. ``provider_id`` names the paid provider a step bills.
    ``paid_fallback`` is true for a paid step that runs only when the caller's
    access plan allows paid fallback: a paid edge after an earlier step of the
    same chain, on a name that is not a pin.
    """
    edge: RouteEdge
    kind: str
    connection_id: str | None
    provider_id: str | None
    paid_fallback: bool = False


def route_step(services: Mapping[str, Any], edge: RouteEdge) -> RouteStep:
    """What serves ``edge``: a connected plan, a paid provider, or a local engine."""
    from gateway.service_registry import canonical_provider_id, is_remote_provider_config

    config = services[edge.service_id].get("config") or {}
    source = config.get("credential_source") or {"kind": "static-key"}
    plan = source.get("kind") in {"connection", "cli-session"}
    connection_id = source.get("connection_id") if plan else None
    provider_id = (canonical_provider_id(config.get("provider_id") or edge.service_id)
                   if connection_id is None and is_remote_provider_config(config) else None)
    return RouteStep(edge, "plan" if plan else "paid" if provider_id else "local", connection_id, provider_id)


def route_chain(services: Mapping[str, Any], edges: Iterable[RouteEdge], *, pinned: bool,
                granted: Any = None) -> tuple[RouteStep, ...]:
    """The ordered steps dispatch walks for ``edges``.

    ``granted`` keeps the edges a caller may use, before the order decides
    which paid step is a fallback. Without it the chain is every edge, as an
    owner who may use all of them sees it. Dispatch and the owner's view both
    read this, so the listed chain is the walked chain.
    """
    steps = [route_step(services, edge) for edge in edges if granted is None or granted(edge)]
    return tuple(replace(step, paid_fallback=step.provider_id is not None and number > 0 and not pinned)
                 for number, step in enumerate(steps))


class RouteDispatchRefusal(Exception):
    def __init__(self, code: str, status: int = 403, *, side: str | None = None):
        self.code, self.status, self.side = code, status, side
        super().__init__(code)


def carries_route_state(body: Any) -> bool:
    """Recognize upstream references only in protocol-defined request slots.

    Tool schemas and application data may use the same field names. They are
    declarations, not references to an upstream account or conversation.
    """
    if not isinstance(body, Mapping):
        return False
    if body.get("previous_response_id") or body.get("session_id"):
        return True
    conversation = body.get("conversation")
    if isinstance(conversation, str) and conversation:
        return True
    if isinstance(conversation, Mapping) and conversation.get("id"):
        return True

    def bound_item(item):
        if not isinstance(item, Mapping):
            return False
        kind = item.get("type")
        if kind == "reasoning":
            return bool(item.get("encrypted_content"))
        if kind == "item_reference":
            return bool(item.get("id"))
        if kind == "input_file":
            return bool(item.get("file_id"))
        if kind == "file":
            file = item.get("file")
            return isinstance(file, Mapping) and bool(file.get("file_id"))
        if item.get("role") or kind == "message":
            content = item.get("content")
            return isinstance(content, (list, tuple)) and any(bound_item(part) for part in content)
        return False

    return any(bound_item(item) for field in ("input", "messages")
               for item in (body.get(field) if isinstance(body.get(field), (list, tuple)) else ()))


async def dispatch_routes(*, snapshot, target, request, body, surface, identity,
                          fresh_identity, admission, prepare_body, subscriptions,
                          runtime, ledger_path, admin_db_path=None, local_executor=None,
                          deadline=None, available=None):
    """One request owner, one ordered pass, one deadline, no speculative sends.

    Callbacks supply the ingress's existing authentication, admission,
    transforms and local activation. None of those policies belong to a wire
    protocol. A callback denial is terminal; only explicit attempt facts allow
    advancing to an already authorized edge.
    """
    import asyncio
    import copy
    import json
    import re
    import time
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    from gateway import category_enforcement, provider_execution, providers, subscription_execution
    from gateway.backends.llm_execution import RequestDisconnectWatcher
    from gateway.policy import ALLOWED, APP_SIDE, NOT_IN_ACCESS_PLAN, PERSON_SIDE, PolicyDecision, PolicyRequest, evaluate, not_in_plan
    from gateway.provider_protocols import AttemptResult, RequestError
    from gateway.receipts import AttemptRecorder, ReceiptLifecycleOwner, RouteReceipt

    clock = asyncio.get_running_loop().time
    deadline = deadline if deadline is not None else clock() + 300.0
    logical = target.base_model_id
    protocol = "openai.responses" if surface == "responses" else "openai.chat"
    intent = "chat.responses" if surface == "responses" else "chat.completions"
    policy_request = PolicyRequest(protocol=protocol, intent=intent, model=logical, requested_model=target.requested_model)
    receipt = RouteReceipt(protocol=protocol, intent=intent, path=request.url.path,
                           requested_model=target.requested_model, resolved_model=logical,
                           app_id=getattr(identity, "app_id", None), api_key_name=getattr(identity, "name", None),
                           maker_person_id=getattr(identity, "person_id", None), owner_key_hash=getattr(identity, "key_hash", None),
                           app_profile="+".join(identity.access.plan_ids), stream=bool(body.get("stream")),
                           visibility=identity.access.receipt_detail)
    receipt.metadata.update(logical_model=logical, route_attempts=[])
    def plain(value):
        if isinstance(value, Mapping):
            return {key: plain(child) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(child) for child in value]
        return value
    attempts = receipt.metadata["route_attempts"]
    pinned = target.requested_model in snapshot.model_routes.pins
    bound_state = carries_route_state(body)
    watcher = None
    fact = None
    recorder = None

    def refusal(code, status=403):
        receipt.metadata["status_code"] = status
        route_id = receipt.metadata.get("route_id")
        headers = {"x-azizam-request-id": receipt.request_id}
        if isinstance(route_id, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,512}", route_id):
            headers["x-azizam-route"] = route_id
        return JSONResponse({"error": {"code": code, "message": "The requested model route could not complete this request."}}, status_code=status,
                            headers=headers)

    def record_served_adjustments(response, facts):
        """Disclose what the serving attempt did not send as asked: the header's fields and the attempt's own receipt facts."""
        for name in response.headers.get("x-azizam-dropped-params", "").split(","):
            if name:
                receipt.ignore_input(name, "not sent as requested by the selected route")
        receipt.metadata.update(facts)

    def row_for(edge):
        return next(row for row in model_records(snapshot.services[edge.service_id].get("config") or {}) if row.get("id") == edge.model_id)

    def route_granted(current, edge):
        return current.access.complete and plan_grants(current.access, edge)

    def plan_grants(plan, edge):
        """Whether ``plan`` (one access plan, or the pair's effective access) holds the connection or provider ``edge`` needs."""
        step = route_step(snapshot.services, edge)
        if step.kind == "plan":
            return step.connection_id in plan.connections
        if step.provider_id is not None:
            return step.provider_id in plan.paid_providers
        return True

    def not_in_access_plan(side=None):
        """No route is granted: name the plan that lacks it. The app's, when the person's own plan holds a route."""
        access = identity.access
        if side is None:
            side = APP_SIDE if access.complete and any(plan_grants(access.person_plan, edge) for edge in target.routes) else PERSON_SIDE
        reason = not_in_plan(side, target.requested_model or logical)
        receipt.set_policy_decision("denied", reason=reason, source="gateway:model-routes")
        receipt.set_refusal({"code": NOT_IN_ACCESS_PLAN, "side": side})
        receipt.metadata["status_code"] = 403
        owner.report(status="rejected", reason="failed")
        return JSONResponse({"error": {"message": reason, "type": "access_error", "code": NOT_IN_ACCESS_PLAN,
                                       "details": {"side": side}}},
                            status_code=403, headers={"x-azizam-request-id": receipt.request_id})

    def check_policy(current):
        if current is None or getattr(current, "denial", None):
            raise RouteDispatchRefusal("route_permission_revoked")
        decision = category_enforcement.apply(current, policy_request, evaluate(current.access, policy_request))
        if decision.denied:
            if decision.code == NOT_IN_ACCESS_PLAN:
                raise RouteDispatchRefusal(NOT_IN_ACCESS_PLAN, side=decision.side)
            raise RouteDispatchRefusal("route_not_allowed")
        return decision

    async def race_disconnect(operation):
        task = asyncio.create_task(operation)
        disconnected = asyncio.create_task(watcher.wait())
        try:
            done, _ = await asyncio.wait((task, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                if task.done() and not task.cancelled() and task.exception() is None:
                    completed = task.result()
                    response = completed.response if isinstance(completed, AttemptResult) else completed
                    source = getattr(response, "body_iterator", None)
                    try:
                        close = getattr(source, "aclose", None)
                        if close is not None:
                            await close()
                    finally:
                        # An unstarted generator does not run its finally block.
                        # Attempt-owned cleanup must also cover this boundary.
                        if response.background is not None:
                            await response.background()
                raise RouteDispatchRefusal("client_cancelled", 499)
            return await task
        finally:
            for pending in (task, disconnected):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, disconnected, return_exceptions=True)

    async with ReceiptLifecycleOwner(receipt) as owner:
        try:
            decision = check_policy(identity)
            receipt.set_policy_decision("allowed", source="gateway:model-routes")
            authorized = route_chain(snapshot.services, target.routes, pinned=pinned,
                                     granted=lambda edge: route_granted(identity, edge))
            if not authorized:
                return not_in_access_plan()
            watcher = RequestDisconnectWatcher(request.receive)
            last = None
            last_facts: Mapping[str, Any] = {}
            for number, step in enumerate(authorized):
                edge = step.edge
                config = snapshot.services[edge.service_id].get("config") or {}
                service = snapshot.services[edge.service_id]
                connection_id = step.connection_id
                paid = step.provider_id is not None
                escalation = step.paid_fallback
                started = clock()
                fact = {"route_id": edge.id, "outcome": "skipped", "elapsed_ms": 0}
                attempts.append(fact)
                recorder = AttemptRecorder(owner)
                # A sent plan attempt may spend the plan's allowance even when it
                # refuses, so its facts name the connection and sign-in it ran on.
                def settle(fact=fact, recorder=recorder, connection_id=connection_id, **fields):
                    fact.update(fields)
                    if connection_id and recorder.sent:
                        fact["connection_id"] = connection_id
                        if recorder.generation is not None:
                            fact["generation"] = recorder.generation
                if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,512}", edge.id):
                    raise RouteDispatchRefusal("invalid_route_header", 503)

                async def recheck():
                    current = await fresh_identity()
                    check_policy(current)
                    if not route_granted(current, edge) or (escalation and not current.access.provider_fallback):
                        raise RouteDispatchRefusal("route_permission_revoked")
                    # Target category restrictions can narrow the logical grant,
                    # but hidden execution IDs never grant a capability.
                    target_policy = PolicyRequest(protocol=protocol, intent=intent, model=edge.model_id, requested_model=logical)
                    decision = category_enforcement.apply(current, target_policy, PolicyDecision(ALLOWED))
                    if decision.denied:
                        raise RouteDispatchRefusal("route_not_allowed")
                    denied = await admission(edge, receipt)
                    if denied is not None:
                        raise RouteDispatchRefusal("route_admission_denied", denied.status_code)
                    return current

                if escalation and not identity.access.provider_fallback:
                    fact["safe_error_code"] = "route_paid_fallback_not_allowed"
                    owner.report(status="failed", reason="failed")
                    if last is not None:
                        record_served_adjustments(last, last_facts)
                    return last if last is not None else refusal("route_paid_fallback_not_allowed")
                if number > 0 and bound_state:
                    raise RouteDispatchRefusal("route_state_incompatible", 400)
                receipt.backend = edge.service_id
                receipt.resolved_model = str(row_for(edge).get("upstream_model") or edge.model_id)
                protocol_id = str(config.get("request_protocol") or "litellm")
                receipt.metadata.update(route_id=edge.id, request_protocol=protocol_id)
                if connection_id:
                    receipt.metadata["connection_id"] = connection_id
                else:
                    receipt.metadata.pop("connection_id", None)
                read_timeout = 120.0
                try:
                    async with asyncio.timeout_at(deadline):
                        current = await fresh_identity()
                        check_policy(current)
                        if not route_granted(current, edge):
                            raise RouteDispatchRefusal("route_permission_revoked")
                        # Availability narrowing happens after authorization order
                        # is fixed, so a disconnected plan still makes paid use
                        # an escalation.
                        configured = not connection_id or await subscription_execution.route_configured(subscriptions, connection_id)
                        if service.get("enabled", True) is False or not configured or (available is not None and not available(edge)):
                            result = AttemptResult(refusal("route_unavailable", 503), advance=True, safe_error_code="route_unavailable")
                        else:
                            transformed = await prepare_body(edge, copy.deepcopy(dict(body)))
                            capabilities = service.get("capabilities") or ()
                            if capabilities and not set(capabilities) & {"openai", "chat", "chat.completions", "chat.responses", "openai.responses", "responses"}:
                                raise RequestError("route_capability_unsupported", "This route cannot serve this capability.")
                            record = row_for(edge)
                            if connection_id:
                                prepared = await subscription_execution.prepare_route_request(subscriptions, connection_id, protocol_id,
                                    receipt.resolved_model, surface=surface, body=transformed)
                                read_timeout = prepared[0].read_timeout
                                current = await recheck()
                                result = await race_disconnect(subscription_execution.execute_route_attempt(
                                    subscriptions, connection_id, protocol_id, receipt.resolved_model, surface=surface, body=transformed,
                                    prepared=prepared, recorder=recorder, before_send=recheck))
                            else:
                                if bound_state:
                                    raise RouteDispatchRefusal("route_state_incompatible", 400)
                                provider = providers.provider_lookup({"services": plain(snapshot.services)}, edge.service_id) if paid else None
                                prepared_body, original = provider_execution.prepare_route_request(transformed, record, provider, surface=surface)
                                current = await recheck()
                                if paid:
                                    response = await race_disconnect(provider_execution.execute_paid_litellm(
                                        request, request_path="/v1/chat/completions", body=prepared_body, provider=provider, identity=current,
                                        runtime=runtime, ledger_path=ledger_path, admin_db_path=admin_db_path,
                                        paid_fallback=escalation, requested_model=target.requested_model,
                                        attempt_recorder=recorder, route_record=record, deployment_id=edge.deployment_id,
                                        before_send=recheck, disconnect_watcher=watcher))
                                elif local_executor is not None:
                                    response = await race_disconnect(local_executor(edge, prepared_body, current, recorder, watcher))
                                else:
                                    raise RequestError("route_protocol_unsupported", "This route has no executor.")
                                response = await provider_execution.route_response(response, surface=surface, original=original)
                                result = AttemptResult(response, sent=recorder.sent, committed=recorder.sent)
                except RequestError as error:
                    if bound_state:
                        raise RouteDispatchRefusal("route_state_incompatible", 400) from error
                    # Local incompatibility never permits dropping fields; only
                    # a later protocol that accepts the original request may run.
                    safe = error.code in {"route_fields_unsupported", "route_capability_unsupported", "route_protocol_unsupported",
                                          "unsupported_reasoning_effort", "paid_option_unsupported", "plan_unsupported_parameter",
                                          "plan_store_unsupported", "plan_system_message_unsupported", "plan_unsupported_reasoning_effort"}
                    if error.param == "reasoning":
                        safe = False
                    # RequestError is raised by gateway request validation, not
                    # by upstream error decoding. Echo its local detail only at
                    # a terminal pre-send boundary; sent failures stay generic.
                    if recorder.sent:
                        safe = False
                    # A refusal that may still advance is generic; once no later edge can run, it is terminal.
                    terminal = number + 1 >= len(authorized) or pinned
                    local_validation = (not recorder.sent and error.status == 400
                                        and (not safe or terminal or error.code == "plan_unsupported_reasoning_effort"))
                    if local_validation:
                        detail = {"code": error.code, "message": error.message, "type": "invalid_request_error"}
                        if error.param:
                            detail["param"] = error.param
                        result = AttemptResult(JSONResponse({"error": detail}, status_code=400), safe_error_code=error.code)
                    else:
                        result = AttemptResult(refusal(error.code, error.status), advance=safe, safe_error_code=error.code)
                settle(elapsed_ms=max(0, int((clock() - started) * 1000)), outcome="sent" if result.sent else "skipped")
                if result.response.headers.get("retry-after"):
                    fact["retry_after"] = result.response.headers["retry-after"]
                if result.safe_error_code:
                    fact["safe_error_code"] = result.safe_error_code
                if receipt.usage:
                    fact["usage"] = dict(receipt.usage)
                result.response.headers["x-azizam-route"] = edge.id
                if result.advance and number + 1 < len(authorized) and not pinned:
                    last, last_facts = result.response, result.facts
                    receipt.usage = {}
                    continue
                response = result.response
                response.headers["x-azizam-route"] = edge.id
                response.headers["x-azizam-request-id"] = receipt.request_id
                record_served_adjustments(response, result.facts)
                if isinstance(response, StreamingResponse):
                    await watcher.aclose()
                    source_body = response.body_iterator
                    async def stream():
                        try:
                            while True:
                                try:
                                    async with asyncio.timeout(read_timeout):
                                        chunk = await anext(source_body)
                                except StopAsyncIteration:
                                    break
                                fact["outcome"] = "committed"
                                yield chunk
                            owner.report(status=(recorder.outcome or ("succeeded", "completed"))[0],
                                         reason=(recorder.outcome or ("succeeded", "completed"))[1])
                        except TimeoutError:
                            fact.update(outcome="refused", safe_error_code="route_idle_timeout")
                            owner.report(status="failed", reason="idle_timeout")
                        except BaseException:
                            fact["outcome"] = "cancelled"
                            raise
                        finally:
                            if receipt.usage:
                                fact["usage"] = dict(receipt.usage)
                            fact["elapsed_ms"] = max(0, int((clock() - started) * 1000))
                            close = getattr(source_body, "aclose", None)
                            if close:
                                await close()
                    response.body_iterator = owner.handoff_stream(stream())
                    return response
                fact["outcome"] = "committed" if response.status_code < 400 else "refused"
                outcome = recorder.outcome
                if outcome is None:
                    outcome = ("succeeded", "completed") if response.status_code < 400 else ("failed", "failed")
                owner.report(status=outcome[0], reason=outcome[1])
                return response
            owner.report(status="failed", reason="failed")
            return last or refusal("route_unavailable", 503)
        except RouteDispatchRefusal as error:
            if fact is not None:
                settle(outcome="cancelled" if error.status == 499 else "refused", safe_error_code=error.code,
                       elapsed_ms=max(0, int((clock() - started) * 1000)))
            if error.code == NOT_IN_ACCESS_PLAN:
                return not_in_access_plan(error.side)
            owner.report(status="cancelled" if error.status == 499 else "failed", reason="cancelled" if error.status == 499 else "failed")
            return refusal(error.code, error.status)
        except TimeoutError:
            if fact is not None:
                settle(outcome="refused", safe_error_code="route_deadline_exceeded",
                       elapsed_ms=max(0, int((clock() - started) * 1000)))
            owner.report(status="failed", reason="idle_timeout")
            return refusal("route_deadline_exceeded", 504)
        finally:
            if watcher is not None:
                await watcher.aclose()
