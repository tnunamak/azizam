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
    "required_file_missing": "A model file or custom node the workflow declares is not on its engine.",
}

# The delay a refusal and the list give for an operator override that sets none.
DEFAULT_RETRY_AFTER_S = 10

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


def name_reason(body: dict[str, Any], reason: str | None) -> None:
    """Put ``reason`` in the ``error`` object of a refusal body, when there is a reason and the body has such an object
    (an Ollama-shaped refusal carries its error as text and stays as it is)."""
    error = body.get("error")
    if reason is not None and isinstance(error, dict):
        error["reason"] = reason


def override_reason(override: Mapping[str, Any] | None) -> str | None:
    """The reason code for an operator override of a service, or ``None`` when it leaves the service in use."""
    state = str((override or {}).get("state") or "").strip().lower()
    return OVERRIDE_REASONS.get(state)


def override_retry_after_s(override: Mapping[str, Any] | None) -> int:
    """The delay in whole seconds an operator override gives, or the default when it sets none; one reading for the
    refusal's ``Retry-After`` and the list's ``retry_after_s``."""
    try:
        seconds = int((override or {}).get("retry_after_seconds"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_S
    return seconds if seconds > 0 else DEFAULT_RETRY_AFTER_S
