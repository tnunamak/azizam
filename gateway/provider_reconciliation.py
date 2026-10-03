"""Provider-side reconciliation helpers.

The gateway can recover some unknown paid-provider charges after dispatch by
asking a provider-owned metadata endpoint for the final generation cost.  The
endpoint shape is provider configuration, not production code: this module only
knows how to issue one configured GET request and read one configured JSON path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from gateway import admin_store
from gateway.providers import Cost, ProviderRef, USD_QUANTUM, resolve_provider_api_key

DEFAULT_STATS_TIMEOUT = httpx.Timeout(10.0)


async def lookup_generation_cost(
    provider: ProviderRef,
    generation_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> Cost:
    """Return provider-reported generation cost, or unknown on any failure."""

    generation_id = str(generation_id or "").strip()
    stats_config = (
        provider.config.get("generation_stats")
        if isinstance(provider.config, Mapping)
        else None
    )
    if not generation_id or not isinstance(stats_config, Mapping):
        return _unknown_cost(provider)

    path = _stats_path(stats_config)
    id_param = _stats_id_param(stats_config)
    cost_path = _stats_cost_path(stats_config)
    if path is None or id_param is None or cost_path is None:
        return _unknown_cost(provider)

    token = _provider_token(provider)
    if token is None:
        return _unknown_cost(provider)

    close_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=DEFAULT_STATS_TIMEOUT,
        follow_redirects=False,
        trust_env=False,
    )
    try:
        response = await active_client.get(
            _join_url(provider.base_url, path),
            params={id_param: generation_id},
            headers=_provider_auth_headers(provider, token),
            follow_redirects=False,
        )
        if response.status_code != 200:
            return _unknown_cost(provider)
        payload = response.json()
        cost = _decimal_at_path(payload, cost_path)
        if cost is None or cost < 0:
            return _unknown_cost(provider)
        return Cost(
            cost,
            "provider-generation-stats",
            provider.id,
            cost_class=provider.cost_class,
        )
    except (httpx.HTTPError, ValueError, TypeError, InvalidOperation):
        return _unknown_cost(provider)
    finally:
        if close_client:
            await active_client.aclose()


def _unknown_cost(provider: ProviderRef) -> Cost:
    return Cost(
        None,
        "provider-generation-stats-unavailable",
        provider.id,
        cost_class=provider.cost_class,
    )


def _stats_path(config: Mapping[str, Any]) -> str | None:
    path = config.get("path")
    if not isinstance(path, str):
        return None
    path = path.strip()
    if not path or "://" in path:
        return None
    return "/" + path.lstrip("/")


def _stats_id_param(config: Mapping[str, Any]) -> str | None:
    param = config.get("id_param")
    if not isinstance(param, str):
        return None
    param = param.strip()
    return param or None


def _stats_cost_path(config: Mapping[str, Any]) -> tuple[str, ...] | None:
    path = config.get("cost_path")
    if not isinstance(path, Sequence) or isinstance(path, (str, bytes)):
        return None
    out = tuple(str(part).strip() for part in path if str(part).strip())
    return out or None


def _provider_token(provider: ProviderRef) -> str | None:
    value = resolve_provider_api_key(provider, db_path=admin_store.DB_PATH)
    value = value.strip() if isinstance(value, str) else ""
    return value or None



def _provider_auth_headers(provider: ProviderRef, token: str) -> dict[str, str]:
    auth = provider.config.get("auth") if isinstance(provider.config, Mapping) else None
    header = "Authorization"
    prefix = "Bearer"
    if isinstance(auth, Mapping):
        configured_header = str(auth.get("header") or "").strip()
        if configured_header:
            header = configured_header
        configured_prefix = str(auth.get("prefix") if auth.get("prefix") is not None else "Bearer").strip()
        prefix = configured_prefix
    value = f"{prefix} {token}".strip() if prefix else token
    return {header: value}

def _join_url(base_url: str, path: str) -> str:
    return f"{str(base_url).rstrip('/')}{path}"


def _decimal_at_path(payload: Any, path: tuple[str, ...]) -> Decimal | None:
    value = payload
    for part in path:
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return _decimal_or_none(value)


def _decimal_or_none(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        cost = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not cost.is_finite():
        return None
    try:
        return cost.quantize(USD_QUANTUM)
    except InvalidOperation:
        return None


async def reconcile_pending_generations(provider: ProviderRef, *, path) -> None:
    """Retry provider stats on a later admission; unresolved caps stay blocked."""
    from gateway import providers
    if not isinstance(provider.config.get("generation_stats"), Mapping):
        return
    # Bound metadata traffic per attempt. Remaining charges continue to block caps.
    for row in [row for row in providers.list_provider_reconciliations(path=path, provider_id=provider.id)
                if row.get("generation_id")][:2]:
        cost = await lookup_generation_cost(provider, row["generation_id"])
        if cost.usd is not None:
            reservation = providers.Reservation(row["id"], row["provider"], row["profile_id"],
                                                Decimal(row["estimated_usd"]), row["cost_source"])
            providers.settle_provider_spend(reservation, cost, path=path)
