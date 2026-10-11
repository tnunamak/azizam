"""HTTP-facing helpers for resource admission route wiring.

The helpers here keep route handlers thin: handlers collect runtime facts,
call the existing pure admission advisor, then translate the decision into a
stable response shape. This module does not import FastAPI or executor code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from gateway.resource_decisions import Quality, project_decision

from gateway.resource_admission import (
    AdmissionConstraints,
    AdmissionOutcome,
    CurrentLoad,
    ResourceAdmissionDecision,
    ResourceEstimate,
    ServiceAvailability,
    advise_resource_admission,
)


@dataclass(frozen=True)
class RouteAdmissionHTTP:
    """Transport-neutral HTTP response advice for an admission decision."""

    status_code: int
    body: dict[str, object]
    headers: Mapping[str, str] = field(default_factory=dict)


def advise_route_admission(
    *,
    intent: str,
    service_available: bool | None = None,
    service_degraded: bool = False,
    service_retry_after: int | None = None,
    service_reasons: tuple[str, ...] = (),
    app_id: str | None = None,
    resource_class: str | None = None,
    queue_key: str | None = None,
    requires_gpu: bool = False,
    required_tokens: tuple[str, ...] = (),
    optional_tokens: tuple[str, ...] = (),
    estimate_reasons: tuple[str, ...] = (),
    disabled_intents: tuple[str, ...] = (),
    disabled_resource_classes: tuple[str, ...] = (),
    reserved_tokens: tuple[str, ...] = (),
    max_queue_depth: int | None = None,
    max_app_concurrency: Mapping[str, int] | None = None,
    constraint_retry_after: int | None = None,
    constraint_reasons: tuple[str, ...] = (),
    queue_depths: Mapping[str, int | None] | None = None,
    active_by_app: Mapping[str, int | None] | None = None,
    load_retry_after: int | None = None,
    observed_at: float | None = None,
    load_quality: Quality = "declared",
) -> ResourceAdmissionDecision:
    """Build admission inputs from route/runtime facts and evaluate them."""

    return advise_resource_admission(
        ServiceAvailability(
            available=service_available,
            degraded=service_degraded,
            retry_after=service_retry_after,
            reasons=service_reasons,
            observed_at=observed_at,
        ),
        ResourceEstimate(
            intent=intent,
            app_id=app_id,
            resource_class=resource_class,
            queue_key=queue_key,
            requires_gpu=requires_gpu,
            required_tokens=required_tokens,
            optional_tokens=optional_tokens,
            reasons=estimate_reasons,
        ),
        AdmissionConstraints(
            disabled_intents=disabled_intents,
            disabled_resource_classes=disabled_resource_classes,
            reserved_tokens=reserved_tokens,
            max_queue_depth=max_queue_depth,
            max_app_concurrency=max_app_concurrency or {},
            retry_after=constraint_retry_after,
            reasons=constraint_reasons,
        ),
        CurrentLoad(
            queue_depths=queue_depths or {},
            active_by_app=active_by_app or {},
            retry_after=load_retry_after,
            observed_at=observed_at,
            quality=load_quality,
        ),
    )


def admission_http_response(
    decision: ResourceAdmissionDecision,
    *,
    allowed_status: int = 200,
    degraded_status: int = 200,
    denied_status: int = 503,
    audience: str = "client",
    wait_deadline: float | None = None,
) -> RouteAdmissionHTTP:
    """Translate an admission decision into a response-ready HTTP shape."""

    evidence = decision.details.as_dict(decision.outcome.value)
    evidence["wait_deadline"] = wait_deadline
    projected = project_decision(evidence, audience)
    admission = decision.as_dict()
    if audience not in {"admin", "internal", "owner"}:
        admission["reasons"] = tuple(r["message"] for r in projected["reasons"])
    headers: dict[str, str] = {}
    if decision.retry_after is not None:
        headers["Retry-After"] = str(decision.retry_after)

    if decision.outcome is AdmissionOutcome.DENIED:
        return RouteAdmissionHTTP(
            status_code=denied_status,
            headers=headers,
            body={
                "error": {
                    "code": "resource_admission_denied",
                    "message": (admission["reasons"][0] if admission["reasons"] else "resource admission denied"),
                    "decision": projected,
                },
                "admission": admission,
            },
        )

    status_code = degraded_status if decision.degraded else allowed_status
    return RouteAdmissionHTTP(
        status_code=status_code,
        headers=headers,
        body={"admission": admission, "decision": projected},
    )
