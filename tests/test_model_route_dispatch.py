"""Synthetic route graphs execute once, preserve policy and settle one receipt."""
import asyncio
import copy
import itertools
import json
from functools import wraps
from dataclasses import replace

import httpx
import pytest
from starlette.responses import StreamingResponse

from gateway import model_routes, provider_execution, providers, receipt_store, registry_snapshot
from gateway.alias_policy import resolve_request_target
from gateway.backends.llm_execution import LiteLlmRuntimeConfig
from tests.support import access as acc
from tests.support.fake_upstream.route_dispatch import EventStream, event, request_for, paid_sender
from tests.support.subscription_fakes import KIND, Upstream, connect, make_service

COMPLETE = event(type="response.completed", response={"id": "resp-synthetic", "model": "gpt-test", "output": [],
                                                       "usage": {"input_tokens": 4, "output_tokens": 2}})
CREATED = event(type="response.created", response={"id": "resp-synthetic"})
QUOTA = event(type="response.failed", response={"error": {"code": "subscription_sharing_usage_limit_exceeded"}})


def graph():
    return {"plan-source": {"id": "plan-source", "kind": "provider", "enabled": True, "capabilities": ["openai"], "config": {
        "credential_source": {"kind": "connection", "connection_id": "chatgpt-owner"}, "request_protocol": KIND.protocol,
        "models": [{"id": "logical-test", "upstream_model": "gpt-test", "routes": [
            {"id": "chatgpt-plan/gpt-test", "service_id": "plan-source", "model_id": "logical-test"},
            {"id": "paid/gpt-test", "service_id": "cloud", "model_id": "paid-target"}]},
            {"id": "speed-test", "base_model_id": "logical-test", "upstream_model": "gpt-test"}]}},
        "cloud": {"id": "cloud", "kind": "provider", "enabled": True, "capabilities": ["openai"], "config": {
            "remote_provider": True, "provider_id": "cloud", "api_key_env": "ROUTE_TEST_KEY", "credential_origin": "https://example.test:443",
            "base_url": "https://example.test/v1", "protocol": "openai-compatible", "request_protocol": "litellm",
            "models": [{"id": "paid-target", "upstream_model": "wire-exact", "execution_only": True,
                        "reasoning_efforts": ["low", "high"]}],
            "price": {"input_usd_per_million_tokens": "1", "output_usd_per_million_tokens": "2"}}}}


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


@pytest.fixture
def harness(tmp_path, monkeypatch):
    from gateway import litellm_content, provider_reconciliation
    from types import SimpleNamespace
    monkeypatch.setenv("ROUTE_TEST_KEY", "synthetic-external-key")
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    asyncio.run(connect(service, jwks, upstream))
    receipts, begins = [], []
    monkeypatch.setattr(receipt_store, "begin", lambda row: begins.append(row.request_id))
    monkeypatch.setattr(receipt_store, "finalize", lambda row: receipts.append(row.to_dict()))
    monkeypatch.setattr(receipt_store, "update_phases", lambda row: None)
    monkeypatch.setattr(receipt_store, "touch", lambda row: None)
    async def no_probe(*args, **kwargs):
        return None
    monkeypatch.setattr(litellm_content, "execution_refusal", no_probe)
    monkeypatch.setattr(litellm_content, "wait_while_starting", no_probe)
    monkeypatch.setattr(provider_reconciliation, "reconcile_pending_generations", no_probe)
    paid_calls = []
    send_paid = paid_sender(paid_calls)
    monkeypatch.setattr(provider_execution, "send_llm_execution_request", send_paid)
    runtime = LiteLlmRuntimeConfig(enabled=True, base_url="http://127.0.0.1:18444", api_key="test-key", service_id="router")
    state = SimpleNamespace(service=service, upstream=upstream, services=graph(), receipts=receipts, begins=begins,
                            paid_calls=paid_calls, runtime=runtime, ledger=tmp_path / "spend.db", identity=None,
                            disconnect=None, admission=None, fresh=None, deadline=None)
    async def call(*, surface="responses", body=None):
        body = body or {"model": "logical-test", **({"input": [{"role": "user", "content": "hi"}]} if surface == "responses" else {"messages": [{"role": "user", "content": "hi"}]})}
        snapshot = registry_snapshot.build({"services": state.services}, registry_snapshot.BundledRelease({}), generation=1)
        target = resolve_request_target(body["model"], runtime_config={"backends": {sid: row["config"] for sid, row in state.services.items()}}, route_index=snapshot.model_routes)
        request = request_for(body, surface=surface, disconnected=state.disconnect)
        await request.body()
        default = acc.plan_for(connections=["chatgpt-owner"], paid=["cloud"], fallback=True)
        identity = state.identity or acc.identity(default)
        async def fresh():
            return await state.fresh() if state.fresh else identity
        async def admit(edge, receipt):
            return state.admission(edge) if state.admission else None
        async def prepare(edge, original):
            return original
        return await model_routes.dispatch_routes(snapshot=snapshot, target=target, request=request, body=body, surface=surface,
            identity=identity, fresh_identity=fresh, admission=admit, prepare_body=prepare, subscriptions=service,
            runtime=runtime, ledger_path=state.ledger, deadline=state.deadline)
    reserve = providers.reserve_provider_spend
    state.reservations = []
    def reserve_once(*args, **kwargs):
        reservation = reserve(*args, **kwargs)
        state.reservations.append(reservation.id)
        return reservation
    monkeypatch.setattr(providers, "reserve_provider_spend", reserve_once)
    state.call = call
    upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(200, stream=EventStream([CREATED, COMPLETE]), headers={"content-type": "text/event-stream"})
    return state


async def consume(response):
    if isinstance(response, StreamingResponse):
        return b"".join([piece async for piece in response.body_iterator])
    return response.body


@async_test
@pytest.mark.parametrize("surface,stream", list(itertools.product(["responses", "chat"], [True, False])))
async def test_plan_wire_and_one_receipt_no_dollar_reservation(harness, surface, stream, monkeypatch):
    def reserve(*a, **kw):
        raise AssertionError("plan must not reserve dollars")
    monkeypatch.setattr(providers, "reserve_provider_spend", reserve)
    body = {"model": "logical-test", "stream": stream, **({"input": [{"role": "user", "content": "hi"}]} if surface == "responses" else {"messages": [{"role": "user", "content": "hi"}]})}
    response = await harness.call(surface=surface, body=body)
    assert response.status_code == 200
    output = await consume(response)
    assert output
    calls = [json.loads(r.content) for r in harness.upstream.calls if r.url.path == "/v1/responses"]
    assert len(calls) == 1 and calls[0]["model"] == "gpt-test"
    assert calls[0]["store"] is False and calls[0]["stream"] is True
    assert harness.reservations == []
    assert response.headers["x-azizam-route"] == "chatgpt-plan/gpt-test"
    assert len(harness.begins) == len(harness.receipts) == 1
    assert harness.receipts[0]["cost"]["usd"] is None
    assert harness.receipts[0]["cost"]["source"] == "plan"


@async_test
@pytest.mark.parametrize("person_paid,app_paid,person_fallback,app_fallback", list(itertools.product([False, True], repeat=4)))
async def test_paid_fallback_matrix_and_settlement(harness, person_paid, app_paid, person_fallback, app_fallback):
    person = acc.plan_for("person", connections=["chatgpt-owner"], paid=["cloud"] if person_paid else [], fallback=person_fallback)
    app = acc.plan_for("app", connections=["chatgpt-owner"], paid=["cloud"] if app_paid else [], fallback=app_fallback)
    harness.identity = replace(acc.identity(person), access=acc.access(person, app))
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(429, json={"error": {"code": "subscription_sharing_usage_limit_exceeded"}}, headers={"retry-after": "9"})
    response = await harness.call()
    await consume(response)
    allowed = person_paid and app_paid and person_fallback and app_fallback
    assert response.status_code == (200 if allowed else 429)
    assert len(harness.paid_calls) == len(harness.reservations) == int(allowed)
    assert harness.upstream.count("/v1/responses") == 1
    assert len(harness.receipts) == 1
    if allowed:
        assert harness.paid_calls[0]["model"] == model_routes.RouteEdge("", "cloud", "paid-target").deployment_id
        assert harness.receipts[0]["resolved_model"] == "wire-exact"
        assert harness.receipts[0]["cost"]["reservation_id"]
        assert harness.receipts[0]["cost"]["reconciliation_status"] == "settled"


@async_test
@pytest.mark.parametrize("connections,fallback,served", [
    (["chatgpt-owner"], True, "paid/gpt-test"), (["chatgpt-owner"], False, None), ([], False, "paid/gpt-test")])
async def test_listed_chain_is_the_chain_dispatch_walks(harness, connections, fallback, served):
    from gateway.subscription_oauth import plain_name_chain
    snapshot = registry_snapshot.build({"services": harness.services}, registry_snapshot.BundledRelease({}), generation=1)
    listed = plain_name_chain(snapshot, "logical-test")["steps"]
    assert [(step["kind"], step["route_id"], step["paid_fallback"]) for step in listed] == [
        ("plan", "chatgpt-plan/gpt-test", False), ("paid", "paid/gpt-test", True)]
    harness.identity = acc.identity(acc.plan_for(connections=connections, paid=["cloud"], fallback=fallback))
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(
        429, json={"error": {"code": "subscription_sharing_usage_limit_exceeded"}})
    response = await harness.call()
    await consume(response)
    attempts = harness.receipts[0]["metadata"]["route_attempts"]
    walked = [step["route_id"] for step in listed if step["kind"] != "plan" or connections]
    assert [attempt["route_id"] for attempt in attempts] == walked
    if served:
        # The plan refused, or this person may not use it: the next listed step answered, and the header names it.
        assert response.status_code == 200 and response.headers["x-azizam-route"] == served == walked[-1]
        assert len(harness.paid_calls) == 1
    else:
        # The listed condition held: no paid fallback for this person, so the paid step refused before sending.
        assert response.status_code == 429 and not harness.paid_calls
        assert attempts[1]["safe_error_code"] == "route_paid_fallback_not_allowed"
    assert harness.upstream.count("/v1/responses") == int(bool(connections))


@async_test
@pytest.mark.parametrize("stream", [False, True])
async def test_explicit_failed_event_before_first_output_may_advance(harness, stream):
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(200, stream=EventStream([QUOTA]), headers={"content-type": "text/event-stream"})
    response = await harness.call(body={"model": "logical-test", "input": [{"role": "user", "content": "hi"}], "stream": stream})
    assert response.status_code == 200
    await consume(response)
    assert len(harness.paid_calls) == 1 and len(harness.receipts) == 1


@async_test
@pytest.mark.parametrize("events", [[CREATED, QUOTA], [COMPLETE, QUOTA]])
async def test_first_upstream_event_commits_even_nonstream(harness, events):
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(200, stream=EventStream(events), headers={"content-type": "text/event-stream"})
    response = await harness.call()
    await consume(response)
    assert len(harness.paid_calls) == 0
    assert harness.upstream.count("/v1/responses") == 1


@async_test
async def test_bare_429_is_not_quota_evidence(harness):
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(429, json={"error": {"code": "unclassified"}})
    response = await harness.call()
    assert response.status_code == 429 and not harness.paid_calls


@async_test
async def test_opaque_reasoning_stays_on_first_route(harness):
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(429, json={"error": {"code": "subscription_sharing_usage_limit_exceeded"}})
    response = await harness.call(body={"model": "logical-test", "input": [{"type": "reasoning", "encrypted_content": "opaque"}]})
    assert response.status_code == 400 and json.loads(response.body)["error"]["code"] == "route_state_incompatible"
    calls = [json.loads(r.content) for r in harness.upstream.calls if r.url.path == "/v1/responses"]
    assert len(calls) == 1 and calls[0]["input"][0]["encrypted_content"] == "opaque"
    assert not harness.paid_calls


@async_test
async def test_pin_never_falls_back(harness):
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(429, json={"error": {"code": "subscription_sharing_usage_limit_exceeded"}})
    response = await harness.call(body={"model": "chatgpt-plan/gpt-test", "input": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 429 and not harness.paid_calls


@async_test
async def test_capability_skip_and_effort_skip_are_presend(harness):
    harness.services["plan-source"]["capabilities"] = ["image"]
    response = await harness.call()
    assert response.status_code == 200 and harness.upstream.count("/v1/responses") == 0 and len(harness.paid_calls) == 1


@async_test
async def test_unsupported_effort_skips_without_acquiring(harness):
    response = await harness.call(body={"model": "logical-test", "input": [{"role": "user", "content": "hi"}], "reasoning": {"effort": "not-advertised"}})
    assert response.status_code == 400
    assert harness.upstream.count("/v1/responses") == 0 and not harness.paid_calls


@async_test
async def test_timeout_after_send_is_terminal(harness):
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic"))
    response = await harness.call()
    assert response.status_code == 504 and not harness.paid_calls
    assert harness.upstream.count("/v1/responses") == 1


@async_test
async def test_permission_revoked_after_selection_sends_nothing(harness):
    original = acc.plan_for(connections=["chatgpt-owner"], paid=["cloud"], fallback=True)
    revoked = acc.identity(acc.plan_for())
    harness.identity = acc.identity(original)
    async def fresh():
        return revoked
    harness.fresh = fresh
    response = await harness.call()
    assert response.status_code == 403 and harness.upstream.count("/v1/responses") == 0 and not harness.paid_calls


@async_test
async def test_one_deadline_includes_selection_and_attempts(harness):
    harness.deadline = asyncio.get_running_loop().time() + .03
    async def fresh():
        await asyncio.sleep(.1)
        return acc.identity(acc.plan_for(connections=["chatgpt-owner"], paid=["cloud"], fallback=True))
    harness.fresh = fresh
    response = await harness.call()
    assert response.status_code == 504 and not harness.paid_calls and harness.upstream.count("/v1/responses") == 0


@async_test
async def test_client_cancel_cancels_current_transport(harness):
    harness.disconnect = asyncio.Event()
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(200, stream=EventStream([CREATED, COMPLETE], delay=.2), headers={"content-type": "text/event-stream"})
    task = asyncio.create_task(harness.call())
    await asyncio.sleep(.03)
    harness.disconnect.set()
    response = await task
    assert response.status_code == 499 and not harness.paid_calls
    assert len(harness.receipts) == 1 and harness.receipts[0]["status"] == "cancelled"


def test_route_metadata_is_closed_and_credential_label_is_owner_only():
    from gateway.receipts import sanitize_admin_metadata, sanitize_route_metadata, serialize_receipt_for_audience
    metadata = {"logical_model": "logical-test", "route_id": "plan/exact", "request_protocol": KIND.protocol,
                "connection_id": "plan-owner", "token": "never-show", "route_attempts": [
                    {"route_id": "plan/exact", "outcome": "refused", "safe_error_code": "quota", "elapsed_ms": 2, "token": "never-show"}]}
    admin = sanitize_admin_metadata(metadata)
    assert "connection_id" not in admin and "never-show" not in json.dumps(admin)
    assert sanitize_route_metadata(metadata, owner=True)["route_connection_id"] == "plan-owner"
    assert "route_id" not in serialize_receipt_for_audience({"metadata": metadata}, "client")


@async_test
async def test_permission_revoked_after_acquire_is_terminal(harness, monkeypatch):
    original = harness.service.acquire
    revoked = False
    async def acquire(*args, **kwargs):
        nonlocal revoked
        credential = await original(*args, **kwargs)
        revoked = True
        return credential
    monkeypatch.setattr(harness.service, "acquire", acquire)
    async def fresh():
        return acc.identity(acc.plan_for() if revoked else acc.plan_for(connections=["chatgpt-owner"], paid=["cloud"], fallback=True))
    harness.fresh = fresh
    response = await harness.call()
    assert response.status_code == 403 and harness.upstream.count("/v1/responses") == 0 and not harness.paid_calls


@async_test
async def test_disconnected_plan_is_still_paid_escalation(harness):
    harness.services["plan-source"]["enabled"] = False
    harness.identity = acc.identity(acc.plan_for(connections=["chatgpt-owner"], paid=["cloud"], fallback=False))
    response = await harness.call()
    assert response.status_code == 503 and not harness.paid_calls
    assert harness.upstream.count("/v1/responses") == 0


@async_test
async def test_no_plan_grant_makes_first_authorized_paid_route_direct(harness):
    harness.identity = acc.identity(acc.plan_for(paid=["cloud"], fallback=False))
    response = await harness.call()
    assert response.status_code == 200 and len(harness.paid_calls) == 1
    assert harness.upstream.count("/v1/responses") == 0


@async_test
async def test_generation_invalidated_after_acquire_cannot_send_old_lease(harness, monkeypatch):
    acquire = harness.service.acquire
    async def disconnected(*args, **kwargs):
        credential = await acquire(*args, **kwargs)
        await harness.service.disconnect("chatgpt-owner", session=None)
        return credential
    monkeypatch.setattr(harness.service, "acquire", disconnected)
    response = await harness.call()
    assert response.status_code == 200 and harness.upstream.count("/v1/responses") == 0
    assert len(harness.paid_calls) == 1


@async_test
@pytest.mark.parametrize("requested,allowed", [("logical-test", False), ("speed-test", False), ("chatgpt-plan/gpt-test", True)])
async def test_pin_only_grants_do_not_admit_logical_or_alias_names(harness, requested, allowed):
    harness.identity = acc.identity(acc.plan_for(models=["chatgpt-plan/gpt-test"], connections=["chatgpt-owner"]))
    response = await harness.call(body={"model": requested, "input": [{"role": "user", "content": "hi"}]})
    assert response.status_code == (200 if allowed else 403)
    assert harness.upstream.count("/v1/responses") == int(allowed)


@async_test
async def test_alias_grant_uses_canonical_routes_but_not_canonical_name(harness):
    harness.identity = acc.identity(acc.plan_for(models=["speed-test"], connections=["chatgpt-owner"]))
    denied = await harness.call()
    assert denied.status_code == 403
    response = await harness.call(body={"model": "speed-test", "input": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200 and harness.upstream.count("/v1/responses") == 1


@async_test
async def test_policy_admission_refusal_is_not_fallback(harness):
    from starlette.responses import Response
    harness.admission = lambda edge: Response(status_code=503)
    response = await harness.call()
    assert response.status_code == 503 and not harness.paid_calls and harness.upstream.count("/v1/responses") == 0


@async_test
async def test_paid_attempt_uses_remaining_deadline_not_new_timeout(harness, monkeypatch):
    send = provider_execution.send_llm_execution_request
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(200, stream=EventStream([QUOTA], delay=.02), headers={"content-type": "text/event-stream"})
    async def slow_paid(execution, **kwargs):
        assert execution.read_timeout == 120.0
        await asyncio.sleep(1.0)
        return await send(execution, **kwargs)
    monkeypatch.setattr(provider_execution, "send_llm_execution_request", slow_paid)
    # The plan attempt must finish inside the deadline even on a loaded gate host (0.08 s did not: 1 in 3 under -n 4).
    harness.deadline = asyncio.get_running_loop().time() + .4
    response = await harness.call()
    assert response.status_code == 504 and harness.upstream.count("/v1/responses") == 1
    assert not harness.paid_calls and len(harness.receipts) == 1
    assert harness.receipts[0]["cost"]["reconciliation_status"] == "needs_reconciliation"


@async_test
@pytest.mark.parametrize("surface", ["responses", "chat"])
async def test_ingress_bridge_uses_one_captured_snapshot(harness, monkeypatch, surface, tmp_path):
    import proxy
    plan = acc.plan_for(connections=["chatgpt-owner"])
    identity = acc.identity(plan)
    context = proxy.AuthContext(identity=identity)
    monkeypatch.setattr(proxy, "SUBSCRIPTIONS", harness.service)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", tmp_path / "synthetic-admin.db")
    monkeypatch.setattr(proxy, "_litellm_runtime_config", lambda: harness.runtime)
    monkeypatch.setattr(proxy, "_auth_context_from_image_job_payload", lambda value: context)
    monkeypatch.setattr(proxy, "_dispatch_admission_response", lambda *a, **kw: None)
    monkeypatch.setattr(proxy, "_operator_service_availability_overrides", lambda: {})
    body = {"model": "logical-test", **({"input": [{"role": "user", "content": "hi"}]} if surface == "responses" else {"messages": [{"role": "user", "content": "hi"}]})}
    request = request_for(body, surface=surface)
    await request.body()
    request.state.registry_snapshot = registry_snapshot.build({"services": harness.services}, registry_snapshot.BundledRelease({}), generation=8)
    result = await proxy._model_route_response(request, context, body, surface=surface)
    assert result.status_code == 200
    assert result.headers["x-azizam-route"] == "chatgpt-plan/gpt-test"
    assert harness.upstream.count("/v1/responses") == 1
    assert len(harness.receipts) == 1


@pytest.mark.parametrize("property_name", ["file_id", "session_id"])
def test_review_tool_schema_property_is_not_route_state(property_name):
    body = {"tools": [{"type": "function", "function": {"name": "lookup", "parameters": {
        "type": "object", "properties": {property_name: {"type": "string"}}}}}]}
    assert model_routes.carries_route_state(body) is False


@async_test
async def test_review_paid_tool_schema_session_property_is_usable(harness):
    harness.identity = acc.identity(acc.plan_for(paid=["cloud"]))
    response = await harness.call(surface="chat", body={"model": "logical-test", "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {
            "type": "object", "properties": {"session_id": {"type": "string"}}}}}]})
    assert response.status_code == 200
    assert len(harness.paid_calls) == len(harness.reservations) == 1


def test_review_scoped_lookup_still_requires_a_legacy_local_host(monkeypatch):
    import proxy
    from types import SimpleNamespace
    monkeypatch.setattr(proxy, "DERIVED", SimpleNamespace(LLM_BACKEND_HOSTS={}))
    cfg = {"backends": {"cloud": {"enabled": True, "capabilities": ["openai"], "models": [
        {"id": "speed", "upstream_model": "paid-target"}]}}}
    assert proxy._configured_litellm_local_model_target("speed", service_id="cloud", runtime_config=cfg) is None


@async_test
async def test_review_ambiguous_plain_singletons_bypass_route_dispatch(harness):
    import proxy
    services = copy.deepcopy(harness.services)
    for sid in ("local-a", "local-b"):
        services[sid] = {"id": sid, "capabilities": ["openai"], "config": {"models": [
            {"id": "shared-plain", "upstream_model": "wire"}]}}
    body = {"model": "shared-plain", "input": [{"role": "user", "content": "hi"}]}
    request = request_for(body)
    request.state.registry_snapshot = registry_snapshot.build({"services": services}, registry_snapshot.BundledRelease({}), generation=1)
    result = await proxy._model_route_response(request, proxy.AuthContext(identity=acc.identity(acc.plan_for())), body, surface="responses")
    assert result is None
    assert not harness.paid_calls and harness.upstream.count("/v1/responses") == 0


@async_test
async def test_review_long_stream_outlives_start_deadline_without_idle_gap(harness):
    harness.deadline = asyncio.get_running_loop().time() + .5
    delta = event(type="response.output_text.delta", delta="still working")
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(
        200, stream=EventStream([CREATED, delta, delta, COMPLETE], delay=.2), headers={"content-type": "text/event-stream"})
    response = await harness.call(body={"model": "logical-test", "input": [{"role": "user", "content": "hi"}], "stream": True})
    output = await consume(response)
    assert COMPLETE in output
    assert harness.receipts[0]["status"] == "succeeded"
    assert harness.receipts[0]["metadata"]["terminal_reason"] == "completed"
    assert harness.upstream.count("/v1/responses") == 1 and not harness.paid_calls


@async_test
async def test_review_simultaneous_stream_start_and_disconnect_settles_reservation(harness, monkeypatch):
    import sqlite3
    from gateway.backends import llm_execution
    from tests.support.fake_upstream.route_dispatch import ReadyDisconnectWatcher
    ready = asyncio.Event()
    watcher = ReadyDisconnectWatcher(ready)
    monkeypatch.setattr(llm_execution, "RequestDisconnectWatcher", lambda receive: watcher)
    send = provider_execution.send_llm_execution_request
    transports = []
    async def completed_start(execution, **kwargs):
        client, upstream = await send(execution, **kwargs)
        transports.append((client, upstream))
        ready.set()
        return client, upstream
    monkeypatch.setattr(provider_execution, "send_llm_execution_request", completed_start)
    harness.identity = acc.identity(acc.plan_for(paid=["cloud"]))
    response = await harness.call(surface="chat", body={"model": "logical-test", "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert response.status_code == 499
    assert len(harness.reservations) == len(harness.paid_calls) == 1
    with sqlite3.connect(harness.ledger) as db:
        assert db.execute("SELECT status FROM provider_spend").fetchall() == [("needs_reconciliation",)]
    assert all(client.is_closed and upstream.is_closed for client, upstream in transports)
    assert len(harness.receipts) == 1 and harness.receipts[0]["status"] == "cancelled"


@async_test
@pytest.mark.parametrize("outcome", ["idle_timeout", "cancelled"])
async def test_review_nonstream_paid_receipt_keeps_executor_outcome(harness, monkeypatch, outcome):
    from gateway.backends.llm_execution import ClientDisconnectedBeforeHeaders
    async def failed_start(*args, **kwargs):
        if outcome == "idle_timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        raise ClientDisconnectedBeforeHeaders()
    monkeypatch.setattr(provider_execution, "send_llm_execution_request", failed_start)
    harness.identity = acc.identity(acc.plan_for(paid=["cloud"]))
    response = await harness.call(surface="chat", body={"model": "logical-test", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == (504 if outcome == "idle_timeout" else 499)
    receipt = harness.receipts[0]
    assert receipt["metadata"]["terminal_reason"] == outcome
    assert receipt["status"] == ("failed" if outcome == "idle_timeout" else "cancelled")
    assert receipt["cost"]["reconciliation_status"] == "needs_reconciliation"


@async_test
async def test_review_paid_grant_uses_canonical_provider_identity(harness):
    harness.services["cloud"]["config"]["provider_id"] = "CLOUD"
    harness.identity = acc.identity(acc.plan_for(paid=["cloud"]))
    response = await harness.call()
    assert response.status_code == 200
    assert len(harness.paid_calls) == len(harness.reservations) == 1


@pytest.mark.parametrize("body", [
    {"previous_response_id": "resp-origin"},
    {"session_id": "cli-origin"},
    {"conversation": {"id": "conversation-origin"}},
    {"input": [{"type": "reasoning", "encrypted_content": "opaque"}]},
    {"input": [{"type": "item_reference", "id": "item-origin"}]},
    {"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "file-origin"}]}]},
    {"messages": [{"role": "user", "content": [{"type": "file", "file": {"file_id": "file-origin"}}]}]},
])
def test_review_known_state_positions_remain_bound(body):
    assert model_routes.carries_route_state(body) is True


def test_review_explicit_local_edge_can_resolve_captured_unlisted_service(monkeypatch):
    import proxy
    from types import SimpleNamespace
    monkeypatch.setattr(proxy, "DERIVED", SimpleNamespace(LLM_BACKEND_HOSTS={}))
    cfg = {"backends": {"captured-local": {"models": [{"id": "speed", "upstream_model": "base", "activation_model": "base"}]}}}
    target = proxy._configured_litellm_local_model_target("speed", service_id="captured-local", runtime_config=cfg, route_edge=True)
    assert target.backend == "captured-local" and target.model == "base"


@async_test
async def test_review_group_resolution_error_stays_fail_closed(harness, monkeypatch):
    import proxy
    from gateway.alias_policy import AliasPolicyResolutionError
    def unavailable(*args, **kwargs):
        raise AliasPolicyResolutionError("synthetic group ambiguity")
    monkeypatch.setattr(proxy, "resolve_public_target", unavailable)
    body = {"model": "logical-test", "input": [{"role": "user", "content": "hi"}]}
    request = request_for(body)
    request.state.registry_snapshot = registry_snapshot.build({"services": harness.services}, registry_snapshot.BundledRelease({}), generation=1)
    result = await proxy._model_route_response(request, proxy.AuthContext(identity=acc.identity(acc.plan_for())), body, surface="responses")
    assert result.status_code == 503
    assert json.loads(result.body)["error"]["code"] == "alias_policy_unavailable"


@async_test
async def test_review_paid_stream_transport_keeps_legacy_read_timeout(harness, monkeypatch):
    send = provider_execution.send_llm_execution_request
    async def start(execution, **kwargs):
        assert execution.read_timeout == 120.0
        return await send(execution, **kwargs)
    monkeypatch.setattr(provider_execution, "send_llm_execution_request", start)
    harness.deadline = asyncio.get_running_loop().time() + 1
    harness.identity = acc.identity(acc.plan_for(paid=["cloud"]))
    response = await harness.call(surface="chat", body={"model": "logical-test", "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert response.status_code == 200
    await consume(response)
    assert harness.receipts[0]["status"] == "succeeded"


@async_test
async def test_review_stream_read_gap_uses_protocol_idle_timeout(harness):
    harness.service.kinds[KIND.id] = replace(KIND, upstream={**KIND.upstream, "read_timeout_seconds": .04})
    harness.deadline = asyncio.get_running_loop().time() + 2
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(
        200, stream=EventStream([CREATED, COMPLETE], delay=.1), headers={"content-type": "text/event-stream"})
    response = await harness.call(body={"model": "logical-test", "input": [{"role": "user", "content": "hi"}], "stream": True})
    output = await consume(response)
    assert COMPLETE not in output
    assert harness.receipts[0]["status"] == "failed"
    assert harness.receipts[0]["metadata"]["terminal_reason"] == "idle_timeout"
    assert not harness.paid_calls


def paid_first(services):
    record = services["plan-source"]["config"]["models"][0]
    record["routes"] = list(reversed(record["routes"]))
    return services


def test_a_paid_first_group_lists_paid_then_plan():
    from gateway.subscription_oauth import plain_name_chain
    snapshot = registry_snapshot.build({"services": paid_first(graph())}, registry_snapshot.BundledRelease({}), generation=1)
    steps = plain_name_chain(snapshot, "logical-test")["steps"]
    assert [(step["kind"], step["route_id"], step["paid_fallback"]) for step in steps] == [
        ("paid", "paid/gpt-test", False), ("plan", "chatgpt-plan/gpt-test", False)]


@async_test
async def test_a_paid_first_group_sends_paid_directly_without_the_fallback_flag(harness):
    paid_first(harness.services)
    harness.identity = acc.identity(acc.plan_for(connections=["chatgpt-owner"], paid=["cloud"], fallback=False))
    response = await harness.call()
    await consume(response)
    assert response.status_code == 200 and response.headers["x-azizam-route"] == "paid/gpt-test"
    assert len(harness.paid_calls) == 1 and harness.upstream.count("/v1/responses") == 0
    assert [a["route_id"] for a in harness.receipts[0]["metadata"]["route_attempts"]] == ["paid/gpt-test"]


@async_test
async def test_a_paid_first_group_serves_the_plan_to_a_person_without_paid_access(harness):
    paid_first(harness.services)
    harness.identity = acc.identity(acc.plan_for(connections=["chatgpt-owner"], paid=[], fallback=False))
    response = await harness.call()
    await consume(response)
    assert response.status_code == 200 and response.headers["x-azizam-route"] == "chatgpt-plan/gpt-test"
    assert not harness.paid_calls and harness.upstream.count("/v1/responses") == 1


@async_test
async def test_a_paid_first_pin_still_selects_only_its_own_route(harness):
    paid_first(harness.services)
    response = await harness.call(body={"model": "chatgpt-plan/gpt-test", "input": [{"role": "user", "content": "hi"}]})
    await consume(response)
    assert response.status_code == 200 and response.headers["x-azizam-route"] == "chatgpt-plan/gpt-test"
    assert not harness.paid_calls


# A2: each sent attempt names the plan connection it ran on and keeps its own usage;
# the request has one receipt and the winner's usage is never counted twice.
QUOTA_WITH_USAGE = event(type="response.failed", response={"error": {"code": "subscription_sharing_usage_limit_exceeded"},
                                                           "usage": {"input_tokens": 9, "output_tokens": 0}})
COMPLETE_NO_USAGE = event(type="response.completed", response={"id": "resp-synthetic", "model": "gpt-test", "output": []})
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")


def attempt_sum(receipt):
    totals = dict.fromkeys(USAGE_KEYS, 0)
    for attempt in receipt["metadata"]["route_attempts"]:
        for key, value in (attempt.get("usage") or {}).items():
            totals[key] += value
    return {key: value for key, value in totals.items() if value}


def stream_route(events):
    return lambda req: httpx.Response(200, stream=EventStream(events), headers={"content-type": "text/event-stream"})


@async_test
@pytest.mark.parametrize("stream", [False, True])
async def test_a_plan_refusal_with_usage_then_paid_success_attributes_each_attempt(harness, stream):
    harness.upstream.routes[("POST", "/v1/responses")] = stream_route([QUOTA_WITH_USAGE])
    response = await harness.call(body={"model": "logical-test", "input": [{"role": "user", "content": "hi"}], "stream": stream})
    await consume(response)
    assert response.headers["x-azizam-route"] == "paid/gpt-test"
    assert len(harness.receipts) == 1
    receipt = harness.receipts[0]
    plan, paid = receipt["metadata"]["route_attempts"]
    assert (plan["route_id"], plan["outcome"], plan["connection_id"]) == ("chatgpt-plan/gpt-test", "sent", "chatgpt-owner")
    assert plan["usage"] == {"input_tokens": 9, "output_tokens": 0}
    assert paid["route_id"] == "paid/gpt-test" and "connection_id" not in paid
    assert paid["usage"] == receipt["usage"] == {"input_tokens": 4, "output_tokens": 2}
    assert "connection_id" not in receipt["metadata"]
    assert attempt_sum(receipt) == {"input_tokens": 13, "output_tokens": 2}


@async_test
@pytest.mark.parametrize("stream", [False, True])
async def test_a_plan_success_counts_its_usage_once_after_the_stream_closes(harness, stream):
    response = await harness.call(body={"model": "logical-test", "input": [{"role": "user", "content": "hi"}], "stream": stream})
    await consume(response)
    assert len(harness.receipts) == 1
    receipt = harness.receipts[0]
    (plan,) = receipt["metadata"]["route_attempts"]
    assert (plan["outcome"], plan["connection_id"]) == ("committed", "chatgpt-owner")
    assert plan["usage"] == receipt["usage"] == {"input_tokens": 4, "output_tokens": 2}
    assert attempt_sum(receipt) == {"input_tokens": 4, "output_tokens": 2}


@async_test
async def test_a_plan_without_reported_usage_names_its_connection_and_no_counts(harness):
    harness.upstream.routes[("POST", "/v1/responses")] = stream_route([CREATED, COMPLETE_NO_USAGE])
    response = await harness.call()
    await consume(response)
    (plan,) = harness.receipts[0]["metadata"]["route_attempts"]
    assert plan["connection_id"] == "chatgpt-owner" and "usage" not in plan
    assert not harness.receipts[0].get("usage")


@async_test
async def test_a_skipped_plan_names_no_connection_and_no_usage(harness, monkeypatch):
    from gateway import subscription_execution
    async def not_configured(*args, **kwargs):
        return False
    monkeypatch.setattr(subscription_execution, "route_configured", not_configured)
    response = await harness.call()
    await consume(response)
    plan, paid = harness.receipts[0]["metadata"]["route_attempts"]
    assert plan["outcome"] == "skipped" and "connection_id" not in plan and "usage" not in plan
    assert paid["outcome"] == "committed" and harness.upstream.count("/v1/responses") == 0


@async_test
async def test_a_pinned_plan_attempt_names_its_connection(harness):
    response = await harness.call(body={"model": "chatgpt-plan/gpt-test", "input": [{"role": "user", "content": "hi"}]})
    await consume(response)
    (plan,) = harness.receipts[0]["metadata"]["route_attempts"]
    assert (plan["route_id"], plan["connection_id"]) == ("chatgpt-plan/gpt-test", "chatgpt-owner")
    assert plan["usage"] == {"input_tokens": 4, "output_tokens": 2}


@async_test
async def test_a_cancelled_sent_plan_attempt_names_its_connection(harness):
    harness.disconnect = asyncio.Event()
    harness.upstream.routes[("POST", "/v1/responses")] = lambda req: httpx.Response(200, stream=EventStream([CREATED, COMPLETE], delay=.2), headers={"content-type": "text/event-stream"})
    task = asyncio.create_task(harness.call())
    await asyncio.sleep(.03)
    harness.disconnect.set()
    response = await task
    assert response.status_code == 499 and len(harness.receipts) == 1
    (plan,) = harness.receipts[0]["metadata"]["route_attempts"]
    assert plan["outcome"] == "cancelled" and plan["connection_id"] == "chatgpt-owner"


def two_plans(services):
    second = copy.deepcopy(services["plan-source"])
    second["id"] = "plan-second"
    second["config"]["credential_source"] = {"kind": "connection", "connection_id": "second-owner"}
    second["config"]["models"] = [{"id": "logical-second", "upstream_model": "gpt-test"}]
    services["plan-second"] = second
    services["plan-source"]["config"]["models"][0]["routes"].insert(
        1, {"id": "second-plan/gpt-test", "service_id": "plan-second", "model_id": "logical-second"})
    return services


@async_test
async def test_two_plan_connections_each_keep_their_own_attempt_usage(harness, monkeypatch):
    from starlette.responses import JSONResponse
    from gateway import subscription_execution
    from gateway.provider_protocols import AttemptResult
    two_plans(harness.services)
    harness.identity = acc.identity(acc.plan_for(connections=["chatgpt-owner", "second-owner"], paid=["cloud"], fallback=True))
    sent = []
    async def configured(*args, **kwargs):
        return True
    async def prepare(service, connection_id, protocol_id, slug, *, surface, body):
        return KIND, dict(body), {}
    async def execute(service, connection_id, protocol_id, slug, *, surface, body, prepared, recorder, before_send):
        await before_send()
        recorder.mark_phase("started_at_ms")
        sent.append(connection_id)
        if connection_id == "chatgpt-owner":
            recorder.receipt.usage = {"input_tokens": 9, "output_tokens": 0}
            return AttemptResult(JSONResponse({"error": {"code": "quota"}}, status_code=429), sent=True, advance=True,
                                 safe_error_code="plan_quota_exhausted")
        recorder.receipt.usage = {"input_tokens": 5, "output_tokens": 3}
        recorder.report(status="succeeded", reason="completed")
        return AttemptResult(JSONResponse({"id": "resp-second", "output": []}), sent=True, committed=True)
    monkeypatch.setattr(subscription_execution, "route_configured", configured)
    monkeypatch.setattr(subscription_execution, "prepare_route_request", prepare)
    monkeypatch.setattr(subscription_execution, "execute_route_attempt", execute)
    response = await harness.call()
    await consume(response)
    assert sent == ["chatgpt-owner", "second-owner"] and not harness.paid_calls
    assert len(harness.receipts) == 1
    receipt = harness.receipts[0]
    first, second = receipt["metadata"]["route_attempts"]
    assert (first["connection_id"], first["usage"]) == ("chatgpt-owner", {"input_tokens": 9, "output_tokens": 0})
    assert (second["connection_id"], second["usage"]) == ("second-owner", {"input_tokens": 5, "output_tokens": 3})
    assert receipt["metadata"]["connection_id"] == "second-owner"
    assert receipt["usage"] == {"input_tokens": 5, "output_tokens": 3}
    assert attempt_sum(receipt) == {"input_tokens": 14, "output_tokens": 3}
