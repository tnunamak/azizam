"""Gateway-owned admission and receipts for paid LiteLLM execution.

Local calls continue through their existing executor. Remote calls enter here
with an explicit registry model, a gateway identity and a budget reservation.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from gateway import providers
from gateway.backends.llm_execution import (
    ClientDisconnectedBeforeHeaders, LlmExecutionPredispatchError,
    LiteLlmRuntimeConfig, LlmExecutionRequest, RequestDisconnectWatcher,
    litellm_headers, litellm_url, read_llm_response_body,
    sanitize_litellm_execution_request,
    send_llm_execution_request,
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
    return providers.provider_refs(registry).get(providers.canonical_provider_id(provider_id))


def error_response(error: providers.ProviderError) -> JSONResponse:
    headers = {"retry-after": error.retry_after} if error.retry_after else {}
    return JSONResponse(error.error_body(), status_code=error.status_code, headers=headers)


def invalid_request_response(message: str, *, param: str | None = None) -> JSONResponse:
    error: dict[str, Any] = {
        "code": "invalid_request",
        "message": message,
    }
    if param:
        error["param"] = param
    return JSONResponse({"error": error}, status_code=400)


def paid_option_unsupported_response(param: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": "paid_option_unsupported",
                "message": (
                    f"{param} is not supported for paid remote providers because "
                    "the gateway cannot bound its provider cost."
                ),
                "param": param,
            }
        },
        status_code=400,
    )


PAID_PRICED_REQUEST_FIELDS = frozenset(
    {
        "best_of",
        "frequency_penalty",
        "function_call",
        "functions",
        "input",
        "logit_bias",
        "logprobs",
        "max_tokens",
        "messages",
        "model",
        "n",
        "parallel_tool_calls",
        "presence_penalty",
        "prompt",
        "response_format",
        "seed",
        "stop",
        "stream",
        "stream_options",
        "suffix",
        "temperature",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "user",
    }
)

_PAID_OUTPUT_LIMIT_FIELDS = frozenset(
    {"max_tokens", "max_completion_tokens", "max_output_tokens"}
)

_PAID_CALLER_ROUTING_FIELDS = frozenset(
    {
        "context_window_fallback_dict",
        "default_fallbacks",
        "fallbacks",
        "litellm_metadata",
        "metadata",
        "model_group_retry_policy",
        "num_retries",
        "max_retries",
        "router_settings",
        "router_settings_override",
    }
)
_STREAM_ASSEMBLY_MAX_BYTES = 8 * 1024 * 1024
_BILLING_WORD_RE = re.compile(r"(?:^|[_:/.-])(search|online|realtime)(?=$|[_:/.-])")


def _prepare_paid_litellm_payload(
    body: Mapping[str, Any],
    *,
    stream: bool,
    provider: providers.ProviderRef,
    resolved_upstream_model: str | None = None,
) -> tuple[dict[str, Any], bytes] | JSONResponse:
    prepared = _paid_priced_outbound_payload(
        body,
        provider=provider,
        resolved_upstream_model=resolved_upstream_model,
    )
    if isinstance(prepared, JSONResponse):
        return prepared
    payload = prepared
    payload["num_retries"] = 0
    payload["max_retries"] = 0
    if stream:
        raw_stream_options = payload.get("stream_options")
        if raw_stream_options is None:
            stream_options: dict[str, Any] = {}
        elif isinstance(raw_stream_options, Mapping):
            stream_options = dict(raw_stream_options)
        else:
            return invalid_request_response(
                "stream_options must be an object when provided.",
                param="stream_options",
            )
        payload["stream_options"] = {**stream_options, "include_usage": True}
    try:
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return invalid_request_response("request body is not JSON serializable")
    return payload, payload_bytes


def _paid_priced_outbound_payload(
    body: Mapping[str, Any],
    *,
    provider: providers.ProviderRef,
    resolved_upstream_model: str | None = None,
) -> dict[str, Any] | JSONResponse:
    payload: dict[str, Any] = {}
    output_limits: list[int] = []
    for key, value in body.items():
        if key in _PAID_CALLER_ROUTING_FIELDS:
            continue
        if key in _PAID_OUTPUT_LIMIT_FIELDS:
            if key == "max_output_tokens":
                return paid_option_unsupported_response(key)
            limit = _positive_int(value)
            if limit is None:
                return invalid_request_response(
                    f"{key} must be a positive integer when provided.",
                    param=key,
                )
            output_limits.append(limit)
            continue
        if key not in PAID_PRICED_REQUEST_FIELDS:
            return paid_option_unsupported_response(key)
        payload[key] = value

    if output_limits:
        # Paid chat/completion execution uses one outbound limit field. When a
        # client sends multiple aliases, reserve and dispatch the conservative
        # maximum so the estimator and provider see the same bound.
        payload["max_tokens"] = max(output_limits)

    unsupported_tool = _unsupported_paid_tool(payload.get("tools"))
    if unsupported_tool is not None:
        return paid_option_unsupported_response(unsupported_tool)

    pricing_model = resolved_upstream_model or payload.get("model")

    unsupported_modifier = _unsupported_model_modifier(pricing_model, provider)
    if unsupported_modifier is not None:
        return paid_option_unsupported_response(unsupported_modifier)

    unsupported_billing_name = _unsupported_billing_name(pricing_model, provider)
    if unsupported_billing_name is not None:
        return paid_option_unsupported_response(unsupported_billing_name)

    unsupported_nested_dimension = _unsupported_nested_paid_dimension(
        payload,
        provider,
        model=pricing_model,
    )
    if unsupported_nested_dimension is not None:
        return paid_option_unsupported_response(unsupported_nested_dimension)

    return payload



def _provider_price_dimensions(provider: providers.ProviderRef, model: object = None) -> set[str]:
    """Return operator-declared priced dimensions for paid request guards."""

    dimensions: set[str] = set()
    _collect_price_dimensions(provider.price.get("dimensions"), dimensions)
    if isinstance(model, str):
        models = provider.price.get("models")
        if isinstance(models, Mapping):
            override = models.get(model)
            if isinstance(override, Mapping):
                _collect_price_dimensions(override.get("dimensions"), dimensions)
    return dimensions


def _collect_price_dimensions(value: object, out: set[str]) -> None:
    if not isinstance(value, Mapping):
        return
    for key, declaration in value.items():
        if declaration is True:
            out.add(_normalize_dimension_name(str(key)))
            continue
        if isinstance(declaration, Mapping) and declaration.get("enabled") is True:
            out.add(_normalize_dimension_name(str(key)))


def _normalize_dimension_name(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _dimension_declared(dimensions: set[str], *names: str) -> bool:
    normalized = {_normalize_dimension_name(name) for name in names}
    return bool(dimensions & normalized)


def _dimension_declared_for_word(dimensions: set[str], word: str) -> bool:
    aliases = [
        word,
        f"model_modifier:{word}",
        f"model_modifier_{word}",
        f"model_suffix:{word}",
        f"model_suffix_{word}",
    ]
    return _dimension_declared(dimensions, *aliases)


def _price_for_public_model(provider: providers.ProviderRef, model: object) -> Mapping[str, Any]:
    if not isinstance(model, str):
        return provider.price
    models = provider.price.get("models")
    if isinstance(models, Mapping):
        override = models.get(model)
        if isinstance(override, Mapping):
            merged = dict(provider.price)
            merged.update(override)
            return merged
    return provider.price


def _billing_words(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    normalized = value.strip().lower().replace("-", "_")
    return {match.group(1) for match in _BILLING_WORD_RE.finditer(normalized)}


def _price_key_candidates(value: object) -> list[str]:
    keys: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"dimensions", "models"}:
                continue
            keys.append(str(key))
            keys.extend(_price_key_candidates(child))
    elif isinstance(value, (list, tuple)):
        for item in value:
            keys.extend(_price_key_candidates(item))
    return keys


def _unsupported_billing_name(model: object, provider: providers.ProviderRef) -> str | None:
    dimensions = _provider_price_dimensions(provider, model)
    current_price = _price_for_public_model(provider, model)
    candidates: list[object] = [
        model,
        current_price.get("cost_model"),
        provider.price.get("cost_model"),
        provider.config.get("cost_model"),
        provider.config.get("litellm_model"),
    ]
    candidates.extend(_price_key_candidates(current_price))
    for word in {word for candidate in candidates for word in _billing_words(candidate)}:
        if not _dimension_declared_for_word(dimensions, word):
            return word
    return None


def _unsupported_model_modifier(model: object, provider: providers.ProviderRef) -> str | None:
    if not isinstance(model, str) or ":" not in model:
        return None
    leaf = model.rsplit("/", 1)[-1]
    parts = [part for part in leaf.split(":")[1:] if part]
    if not parts:
        return None
    dimensions = _provider_price_dimensions(provider, model)
    for modifier in parts:
        modifier_name = _normalize_dimension_name(modifier)
        if not _dimension_declared(
            dimensions,
            modifier_name,
            f"model_modifier:{modifier_name}",
            f"model_modifier_{modifier_name}",
            f"model_suffix:{modifier_name}",
            f"model_suffix_{modifier_name}",
        ):
            return f"model_modifier:{modifier_name}"
    return None


def _unsupported_nested_paid_dimension(
    value: object,
    provider: providers.ProviderRef,
    *,
    model: object = None,
) -> str | None:
    dimensions = _provider_price_dimensions(
        provider,
        model if model is not None else value.get("model") if isinstance(value, Mapping) else None,
    )
    return _find_unsupported_nested_paid_dimension(value, dimensions, ())


def _is_declared_schema_path(path: tuple[object, ...]) -> bool:
    if len(path) >= 4 and path[-4] == "tools" and path[-2:] == ("function", "parameters"):
        return True
    if len(path) >= 3 and path[-3] == "functions" and path[-1] == "parameters":
        return True
    return path[-3:] == ("response_format", "json_schema", "schema")


def _find_unsupported_nested_paid_dimension(
    value: object, dimensions: set[str], path: tuple[object, ...]
) -> str | None:
    if isinstance(value, Mapping):
        if _is_declared_schema_path(path):
            return None
        if "cache_control" in value and not _dimension_declared(dimensions, "cache_control"):
            return "cache_control"
        value_type = _normalize_dimension_name(str(value.get("type") or ""))
        if value_type == "document" and not _dimension_declared(dimensions, "document"):
            return "document"
        if value_type == "file" and not _dimension_declared(dimensions, "file"):
            return "file"
        for key, child in value.items():
            key_name = _normalize_dimension_name(str(key))
            if key_name in {"file", "files"} and not _dimension_declared(dimensions, "file"):
                return "file"
            found = _find_unsupported_nested_paid_dimension(child, dimensions, (*path, key))
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _find_unsupported_nested_paid_dimension(item, dimensions, (*path, index))
            if found is not None:
                return found
    return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _unsupported_paid_tool(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return "tools"
    for item in value:
        if not isinstance(item, Mapping):
            return "tools"
        tool_type = item.get("type")
        if tool_type != "function":
            return "tools"
    return None


def _merge_usage(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_usage(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalise_stream_event(event: Mapping[str, Any], *, model: str, request_id: str, created: int) -> dict[str, Any]:
    out = dict(event)
    out.setdefault("id", request_id)
    out.setdefault("object", "chat.completion.chunk")
    out.setdefault("created", created)
    out.setdefault("model", model)
    out.setdefault("choices", [])
    choices = out.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, Mapping):
            first_out = dict(first)
            delta = first_out.get("delta")
            if isinstance(delta, Mapping):
                delta_out = dict(delta)
                delta_out.setdefault("role", "assistant")
                first_out["delta"] = delta_out
            choices = [first_out, *choices[1:]]
            out["choices"] = choices
    return out


def _assembled_stream_body(
    chunks: list[Mapping[str, Any]],
    *,
    model: str,
    merged_usage: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not chunks or not merged_usage:
        return None
    try:
        from litellm.main import stream_chunk_builder  # type: ignore[import-not-found]
        assembled = stream_chunk_builder(chunks=list(chunks))
    except Exception:
        return None
    if assembled is None:
        return None
    try:
        if hasattr(assembled, "model_dump"):
            body = assembled.model_dump()
        elif isinstance(assembled, Mapping):
            body = dict(assembled)
        else:
            return None
    except Exception:
        return None
    body["model"] = str(body.get("model") or model)
    body["usage"] = dict(merged_usage)
    return body


def _model_declared(provider: providers.ProviderRef, model: str) -> bool:
    return _provider_model_facts(provider, model) is not None


def _provider_model_facts(
    provider: providers.ProviderRef,
    model: str,
) -> Mapping[str, Any] | None:
    if not set(provider.protocols) & {"openai", "openai-chat", "openai-compatible", "anthropic-messages"}:
        return None
    registry = {"services": {provider.id: {
        "id": provider.id, "kind": "provider", "capabilities": ["openai"],
        "config": {**provider.config, "provider_id": provider.id,
                   "provider_type": "paid-remote", "api_key_env": provider.api_key_env},
    }}}
    return litellm_provider_model_lookup(registry).get(model)


def _resolved_upstream_model(provider: providers.ProviderRef, model: str) -> str:
    facts = _provider_model_facts(provider, model)
    if not facts:
        return model
    upstream = str(facts.get("upstream_model") or "").strip()
    return upstream or model


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
        else ("openai.chat", "text.completions" if request_path == "/v1/completions" else "chat.completions")
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
            resolved_upstream_model = _resolved_upstream_model(provider, model)
            decision = evaluate(profile, PolicyRequest(protocol=protocol, intent=intent, model=model))
            if decision.denied:
                raise providers.RemoteProviderNotAllowed("; ".join(decision.reasons))
            if not runtime.enabled:
                raise providers.ProviderError("LiteLLM execution substrate is unavailable")
            prepared = _prepare_paid_litellm_payload(
                body,
                stream=receipt.stream,
                provider=provider,
                resolved_upstream_model=resolved_upstream_model,
            )
            if isinstance(prepared, JSONResponse):
                receipt.set_policy_decision("denied", reason="invalid_request", source="gateway:remote-provider")
                owner.report(status="failed", reason="failed")
                return prepared
            _, payload_bytes = prepared
            execution = sanitize_litellm_execution_request(
                LlmExecutionRequest(
                    method=request.method,
                    url=litellm_url(base_url=runtime.base_url, path=request_path),
                    headers=litellm_headers(
                        {},
                        api_key=runtime.api_key,
                        gateway_metadata={"request-id": receipt.request_id},
                    ),
                    body=payload_bytes,
                    read_timeout=120.0,
                    trusted_num_retries=0,
                    trusted_max_retries=0,
                )
            )
            try:
                estimate_payload = json.loads(execution.body or b"{}")
            except (UnicodeDecodeError, ValueError):
                return invalid_request_response("request body is not JSON serializable")
            if not isinstance(estimate_payload, Mapping):
                return invalid_request_response("request body must be a JSON object")
            estimate_pricing_payload = {
                **estimate_payload,
                "model": resolved_upstream_model,
            }
            estimate = providers.estimate_request_cost(provider, estimate_pricing_payload, protocol=protocol, intent=intent)
            reservation = providers.reserve_provider_spend(
                profile, provider, estimate, path=ledger_path,
                admin_db_path=admin_db_path, paid_fallback=paid_fallback,
            )
        except providers.ProviderError as error:
            receipt.set_policy_decision("denied", reason=error.code, source="gateway:remote-provider")
            owner.report(status="failed", reason="failed")
            return error_response(error)
        providers.identify_provider_spend(reservation, request_id=receipt.request_id, path=ledger_path)
        receipt.set_policy_decision("allowed", source="gateway:remote-provider")
        # Neither disconnect nor transport failure proves this request was unbilled.
        client = None
        response = None
        watcher = RequestDisconnectWatcher(request.receive)
        try:
            client, response = await send_llm_execution_request(execution, disconnect_watcher=watcher)
        except ClientDisconnectedBeforeHeaders:
            providers.settle_provider_spend(reservation, None, path=ledger_path)
            receipt.cost.update({"reservation_id": reservation.id, "reconciliation_status": "needs_reconciliation"})
            owner.report(status="cancelled", reason="cancelled")
            return Response(status_code=499)
        except httpx.TimeoutException:
            providers.settle_provider_spend(reservation, None, path=ledger_path)
            receipt.cost.update({"reservation_id": reservation.id, "reconciliation_status": "needs_reconciliation"})
            owner.report(status="failed", reason="idle_timeout")
            return error_response(providers.ProviderTimeout("Remote provider request timed out"))
        except LlmExecutionPredispatchError:
            providers.release_provider_spend(reservation, path=ledger_path)
            owner.report(status="failed", reason="connect_error")
            return JSONResponse({"error": {"code": "provider_unavailable", "message": "Remote execution is unavailable"}}, status_code=502)
        except httpx.HTTPError:
            providers.settle_provider_spend(reservation, None, path=ledger_path)
            receipt.cost.update({"reservation_id": reservation.id, "reconciliation_status": "needs_reconciliation"})
            owner.report(status="failed", reason="connect_error")
            return JSONResponse({"error": {"code": "provider_unavailable", "message": "Remote execution is unavailable"}}, status_code=502)
        finally:
            if response is None:
                await watcher.aclose()
        cost = providers.extract_provider_cost(provider, headers=response.headers)
        headers = {"x-gateway-request-id": receipt.request_id, "x-gateway-remote": "1"}
        if response.headers.get("retry-after"):
            headers["retry-after"] = response.headers["retry-after"]
        if response.status_code >= 400:
            await watcher.aclose()
            await response.aclose()
            await client.aclose()
            # Do not reflect vendor exception text, URLs or auth headers.
            providers.settle_provider_spend(reservation, cost, path=ledger_path)
            providers.attach_cost_to_receipt(receipt, cost)
            receipt.cost.update({"reservation_id": reservation.id, "reconciliation_status": "needs_reconciliation" if cost.usd is None else "settled"})
            owner.report(status="failed", reason="failed")
            if response.status_code in {401, 403}:
                return error_response(providers.ProviderAuthFailed("Remote provider rejected the operator key"))
            if response.status_code == 429:
                return error_response(providers.ProviderRateLimited("Remote provider rate limited the request", retry_after=headers.get("retry-after")))
            if response.status_code == 408:
                return error_response(providers.ProviderTimeout("Remote provider request timed out"))
            return JSONResponse({"error": {"code": "provider_error", "message": "Remote provider request failed"}}, status_code=response.status_code, headers=headers)

        async def finish(body: Mapping[str, Any], generation_id: str | None = None) -> None:
            final_cost = providers.extract_provider_cost(
                provider,
                body={**body, "model": model},
                headers=response.headers,
                stream="text/event-stream" in response.headers.get("content-type", ""),
            )
            generation_id = generation_id or (body.get("id") if isinstance(body.get("id"), str) else None)
            providers.identify_provider_spend(reservation, generation_id=generation_id, path=ledger_path)
            # Mark unknown charges before any lookup so concurrent admission fails closed.
            providers.settle_provider_spend(reservation, final_cost, path=ledger_path)
            if final_cost.usd is None and generation_id:
                from gateway.provider_reconciliation import lookup_generation_cost
                reported_cost = await lookup_generation_cost(provider, generation_id)
                if reported_cost.usd is not None:
                    final_cost = reported_cost
                    providers.settle_provider_spend(reservation, final_cost, path=ledger_path)
            providers.attach_cost_to_receipt(receipt, final_cost, providers.extract_usage(body))
            receipt.cost.update({
                "reservation_id": reservation.id,
                "reconciliation_status": "needs_reconciliation" if final_cost.usd is None else "settled",
            })

        if "text/event-stream" in response.headers.get("content-type", ""):
            await watcher.aclose()
            async def stream():
                pending = b""
                final_body: dict[str, Any] = {}
                merged_usage: dict[str, Any] = {}
                assembled_chunks: list[dict[str, Any]] = []
                finished = False
                assembly_failed = False
                assembled_bytes = 0
                generation_id = None
                created = int(time.time())
                try:
                    async for chunk in response.aiter_bytes():
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            if line.startswith(b"data:"):
                                data = line[5:].strip()
                                if data == b"[DONE]":
                                    continue
                                try:
                                    event = json.loads(data)
                                except (ValueError, UnicodeError):
                                    assembly_failed = True
                                    continue
                                if not isinstance(event, Mapping):
                                    assembly_failed = True
                                    continue
                                if isinstance(event.get("id"), str):
                                    generation_id = event["id"]
                                assembled_bytes += len(data)
                                if assembled_bytes > _STREAM_ASSEMBLY_MAX_BYTES:
                                    assembly_failed = True
                                elif not assembly_failed:
                                    assembled_chunks.append(
                                        _normalise_stream_event(
                                            event,
                                            model=model,
                                            request_id=receipt.request_id,
                                            created=created,
                                        )
                                    )
                                if isinstance(event.get("usage"), Mapping):
                                    merged_usage = _merge_usage(merged_usage, event["usage"])
                        if len(pending) > 2 * 1024 * 1024:
                            assembly_failed = True
                            pending = b""
                        yield chunk
                    if pending.strip():
                        assembly_failed = True
                    if not assembly_failed:
                        assembled = _assembled_stream_body(
                            assembled_chunks,
                            model=model,
                            merged_usage=merged_usage,
                        )
                        if assembled is not None:
                            final_body = assembled
                    await finish(final_body, generation_id)
                    finished = True
                    owner.report(status="succeeded", reason="completed")
                finally:
                    if not finished:
                        providers.settle_provider_spend(reservation, None, path=ledger_path)
                        receipt.cost.update({"reservation_id": reservation.id, "reconciliation_status": "needs_reconciliation"})
                    await response.aclose()
                    await client.aclose()
            return StreamingResponse(owner.handoff_stream(stream()), media_type="text/event-stream", headers=headers)
        try:
            content = await read_llm_response_body(client, response, disconnect_watcher=watcher)
            if content is None:
                providers.settle_provider_spend(reservation, None, path=ledger_path)
                receipt.cost.update({"reservation_id": reservation.id, "reconciliation_status": "needs_reconciliation"})
                owner.report(status="cancelled", reason="cancelled")
                return Response(status_code=499)
            try:
                result = json.loads(content)
            except (ValueError, UnicodeError):
                result = {}
            await finish(result if isinstance(result, Mapping) else {})
            cost_usd = receipt.cost["usd"]
            headers["x-gateway-cost-usd"] = str(cost_usd) if cost_usd is not None else "unknown"
            owner.report(status="succeeded", reason="completed")
            return Response(content, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"), headers=headers)
        except httpx.TimeoutException:
            providers.settle_provider_spend(reservation, None, path=ledger_path)
            receipt.cost.update({"reservation_id": reservation.id, "reconciliation_status": "needs_reconciliation"})
            owner.report(status="failed", reason="idle_timeout")
            return error_response(providers.ProviderTimeout("Remote provider request timed out"))
        finally:
            await watcher.aclose()
            await response.aclose()
            await client.aclose()
