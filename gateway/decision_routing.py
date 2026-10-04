"""Local System One routing over registry services, without engine knowledge."""
from __future__ import annotations

import time
import asyncio
from collections.abc import Callable, Mapping
from typing import Any
from dataclasses import dataclass, field

import httpx

from gateway.capabilities import Capability
from gateway.decisions_api import DecisionDispatchResult, DecisionModel, DecisionsError
from gateway.stable_aliases import AliasUnavailableError, resolve_alias


ROUTING_TIMEOUT_SECONDS = 15.0


@dataclass
class _DecisionAttempts:
    backend: str | None = None
    fallbacks: list[str] = field(default_factory=list)
    fallback_declared: bool = False


class DecisionRuntime:
    """Resolve, authorize and start configured services; expose observed placement."""

    def __init__(self, config: Callable[[], Mapping[str, Any]], authorize: Callable,
                 ensure_ready: Callable, catalog_authorize: Callable | None = None):
        self.config = config
        self.authorize = authorize
        self.catalog_authorize = catalog_authorize or authorize
        self.ensure_ready = ensure_ready
        self.observed: dict[str, tuple[float, str | None]] = {}

    def available(self, _name: str, spec: Mapping[str, Any]) -> bool:
        service = self.config().get("backends", {}).get(spec.get("service"), {})
        return bool(service.get("enabled", True) and service.get("host") and service.get("port"))

    def resolve(self, model: str):
        section = self.config().get("model_aliases", {}).get("decisions", {}).get("engines", {})
        lookup = next((name for name, spec in section.items() if spec.get("model") == model), model)
        try:
            result = resolve_alias(self.config().get("model_aliases"), "decisions", lookup,
                                   engine_available=self.available)
        except AliasUnavailableError as exc:
            status = 529 if exc.fallback_declared else 503
            message = str(exc) if exc.fallback_declared else f"{exc} No fallback is configured."
            raise DecisionsError(status, exc.code, message, headers={"retry-after": "5"}) from exc
        if result is None or result.engine is None:
            raise DecisionsError(422, "invalid_model", "This decision model is not configured.", param="model")
        return result

    def observed_engine(self, service_id: str, *, fresh: bool = False) -> str | None:
        cached = self.observed.get(service_id)
        # Catalog reads must stay pure: placement is unknown until a request or
        # an explicit existing-only authorization probe observes the service.
        if not fresh:
            return cached[1] if cached is not None and time.monotonic() - cached[0] < 10 else None
        service = self.config().get("backends", {}).get(service_id, {})
        engine = None
        try:
            with httpx.Client(timeout=0.3, trust_env=False) as client:
                response = client.get(f"http://{service['host']}:{service['port']}{service.get('health_endpoint', '/health')}")
            if response.status_code == 200:
                candidate = response.json().get("engine")
                if candidate in {"gpu", "cpu"}:
                    engine = candidate
        except (httpx.HTTPError, ValueError, KeyError, AttributeError):
            pass
        self.observed[service_id] = (time.monotonic(), engine)
        return engine

    def model_names(self) -> list[str]:
        section = self.config().get("model_aliases", {}).get("decisions", {})
        names = list(dict.fromkeys(
            list(section.get("stable", {})) + list(section.get("engines", {}))
            + [spec["model"] for spec in section.get("engines", {}).values() if spec.get("model")]
        ))
        if any(pattern.get("match") == "jev-*" for pattern in section.get("openai", [])):
            names += ["jev-latest", "jev-preview"]
        return names

    def capability_for(self, model: str, service_id: str | None = None) -> Capability:
        """One metadata contract for discovery and each authorized routing target."""
        resolved = self.resolve(model)
        service = service_id or resolved.service
        candidates = {resolved.service, *(candidate for _, candidate in resolved.fallbacks)}
        if service not in candidates:
            raise DecisionsError(403, "permission_denied", "This backend is not a configured decision target.")
        section = self.config().get("model_aliases", {}).get("decisions", {})
        spec = next((row for row in section.get("engines", {}).values()
                     if row.get("service") == service), {})
        engine = self.observed_engine(str(service))
        is_alias = model in section.get("stable", {}) or model.startswith("jev-")
        return Capability(
            intent="decision.systemone", backend=service, model=model,
            input_modalities=("text", "json"), output_modalities=("decision",),
            tags=("alias" if is_alias else "model", "free-local", "local", f"engine:{engine or 'unknown'}"),
            limits=tuple((key, spec[key]) for key in ("max_options", "max_levels", "max_state_tokens", "context_tokens", "calibrated") if key in spec),
            backend_kind="local", cost_class="free-local", engine=engine,
            calibrated=bool(spec.get("calibrated", False)),
            latency_class="gpu-sub-second" if engine == "gpu" else "cpu-seconds" if engine == "cpu" else "unknown",
            reason="engine_not_running_or_unprobed" if engine is None else None,
            caveats=("Placement is unknown until the decision service reports its running engine.",) if engine is None else (),
        )

    def models_for(self, auth: Any, _profile: Any = None) -> list[DecisionModel]:
        rows = []
        for name in self.model_names():
            try:
                resolved = self.resolve(name)
                capability = self.capability_for(name)
                if not self.catalog_authorize(auth, name, resolved.service):
                    continue
            except DecisionsError:
                continue
            spec = resolved.engine_spec or {}
            engine = capability.engine
            target_policy = getattr(_profile, "target_policy", None)
            if getattr(target_policy, "visibility", "catalog") == "available" and engine is None:
                continue
            rows.append(DecisionModel(
                id=name, root=resolved.upstream_model, backend=resolved.service, engine=engine,
                limits=dict(capability.limits),
                capabilities={"calibrated": capability.calibrated,
                              "latency_class": capability.latency_class,
                              "placement": spec.get("placement", "unknown"),
                              "residency": spec.get("residency", "unknown")},
            ))
        return rows

    def capabilities(self) -> list[Capability]:
        rows = []
        for name in self.model_names():
            try:
                rows.append(self.capability_for(name))
            except DecisionsError:
                continue
        return rows

    async def dispatch(self, auth: Any, _profile: Any, body: Mapping[str, Any], _request_id: str) -> DecisionDispatchResult:
        attempts = _DecisionAttempts()
        deadline = time.monotonic() + ROUTING_TIMEOUT_SECONDS
        try:
            # httpx timeouts bound individual phases; this bounds the entire
            # route, including authorization, activation, response and fallback.
            async with asyncio.timeout_at(deadline):
                return await self._dispatch_before_deadline(auth, _profile, body, deadline, attempts)
        except TimeoutError:
            if attempts.backend:
                attempts.fallbacks.append(f"{attempts.backend}:deadline_exceeded")
            return self._unavailable_result(attempts)

    async def _dispatch_before_deadline(self, auth: Any, _profile: Any,
                                        body: Mapping[str, Any], deadline: float,
                                        attempts: _DecisionAttempts) -> DecisionDispatchResult:
        resolved = self.resolve(str(body["model"]))
        attempts.fallback_declared = resolved.fallback_declared
        candidates = [(resolved.engine, resolved.service)] + list(resolved.fallbacks)
        fallbacks = attempts.fallbacks
        fallbacks.extend(f"{name}:not_deployed" for name in resolved.skipped_engines)
        section = self.config().get("model_aliases", {}).get("decisions", {}).get("engines", {})
        for engine_id, service_id in candidates:
            if time.monotonic() >= deadline:
                break
            attempts.backend = service_id
            if not await asyncio.to_thread(self.authorize, auth, body["model"], service_id):
                raise DecisionsError(403, "permission_denied", "Your access plan does not allow this decision model.")
            service = self.config().get("backends", {}).get(service_id, {})
            # Only free local services are admitted here. Paid forwarding belongs to the provider hook.
            if service.get("cost_class", "free-local") != "free-local" or service.get("kind") == "provider":
                raise DecisionsError(403, "remote_provider_not_allowed", "This dispatcher only serves configured local decision engines.")
            try:
                policy = getattr(_profile, "target_policy", None)
                if getattr(policy, "activation", "may_activate") == "existing_only":
                    readiness = None  # Authorization probed it; never activate a cold service for this plan.
                else:
                    readiness = await asyncio.wait_for(self.ensure_ready(service_id), max(0.001, deadline - time.monotonic()))
            except TimeoutError:
                readiness = False
            if readiness is not None:
                fallbacks.append(f"{service_id}:unavailable")
                continue
            spec = section.get(engine_id, {})
            forwarded = {**body, "model": spec.get("model") or resolved.upstream_model}
            try:
                async with httpx.AsyncClient(timeout=max(0.001, min(float(service.get("timeout") or 15), deadline - time.monotonic())), trust_env=False) as client:
                    remaining_ms = max(0, (deadline - time.monotonic()) * 1000)
                    response = await client.post(
                        f"http://{service['host']}:{service['port']}/v1/systemone", json=forwarded,
                        headers={"x-decision-deadline-ms": str(remaining_ms)},
                    )
            except httpx.HTTPError:
                fallbacks.append(f"{service_id}:unavailable")
                continue
            if response.status_code in (503, 504, 529):
                reason = "deadline_exceeded" if response.status_code == 504 else "overloaded"
                fallbacks.append(f"{service_id}:{reason}")
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                raise DecisionsError(502, "decision_backend_error", "The decision engine returned invalid JSON.") from exc
            if not isinstance(payload, dict):
                raise DecisionsError(502, "decision_backend_error", "The decision engine returned an invalid response.")
            if response.status_code == 200 and (not isinstance(payload.get("answers"), dict) or set(payload["answers"]) != set(body["questions"]) or not isinstance(payload.get("model"), str)):
                raise DecisionsError(502, "decision_backend_error", "The decision engine did not answer every question.")
            if response.status_code == 200 and payload["model"] != forwarded["model"]:
                raise DecisionsError(503, "model_not_deployed", "The decision service did not serve the configured model. Undeclared model substitution is not allowed.")
            engine = response.headers.get("x-decision-engine")
            engine = engine if engine in {"gpu", "cpu"} else None
            self.observed[str(service_id)] = (time.monotonic(), engine)
            status = response.status_code if response.status_code in (200, 422, 429) else 502
            if status == 502:
                raise DecisionsError(502, "decision_backend_error", "The configured decision engine failed.")
            headers = {"retry-after": response.headers["retry-after"]} if "retry-after" in response.headers else {}
            if fallbacks:
                headers["x-gateway-fallback"] = ",".join(fallbacks)
            return DecisionDispatchResult(payload, status_code=status, backend=service_id,
                                          resolved_model=payload.get("model"), engine=engine,
                                          headers=headers,
                                          fallback_reasons=tuple(fallbacks))
        return self._unavailable_result(attempts)

    @staticmethod
    def _unavailable_result(attempts: _DecisionAttempts) -> DecisionDispatchResult:
        status = 529 if attempts.fallback_declared else 503
        headers = {"retry-after": "5"}
        if attempts.fallback_declared and len(attempts.fallbacks) > 1:
            headers["x-gateway-fallback"] = ",".join(attempts.fallbacks)
        return DecisionDispatchResult(
            {"error": {"message": "The local decision engines are starting or unavailable." if attempts.fallback_declared else "The configured decision engine is unavailable. No fallback is configured.",
                       "type": "server_error", "code": "overloaded" if attempts.fallback_declared else "model_not_deployed"}},
            status_code=status, headers=headers,
            backend=attempts.backend, fallback_reasons=tuple(attempts.fallbacks),
        )
