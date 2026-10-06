"""App profile policy evaluator.

Pure decision logic. Given an :class:`AppProfile` and a :class:`PolicyRequest`,
return a :class:`PolicyDecision` describing whether the requested action is
allowed and why. Decisions are receipt-compatible: ``decision``, ``sources``,
and ``reasons`` map directly onto :mod:`gateway.receipts` fields.

This module has no dependencies on live auth, request handlers, or
``proxy.py``. It does not read configuration from disk. Callers wire an
:class:`AppProfile` in and consume the :class:`PolicyDecision` out.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Iterable


ALLOWED = "allowed"
DENIED = "denied"
NOT_EVALUATED = "not_evaluated"

VISIBILITY_LEVELS = ("none", "basic", "standard", "full")
TARGET_VISIBILITY_MODES = ("catalog", "available")
TARGET_ACTIVATION_MODES = ("may_activate", "existing_only")
MODEL_ACCESS_MODES = ("catalog", "current_loaded")
MEDIA_METADATA_MODES = ("full", "none")

DEFAULT_PROFILE_ID = "default-local"


def _canonical_provider_id(value: object) -> str:
    from gateway.service_registry import canonical_provider_id

    return canonical_provider_id(value)


def default_profile_id() -> str:
    """Return the one runtime authority for the built-in fallback identity."""
    return DEFAULT_PROFILE_ID


def is_default_profile_id(profile_id: object) -> bool:
    """Whether ``profile_id`` denotes the built-in fallback profile."""
    return str(profile_id or "").strip() == default_profile_id()


@dataclass(frozen=True)
class CapabilitySelector:
    """Category selectors used to keep profiles from becoming ID lists.

    ``None`` means "no opinion" and an empty set means "deny all", matching
    the existing profile allow-list semantics.
    """

    intents: frozenset[str] | None = None
    exposure: frozenset[str] | None = None
    trust_tiers: frozenset[str] | None = None
    resource_tiers: frozenset[str] | None = None
    tags: frozenset[str] | None = None


@dataclass(frozen=True)
class FallbackPolicy:
    cloud_allowed: bool = False


@dataclass(frozen=True)
class RemoteProviderPolicy:
    providers: frozenset[str] = field(default_factory=frozenset)
    max_usd_per_request: Decimal | None = None
    budget_usd_per_day: Decimal | None = None
    budget_usd_per_month: Decimal | None = None
    allow_paid_fallback: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "providers",
            frozenset(
                provider
                for provider in (
                    _canonical_provider_id(item) for item in self.providers
                )
                if provider
            ),
        )


@dataclass(frozen=True)
class NativePassthroughPolicy:
    enabled: bool = False
    allowed_backends: frozenset[str] = field(default_factory=frozenset)
    allowed_operations: frozenset[str] = field(default_factory=frozenset)
    audit: bool = True


@dataclass(frozen=True)
class StatusVisibilityPolicy:
    level: str = "basic"

    def __post_init__(self) -> None:
        if self.level not in VISIBILITY_LEVELS:
            raise ValueError(
                f"status visibility level must be one of {VISIBILITY_LEVELS}"
            )


@dataclass(frozen=True)
class TargetPolicy:
    """Runtime target discovery and activation behavior.

    ``visibility`` controls whether clients see configured targets or only
    targets currently available under the live runtime. ``activation`` controls
    whether a request may load/switch/queue a target, or must use an existing
    running/resident/selected target. The runtime interprets these axes for
    each target family; policy.py only carries the operator's intent.
    """

    visibility: str = "catalog"
    activation: str = "may_activate"

    def __post_init__(self) -> None:
        if self.visibility not in TARGET_VISIBILITY_MODES:
            raise ValueError(
                f"target visibility must be one of {TARGET_VISIBILITY_MODES}"
            )
        if self.activation not in TARGET_ACTIVATION_MODES:
            raise ValueError(
                f"target activation must be one of {TARGET_ACTIVATION_MODES}"
            )


@dataclass(frozen=True)
class ModelAccessPolicy:
    """Backward-compatible alias for older LLM-only profile configs."""

    mode: str = "catalog"

    def __post_init__(self) -> None:
        if self.mode not in MODEL_ACCESS_MODES:
            raise ValueError(
                f"model access mode must be one of {MODEL_ACCESS_MODES}"
            )


@dataclass(frozen=True)
class MediaMetadataPolicy:
    """Default for the metadata embedded in generated media bytes at delivery.

    ``full`` delivers the stored copy untouched (ComfyUI ``prompt``/``workflow``
    plus the gateway provenance record). ``none`` strips every metadata block
    from the delivered bytes. A request can override this per call with
    ``extensions["com.azizam.workflow"].metadata``. ``none`` is the safe
    default: only profiles that opt in deliver workflow data.
    """

    default: str = "none"

    def __post_init__(self) -> None:
        if self.default not in MEDIA_METADATA_MODES:
            raise ValueError(
                f"media metadata default must be one of {MEDIA_METADATA_MODES}"
            )


def target_policy_from_model_access(model_access: ModelAccessPolicy) -> TargetPolicy:
    if model_access.mode == "current_loaded":
        return TargetPolicy(visibility="available", activation="existing_only")
    return TargetPolicy()


@dataclass(frozen=True)
class AppProfile:
    """Named policy bundle attached to an API key or client identity.

    ``None`` allow-lists mean "no opinion": a profile that does not enumerate
    workflows allows any workflow unless another rule denies it. An empty
    frozenset means "deny all" for that dimension.
    """

    id: str = DEFAULT_PROFILE_ID
    protocols: frozenset[str] | None = None
    workflows: frozenset[str] | None = None
    models: frozenset[str] | None = None
    backends: frozenset[str] | None = None
    allow: CapabilitySelector = field(default_factory=CapabilitySelector)
    fallback: FallbackPolicy = field(default_factory=FallbackPolicy)
    native_passthrough: NativePassthroughPolicy = field(
        default_factory=NativePassthroughPolicy
    )
    remote: RemoteProviderPolicy = field(default_factory=RemoteProviderPolicy)
    status_visibility: StatusVisibilityPolicy = field(
        default_factory=StatusVisibilityPolicy
    )
    target_policy: TargetPolicy = field(default_factory=TargetPolicy)
    model_access: ModelAccessPolicy = field(default_factory=ModelAccessPolicy)
    media_metadata: MediaMetadataPolicy = field(default_factory=MediaMetadataPolicy)

    def __post_init__(self) -> None:
        if (
            self.target_policy == TargetPolicy()
            and self.model_access.mode == "current_loaded"
        ):
            object.__setattr__(
                self,
                "target_policy",
                target_policy_from_model_access(self.model_access),
            )

    def with_overrides(self, **changes: object) -> "AppProfile":
        return replace(self, **changes)


@dataclass(frozen=True)
class PolicyRequest:
    """Inputs the policy evaluator reasons about for a single request."""

    protocol: str | None = None
    intent: str | None = None
    workflow: str | None = None
    model: str | None = None
    backend: str | None = None
    capability_exposure: str | None = None
    capability_trust_tier: str | None = None
    capability_resource_tier: str | None = None
    capability_tags: frozenset[str] | None = None
    requires_cloud_fallback: bool = False
    native_passthrough: bool = False
    native_operation: str | None = None
    native_backend: str | None = None
    remote_provider: str | None = None
    remote_paid_fallback: bool = False

    def __post_init__(self) -> None:
        if self.capability_tags is None:
            return
        if isinstance(self.capability_tags, (str, bytes)):
            raise ValueError("capability_tags must be an iterable of strings")
        tags = frozenset(self.capability_tags)
        if not all(isinstance(tag, str) for tag in tags):
            raise ValueError("capability_tags must be an iterable of strings")
        object.__setattr__(self, "capability_tags", tags)


@dataclass(frozen=True)
class PolicyDecision:
    """Receipt-compatible policy decision."""

    decision: str
    sources: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    visibility_level: str = "basic"

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOWED

    @property
    def denied(self) -> bool:
        return self.decision == DENIED

    def receipt_fields(self) -> dict[str, object]:
        """Return a kwargs dict suitable for :meth:`RouteReceipt.set_policy_decision`-like usage.

        The caller decides whether to fold ``visibility_level`` into the
        receipt's ``visibility`` field; this module does not import receipts.
        """
        return {
            "decision": self.decision,
            "sources": list(self.sources),
            "reasons": list(self.reasons),
            "visibility_level": self.visibility_level,
        }


DEFAULT_PROFILE = AppProfile()


def _profile_source(profile: AppProfile) -> str:
    return f"profile:{profile.id}"


def _denied(
    profile: AppProfile,
    reasons: Iterable[str],
    *,
    extra_sources: Iterable[str] = (),
) -> PolicyDecision:
    sources = (_profile_source(profile), *extra_sources)
    return PolicyDecision(
        decision=DENIED,
        sources=sources,
        reasons=tuple(reasons),
        visibility_level=profile.status_visibility.level,
    )


def _allowed(
    profile: AppProfile,
    reasons: Iterable[str],
    *,
    extra_sources: Iterable[str] = (),
) -> PolicyDecision:
    sources = (_profile_source(profile), *extra_sources)
    return PolicyDecision(
        decision=ALLOWED,
        sources=sources,
        reasons=tuple(reasons),
        visibility_level=profile.status_visibility.level,
    )


def _matches_scope(value: str, allowed: frozenset[str]) -> bool:
    """Return True when ``value`` matches an exact or family allow-list item."""
    return value in allowed or any(value.startswith(f"{item}.") for item in allowed)


def _matches_any(value: str, allowed: frozenset[str]) -> bool:
    return value in allowed


def _matches_native_operation(value: str, allowed: frozenset[str]) -> bool:
    """Match native passthrough operations.

    Native operation scopes are intentionally small: exact strings, ``*``,
    method-wide scopes such as ``GET *``, and prefix scopes ending in ``*``.
    Path-only legacy scopes (``/v1/models`` or ``v1/models``) also match a
    method-qualified request such as ``GET /v1/models``.
    """
    if not allowed:
        return True
    if "*" in allowed or value in allowed:
        return True

    method: str | None = None
    path = value
    if " " in value:
        method, path = value.split(" ", 1)
        method = method.upper()
        if f"{method} *" in allowed:
            return True

    path_variants = {path, path.lstrip("/"), f"/{path.lstrip('/')}"}
    if path_variants & allowed:
        return True

    for scope in allowed:
        if not scope.endswith("*"):
            continue
        prefix = scope[:-1]
        if value.startswith(prefix):
            return True
        for variant in path_variants:
            if variant.startswith(prefix):
                return True
            if variant.startswith(prefix.lstrip("/")):
                return True
    return False


def _matches_any_tag(tags: frozenset[str], allowed: frozenset[str]) -> bool:
    return bool(tags & allowed)


def evaluate(profile: AppProfile, request: PolicyRequest) -> PolicyDecision:
    """Evaluate a request against a profile.

    Conservative order:

    1. Protocol allow-list
    2. Workflow allow-list (when configured)
    3. Model allow-list (when configured)
    4. Backend allow-list (when configured)
    5. Capability metadata selectors (when configured and supplied)
    6. Cloud fallback
    7. Remote paid provider access
    8. Native passthrough (enabled + allowed backend + allowed operation)

    The first denial wins. An allow-list of ``None`` means "no opinion";
    an empty frozenset means "deny all".
    """
    reasons: list[str] = []

    if profile.protocols is not None and request.protocol is not None:
        if not _matches_scope(request.protocol, profile.protocols):
            return _denied(
                profile,
                [f"protocol {request.protocol!r} not permitted by profile"],
            )
        reasons.append(f"protocol {request.protocol!r} permitted")

    if profile.workflows is not None and request.workflow is not None:
        if request.workflow not in profile.workflows:
            return _denied(
                profile,
                [f"workflow {request.workflow!r} not permitted by profile"],
            )
        reasons.append(f"workflow {request.workflow!r} permitted")

    if profile.models is not None and request.model is not None:
        if request.model not in profile.models:
            return _denied(
                profile,
                [f"model {request.model!r} not permitted by profile"],
            )
        reasons.append(f"model {request.model!r} permitted")

    if profile.backends is not None and request.backend is not None:
        if request.backend not in profile.backends:
            return _denied(
                profile,
                [f"backend {request.backend!r} not permitted by profile"],
            )
        reasons.append(f"backend {request.backend!r} permitted")

    selector_decision = _evaluate_allow_selectors(profile, request)
    if selector_decision.denied:
        return selector_decision
    reasons.extend(selector_decision.reasons)

    if request.requires_cloud_fallback and not profile.fallback.cloud_allowed:
        return _denied(
            profile,
            ["cloud fallback disabled by profile"],
            extra_sources=("policy:local-only",),
        )

    if request.remote_provider is not None:
        remote_decision = _evaluate_remote(profile, request)
        if remote_decision.denied:
            return remote_decision
        reasons.extend(remote_decision.reasons)

    if request.native_passthrough:
        native_decision = _evaluate_native(profile, request)
        if native_decision.denied:
            return native_decision
        reasons.extend(native_decision.reasons)

    if not reasons:
        reasons.append("no restrictions configured")
    return _allowed(profile, reasons)


def _evaluate_allow_selectors(
    profile: AppProfile, request: PolicyRequest
) -> PolicyDecision:
    allow = profile.allow
    reasons: list[str] = []

    if allow.intents is not None and request.intent is not None:
        if not _matches_scope(request.intent, allow.intents):
            return _denied(
                profile,
                [f"intent {request.intent!r} not permitted by profile"],
                extra_sources=("policy:capability-selectors",),
            )
        reasons.append(f"intent {request.intent!r} permitted")

    if allow.exposure is not None and request.capability_exposure is not None:
        exposure = request.capability_exposure
        if not _matches_any(exposure, allow.exposure):
            return _denied(
                profile,
                [f"exposure {exposure!r} not permitted by profile"],
                extra_sources=("policy:capability-selectors",),
            )
        reasons.append(f"exposure {exposure!r} permitted")

    if allow.trust_tiers is not None and request.capability_trust_tier is not None:
        trust_tier = request.capability_trust_tier
        if not _matches_any(trust_tier, allow.trust_tiers):
            return _denied(
                profile,
                [f"trust tier {trust_tier!r} not permitted by profile"],
                extra_sources=("policy:capability-selectors",),
            )
        reasons.append(f"trust tier {trust_tier!r} permitted")

    if (
        allow.resource_tiers is not None
        and request.capability_resource_tier is not None
    ):
        resource_tier = request.capability_resource_tier
        if not _matches_any(resource_tier, allow.resource_tiers):
            return _denied(
                profile,
                [f"resource tier {resource_tier!r} not permitted by profile"],
                extra_sources=("policy:capability-selectors",),
            )
        reasons.append(f"resource tier {resource_tier!r} permitted")

    if allow.tags is not None and request.capability_tags is not None:
        if not _matches_any_tag(request.capability_tags, allow.tags):
            return _denied(
                profile,
                ["capability tags not permitted by profile"],
                extra_sources=("policy:capability-selectors",),
            )
        reasons.append("capability tag permitted")

    return _allowed(
        profile,
        reasons,
        extra_sources=("policy:capability-selectors",) if reasons else (),
    )


def _evaluate_remote(profile: AppProfile, request: PolicyRequest) -> PolicyDecision:
    remote = profile.remote
    provider = request.remote_provider
    if provider is None:
        return _allowed(profile, ())
    provider = _canonical_provider_id(provider)
    if provider not in remote.providers:
        return _denied(
            profile,
            [f"remote provider {provider!r} not permitted by profile"],
            extra_sources=("policy:remote-provider",),
        )
    if request.remote_paid_fallback and not remote.allow_paid_fallback:
        return _denied(
            profile,
            ["paid fallback disabled by profile"],
            extra_sources=("policy:remote-provider",),
        )
    return _allowed(
        profile,
        [f"remote provider {provider!r} permitted"],
        extra_sources=("policy:remote-provider",),
    )


def _evaluate_native(profile: AppProfile, request: PolicyRequest) -> PolicyDecision:
    np = profile.native_passthrough
    if not np.enabled:
        return _denied(
            profile,
            ["native passthrough disabled by profile"],
            extra_sources=("policy:native-passthrough",),
        )
    backend = request.native_backend or request.backend
    if backend is not None and np.allowed_backends and backend not in np.allowed_backends:
        return _denied(
            profile,
            [f"native passthrough backend {backend!r} not in allow-list"],
            extra_sources=("policy:native-passthrough",),
        )
    if (
        request.native_operation is not None
        and np.allowed_operations
        and not _matches_native_operation(
            request.native_operation, np.allowed_operations
        )
    ):
        return _denied(
            profile,
            [
                f"native passthrough operation {request.native_operation!r} "
                "not in allow-list"
            ],
            extra_sources=("policy:native-passthrough",),
        )
    return _allowed(
        profile,
        ["native passthrough permitted"],
        extra_sources=("policy:native-passthrough",),
    )


def visibility_for(profile: AppProfile) -> str:
    """Return the receipt visibility level (basic/standard/full/none) for a profile."""
    return profile.status_visibility.level


def apply_to_receipt(decision: PolicyDecision, receipt: object) -> None:
    """Apply a decision to a receipt-like object without importing receipts.

    Uses duck typing so this module stays decoupled. Calls
    ``receipt.set_policy_decision(decision, reason=..., source=...)`` once per
    reason/source pair, mirroring how a caller would record the decision today.
    """
    set_decision = getattr(receipt, "set_policy_decision", None)
    if not callable(set_decision):
        raise TypeError("receipt must expose set_policy_decision(decision, ...)")

    sources = list(decision.sources) or [None]
    reasons = list(decision.reasons) or [None]
    pairs = max(len(sources), len(reasons))
    for index in range(pairs):
        set_decision(
            decision.decision,
            reason=reasons[index] if index < len(reasons) else None,
            source=sources[index] if index < len(sources) else None,
        )
    if hasattr(receipt, "visibility"):
        setattr(receipt, "visibility", decision.visibility_level)
