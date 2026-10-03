"""Local System One routing over registry services, without engine knowledge."""
from __future__ import annotations

import time
import asyncio
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from gateway.capabilities import Capability
from gateway.decisions_api import DecisionDispatchResult, DecisionModel, DecisionsError
from gateway.stable_aliases import AliasUnavailableError, describe_aliases, resolve_alias


class DecisionRuntime:
    """Resolve, authorize and start configured services; expose observed placement."""

    def __init__(self, config: Callable[[], Mapping[str, Any]], authorize: Callable,
                 ensure_ready: Callable):
        self.config = config
        self.authorize = authorize
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
            raise DecisionsError(529, exc.code, str(exc)) from exc
        if result is None or result.engine is None:
            raise DecisionsError(422, "invalid_model", "This decision model is not configured.", param="model")
        return result

    def observed_engine(self, service_id: str) -> str | None:
        cached = self.observed.get(service_id)
        if cached is not None and time.monotonic() - cached[0] < 10:
            return cached[1]
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

    def models_for(self, auth: Any, _profile: Any = None) -> list[DecisionModel]:
        cfg = self.config().get("model_aliases", {})
        section = cfg.get("decisions", {})
        names = list(dict.fromkeys(list(section.get("stable", {})) + list(section.get("engines", {})) + [spec["model"] for spec in section.get("engines", {}).values() if spec.get("model")]))
        names += ["jev-latest", "jev-preview"] if any(
            p.get("match") == "jev-*" for p in section.get("openai", [])
        ) else []
        rows = []
        for name in names:
            try:
                resolved = self.resolve(name)
            except DecisionsError:
                continue
            if not self.authorize(auth, name, resolved.service):
                continue
            spec = resolved.engine_spec or {}
            engine = self.observed_engine(str(resolved.service))
            rows.append(DecisionModel(
                id=name, root=resolved.upstream_model, backend=resolved.service, engine=engine,
                limits={key: spec[key] for key in ("max_options", "max_levels", "max_state_tokens", "context_tokens") if key in spec},
                capabilities={"calibrated": bool(spec.get("calibrated", False)),
                              "latency_class": "gpu-sub-second" if engine == "gpu" else "cpu-seconds" if engine == "cpu" else "unknown",
                              "placement": spec.get("placement", "unknown"),
                              "residency": spec.get("residency", "unknown")},
            ))
        return rows

    def capabilities(self) -> list[Capability]:
        views = describe_aliases(self.config().get("model_aliases"), operation="decisions",
                                 engine_available=self.available).get("decisions", {})
        rows = []
        for row in views.get("stable", []):
            if not row.get("resolves_to"):
                continue
            service = str(row.get("service"))
            engine = self.observed_engine(service)
            resolved = self.resolve(row["alias"])
            limits = resolved.engine_spec or {}
            rows.append(Capability(
                intent="decision.systemone", backend=service, model=row["alias"],
                input_modalities=("text", "json"), output_modalities=("decision",),
                tags=("alias", "free-local", "local", f"engine:{engine or 'unknown'}"),
                limits=tuple((key, limits[key]) for key in ("max_options", "max_levels", "max_state_tokens", "context_tokens", "calibrated") if key in limits),
                backend_kind="local", cost_class="free-local", engine=engine,
                calibrated=bool(limits.get("calibrated", False)),
                latency_class="gpu-sub-second" if engine == "gpu" else "cpu-seconds" if engine == "cpu" else "unknown",
                reason="engine_not_running_or_unprobed" if engine is None else None,
                caveats=("Placement is unknown until the decision service reports its running engine.",) if engine is None else (),
            ))
        return rows

    async def dispatch(self, auth: Any, _profile: Any, body: Mapping[str, Any], _request_id: str) -> DecisionDispatchResult:
        resolved = self.resolve(str(body["model"]))
        candidates = [(resolved.engine, resolved.service)] + list(resolved.fallbacks)
        fallbacks = [f"{name}:not_deployed" for name in resolved.skipped_engines]
        deadline = time.monotonic() + 15
        section = self.config().get("model_aliases", {}).get("decisions", {}).get("engines", {})
        for engine_id, service_id in candidates:
            if time.monotonic() >= deadline:
                break
            if not self.authorize(auth, body["model"], service_id):
                raise DecisionsError(403, "permission_denied", "Your access plan does not allow this decision model.")
            service = self.config().get("backends", {}).get(service_id, {})
            # Only free local services are admitted here. Paid forwarding belongs to the provider hook.
            if service.get("cost_class", "free-local") != "free-local" or service.get("kind") == "provider":
                raise DecisionsError(403, "remote_provider_not_allowed", "This dispatcher only serves configured local decision engines.")
            try:
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
                    response = await client.post(f"http://{service['host']}:{service['port']}/v1/systemone", json=forwarded)
            except httpx.HTTPError:
                fallbacks.append(f"{service_id}:unavailable")
                continue
            if response.status_code in (503, 529):
                fallbacks.append(f"{service_id}:overloaded")
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                raise DecisionsError(502, "decision_backend_error", "The decision engine returned invalid JSON.") from exc
            if not isinstance(payload, dict):
                raise DecisionsError(502, "decision_backend_error", "The decision engine returned an invalid response.")
            if response.status_code == 200 and (not isinstance(payload.get("answers"), dict) or set(payload["answers"]) != set(body["questions"]) or not isinstance(payload.get("model"), str)):
                raise DecisionsError(502, "decision_backend_error", "The decision engine did not answer every question.")
            engine = response.headers.get("x-decision-engine")
            engine = engine if engine in {"gpu", "cpu"} else None
            self.observed[str(service_id)] = (time.monotonic(), engine)
            status = response.status_code if response.status_code in (200, 422, 429) else 502
            if status == 502:
                raise DecisionsError(502, "decision_backend_error", "The configured decision engine failed.")
            return DecisionDispatchResult(payload, status_code=status, backend=service_id,
                                          resolved_model=payload.get("model"), engine=engine,
                                          headers={"retry-after": response.headers["retry-after"]} if "retry-after" in response.headers else {},
                                          fallback_reasons=tuple(fallbacks))
        raise DecisionsError(529, "overloaded", "The local decision engines are starting or unavailable.", headers={"retry-after": "5"})
