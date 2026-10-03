"""Gateway-owned admission and receipts for paid LiteLLM execution.

Local calls continue through their existing executor. Remote calls enter here
with an explicit registry model, a gateway identity and a budget reservation.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from gateway import providers
from gateway.backends.llm_execution import (
    LiteLlmRuntimeConfig, LlmExecutionRequest, litellm_headers, litellm_url,
    send_llm_execution_request, RequestDisconnectWatcher,
    ClientDisconnectedBeforeHeaders,
)
from gateway.policy import AppProfile, PolicyRequest, evaluate
from gateway.receipts import ReceiptLifecycleOwner, RouteReceipt
from gateway.litellm_substrate import litellm_provider_model_lookup


def provider_for_model(registry: Mapping[str, Any], model: Any) -> providers.ProviderRef | None:
    if not isinstance(model, str):
        return None
    facts = litellm_provider_model_lookup(registry).get(model)
    if facts and facts["remote"]:
        return providers.provider_lookup(registry, facts["service_id"])
    provider_id, separator, _ = model.partition("/")
    if not separator:
        return None
    return providers.provider_refs(registry).get(provider_id)


def error_response(error: providers.ProviderError) -> JSONResponse:
    headers = {"retry-after": error.retry_after} if error.retry_after else {}
    return JSONResponse(error.error_body(), status_code=error.status_code, headers=headers)


def _model_declared(provider: providers.ProviderRef, model: str) -> bool:
    if not set(provider.protocols) & {"openai", "openai-chat", "openai-compatible", "anthropic-messages"}:
        return False
    registry = {"services": {provider.id: {
        "id": provider.id, "kind": "provider", "capabilities": ["openai"],
        "config": {**provider.config, "provider_id": provider.id,
                   "provider_type": "paid-remote", "api_key_env": provider.api_key_env},
    }}}
    return model in litellm_provider_model_lookup(registry)


async def execute_paid_litellm(
    request: Request, *, request_path: str, body: Mapping[str, Any],
    provider: providers.ProviderRef, profile: AppProfile,
    runtime: LiteLlmRuntimeConfig, ledger_path: Path,
    admin_db_path: Path | None = None,
    app_id: str | None = None, app_name: str | None = None,
    delegated: bool = False, paid_fallback: bool = False,
    requested_model: str | None = None,
) -> Response:
    model = str(body.get("model") or "")
    protocol, intent = (
        ("openai.embeddings", "text.embeddings") if request_path == "/v1/embeddings"
        else ("openai.chat", "chat.generate")
    )
    receipt = RouteReceipt(
        protocol=protocol, intent=intent, path=request_path, backend=provider.id,
        app_profile=profile.id, app_id=app_id, api_key_name=app_name,
        requested_model=requested_model or model, resolved_model=model,
        stream=bool(body.get("stream")), visibility=profile.status_visibility.level,
    )
    providers.attach_cost_to_receipt(receipt, providers.Cost(None, "unknown", provider.id))
    if paid_fallback:
        receipt.add_fallback_reason("explicit alias selected a paid fallback")
    async with ReceiptLifecycleOwner(receipt) as owner:
        try:
            if delegated:
                raise providers.RemoteProviderNotAllowed("Paid providers require a gateway App key and access plan")
            if not _model_declared(provider, model):
                raise providers.RemoteProviderNotAllowed("Remote model is not declared in the provider model allow-list")
            decision = evaluate(profile, PolicyRequest(protocol=protocol, intent=intent, model=model))
            if decision.denied:
                raise providers.RemoteProviderNotAllowed("; ".join(decision.reasons))
            if not runtime.enabled:
                raise providers.ProviderError("LiteLLM execution substrate is unavailable")
            estimate = providers.estimate_request_cost(provider, body)
            reservation = providers.reserve_provider_spend(
                profile, provider, estimate, path=ledger_path,
                admin_db_path=admin_db_path, paid_fallback=paid_fallback,
            )
        except providers.ProviderError as error:
            receipt.set_policy_decision("denied", reason=error.code, source="gateway:remote-provider")
            owner.report(status="failed", reason="failed")
            return error_response(error)
        receipt.set_policy_decision("allowed", source="gateway:remote-provider")
        # A missing final cost retains the reservation. Neither disconnect nor
        # transport failure proves that the provider did not bill this request.
        client = None
        response = None
        watcher = RequestDisconnectWatcher(request.receive)
        payload = dict(body)
        if receipt.stream:
            payload["stream_options"] = {**dict(payload.get("stream_options") or {}), "include_usage": True}
        execution = LlmExecutionRequest(
            method=request.method, url=litellm_url(base_url=runtime.base_url, path=request_path),
            headers=litellm_headers({}, api_key=runtime.api_key, gateway_metadata={"request-id": receipt.request_id}),
            body=json.dumps(payload).encode(), read_timeout=120.0,
        )
        try:
            client, response = await send_llm_execution_request(execution, disconnect_watcher=watcher)
        except ClientDisconnectedBeforeHeaders:
            owner.report(status="cancelled", reason="cancelled")
            return Response(status_code=499)
        except httpx.TimeoutException:
            owner.report(status="failed", reason="idle_timeout")
            return error_response(providers.ProviderTimeout("Remote provider request timed out"))
        except httpx.HTTPError:
            owner.report(status="failed", reason="connect_error")
            return JSONResponse({"error": {"code": "provider_unavailable", "message": "Remote execution is unavailable"}}, status_code=502)
        finally:
            await watcher.aclose()
        cost = providers.extract_provider_cost(provider, headers=response.headers)
        headers = {"x-gateway-request-id": receipt.request_id, "x-gateway-remote": "1"}
        if response.headers.get("retry-after"):
            headers["retry-after"] = response.headers["retry-after"]
        if response.status_code >= 400:
            await response.aclose()
            await client.aclose()
            # Do not reflect vendor exception text, URLs or auth headers.
            if cost.usd is not None:
                providers.settle_provider_spend(reservation, cost, path=ledger_path)
            providers.attach_cost_to_receipt(receipt, cost)
            owner.report(status="failed", reason="failed")
            if response.status_code in {401, 403}:
                return error_response(providers.ProviderAuthFailed("Remote provider rejected the operator key"))
            if response.status_code == 429:
                return error_response(providers.ProviderRateLimited("Remote provider rate limited the request", retry_after=headers.get("retry-after")))
            return JSONResponse({"error": {"code": "provider_error", "message": "Remote provider request failed"}}, status_code=response.status_code, headers=headers)

        def finish(body: Mapping[str, Any]) -> None:
            final_cost = providers.extract_provider_cost(provider, body={**body, "model": model}, headers=response.headers)
            providers.attach_cost_to_receipt(receipt, final_cost, providers.extract_usage(body))
            providers.settle_provider_spend(reservation, final_cost, path=ledger_path)

        if "text/event-stream" in response.headers.get("content-type", ""):
            async def stream():
                pending = b""
                final_body: Mapping[str, Any] = {}
                try:
                    async for chunk in response.aiter_bytes():
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            if line.startswith(b"data:"):
                                try:
                                    event = json.loads(line[5:])
                                except (ValueError, UnicodeError):
                                    continue
                                if isinstance(event, Mapping) and isinstance(event.get("usage"), Mapping):
                                    final_body = event
                        if len(pending) > 2 * 1024 * 1024:
                            pending = b""
                        yield chunk
                    finish(final_body)
                    owner.report(status="succeeded", reason="completed")
                finally:
                    await response.aclose()
                    await client.aclose()
            return StreamingResponse(owner.handoff_stream(stream()), media_type="text/event-stream", headers=headers)
        try:
            content = await response.aread()
            try:
                result = json.loads(content)
            except (ValueError, UnicodeError):
                result = {}
            finish(result if isinstance(result, Mapping) else {})
            headers["x-gateway-cost-usd"] = str(receipt.cost["usd"] or "unknown")
            owner.report(status="succeeded", reason="completed")
            return Response(content, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"), headers=headers)
        except httpx.TimeoutException:
            owner.report(status="failed", reason="idle_timeout")
            return error_response(providers.ProviderTimeout("Remote provider request timed out"))
        finally:
            await response.aclose()
            await client.aclose()
