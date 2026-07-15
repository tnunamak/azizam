"""Pure request-target resolution from service-declared alias policy data.

This module owns the value-only policy path used to resolve a public model
name to one service-scoped base and optional alias overlay.  It deliberately
has no access to gateway startup, stores, registry recovery, or network clients.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ResolvedRequestTarget:
    """The one data-derived request-policy target for a public model name."""

    requested_model: str
    logical_service_id: str
    base_model_id: str
    base_transform_specs: tuple[str | dict[str, Any], ...]
    alias_overlay_specs: tuple[str | dict[str, Any], ...]


class AliasPolicyResolutionError(ValueError):
    """A required public alias has no single declared logical policy."""


def service_model_records(service_cfg: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Normalize the supported in-memory model-record shapes."""
    raw_models = service_cfg.get("models")
    if isinstance(raw_models, Mapping):
        return [
            {"id": model_id, **dict(raw)}
            if isinstance(raw, Mapping)
            else {"id": model_id, "upstream_model": raw}
            for model_id, raw in raw_models.items()
        ]
    if isinstance(raw_models, list):
        return [raw for raw in raw_models if isinstance(raw, Mapping)]
    return []


def transform_specs(raw: Any) -> list[str | dict[str, Any]]:
    """Keep the declared request-transform specs without interpreting them."""
    if not isinstance(raw, list):
        return []
    return [
        copy.deepcopy(spec)
        for spec in raw
        if (isinstance(spec, str) and spec) or isinstance(spec, Mapping)
    ]


def per_backend_transform_specs(
    service_id: str,
    *,
    model_name: str = "",
    runtime_config: Mapping[str, Any],
) -> list[str | dict[str, Any]]:
    """Return service then exact-public-model transform specs from data."""
    if not service_id:
        return []
    backends = runtime_config.get("backends")
    if not isinstance(backends, Mapping):
        return []
    service_cfg = backends.get(service_id)
    if not isinstance(service_cfg, Mapping):
        return []
    specs = transform_specs(service_cfg.get("transforms"))
    if not model_name:
        return specs
    normalized_model = _normalize_model_id(model_name)
    for item in service_model_records(service_cfg):
        public_names = (item.get("model_group"), item.get("id"))
        if any(_normalize_model_id(str(name or "")) == normalized_model for name in public_names):
            specs.extend(transform_specs(item.get("transforms")))
            break
    return specs


def resolve_request_target(
    requested_model: Any,
    *,
    runtime_config: Mapping[str, Any],
    service_id: str | None = None,
    required: bool = False,
    aliases_only: bool = False,
) -> ResolvedRequestTarget | None:
    """Resolve a public model to one service-scoped base and alias overlay."""
    if not isinstance(requested_model, str) or not requested_model:
        return None
    backends = runtime_config.get("backends")
    if not isinstance(backends, Mapping):
        backends = {}
    candidates = [service_id] if service_id else list(backends)
    normalized_requested = _normalize_model_id(requested_model)
    matches: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
    for candidate_service_id in candidates:
        if not candidate_service_id:
            continue
        service_cfg = backends.get(candidate_service_id)
        if not isinstance(service_cfg, Mapping):
            continue
        records = service_model_records(service_cfg)
        by_id = {
            str(record.get("id") or "").strip(): record
            for record in records
            if str(record.get("id") or "").strip()
        }
        matching_records = [
            record
            for record in records
            if not aliases_only or record.get("base_model_id") not in (None, "")
            if normalized_requested in {
                _normalize_model_id(str(record.get("id") or "")),
                _normalize_model_id(str(record.get("model_group") or "")),
            }
        ]
        if not matching_records:
            continue
        if len(matching_records) != 1:
            raise AliasPolicyResolutionError(
                f"model {requested_model!r} is ambiguous within service {candidate_service_id!r}"
            )
        alias = matching_records[0]
        alias_id = str(alias.get("id") or "").strip()
        base_id = str(alias.get("base_model_id") or alias_id).strip()
        base = by_id.get(base_id)
        if base is None:
            raise AliasPolicyResolutionError(
                f"model {requested_model!r} in service {candidate_service_id!r} "
                f"has no declared base policy {base_id!r}"
            )
        matches.append((str(candidate_service_id), alias, base))
    if not matches:
        if required:
            raise AliasPolicyResolutionError(
                f"model {requested_model!r} has no declared logical alias policy"
            )
        return None
    if len(matches) != 1:
        services = sorted(service for service, _, _ in matches)
        raise AliasPolicyResolutionError(
            f"model {requested_model!r} has ambiguous logical alias policies in {services}"
        )
    logical_service_id, alias, base = matches[0]
    alias_id = str(alias.get("id") or "").strip()
    base_id = str(alias.get("base_model_id") or alias_id).strip()
    return ResolvedRequestTarget(
        requested_model=requested_model,
        logical_service_id=logical_service_id,
        base_model_id=base_id,
        base_transform_specs=tuple(transform_specs(base.get("transforms"))),
        alias_overlay_specs=tuple(transform_specs(alias.get("policy_overlay"))),
    )


def _normalize_model_id(model_name: str) -> str:
    return (model_name or "").strip().lower()
