"""Why a model is not usable right now: one closed set of codes for callers to branch on.

The same code is in ``azizam_availability.reason`` on ``GET /v1/models`` and in ``error.reason`` of a refusal the
gateway makes for that model, so a program reads one word in both places. A name that is already a refusal code
(``GATEWAY_CODES`` in ``gateway.client_errors``) keeps that name. The sentence is for the docs
(docs/api/MODEL_METADATA.md, "Reason codes"); a reply to a caller never carries text from an engine.

The set is closed on purpose: a cause that fits none of these is a new row here and in the doc, with its test, not
a new string at the place that notices it.
"""

from __future__ import annotations

from typing import Any, Mapping

REASONS: dict[str, str] = {
    "backend_unavailable": "The engine behind this model is stopped, failed, unreachable or not set up.",
    "engine_transitioning": "The engine is starting or loading and does not take requests yet.",
    "model_not_deployed": "No engine behind this name is deployed.",
    "current_model_unavailable": "The name follows the loaded model, and no backend has one loaded.",
    "service_disabled": "The owner turned off the service that runs this model.",
    "service_maintenance": "The owner took the service out of use for now (maintenance or draining).",
    "health_unknown": "The gateway has no recent health reading for the service.",
}

# What an operator override of a service says, as the reason a caller sees. `available` and `degraded` take a
# service out of no use.
OVERRIDE_REASONS: dict[str, str] = {
    "disabled": "service_disabled",
    "maintenance": "service_maintenance",
    "draining": "service_maintenance",
}


def stamp(error: dict[str, Any]) -> dict[str, Any]:
    """Add ``reason`` to a refusal's ``error`` object when its ``code`` is a reason code; never replace one already set."""
    code = error.get("code")
    if isinstance(code, str) and code in REASONS and "reason" not in error:
        error["reason"] = code
    return error


def override_reason(override: Mapping[str, Any] | None) -> str | None:
    """The reason code for an operator override of a service, or ``None`` when it leaves the service in use."""
    state = str((override or {}).get("state") or "").strip().lower()
    return OVERRIDE_REASONS.get(state)
