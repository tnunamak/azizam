"""Paid remote execution seam for decision APIs."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Mapping

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from gateway import providers
from gateway.capabilities import Capability
from gateway.policy import AppProfile, PolicyRequest, evaluate
from gateway.decisions_api import DecisionDispatchResult
from gateway.receipts import ReceiptLifecycleOwner, RouteReceipt, new_request_id

DEFAULT_DECISION_TIMEOUT_S = 15.0
UNKNOWN_COST_SOURCE = "unknown"


class PaidDecisionError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, *, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = dict(headers or {})

    def body(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "type": "invalid_request_error", "code": self.code}}

    def response(self) -> JSONResponse:
        return JSONResponse(self.body(), status_code=self.status, headers=self.headers)


def capability_for(provider: providers.ProviderRef, qualified_model: str, protocol: str = "typesafe.systemone") -> Capability:
    return Capability(
        intent="decision.systemone",
        backend=provider.id,
        model=qualified_model,
        input_modalities=("json", "text"),
        output_modalities=("json",),
        execution="sync",
        exposure="default",
        trust_tier="stable",
        resource_tier="interactive",
        tags=("remote", "paid-remote"),
        lifecycle="unknown",
        health="unknown",
        backend_kind="provider",
        cost_class=provider.cost_class or "paid-remote",
        calibrated=True if protocol in {"typesafe.systemone", "typesafe-systemone"} else None,
    )


def _auth_value(auth: Any, name: str) -> Any:
    if hasattr(auth, name):
        return getattr(auth, name)
    identity = getattr(auth, "identity", None)
    if identity is not None and hasattr(identity, name):
        return getattr(identity, name)
    return None


def _auth_app_id(auth: Any) -> str | None:
    value = _auth_value(auth, "app_id")
    return str(value) if value else None


def _auth_name(auth: Any) -> str | None:
    value = _auth_value(auth, "name") or _auth_value(auth, "api_key_name")
    return str(value) if value else None


def _is_delegated(auth: Any) -> bool:
    return bool(_auth_value(auth, "delegated") or _auth_value(auth, "delegated_auth"))


def _require_real_app_identity(auth: Any) -> str:
    app_id = _auth_app_id(auth)
    if not app_id or _is_delegated(auth):
        raise PaidDecisionError(
            403,
            "remote_provider_not_allowed",
            "Paid decision providers require a gateway App key and access plan.",
        )
    return app_id


def _provider_has_capability(provider: providers.ProviderRef, registry: Mapping[str, Any], capability: str) -> bool:
    wanted = capability.strip().lower()
    values: list[Any] = [*provider.protocols]
    services = registry.get("services") if isinstance(registry, Mapping) else None
    raw_service = services.get(provider.id) if isinstance(services, Mapping) else None
    raw_config = raw_service.get("config") if isinstance(raw_service, Mapping) and isinstance(raw_service.get("config"), Mapping) else {}
    for source in (provider.config, raw_config, raw_service if isinstance(raw_service, Mapping) else {}):
        for key in ("capabilities", "decision_capabilities"):
            raw = source.get(key) if isinstance(source, Mapping) else None
            if isinstance(raw, str):
                values.append(raw)
            elif isinstance(raw, (list, tuple, set)):
                values.extend(raw)
    return wanted in {str(item).strip().lower() for item in values}



def catalog_models(provider: providers.ProviderRef) -> list[str]:
    raw = provider.config.get("models")
    if raw is None:
        return [f"{provider.id}/jev-latest"] if provider.id == "typesafe" else []
    out: list[str] = []

    def add(value: Any) -> None:
        if not value:
            return
        item = str(value)
        if "/" not in item and provider.id == "typesafe":
            item = f"{provider.id}/{item}"
        if item not in out:
            out.append(item)

    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if isinstance(value, Mapping):
                add(value.get("id") or value.get("model_group") or value.get("model") or value.get("upstream_model") or key)
            else:
                add(key)
    elif isinstance(raw, (list, tuple, set)):
        for item in raw:
            if isinstance(item, Mapping):
                add(item.get("id") or item.get("model_group") or item.get("model") or item.get("upstream_model"))
            else:
                add(item)
    return out

def _model_declared(provider: providers.ProviderRef, model: str) -> bool:
    raw = provider.config.get("models")
    if raw is None:
        return True
    candidates = {model, model.split("/", 1)[-1]}
    declared = set(catalog_models(provider))
    declared |= {item.split("/", 1)[-1] for item in declared}
    return bool(candidates & declared)


def _typesafe_provider_for_model(registry: Mapping[str, Any], model: Any) -> providers.ProviderRef | None:
    if not isinstance(model, str):
        return None
    provider_id, separator, rest = model.partition("/")
    if not separator or providers.canonical_provider_id(provider_id) != "typesafe":
        return None
    if not rest:
        raise PaidDecisionError(422, "invalid_model", "TypeSafe provider model must include a model name.")
    provider = providers.provider_refs(registry).get(providers.canonical_provider_id(provider_id))
    if provider is None or "typesafe-systemone" not in provider.protocols:
        return None
    return provider


def _openai_decisions_provider(registry: Mapping[str, Any]) -> providers.ProviderRef | None:
    provider = providers.provider_refs(registry).get("openai")
    if provider is None:
        return None
    if not ({"openai-compatible", "openai-chat"} & set(provider.protocols)):
        return None
    if not _provider_has_capability(provider, registry, "decisions"):
        return None
    return provider


def _map_provider_error(error: providers.ProviderError) -> PaidDecisionError:
    headers = {"retry-after": error.retry_after} if error.retry_after else None
    return PaidDecisionError(error.status_code, error.code, str(error), headers=headers)


def _error_result(error: PaidDecisionError, *, request_id: str, provider: providers.ProviderRef | None, model: str) -> DecisionDispatchResult:
    headers = {"x-gateway-request-id": request_id, "x-gateway-remote": "1", **error.headers}
    if provider is not None:
        headers["x-gateway-provider"] = provider.id
    return DecisionDispatchResult(
        body=error.body(),
        status_code=error.status,
        headers=headers,
        backend=provider.id if provider is not None else None,
        resolved_model=model,
        remote=True,
        cost_usd=None,
        cost_source=UNKNOWN_COST_SOURCE,
    )


def _decision_receipt(
    *,
    request_id: str,
    protocol: str,
    path: str,
    provider: providers.ProviderRef,
    profile: AppProfile,
    auth: Any,
    requested_model: str,
    resolved_model: str,
) -> RouteReceipt:
    return RouteReceipt(
        request_id=request_id,
        protocol=protocol,
        intent="decision.systemone",
        path=path,
        backend=provider.id,
        app_profile=profile.id,
        app_id=_auth_app_id(auth),
        api_key_name=_auth_name(auth),
        requested_model=requested_model,
        resolved_model=resolved_model,
        visibility=profile.status_visibility.level,
        remote=True,
    )


async def systemone(
    auth: Any,
    profile: AppProfile,
    body: Mapping[str, Any],
    request_id: str,
    registry: Mapping[str, Any],
    ledger_path: Path,
    admin_db_path: Path | None = None,
) -> DecisionDispatchResult:
    requested_model = str(body.get("model") or "")
    provider = None
    try:
        provider = _typesafe_provider_for_model(registry, requested_model)
        _require_real_app_identity(auth)
        if provider is None:
            raise PaidDecisionError(501, "provider_not_configured", "TypeSafe decision provider is not configured.")
        cap = capability_for(provider, requested_model)
        decision = evaluate(
            profile,
            PolicyRequest(
                protocol="typesafe.systemone",
                intent=cap.intent,
                model=requested_model,
                backend=provider.id,
                capability_exposure=cap.exposure,
                capability_trust_tier=cap.trust_tier,
                capability_resource_tier=cap.resource_tier,
                capability_tags=frozenset(cap.tags),
                remote_provider=provider.id,
            ),
        )
        if decision.denied:
            raise PaidDecisionError(403, "remote_provider_not_allowed", "; ".join(decision.reasons))
        if not _model_declared(provider, requested_model):
            raise PaidDecisionError(403, "remote_provider_not_allowed", "Remote model is not declared in the provider model allow-list.")
    except PaidDecisionError as exc:
        return _error_result(exc, request_id=request_id, provider=provider, model=requested_model)

    try:
        async with asyncio.timeout(DEFAULT_DECISION_TIMEOUT_S):
            payload, cost, usage, _reservation = await providers.call_typesafe_systemone(
                provider,
                profile,
                body,
                ledger_path=ledger_path,
                admin_db_path=admin_db_path,
                timeout=DEFAULT_DECISION_TIMEOUT_S,
            )
    except TimeoutError:
        return _error_result(
            PaidDecisionError(504, "deadline_exceeded", "Remote decision provider timed out."),
            request_id=request_id,
            provider=provider,
            model=requested_model,
        )
    except providers.ProviderError as exc:
        return _error_result(_map_provider_error(exc), request_id=request_id, provider=provider, model=requested_model)
    cost_usd = str(cost.usd) if cost.usd is not None else None
    headers = {
        "x-gateway-request-id": request_id,
        "x-gateway-remote": "1",
        "x-gateway-provider": provider.id,
        "x-gateway-cost-usd": cost_usd if cost_usd is not None else "unknown",
    }
    payload_model = payload.get("model") if isinstance(payload, Mapping) else None
    resolved = str(payload_model) if isinstance(payload_model, str) and payload_model else (requested_model.split("/", 1)[1] if "/" in requested_model else requested_model)
    return DecisionDispatchResult(
        body=payload,
        status_code=200,
        headers=headers,
        backend=provider.id,
        resolved_model=resolved,
        remote=True,
        cost_usd=cost_usd,
        cost_source=cost.source,
    )


async def openai(
    request: Request,
    auth: Any,
    profile: AppProfile,
    registry: Mapping[str, Any],
    ledger_path: Path,
    admin_db_path: Path | None = None,
) -> Response:
    provider = _openai_decisions_provider(registry)
    if provider is None:
        return PaidDecisionError(501, "decisions_backend_unavailable", "OpenAI decisions provider is not configured.").response()
    try:
        _require_real_app_identity(auth)
    except PaidDecisionError as exc:
        return exc.response()
    try:
        raw_body = await request.body()
        body = json.loads(raw_body or b"{}")
        if not isinstance(body, Mapping):
            raise ValueError("body must be object")
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return PaidDecisionError(400, "invalid_request", "Request body must be a JSON object.").response()

    model = str(body.get("model") or "")
    request_id = str(getattr(request.state, "request_id", "") or new_request_id())
    receipt = _decision_receipt(
        request_id=request_id,
        protocol="openai.decisions",
        path=str(request.url.path),
        provider=provider,
        profile=profile,
        auth=auth,
        requested_model=model,
        resolved_model=model,
    )
    providers.attach_cost_to_receipt(receipt, providers.Cost(None, UNKNOWN_COST_SOURCE, provider.id))
    async with ReceiptLifecycleOwner(receipt) as owner:
        reservation = None
        try:
            cap = capability_for(provider, model, protocol="openai.decisions")
            decision = evaluate(
                profile,
                PolicyRequest(
                    protocol="openai.decisions",
                    intent=cap.intent,
                    model=model,
                    backend=provider.id,
                    capability_exposure=cap.exposure,
                    capability_trust_tier=cap.trust_tier,
                    capability_resource_tier=cap.resource_tier,
                    capability_tags=frozenset(cap.tags),
                    remote_provider=provider.id,
                ),
            )
            if decision.denied:
                raise providers.RemoteProviderNotAllowed("; ".join(decision.reasons))
            if not _model_declared(provider, model):
                raise providers.RemoteProviderNotAllowed("Remote model is not declared in the provider model allow-list")
            api_key = os.environ.get(provider.api_key_env or "")
            if not api_key:
                raise providers.ProviderKeyMissing(f"provider {provider.id!r} key is missing")
            estimate = providers.Cost(None, "unknown", provider.id, provider.cost_class)
            reservation = providers.reserve_provider_spend(profile, provider, estimate, path=ledger_path, admin_db_path=admin_db_path)
            async with asyncio.timeout(DEFAULT_DECISION_TIMEOUT_S):
                payload, cost, usage, upstream_headers, upstream_content, upstream_status = await providers.call_openai_json(
                    provider,
                    "/v1/decisions",
                    body,
                    content=raw_body,
                    timeout=DEFAULT_DECISION_TIMEOUT_S,
                )
            providers.attach_cost_to_receipt(receipt, cost, usage)
            if reservation is not None and cost.usd is not None:
                providers.settle_provider_spend(reservation, cost, path=ledger_path)
            elif reservation is not None and upstream_status in {401, 429}:
                providers.release_provider_spend(reservation, path=ledger_path)
            receipt.set_policy_decision("allowed", source="gateway:remote-provider")
            owner.report(status="succeeded" if upstream_status < 400 else "failed", reason="completed" if upstream_status < 400 else "failed")
            headers = {
                "x-gateway-request-id": receipt.request_id,
                "x-gateway-remote": "1",
                "x-gateway-provider": provider.id,
                "x-gateway-cost-usd": str(receipt.cost["usd"]) if receipt.cost and receipt.cost.get("usd") is not None else "unknown",
            }
            if upstream_headers.get("retry-after"):
                headers["retry-after"] = upstream_headers["retry-after"]
            return Response(upstream_content, status_code=upstream_status, media_type=upstream_headers.get("content-type", "application/json"), headers=headers)
        except TimeoutError:
            owner.report(status="failed", reason="deadline_exceeded")
            return PaidDecisionError(504, "deadline_exceeded", "Remote decision provider timed out.").response()
        except providers.ProviderError as exc:
            if reservation is not None and isinstance(exc, (providers.ProviderAuthFailed, providers.ProviderRateLimited)):
                providers.release_provider_spend(reservation, path=ledger_path)
            owner.report(status="failed", reason="failed")
            return _map_provider_error(exc).response()


OpenAI = openai
