"""App profile loader.

Pure mapping from plain configuration dicts into
:class:`gateway.policy.AppProfile` instances.

This module:

* never reads files or environment variables itself,
* never imports :mod:`proxy`,
* makes no network or secret lookups,
* falls back to :data:`gateway.policy.DEFAULT_PROFILE` when an entry has no
  ``profile`` block.

Callers are expected to read database/config data and pass already-parsed dicts
to :func:`load_profile_from_entry` or :func:`load_profiles_from_entries`.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

from gateway.policy import (
    DEFAULT_PROFILE,
    DEFAULT_PROFILE_ID,
    MEDIA_METADATA_MODES,
    TARGET_ACTIVATION_MODES,
    TARGET_VISIBILITY_MODES,
    VISIBILITY_LEVELS,
    AppProfile,
    CapabilitySelector,
    FallbackPolicy,
    MediaMetadataPolicy,
    NativePassthroughPolicy,
    ModelAccessPolicy,
    RemoteProviderPolicy,
    StatusVisibilityPolicy,
    TargetPolicy,
    target_policy_from_model_access,
)
from gateway.plan_vocabulary import canonical_intent

__all__ = [
    "ProfileLoadError",
    "load_profile",
    "load_profile_from_entry",
    "load_profiles_from_entries",
    "profile_for_key_entry",
    "serialize_default_profile",
]

_PROFILE_KEYS = {
    "id",
    "protocols",
    "workflows",
    "models",
    "backends",
    "allow",
    "fallback",
    "native_passthrough",
    "remote",
    "status_visibility",
    "target_policy",
    "model_access",
    "media_metadata",
}

_ALLOW_KEYS = {
    "intents",
    "exposure",
    "trust_tiers",
    "resource_tiers",
    "tags",
}


class ProfileLoadError(ValueError):
    """Raised when a profile dict cannot be loaded into an AppProfile."""


def serialize_default_profile() -> dict[str, object]:
    """Return a document with exactly ``DEFAULT_PROFILE`` policy semantics."""
    profile = DEFAULT_PROFILE
    document: dict[str, object] = {
        "fallback": {"cloud_allowed": profile.fallback.cloud_allowed},
        "native_passthrough": {
            "enabled": profile.native_passthrough.enabled,
            "allowed_backends": sorted(profile.native_passthrough.allowed_backends),
            "allowed_operations": sorted(profile.native_passthrough.allowed_operations),
            "audit": profile.native_passthrough.audit,
        },
        "remote": {
            "providers": sorted(profile.remote.providers),
            "max_usd_per_request": (
                str(profile.remote.max_usd_per_request)
                if profile.remote.max_usd_per_request is not None
                else None
            ),
            "budget_usd_per_day": (
                str(profile.remote.budget_usd_per_day)
                if profile.remote.budget_usd_per_day is not None
                else None
            ),
            "budget_usd_per_month": (
                str(profile.remote.budget_usd_per_month)
                if profile.remote.budget_usd_per_month is not None
                else None
            ),
            "allow_paid_fallback": profile.remote.allow_paid_fallback,
        },
        "status_visibility": {"level": profile.status_visibility.level},
        "media_metadata": {"default": profile.media_metadata.default},
        "target_policy": {
            "visibility": profile.target_policy.visibility,
            "activation": profile.target_policy.activation,
        },
    }
    for field_name, values in (
        ("protocols", profile.protocols), ("workflows", profile.workflows),
        ("models", profile.models), ("backends", profile.backends),
    ):
        if values is not None:
            document[field_name] = sorted(values)
    allow = {key: sorted(values) for key, values in (
        ("intents", profile.allow.intents), ("exposure", profile.allow.exposure),
        ("trust_tiers", profile.allow.trust_tiers), ("resource_tiers", profile.allow.resource_tiers),
        ("tags", profile.allow.tags),
    ) if values is not None}
    if allow:
        document["allow"] = allow
    return document


def _require_mapping(value: object, *, where: str) -> Mapping[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProfileLoadError(
            f"{where} must be a mapping, got {type(value).__name__}"
        )
    return value


def _coerce_str_set(
    value: object, *, where: str
) -> frozenset[str] | None:
    """Coerce an optional sequence of strings to a frozenset.

    ``None`` and missing keys mean "no opinion" (returns ``None``). Empty
    sequences mean "deny all" (returns an empty frozenset).
    """
    if value is None:
        return None
    if (
        isinstance(value, (str, bytes, Mapping))
        or not isinstance(value, Iterable)
    ):
        raise ProfileLoadError(
            f"{where} must be a list of strings, got {type(value).__name__}"
        )
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ProfileLoadError(
                f"{where}[{index}] must be a string, got {type(item).__name__}"
            )
        items.append(item)
    return frozenset(items)


def _load_fallback(value: object) -> FallbackPolicy:
    data = _require_mapping(value, where="fallback")
    if not data:
        return FallbackPolicy()
    cloud_allowed = data.get("cloud_allowed", False)
    if not isinstance(cloud_allowed, bool):
        raise ProfileLoadError(
            "fallback.cloud_allowed must be a boolean"
        )
    return FallbackPolicy(cloud_allowed=cloud_allowed)


def _load_capability_selector(value: object) -> CapabilitySelector:
    data = _require_mapping(value, where="allow")
    if not data:
        return CapabilitySelector()

    unknown = set(data.keys()) - _ALLOW_KEYS
    if unknown:
        raise ProfileLoadError(
            f"unknown allow keys: {sorted(unknown)}"
        )

    intents = _coerce_str_set(data.get("intents"), where="allow.intents")
    return CapabilitySelector(
        intents=None if intents is None else frozenset(canonical_intent(intent) for intent in intents),
        exposure=_coerce_str_set(data.get("exposure"), where="allow.exposure"),
        trust_tiers=_coerce_str_set(
            data.get("trust_tiers"), where="allow.trust_tiers"
        ),
        resource_tiers=_coerce_str_set(
            data.get("resource_tiers"), where="allow.resource_tiers"
        ),
        tags=_coerce_str_set(data.get("tags"), where="allow.tags"),
    )


def _load_native_passthrough(value: object) -> NativePassthroughPolicy:
    data = _require_mapping(value, where="native_passthrough")
    if not data:
        return NativePassthroughPolicy()

    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ProfileLoadError("native_passthrough.enabled must be a boolean")

    audit = data.get("audit", True)
    if not isinstance(audit, bool):
        raise ProfileLoadError("native_passthrough.audit must be a boolean")

    backends = _coerce_str_set(
        data.get("allowed_backends"),
        where="native_passthrough.allowed_backends",
    ) or frozenset()
    operations = _coerce_str_set(
        data.get("allowed_operations"),
        where="native_passthrough.allowed_operations",
    ) or frozenset()

    return NativePassthroughPolicy(
        enabled=enabled,
        allowed_backends=backends,
        allowed_operations=operations,
        audit=audit,
    )


def _coerce_optional_decimal(value: object, *, where: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ProfileLoadError(f"{where} must be a number or decimal string")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ProfileLoadError(f"{where} must be a valid decimal amount") from exc
    if not amount.is_finite():
        raise ProfileLoadError(f"{where} must be a finite decimal amount")
    if amount < 0:
        raise ProfileLoadError(f"{where} must be non-negative")
    return amount


def _load_remote(value: object) -> RemoteProviderPolicy:
    data = _require_mapping(value, where="remote")
    if not data:
        return RemoteProviderPolicy()
    unknown = set(data.keys()) - {
        "providers",
        "max_usd_per_request",
        "budget_usd_per_day",
        "budget_usd_per_month",
        "allow_paid_fallback",
    }
    if unknown:
        raise ProfileLoadError(f"unknown remote keys: {sorted(unknown)}")
    allow_paid_fallback = data.get("allow_paid_fallback", False)
    if not isinstance(allow_paid_fallback, bool):
        raise ProfileLoadError("remote.allow_paid_fallback must be a boolean")
    return RemoteProviderPolicy(
        providers=_coerce_str_set(data.get("providers"), where="remote.providers")
        or frozenset(),
        max_usd_per_request=_coerce_optional_decimal(
            data.get("max_usd_per_request"),
            where="remote.max_usd_per_request",
        ),
        budget_usd_per_day=_coerce_optional_decimal(
            data.get("budget_usd_per_day"),
            where="remote.budget_usd_per_day",
        ),
        budget_usd_per_month=_coerce_optional_decimal(
            data.get("budget_usd_per_month"),
            where="remote.budget_usd_per_month",
        ),
        allow_paid_fallback=allow_paid_fallback,
    )


def _load_status_visibility(value: object) -> StatusVisibilityPolicy:
    data = _require_mapping(value, where="status_visibility")
    if not data:
        return StatusVisibilityPolicy()
    level = data.get("level", "basic")
    if not isinstance(level, str):
        raise ProfileLoadError("status_visibility.level must be a string")
    if level not in VISIBILITY_LEVELS:
        raise ProfileLoadError(
            f"status_visibility.level must be one of {VISIBILITY_LEVELS}, "
            f"got {level!r}"
        )
    return StatusVisibilityPolicy(level=level)


def _load_model_access(value: object) -> ModelAccessPolicy:
    data = _require_mapping(value, where="model_access")
    if not data:
        return ModelAccessPolicy()
    mode = data.get("mode", "catalog")
    if not isinstance(mode, str):
        raise ProfileLoadError("model_access.mode must be a string")
    try:
        return ModelAccessPolicy(mode=mode)
    except ValueError as exc:
        raise ProfileLoadError(str(exc)) from exc


def _load_media_metadata(value: object) -> MediaMetadataPolicy:
    data = _require_mapping(value, where="media_metadata")
    if not data:
        return MediaMetadataPolicy()
    unknown = set(data.keys()) - {"default"}
    if unknown:
        raise ProfileLoadError(f"unknown media_metadata keys: {sorted(unknown)}")
    default = data.get("default", "none")
    if not isinstance(default, str) or default not in MEDIA_METADATA_MODES:
        raise ProfileLoadError(
            f"media_metadata.default must be one of {MEDIA_METADATA_MODES}, "
            f"got {default!r}"
        )
    return MediaMetadataPolicy(default=default)


def _load_target_policy(value: object) -> TargetPolicy:
    data = _require_mapping(value, where="target_policy")
    if not data:
        return TargetPolicy()
    visibility = data.get("visibility", "catalog")
    if not isinstance(visibility, str):
        raise ProfileLoadError("target_policy.visibility must be a string")
    activation = data.get("activation", "may_activate")
    if not isinstance(activation, str):
        raise ProfileLoadError("target_policy.activation must be a string")
    try:
        return TargetPolicy(visibility=visibility, activation=activation)
    except ValueError as exc:
        message = str(exc)
        if "visibility" in message:
            raise ProfileLoadError(
                f"target_policy.visibility must be one of {TARGET_VISIBILITY_MODES}"
            ) from exc
        if "activation" in message:
            raise ProfileLoadError(
                f"target_policy.activation must be one of {TARGET_ACTIVATION_MODES}"
            ) from exc
        raise ProfileLoadError(message) from exc


def load_profile(
    profile_data: object,
    *,
    default_id: str | None = None,
) -> AppProfile:
    """Build an :class:`AppProfile` from a plain ``profile`` block.

    Passing ``None`` or an empty mapping returns :data:`DEFAULT_PROFILE`
    (optionally with ``default_id`` substituted).
    """
    if profile_data is None or profile_data == {}:
        if default_id and default_id != DEFAULT_PROFILE_ID:
            return DEFAULT_PROFILE.with_overrides(id=default_id)
        return DEFAULT_PROFILE

    if not isinstance(profile_data, Mapping):
        raise ProfileLoadError(
            f"profile must be a mapping, got {type(profile_data).__name__}"
        )

    unknown = set(profile_data.keys()) - _PROFILE_KEYS
    if unknown:
        raise ProfileLoadError(
            f"unknown profile keys: {sorted(unknown)}"
        )

    profile_id_raw = profile_data.get("id", default_id or DEFAULT_PROFILE_ID)
    if not isinstance(profile_id_raw, str) or not profile_id_raw:
        raise ProfileLoadError("profile id must be a non-empty string")

    model_access = _load_model_access(profile_data.get("model_access"))
    target_policy_raw = profile_data.get("target_policy")
    target_policy = (
        _load_target_policy(target_policy_raw)
        if target_policy_raw is not None
        else target_policy_from_model_access(model_access)
    )

    return AppProfile(
        id=profile_id_raw,
        protocols=_coerce_str_set(
            profile_data.get("protocols"), where="protocols"
        ),
        workflows=_coerce_str_set(
            profile_data.get("workflows"), where="workflows"
        ),
        models=_coerce_str_set(profile_data.get("models"), where="models"),
        backends=_coerce_str_set(
            profile_data.get("backends"), where="backends"
        ),
        allow=_load_capability_selector(profile_data.get("allow")),
        fallback=_load_fallback(profile_data.get("fallback")),
        native_passthrough=_load_native_passthrough(
            profile_data.get("native_passthrough")
        ),
        remote=_load_remote(profile_data.get("remote")),
        status_visibility=_load_status_visibility(
            profile_data.get("status_visibility")
        ),
        target_policy=target_policy,
        model_access=model_access,
        media_metadata=_load_media_metadata(profile_data.get("media_metadata")),
    )


def load_profile_from_entry(entry: Mapping[str, object]) -> AppProfile:
    """Build an :class:`AppProfile` from a mapping with an optional profile.

    When the entry has no profile block, :data:`DEFAULT_PROFILE` (with the
    entry name as id when present) is returned. Any ``downstream`` block is
    ignored here; credential resolution stays in the auth layer.
    """
    if not isinstance(entry, Mapping):
        raise ProfileLoadError(
            f"entry must be a mapping, got {type(entry).__name__}"
        )

    name_raw = entry.get("name")
    if name_raw is not None and not isinstance(name_raw, str):
        raise ProfileLoadError("entry name must be a string when set")
    default_id = name_raw or None

    profile_block = entry.get("profile")
    return load_profile(profile_block, default_id=default_id)


def load_profiles_from_entries(
    entries: Iterable[Mapping[str, object]],
) -> dict[str, AppProfile]:
    """Build a ``name -> AppProfile`` map from a sequence of entries.

    Entries without a usable ``name`` are skipped. When two entries share a
    name, the last one wins — mirroring how :func:`load_proxy_key_entries`
    layers config-defined keys behind file-defined keys.
    """
    profiles: dict[str, AppProfile] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ProfileLoadError(
                f"entries must be mappings, got {type(entry).__name__}"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        profiles[name] = load_profile_from_entry(entry)
    return profiles


def profile_for_key_entry(
    entry: Mapping[str, object] | None,
) -> AppProfile:
    """Conservative lookup helper for ``proxy.py`` to call per-request.

    Accepts ``None`` (unknown / unauthenticated caller) and returns
    :data:`DEFAULT_PROFILE`. Any malformed entry falls back to the default
    profile rather than raising — auth-layer callers can pre-validate with
    :func:`load_profile_from_entry` at startup to surface errors loudly.
    """
    if entry is None:
        return DEFAULT_PROFILE
    try:
        return load_profile_from_entry(entry)
    except ProfileLoadError:
        return DEFAULT_PROFILE
