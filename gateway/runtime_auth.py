"""Runtime auth/profile bridge.

Pure mapping from existing proxy key-entry dicts (the rows already produced by
``proxy.load_proxy_key_entries``) to a runtime identity that carries:

* ``name``
* ``key_hash`` (normalized)
* ``downstream`` config dict — preserved verbatim, *not* read here
* ``profile`` — a loaded :class:`gateway.policy.AppProfile`

This module:

* never imports :mod:`proxy`,
* never reads files,
* never touches ``os.environ`` directly — callers inject an env mapping when
  resolving ``key_env`` entries,
* fails soft on malformed optional ``profile`` blocks (attaches
  :data:`DEFAULT_PROFILE`) unless the caller opts into ``strict`` validation.

The aim is to mirror :func:`proxy.build_auth_registry` semantics 1:1 — same
hash normalization, same precedence (last duplicate hash wins), same support
for ``key_hash`` / ``key`` / ``key_env`` — while attaching a policy profile
and exposing helpers that bridge into :mod:`gateway.policy` and
:mod:`gateway.receipts`.

``proxy.py`` wires the live handlers; this module stays pure and testable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from gateway.policy import (
    DEFAULT_PROFILE,
    AppProfile,
    PolicyDecision,
    PolicyRequest,
    apply_to_receipt,
    evaluate,
)
from gateway.profiles import (
    ProfileLoadError,
    load_profile_from_entry,
    profile_for_key_entry,
)

__all__ = [
    "RuntimeIdentity",
    "build_identity_registry",
    "discovery_audience_for_profile",
    "evaluate_and_apply",
    "hash_api_key",
    "key_hashes_for_entry",
    "make_policy_request",
    "normalize_key_hash",
    "runtime_identity_from_entry",
]


# ---------------------------------------------------------------------------
# Hashing — kept byte-for-byte compatible with proxy.hash_api_key /
# proxy.normalize_key_hash so an identity built here matches one built there.
# ---------------------------------------------------------------------------


def hash_api_key(api_key: str) -> str:
    """Hash a client-facing proxy key for storage and lookup."""
    return f"sha256:{hashlib.sha256(api_key.encode('utf-8')).hexdigest()}"


def normalize_key_hash(key_hash: str) -> str:
    """Ensure a stored hash carries the ``sha256:`` prefix."""
    if key_hash.startswith("sha256:"):
        return key_hash
    return f"sha256:{key_hash}"


# ---------------------------------------------------------------------------
# Runtime identity dataclass.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeIdentity:
    """Identity carried with an authenticated request.

    ``downstream`` is preserved as-is. Credential resolution is the auth
    layer's job — this module never reads secret values out of it.
    """

    name: str
    key_hash: str
    downstream: Mapping[str, object] = field(default_factory=dict)
    profile: AppProfile = DEFAULT_PROFILE


# ---------------------------------------------------------------------------
# Hash extraction — mirrors proxy.key_hashes_for_entry but takes an injected
# env mapping instead of reading os.environ.
# ---------------------------------------------------------------------------


def key_hashes_for_entry(
    entry: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Return every usable hash for one entry.

    Supports the same three shapes as ``proxy.key_hashes_for_entry``:

    * ``key_hash`` — already-hashed, normalized to ``sha256:...``
    * ``key`` — literal client key, hashed here
    * ``key_env`` — env var name; resolved via the injected ``env`` mapping
      and (if present) hashed. If the env var is missing and ``key`` is
      present, ``key`` is used.
    """
    env_map: Mapping[str, str] = env or {}
    hashes: list[str] = []

    key_hash = entry.get("key_hash")
    if key_hash:
        hashes.append(normalize_key_hash(str(key_hash).strip()))

    raw_key = entry.get("key", "") or ""
    key_env = entry.get("key_env", "") or ""
    if key_env:
        raw_key = env_map.get(str(key_env), raw_key)
    if raw_key:
        hashes.append(hash_api_key(str(raw_key).strip()))

    return hashes


# ---------------------------------------------------------------------------
# Identity construction.
# ---------------------------------------------------------------------------


def _entry_name(entry: Mapping[str, object]) -> str:
    name = entry.get("name") or entry.get("id") or "unnamed"
    return str(name)


def _entry_downstream(entry: Mapping[str, object]) -> Mapping[str, object]:
    downstream = entry.get("downstream")
    if not isinstance(downstream, Mapping):
        return {}
    return downstream


def _profile_for_entry(
    entry: Mapping[str, object], *, strict: bool
) -> AppProfile:
    if strict:
        return load_profile_from_entry(entry)
    return profile_for_key_entry(entry)


def runtime_identity_from_entry(
    entry: Mapping[str, object],
    *,
    key_hash: str,
    strict: bool = False,
) -> RuntimeIdentity:
    """Build a single :class:`RuntimeIdentity` for one ``(entry, key_hash)`` pair.

    ``key_hash`` is one of the hashes returned by
    :func:`key_hashes_for_entry` — it is *not* recomputed here so callers can
    register the same entry under multiple hashes (literal + env-resolved).
    """
    profile = _profile_for_entry(entry, strict=strict)
    return RuntimeIdentity(
        name=_entry_name(entry),
        key_hash=normalize_key_hash(key_hash),
        downstream=_entry_downstream(entry),
        profile=profile,
    )


def build_identity_registry(
    entries: Iterable[Mapping[str, object]],
    *,
    env: Mapping[str, str] | None = None,
    strict: bool = False,
) -> dict[str, RuntimeIdentity]:
    """Build a hash-indexed identity registry from proxy key entries.

    Mirrors :func:`proxy.build_auth_registry` semantics:

    * non-mapping entries are skipped silently,
    * each entry can register under multiple hashes,
    * when two entries share a hash, the *last* one wins.

    When ``strict`` is true, malformed ``profile`` blocks raise
    :class:`gateway.profiles.ProfileLoadError`. Otherwise the profile silently
    falls back to :data:`DEFAULT_PROFILE` (matching ``profile_for_key_entry``).
    """
    registry: dict[str, RuntimeIdentity] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        # Resolve the profile once per entry so strict failures surface even
        # for entries with no usable hashes (helps tests catch typos early).
        profile = _profile_for_entry(entry, strict=strict)
        name = _entry_name(entry)
        downstream = _entry_downstream(entry)
        for raw_hash in key_hashes_for_entry(entry, env=env):
            normalized = normalize_key_hash(raw_hash)
            registry[normalized] = RuntimeIdentity(
                name=name,
                key_hash=normalized,
                downstream=downstream,
                profile=profile,
            )
    return registry


# ---------------------------------------------------------------------------
# Policy bridge helpers.
# ---------------------------------------------------------------------------


def make_policy_request(
    *,
    protocol: str | None = None,
    intent: str | None = None,
    workflow: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    requires_cloud_fallback: bool = False,
    native_passthrough: bool = False,
    native_operation: str | None = None,
    native_backend: str | None = None,
) -> PolicyRequest:
    """Construct a :class:`PolicyRequest` for a route.

    A trivial wrapper that lets handler code stay free of direct
    :mod:`gateway.policy` imports while still benefiting from typed
    keyword-only arguments.
    """
    return PolicyRequest(
        protocol=protocol,
        intent=intent,
        workflow=workflow,
        model=model,
        backend=backend,
        requires_cloud_fallback=requires_cloud_fallback,
        native_passthrough=native_passthrough,
        native_operation=native_operation,
        native_backend=native_backend,
    )


def evaluate_and_apply(
    identity: RuntimeIdentity | None,
    request: PolicyRequest,
    receipt: object | None = None,
) -> PolicyDecision:
    """Evaluate a request against an identity's profile and apply to a receipt.

    ``identity`` may be ``None`` (e.g. when auth is disabled or the path is
    public); in that case :data:`DEFAULT_PROFILE` is used so the policy still
    runs deterministically. When ``receipt`` is provided, the decision is
    written via :func:`gateway.policy.apply_to_receipt`.
    """
    profile = identity.profile if identity is not None else DEFAULT_PROFILE
    decision = evaluate(profile, request)
    if receipt is not None:
        apply_to_receipt(decision, receipt)
    return decision


# ---------------------------------------------------------------------------
# Discovery audience derived from profile visibility.
# ---------------------------------------------------------------------------

# Map StatusVisibilityPolicy.level → RouteReceipt audience tier.
# - none / basic → client (least information)
# - standard     → admin
# - full         → internal
_VISIBILITY_TO_AUDIENCE = {
    "none": "client",
    "basic": "client",
    "standard": "admin",
    "full": "internal",
}


def discovery_audience_for_profile(profile: AppProfile | None) -> str:
    """Return the receipt audience tier for a profile's visibility level.

    Falls back to ``"client"`` for unknown / missing profiles, matching the
    most-restrictive default.
    """
    if profile is None:
        return "client"
    level = profile.status_visibility.level
    return _VISIBILITY_TO_AUDIENCE.get(level, "client")


# ---------------------------------------------------------------------------
# Re-export for callers that want to validate at startup without importing
# gateway.profiles directly.
# ---------------------------------------------------------------------------

# Intentional re-export so `proxy.py` can do
# ``from gateway.runtime_auth import ProfileLoadError`` and surface the same
# exception in strict mode without importing gateway.profiles directly.
__all__.append("ProfileLoadError")
