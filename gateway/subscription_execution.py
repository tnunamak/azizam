"""Run a request on the gateway's connected subscription plan: send it, translate it, receipt it, name its refusals.

Billing mode "plan": nothing is reserved or charged in dollars. The receipt records the provider, the model and the
usage the upstream reports. Plan errors (usage limit and the like) become named refusals, never a 500. Everything
provider-specific (fields the endpoint rejects, error codes, addresses) comes from the kind's data row.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse

from gateway import client_errors, turn_state
from gateway.subscription_kinds import Refusal
from gateway.subscription_oauth import SubscriptionError
from gateway.provider_protocols import REQUEST_PROTOCOLS, RequestError


def refusal_response(refusal: Refusal, *, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": refusal.code, "message": refusal.message, "type": "plan_refusal"}},
                        status_code=refusal.status, headers=dict(headers or {}))


async def prepare_route_request(service, connection_id, protocol_id, slug, *, surface, body):
    """Local compatibility only: never acquire or discover credentials/models."""
    kind = await asyncio.to_thread(service.kind, connection_id)
    protocol = REQUEST_PROTOCOLS.get(protocol_id)
    if protocol is None:
        raise RequestError("route_protocol_unsupported", "This route has no compatible request protocol.")
    dropped = set(body) & kind.dropped_fields
    if surface == "responses":
        dropped -= kind.unsupported_fields
    checked_body = {key: value for key, value in body.items() if key not in dropped}
    payload = protocol.check_request(surface, checked_body, kind, slug)
    advertised = await asyncio.to_thread(service.efforts_for, connection_id, slug)
    top_effort = await asyncio.to_thread(service.top_effort_for, connection_id)
    facts = dict(protocol.apply_model_facts(surface, payload, advertised, body, top_effort=top_effort))
    if facts.get("plan_effort_raised_from"):
        dropped.add("reasoning_effort")
    if dropped:
        facts["plan_params_not_sent"] = sorted(dropped)
    return kind, payload, facts


async def execute_route_attempt(service, connection_id, protocol_id, slug, *, surface, body,
                                prepared, recorder, before_send):
    """Execute one plan edge; return facts without owning a request receipt."""
    from gateway.provider_protocols import AttemptResult
    from starlette.background import BackgroundTask

    kind, payload, facts = prepared
    facts = dict(facts)
    protocol = REQUEST_PROTOCOLS[protocol_id]
    receipt = recorder.receipt
    receipt.cost = {"usd": None, "source": "plan", "provider": kind.id, "cost_class": "plan", "mode": kind.billing_mode}
    headers = {"x-azizam-request-id": receipt.request_id, "x-azizam-provider": kind.id, "x-azizam-billing": kind.billing_mode}
    # Fields not sent are disclosed only once the request has been sent: before that, nothing was dropped on the wire.
    # The facts ride on the result, so only the attempt that serves the client records them.
    disclosure = {"x-azizam-dropped-params": ",".join(facts["plan_params_not_sent"])} if facts.get("plan_params_not_sent") else {}

    if facts.get("plan_effort_lowered_from"):
        disclosure["x-azizam-plan-effort-lowered-from"] = facts["plan_effort_lowered_from"]

    def attempt(response, **fields):
        return AttemptResult(response, facts=dict(facts) if sent else {}, sent=sent, **fields)

    namespace = await asyncio.to_thread(service.cache_namespace, connection_id)
    # Who is asking, for a protocol that keeps conversation state per caller (it must never cross gateway users).
    caller = "|".join(str(value or "") for value in (receipt.maker_person_id, receipt.app_id, receipt.owner_key_hash))
    payload = protocol.prepare(payload, kind, namespace, slug, caller=caller)
    turn: _PlanTurn | None = None
    client = upstream = None
    handed_off = False
    sent = False

    async def close():
        if upstream is not None:
            # A local protocol can learn usage only while consuming its stream, including a refused run.
            await service.note_usage_snapshot(connection_id, getattr(upstream, "usage_snapshot", None), generation=token.generation)
            await upstream.aclose()
        if client is not None:
            await client.aclose()

    try:
        token = await service.acquire(connection_id)
        recorder.generation = token.generation
        turn = _open_turn(service, protocol, kind, connection_id, token.generation, receipt, payload)
        turn_headers = {turn.header: turn.value} if turn and turn.value else {}
        if turn:
            facts["plan_turn_state"] = turn.disclosure
            disclosure["x-azizam-turn-state"] = turn.disclosure
        for refresh_attempt in (1, 2):
            await before_send()
            await service.validate_lease(token)
            recorder.mark_phase("started_at_ms")
            sent = True
            headers.update(disclosure)
            client, upstream = await protocol.send(kind, payload, token, service.new_client, turn_headers)
            if upstream.status_code != 401 or refresh_attempt == 2:
                break
            await close()
            client = upstream = None
            token = await service.acquire(connection_id, rejected=token)
            recorder.generation = token.generation
        if turn and upstream.status_code < 400:
            service.turn_states.incoming(turn.scope, turn.turn_id, upstream.headers.get(turn.header))
        # The plan reports its usage windows in the headers of every answer, a refusal too: keep the latest numbers.
        await service.note_usage(connection_id, kind, upstream.headers, generation=token.generation)
        if upstream.status_code >= 400:
            raw = await upstream.aread()
            refusal = protocol.classify_error(kind, upstream.status_code, raw)
            retry = client_errors.retry_after(upstream.headers.get("retry-after"))
            if retry:
                headers["retry-after"] = retry
            await service.note_error(connection_id, refusal.code, status=refusal.status)
            return attempt(refusal_response(refusal, headers=headers),
                           advance=protocol.quota_rejection(kind, upstream.status_code, raw), safe_error_code=refusal.code)
        reply = protocol.open_reply(surface, receipt.requested_model, body)
        source = upstream.aiter_bytes()
        buffered: list[bytes] = []
        buffered_size = 0
        # Read only through the first complete event, never through a generation
        # to manufacture an opportunity to retry a committed request.
        while not reply.event_seen:
            try:
                chunk = await anext(source)
            except StopAsyncIteration:
                break
            buffered_size += len(chunk)
            if buffered_size > 1024 * 1024:
                return attempt(refusal_response(Refusal(502, "plan_stream_too_large", "The plan response could not be read safely."), headers=headers),
                               committed=True, safe_error_code="plan_stream_too_large")
            buffered.extend(reply.feed(chunk))
        if reply.quota_rejected(kind):
            refusal = reply.failure(kind, streaming=False)
            if reply.usage:
                receipt.usage = protocol.usage_facts(reply.usage)
            await service.note_error(connection_id, refusal.code, status=refusal.status)
            return attempt(refusal_response(refusal, headers=headers), advance=True, safe_error_code=refusal.code)

        def usage():
            if reply.has_response:
                observed = reply.result().get("model")
                if isinstance(observed, str):
                    receipt.metadata["observed_model"] = observed
            if reply.usage:
                receipt.usage = protocol.usage_facts(reply.usage)

        if body.get("stream"):
            async def stream():
                try:
                    for piece in buffered:
                        yield piece
                    if not reply.terminal:
                        async for chunk in source:
                            for piece in reply.feed(chunk):
                                yield piece
                            if reply.terminal and reply.translated:
                                break
                    usage()
                    if reply.successful:
                        recorder.report(status="succeeded", reason="completed")
                        await service.note_ok(connection_id)
                    else:
                        refusal = reply.failure(kind, streaming=True)
                        await service.note_error(connection_id, refusal.code, status=refusal.status)
                        if reply.translated:
                            yield reply.error_event(refusal)
                        recorder.report(status="failed", reason=refusal.code)
                    if reply.translated:
                        yield reply.done
                except httpx.HTTPError:
                    recorder.report(status="failed", reason="plan_stream_interrupted")
                    if reply.translated:
                        yield reply.error_event(Refusal(502, "plan_stream_interrupted", "The plan stream was interrupted."))
                        yield reply.done
                finally:
                    usage()
                    await close()
            handed_off = True
            return attempt(StreamingResponse(stream(), media_type="text/event-stream", headers=headers,
                                             background=BackgroundTask(close)), committed=reply.committed)
        if not reply.terminal:
            async for chunk in source:
                reply.feed(chunk)
        usage()
        if not reply.successful or not reply.has_response:
            refusal = reply.failure(kind, streaming=False)
            await service.note_error(connection_id, refusal.code, status=refusal.status)
            return attempt(refusal_response(refusal, headers=headers), committed=reply.committed, safe_error_code=refusal.code)
        recorder.report(status="succeeded", reason="completed")
        await service.note_ok(connection_id)
        return attempt(JSONResponse(reply.result(), headers=headers), committed=reply.committed)
    except SubscriptionError as error:
        # Only a locally unavailable credential, before transmission, permits advancing.
        await service.note_error(connection_id, error.code)
        safe = not sent and error.code in {"plan_not_connected", "plan_connection_invalid", "connection_not_available", "connection_changed"}
        return attempt(JSONResponse(error.body(), status_code=error.status, headers=headers),
                       advance=safe, safe_error_code=error.code)
    except httpx.TimeoutException:
        return attempt(refusal_response(Refusal(504, "plan_upstream_timeout", "The plan service timed out."), headers=headers),
                       safe_error_code="plan_upstream_timeout")
    except httpx.HTTPError:
        return attempt(refusal_response(Refusal(502, "plan_upstream_unavailable", "The plan service could not be reached."), headers=headers),
                       safe_error_code="plan_upstream_unavailable")
    finally:
        if not handed_off:
            await close()


class _PlanTurn:
    """This request's place in its conversation's turn, when the kind's row names a turn-state header."""

    def __init__(self, header: str, scope: str, turn_id: int, carried: bool, value: str | None) -> None:
        self.header, self.scope, self.turn_id, self.value = header, scope, turn_id, value
        # What the client and the receipt are told: never the value, only what was done with it.
        self.disclosure = "replayed" if value else "continued" if carried else "started"


def _open_turn(service, protocol, kind, connection_id, generation, receipt, payload) -> _PlanTurn | None:
    """Decide the request's turn. The conversation is the prompt cache key of one caller on one connection, so two
    people (or two apps) that send the same key never share a turn. The connection's generation changes when the plan
    is connected again, perhaps to another account, which then starts with no turn state (as Codex clears it when
    the signed-in account changes). The model is part of the conversation too: a value one model's turn gave is not
    sent to another model."""
    header = protocol.turn_state_header(kind)
    key = payload.get("prompt_cache_key")
    items = payload.get("input")
    if not header or not isinstance(key, str) or not key or not isinstance(items, list):
        return None
    scope = turn_state.scope_of(connection_id, generation, receipt.app_id, receipt.maker_person_id, receipt.owner_key_hash,
                                receipt.api_key_name, key, payload.get("model"))
    turn_id, carried, value = service.turn_states.outgoing(scope, items, protocol.continues_turn)
    return _PlanTurn(header, scope, turn_id, carried, value)


async def route_configured(service, connection_id):
    try:
        return await asyncio.to_thread(service.configured, connection_id)
    except SubscriptionError:
        return False
