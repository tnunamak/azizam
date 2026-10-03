"""Pure capability model for policy-aware discovery and routing.

A :class:`Capability` is a structured fact: "this gateway, with this profile,
can serve requests of this shape against this backend (with these caveats)".
The model deliberately mirrors the minimum eligibility fields described in
``docs/CORE_CONTRACTS.md`` §Capability so that the same record can drive

* discovery summaries on ``/gateway/v1/capabilities`` and
  ``/gateway/v1/models/{model}/capabilities``
* router pre-filtering before scheduling and backend binding
* route receipts that record *why* a backend was eligible

This module is pure. It does not import FastAPI, does not talk to backends,
does not probe live GPU/health state, and does not import ``proxy.py``. Live
status, queue depth, and resource pressure are layered on top later by the
runtime.

Audiences match :mod:`gateway.discovery`:

* ``client`` — portable contract only. Modalities, streaming, execution mode,
  caveats, and a coarse availability flag. No backend identity, no resource
  estimates, no policy source details.
* ``admin`` — adds backend identity, workflow id, lifecycle/health state,
  resource estimates, and policy source labels.
* ``internal`` — everything ``admin`` sees, plus raw policy reasons and
  any provenance fields populated on the record.

Policy integration is by value: callers pass a :class:`PolicyDecision` (or
an :class:`AppProfile` plus :class:`PolicyRequest` they evaluated elsewhere)
into :func:`capability_with_policy_view`. Capabilities themselves never call
the evaluator — that would re-couple discovery to live request context.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from gateway.discovery import WorkflowRecord
from gateway.policy import ALLOWED, AppProfile, PolicyDecision, PolicyRequest

VALID_AUDIENCES = ("client", "admin", "internal")

EXECUTION_MODES = ("sync", "async", "job")
LIFECYCLE_STATES = ("unknown", "cold", "warming", "warm", "stopping", "stopped")
HEALTH_STATES = ("unknown", "healthy", "degraded", "unhealthy")
EXPOSURE_CLASSES = ("default", "opt_in", "admin", "disabled")
TRUST_TIERS = ("stable", "experimental", "private", "admin")
RESOURCE_TIERS = ("interactive", "batch", "expensive")


@dataclass(frozen=True)
class ResourceEstimate:
    """Coarse, declarative resource estimate.

    All fields are optional. Estimates are *declared* by the adapter manifest
    or workflow author; they are not measured here. The router can compare
    these against a current claim/availability snapshot at scheduling time.
    """

    gpu_required: bool = False
    vram_gb_estimate: float | None = None
    duration_class: str | None = None
    max_parallel: int | None = None

    def to_summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"gpu_required": self.gpu_required}
        if self.vram_gb_estimate is not None:
            out["vram_gb_estimate"] = self.vram_gb_estimate
        if self.duration_class is not None:
            out["duration_class"] = self.duration_class
        if self.max_parallel is not None:
            out["max_parallel"] = self.max_parallel
        return out


@dataclass(frozen=True)
class Capability:
    """A single routing-eligibility fact.

    Field set matches ``docs/CORE_CONTRACTS.md`` §Capability "Minimum
    eligibility fields for routing":

    * intent
    * input/output modalities
    * execution mode
    * streaming support
    * lifecycle state
    * health availability
    * policy availability
    * resource estimate
    * caveats that affect correctness

    Plus identity (``backend``, optional ``model``/``workflow``) so the router
    can pick one row out of a candidate set.

    All collection fields are tuples so the record is hashable and order is
    preserved for deterministic summaries.
    """

    intent: str
    backend: str
    model: str | None = None
    workflow: str | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    execution: str = "sync"
    streaming: bool = False
    exposure: str = "default"
    trust_tier: str = "stable"
    resource_tier: str = "interactive"
    tags: tuple[str, ...] = ()
    lifecycle: str = "unknown"
    health: str = "unknown"
    policy_available: bool = True
    resources: ResourceEstimate = field(default_factory=ResourceEstimate)
    caveats: tuple[str, ...] = ()
    policy_sources: tuple[str, ...] = ()
    policy_reasons: tuple[str, ...] = ()
    # Published numeric/string limits as (name, value) pairs (kept a tuple so the record stays
    # hashable). Video capabilities carry ``max_total_seconds`` and ``segment_seconds`` here.
    limits: tuple[tuple[str, Any], ...] = ()
    backend_kind: str | None = None
    cost_class: str | None = None
    engine: str | None = None
    latency_class: str | None = None
    calibrated: bool | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.execution not in EXECUTION_MODES:
            raise ValueError(
                f"execution must be one of {EXECUTION_MODES!r}, got {self.execution!r}"
            )
        if self.lifecycle not in LIFECYCLE_STATES:
            raise ValueError(
                f"lifecycle must be one of {LIFECYCLE_STATES!r}, got {self.lifecycle!r}"
            )
        if self.health not in HEALTH_STATES:
            raise ValueError(
                f"health must be one of {HEALTH_STATES!r}, got {self.health!r}"
            )
        if self.exposure not in EXPOSURE_CLASSES:
            raise ValueError(
                f"exposure must be one of {EXPOSURE_CLASSES!r}, got {self.exposure!r}"
            )
        if self.trust_tier not in TRUST_TIERS:
            raise ValueError(
                f"trust_tier must be one of {TRUST_TIERS!r}, got {self.trust_tier!r}"
            )
        if self.resource_tier not in RESOURCE_TIERS:
            raise ValueError(
                "resource_tier must be one of "
                f"{RESOURCE_TIERS!r}, got {self.resource_tier!r}"
            )
        if isinstance(self.tags, (str, bytes)):
            raise ValueError("tags must be an iterable of strings")
        tags = tuple(dict.fromkeys(self.tags))
        if not all(isinstance(tag, str) for tag in tags):
            raise ValueError("tags must be an iterable of strings")
        object.__setattr__(self, "tags", tags)

    @property
    def available(self) -> bool:
        """Coarse availability flag exposed to clients.

        Available means: policy permits the capability, lifecycle is not
        stopped, and health is not unhealthy. ``unknown`` lifecycle/health
        is treated as available — discovery should not hide capabilities
        just because a probe hasn't run yet.
        """
        if not self.policy_available:
            return False
        if self.lifecycle == "stopped":
            return False
        if self.health == "unhealthy":
            return False
        return True

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        """Deterministic ordering tuple.

        Sorted by intent, then backend, then model/workflow. ``None`` model
        and workflow sort before any populated value so a generic backend
        capability appears before the same backend's model-specific entry.
        """
        return (
            self.intent,
            self.backend,
            self.model or "",
            self.workflow or "",
        )

    def with_policy_decision(self, decision: PolicyDecision) -> "Capability":
        """Return a copy reflecting an evaluated :class:`PolicyDecision`.

        ``policy_available`` becomes ``True`` iff the decision was ``allowed``.
        Source labels and reasons are carried for receipt-style summaries.
        """
        return replace(
            self,
            policy_available=decision.decision == ALLOWED,
            policy_sources=tuple(decision.sources),
            policy_reasons=tuple(decision.reasons),
        )

    def with_caveats(self, *extra: str) -> "Capability":
        merged: list[str] = list(self.caveats)
        for caveat in extra:
            if caveat and caveat not in merged:
                merged.append(caveat)
        return replace(self, caveats=tuple(merged))


def summarize_capability(capability: Capability, audience: str) -> dict[str, Any]:
    """Project a capability into an audience-scoped summary dict."""
    _check_audience(audience)

    summary: dict[str, Any] = {
        "intent": capability.intent,
        "input_modalities": list(capability.input_modalities),
        "output_modalities": list(capability.output_modalities),
        "execution": capability.execution,
        "streaming": capability.streaming,
        "exposure": capability.exposure,
        "trust_tier": capability.trust_tier,
        "resource_tier": capability.resource_tier,
        "tags": list(capability.tags),
        "available": capability.available,
        "caveats": list(capability.caveats),
    }
    for field_name in ("backend_kind", "cost_class", "engine", "latency_class", "calibrated", "reason"):
        value = getattr(capability, field_name)
        if value is not None:
            summary[field_name] = value
    if capability.model is not None:
        summary["model"] = capability.model
    if capability.workflow is not None:
        summary["workflow"] = capability.workflow
    if capability.limits:
        summary["limits"] = dict(capability.limits)

    if audience == "client":
        return summary

    summary["backend"] = capability.backend
    summary["lifecycle"] = capability.lifecycle
    summary["health"] = capability.health
    summary["policy_available"] = capability.policy_available
    summary["resources"] = capability.resources.to_summary()
    summary["policy_sources"] = list(capability.policy_sources)

    if audience == "internal":
        summary["policy_reasons"] = list(capability.policy_reasons)

    return summary


def summarize_capabilities(
    capabilities: Iterable[Capability], audience: str
) -> dict[str, Any]:
    """Audience-scoped catalog payload with deterministic ordering."""
    _check_audience(audience)
    ordered = sorted(capabilities, key=lambda c: c.sort_key)
    return {
        "schema_version": 1,
        "audience": audience,
        "capabilities": [summarize_capability(c, audience) for c in ordered],
    }


def capability_from_workflow_record(
    record: WorkflowRecord,
    *,
    backend: str | None = None,
    streaming: bool = False,
    execution: str = "job",
    resources: ResourceEstimate | None = None,
    extra_caveats: Iterable[str] = (),
) -> Capability:
    """Derive a :class:`Capability` from a :class:`WorkflowRecord`.

    Output modalities come from the manifest output media types
    (``image/*`` → ``image``, ``audio/*`` → ``audio``, ``video/*`` → ``video``,
    ``text/*`` → ``text``). Input modalities are derived from input ``type``
    declarations (``asset`` → ``image``/``audio``/``video`` is *not* inferred
    here; we default to ``image`` only when the intent name starts with
    ``image.``, otherwise the asset type is left untagged and surfaces as the
    generic ``asset`` modality so the router does not over-promise).

    Workflows are treated as ``job`` execution by default because they back
    the durable media job surface (``POST /gateway/v1/media/jobs``). Callers
    that know better can override.

    ``backend`` selects which binding is canonical for this capability. When
    omitted, the first sorted ``supported_backends`` entry is used. Unknown
    backends raise :class:`ValueError`.
    """
    if execution not in EXECUTION_MODES:
        raise ValueError(
            f"execution must be one of {EXECUTION_MODES!r}, got {execution!r}"
        )

    supported = record.supported_backends
    if not supported:
        raise ValueError(
            f"workflow {record.id!r} has no bindings; cannot derive capability"
        )
    chosen_backend = backend or supported[0]
    if chosen_backend not in supported:
        raise ValueError(
            f"backend {chosen_backend!r} not in workflow bindings {supported!r}"
        )

    manifest = record.manifest
    inputs = manifest.get("inputs") or {}
    outputs = manifest.get("outputs") or {}

    input_modalities = _derive_input_modalities(inputs, intent=record.intent)
    output_modalities = _derive_output_modalities(outputs)

    caveats: list[str] = list(extra_caveats)
    if not record.identity.trusted:
        caveats.append("workflow manifest is untrusted")

    return Capability(
        intent=record.intent,
        backend=chosen_backend,
        workflow=record.id,
        input_modalities=input_modalities,
        output_modalities=output_modalities,
        execution=execution,
        streaming=streaming,
        exposure=_manifest_string(manifest, "exposure", default="default"),
        trust_tier=_manifest_string(
            manifest,
            "trust_tier",
            default="stable" if record.identity.trusted else "experimental",
        ),
        resource_tier=_manifest_string(
            manifest,
            "resource_tier",
            default="batch" if execution == "job" else "interactive",
        ),
        tags=_manifest_tags(manifest),
        lifecycle="unknown",
        health="unknown",
        policy_available=True,
        resources=resources or ResourceEstimate(gpu_required=True),
        caveats=tuple(caveats),
    )


def capability_with_policy_view(
    capability: Capability,
    profile: AppProfile,
    request: PolicyRequest | None = None,
) -> Capability:
    """Return ``capability`` annotated with a policy view for ``profile``.

    Builds a minimal :class:`PolicyRequest` from the capability identity
    (``backend``, ``model``, ``workflow``) when one isn't provided, evaluates
    the profile against it via :func:`gateway.policy.evaluate`, and stamps
    the result onto the capability via :meth:`Capability.with_policy_decision`.

    This is the integration point with the policy module. Capabilities do
    not import or run the evaluator themselves at construction time —
    discovery passes them through this helper when an audience-scoped view
    is requested for a specific profile.
    """
    from gateway.policy import evaluate

    effective_request = request or PolicyRequest(
        intent=capability.intent,
        workflow=capability.workflow,
        model=capability.model,
        backend=capability.backend,
        capability_exposure=capability.exposure,
        capability_trust_tier=capability.trust_tier,
        capability_resource_tier=capability.resource_tier,
        capability_tags=frozenset(capability.tags),
    )
    decision = evaluate(profile, effective_request)
    return capability.with_policy_decision(decision)


def unavailable_capability(
    intent: str,
    backend: str,
    *,
    reason: str,
    model: str | None = None,
    workflow: str | None = None,
    lifecycle: str = "stopped",
) -> Capability:
    """Build a capability record that is explicitly unavailable.

    Used when an adapter is configured but not currently routable (e.g. a
    cold backend that's intentionally not auto-started, or a workflow whose
    backend binding is missing). The router keeps the record so callers can
    *see* the capability and its reason instead of getting a silent miss.
    """
    if lifecycle not in LIFECYCLE_STATES:
        raise ValueError(
            f"lifecycle must be one of {LIFECYCLE_STATES!r}, got {lifecycle!r}"
        )
    return Capability(
        intent=intent,
        backend=backend,
        model=model,
        workflow=workflow,
        lifecycle=lifecycle,
        health="unknown",
        policy_available=True,
        caveats=(reason,),
    )


def _derive_output_modalities(outputs: dict[str, Any]) -> tuple[str, ...]:
    modalities: list[str] = []
    for spec in outputs.values():
        if not isinstance(spec, dict):
            continue
        media_type = str(spec.get("media_type") or "")
        modality = _media_type_modality(media_type)
        if modality and modality not in modalities:
            modalities.append(modality)
    return tuple(modalities)


def _derive_input_modalities(
    inputs: dict[str, Any], *, intent: str
) -> tuple[str, ...]:
    modalities: list[str] = []
    asset_modality = _intent_asset_modality(intent)
    for spec in inputs.values():
        if not isinstance(spec, dict):
            continue
        type_name = str(spec.get("type") or "string")
        if type_name == "string":
            if "text" not in modalities:
                modalities.append("text")
        elif type_name == "asset":
            picked = asset_modality or "asset"
            if picked not in modalities:
                modalities.append(picked)
    return tuple(modalities)


def _media_type_modality(media_type: str) -> str | None:
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("audio/"):
        return "audio"
    if media_type.startswith("video/"):
        return "video"
    if media_type.startswith("text/") or media_type in {"application/json"}:
        return "text"
    return None


def _intent_asset_modality(intent: str) -> str | None:
    if intent.startswith("image."):
        return "image"
    if intent.startswith("audio.") or intent.startswith("speech.") or intent.startswith("sound.") or intent.startswith("music."):
        return "audio"
    if intent.startswith("video."):
        return "video"
    return None


def _manifest_string(
    manifest: dict[str, Any], key: str, *, default: str
) -> str:
    value = manifest.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _manifest_tags(manifest: dict[str, Any]) -> tuple[str, ...]:
    value = manifest.get("tags")
    if not isinstance(value, list):
        return ()
    tags: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in tags:
            tags.append(item)
    return tuple(tags)


def _check_audience(audience: str) -> None:
    if audience not in VALID_AUDIENCES:
        raise ValueError(
            f"audience must be one of {VALID_AUDIENCES!r}, got {audience!r}"
        )


__all__ = [
    "Capability",
    "ResourceEstimate",
    "VALID_AUDIENCES",
    "EXECUTION_MODES",
    "LIFECYCLE_STATES",
    "HEALTH_STATES",
    "EXPOSURE_CLASSES",
    "TRUST_TIERS",
    "RESOURCE_TIERS",
    "capability_from_workflow_record",
    "capability_with_policy_view",
    "summarize_capability",
    "summarize_capabilities",
    "unavailable_capability",
]
