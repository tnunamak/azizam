"""Remote provider policy, spend reservations, and direct HTTP hooks.

This module is intentionally a deep seam for paid remote providers. It does
not read the operator's config files, does not know live auth, and does not
import proxy.py. Callers pass normalized registry records, an AppProfile, and
the already-resolved request facts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import calendar
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import httpx

from gateway import admin_store, secret_store
from gateway.policy import AppProfile, PolicyRequest, evaluate
from gateway.service_registry import canonical_provider_id, is_remote_provider_config

USD_QUANTUM = Decimal("0.000000001")
DEFAULT_PROVIDER_CONCURRENCY_LIMIT = 2
DEFAULT_PROVIDER_REQUEST_TIMEOUT_S = 120.0
DEFAULT_LEDGER_PATH = Path(
    os.environ.get("AI_GATEWAY_PROVIDER_LEDGER_DB", "data/provider_spend.db")
)
_PROCESS_UUID = uuid.uuid4().hex
DEFAULT_USAGE_PRICE_REQUIREMENTS = (
    {
        "usage_path": ("prompt_tokens_details", "cache_creation_tokens"),
        "price_keys": ("cache_creation_input_token_cost",),
    },
    {
        "usage_path": (
            "prompt_tokens_details",
            "cache_creation_token_details",
            "ephemeral_1h_input_tokens",
        ),
        "price_keys": ("cache_creation_input_token_cost_above_1hr",),
    },
    {
        "usage_path": (
            "prompt_tokens_details",
            "cache_creation_token_details",
            "ephemeral_5m_input_tokens",
        ),
        "price_keys": ("cache_creation_input_token_cost",),
    },
    {
        "usage_path": ("prompt_tokens_details", "cached_tokens"),
        "price_keys": ("cache_read_input_token_cost",),
    },
)


class ProviderError(RuntimeError):
    status_code = 500
    code = "provider_error"

    def __init__(self, message: str, *, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after

    def error_body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": str(self)}}


class RemoteProviderNotAllowed(ProviderError):
    status_code = 403
    code = "remote_provider_not_allowed"


class ProviderKeyMissing(ProviderError):
    status_code = 403
    code = "provider_key_missing"


class BudgetExceeded(ProviderError):
    status_code = 402
    code = "budget_exceeded"


class ProviderNeedsReconciliation(BudgetExceeded):
    code = "provider_needs_reconciliation"


class ProviderAuthFailed(ProviderError):
    status_code = 502
    code = "provider_auth_failed"


class ProviderTimeout(ProviderError):
    status_code = 504
    code = "provider_timeout"


class ProviderRateLimited(ProviderError):
    status_code = 429
    code = "provider_rate_limited"


class ProviderConcurrencyLimited(ProviderError):
    status_code = 429
    code = "provider_concurrency_limited"


class ProviderUpstreamError(ProviderError):
    code = "provider_error"

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ProviderRef:
    id: str
    base_url: str
    protocols: tuple[str, ...]
    api_key_env: str | None
    cost_class: str
    price: Mapping[str, Any]
    config: Mapping[str, Any]
    enabled: bool = True
    api_key_secret_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", canonical_provider_id(self.id))

    @property
    def configured(self) -> bool:
        try:
            return bool(resolve_provider_api_key(self, db_path=admin_store.DB_PATH))
        except Exception:
            return False


@dataclass(frozen=True)
class Cost:
    usd: Decimal | None
    source: str
    provider: str
    cost_class: str = "paid-remote"

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", canonical_provider_id(self.provider))


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class Reservation:
    id: int
    provider: str
    profile_id: str
    estimated_usd: Decimal
    cost_source: str


def provider_lookup(
    registry: Mapping[str, Any], provider_id: str, *, protocol: str | None = None
) -> ProviderRef | None:
    services = registry.get("services") if isinstance(registry, Mapping) else None
    if not isinstance(services, Mapping):
        return None
    requested_provider_id = canonical_provider_id(provider_id)
    raw = services.get(requested_provider_id) or services.get(provider_id)
    if raw is None:
        for candidate in services.values():
            if not isinstance(candidate, Mapping):
                continue
            config = candidate.get("config") if isinstance(candidate.get("config"), Mapping) else {}
            if canonical_provider_id(config.get("provider_id") or candidate.get("id")) == requested_provider_id:
                raw = candidate
                break
    if not isinstance(raw, Mapping):
        return None
    config = raw.get("config") if isinstance(raw.get("config"), Mapping) else {}
    raw_protocols = config.get("protocols") or raw.get("capabilities") or ()
    if isinstance(raw_protocols, str):
        raw_protocols = (raw_protocols,)
    protocols = tuple(str(item) for item in raw_protocols)
    config_protocol = config.get("protocol")
    if isinstance(config_protocol, str) and config_protocol not in protocols:
        protocols = (*protocols, config_protocol)
    if protocol is not None and not _protocol_matches(protocol, protocols):
        return None
    base_url = str(
        config.get("base_url")
        or config.get("api_base")
        or config.get("default_base_url")
        or ""
    ).rstrip("/")
    resolved_provider_id = canonical_provider_id(config.get("provider_id") or raw.get("id") or provider_id)
    api_key_env = (
        str(config["api_key_env"])
        if isinstance(config.get("api_key_env"), str) and config.get("api_key_env")
        else None
    )
    api_key_secret_id = str(config.get("api_key_secret_id") or "").strip()
    return ProviderRef(
        id=resolved_provider_id,
        base_url=base_url,
        protocols=protocols,
        api_key_env=api_key_env,
        cost_class=str(config.get("cost_class") or config.get("provider_type") or "paid-remote"),
        price=config.get("price") if isinstance(config.get("price"), Mapping) else {},
        config=config,
        enabled=_bool_from_value(raw.get("enabled"), True),
        api_key_secret_id=api_key_secret_id or None,
    )


def provider_refs(registry: Mapping[str, Any]) -> dict[str, ProviderRef]:
    services = registry.get("services") if isinstance(registry, Mapping) else None
    if not isinstance(services, Mapping):
        return {}
    out: dict[str, ProviderRef] = {}
    for provider_id, raw in services.items():
        if not isinstance(raw, Mapping):
            continue
        config = raw.get("config") if isinstance(raw.get("config"), Mapping) else {}
        if str(raw.get("kind") or "").lower() != "provider":
            continue
        if not is_remote_provider_config(config):
            continue
        ref = provider_lookup(registry, str(provider_id))
        if ref is not None:
            out[ref.id] = ref
    return out


def provider_concurrency_limit(config: Mapping[str, Any]) -> int:
    """Return the paid-provider concurrency limit; default 2, minimum 1."""

    limit = _optional_int(config.get("concurrency_limit"))
    if limit is None or limit < 1:
        return DEFAULT_PROVIDER_CONCURRENCY_LIMIT
    return limit


def require_remote_allowed(
    profile: AppProfile,
    provider: ProviderRef,
    *,
    paid_fallback: bool = False,
) -> None:
    decision = evaluate(
        profile,
        PolicyRequest(
            remote_provider=canonical_provider_id(provider.id),
            remote_paid_fallback=paid_fallback,
        ),
    )
    if decision.denied:
        raise RemoteProviderNotAllowed("; ".join(decision.reasons))
    if not provider.enabled:
        raise RemoteProviderNotAllowed(f"remote provider {provider.id!r} is disabled")
    if not provider.configured:
        raise ProviderKeyMissing(f"provider {provider.id!r} key is missing")


def ensure_ledger(path: Path = DEFAULT_LEDGER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_spend (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                status TEXT NOT NULL,
                estimated_usd TEXT NOT NULL,
                actual_usd TEXT,
                cost_source TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                settled_at INTEGER,
                request_id TEXT,
                generation_id TEXT,
                process_generation TEXT,
                expires_at INTEGER
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_spend)")}
        ownership_columns = {"request_id", "generation_id", "process_generation", "expires_at"}
        if not ownership_columns.issubset(columns):
            # Older ledgers cannot distinguish in-flight work from completed
            # unknown bills. Across this schema upgrade, require owner settlement.
            conn.execute("UPDATE provider_spend SET status = 'needs_reconciliation' "
                         "WHERE status = 'reserved'")
        column_types = {
            "request_id": "TEXT",
            "generation_id": "TEXT",
            "process_generation": "TEXT",
            "expires_at": "INTEGER",
        }
        for name, column_type in column_types.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE provider_spend ADD COLUMN {name} {column_type}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_spend_window "
            "ON provider_spend(provider, profile_id, created_at, status)"
        )


def current_process_generation() -> str:
    return f"{os.getpid()}:{_PROCESS_UUID}"


def recover_provider_spend(
    *, path: Path = DEFAULT_LEDGER_PATH, now: int | None = None
) -> int:
    ensure_ledger(path)
    timestamp = int(now if now is not None else time.time())
    generation = current_process_generation()
    with sqlite3.connect(path, isolation_level=None) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE provider_spend
            SET status = 'needs_reconciliation', actual_usd = NULL,
                settled_at = ?, cost_source = 'unknown'
            WHERE status = 'reserved'
              AND (
                process_generation IS NULL
                OR expires_at IS NULL
                OR process_generation != ?
                OR expires_at <= ?
              )
            """,
            (timestamp, generation, timestamp),
        )
        conn.execute("COMMIT")
        return int(cursor.rowcount or 0)


def reserve_provider_spend(
    profile: AppProfile,
    provider: ProviderRef,
    estimated_cost: Cost,
    *,
    path: Path = DEFAULT_LEDGER_PATH,
    admin_db_path: Path | None = None,
    now: int | None = None,
    paid_fallback: bool = False,
    request_timeout_s: float = DEFAULT_PROVIDER_REQUEST_TIMEOUT_S,
) -> Reservation:
    require_remote_allowed(profile, provider, paid_fallback=paid_fallback)
    provider_admin = _provider_admin_snapshot(
        _effective_admin_db_path(admin_db_path, path)
    )
    provider_id = canonical_provider_id(provider.id)
    _enforce_allowed_plan(profile, provider, provider_admin)
    estimate = estimated_cost.usd
    if estimated_cost.source == "missing-provider-price":
        raise BudgetExceeded(f"paid provider {provider.id!r} requires known model prices")
    if estimate is None:
        if _has_any_cap(profile, provider, provider_admin):
            raise BudgetExceeded(
                f"unknown cost is not allowed when provider {provider.id!r} has a spend cap"
            )
        estimate = Decimal("0")
    estimate = _quantize_usd(estimate)
    if profile.remote.max_usd_per_request is not None and estimate > profile.remote.max_usd_per_request:
        raise BudgetExceeded(
            f"request estimate ${estimate} exceeds per-request cap ${profile.remote.max_usd_per_request}"
        )

    ensure_ledger(path)
    timestamp = int(now if now is not None else time.time())
    recover_provider_spend(path=path, now=timestamp)
    expires_at = timestamp + max(0, int(request_timeout_s))
    process_generation = current_process_generation()
    with sqlite3.connect(path, isolation_level=None) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _enforce_window_cap(
                conn,
                profile=profile,
                provider=None,
                cap=profile.remote.budget_usd_per_day,
                since=timestamp - 24 * 60 * 60,
                estimate=estimate,
                label="daily profile",
            )
            _enforce_window_cap(
                conn,
                profile=None,
                provider=provider_id,
                cap=_provider_cap(provider, "daily"),
                since=timestamp - 24 * 60 * 60,
                estimate=estimate,
                label="daily provider",
                provider_only=True,
            )
            _enforce_window_cap(
                conn,
                profile=None,
                provider=provider_id,
                cap=_admin_provider_cap(provider_admin, provider_id, "daily"),
                since=timestamp - 24 * 60 * 60,
                estimate=estimate,
                label="daily provider admin",
                provider_only=True,
            )
            _enforce_window_cap(
                conn,
                profile=None,
                provider=None,
                cap=_admin_global_cap(provider_admin, "daily"),
                since=timestamp - 24 * 60 * 60,
                estimate=estimate,
                label="daily global admin",
            )
            month_start = _month_start_epoch(timestamp)
            _enforce_window_cap(
                conn,
                profile=profile,
                provider=None,
                cap=profile.remote.budget_usd_per_month,
                since=month_start,
                estimate=estimate,
                label="monthly profile",
            )
            _enforce_window_cap(
                conn,
                profile=None,
                provider=provider_id,
                cap=_provider_cap(provider, "monthly"),
                since=month_start,
                estimate=estimate,
                label="monthly provider",
                provider_only=True,
            )
            _enforce_window_cap(
                conn,
                profile=None,
                provider=provider_id,
                cap=_admin_provider_cap(provider_admin, provider_id, "monthly"),
                since=month_start,
                estimate=estimate,
                label="monthly provider admin",
                provider_only=True,
            )
            _enforce_window_cap(
                conn,
                profile=None,
                provider=None,
                cap=_admin_global_cap(provider_admin, "monthly"),
                since=month_start,
                estimate=estimate,
                label="monthly global admin",
            )
            _enforce_provider_concurrency(conn, provider_id, provider)
            cursor = conn.execute(
                """
                INSERT INTO provider_spend
                (provider, profile_id, status, estimated_usd, cost_source, created_at,
                 process_generation, expires_at)
                VALUES (?, ?, 'reserved', ?, ?, ?, ?, ?)
                """,
                (
                    provider_id,
                    profile.id,
                    str(estimate),
                    estimated_cost.source,
                    timestamp,
                    process_generation,
                    expires_at,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return Reservation(
        id=int(cursor.lastrowid),
        provider=provider_id,
        profile_id=profile.id,
        estimated_usd=estimate,
        cost_source=estimated_cost.source,
    )


def settle_provider_spend(
    reservation: Reservation,
    actual_cost: Cost | None,
    *,
    path: Path = DEFAULT_LEDGER_PATH,
    now: int | None = None,
) -> Cost:
    """Transition a charge atomically and return its persisted cost.

    Unknown finalization cannot overwrite a reconciled charge. Competing owner
    and automatic reconciliations keep the first settlement and report it honestly.
    """
    actual = None if actual_cost is None else actual_cost.usd
    settled_amount = None if actual is None else _quantize_usd(actual)
    timestamp = int(now if now is not None else time.time())
    ensure_ledger(path)
    with sqlite3.connect(path, isolation_level=None) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE provider_spend
            SET status = ?, actual_usd = ?, settled_at = ?, cost_source = ?
            WHERE id = ? AND (status = 'reserved' OR (status = 'needs_reconciliation' AND ? IS NOT NULL))
            """,
            ("needs_reconciliation" if settled_amount is None else "settled",
             None if settled_amount is None else str(settled_amount), timestamp,
             actual_cost.source if actual_cost is not None else "unknown", reservation.id,
             None if settled_amount is None else str(settled_amount)),
        )
        row = conn.execute("SELECT actual_usd, cost_source FROM provider_spend WHERE id = ?", (reservation.id,)).fetchone()
        conn.execute("COMMIT")
    return Cost(_decimal_or_none(row[0]) if row else None, row[1] if row else "unknown",
                reservation.provider, actual_cost.cost_class if actual_cost else "paid-remote")


def identify_provider_spend(
    reservation: Reservation, *, request_id: str | None = None,
    generation_id: str | None = None, path: Path = DEFAULT_LEDGER_PATH,
) -> None:
    ensure_ledger(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE provider_spend SET request_id = COALESCE(?, request_id), "
            "generation_id = COALESCE(?, generation_id) WHERE id = ?",
            (request_id, generation_id, reservation.id),
        )


def list_provider_reconciliations(
    *, path: Path = DEFAULT_LEDGER_PATH, provider_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_ledger(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        where = "status = 'needs_reconciliation'"
        params = []
        if provider_id is not None:
            where += " AND lower(provider) = ?"
            params.append(canonical_provider_id(provider_id))
        return [dict(row) for row in conn.execute(
            f"""
            SELECT id, provider, profile_id, status, estimated_usd, actual_usd,
                   cost_source, created_at, settled_at, request_id, generation_id
            FROM provider_spend WHERE {where} ORDER BY id
            """,
            params,
        )]


def reconcile_provider_spend(
    reservation_id: int, actual_usd: object, *, provider_id: str,
    path: Path = DEFAULT_LEDGER_PATH, source: str = "owner-reported",
) -> dict[str, Any]:
    try:
        amount = _decimal_or_none(actual_usd)
    except InvalidOperation as exc:
        raise ValueError("actual_usd is too large") from exc
    if amount is None:
        raise ValueError("actual_usd must be a finite nonnegative USD amount")
    try:
        amount = _quantize_usd(amount)
    except InvalidOperation as exc:
        raise ValueError("actual_usd is too large") from exc
    ensure_ledger(path)
    with sqlite3.connect(path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM provider_spend WHERE id = ? AND lower(provider) = ?",
                           (reservation_id, canonical_provider_id(provider_id))).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise LookupError("provider charge was not found")
        if row["status"] != "needs_reconciliation":
            conn.execute("ROLLBACK")
            raise ValueError("provider charge does not need reconciliation")
        conn.execute("UPDATE provider_spend SET status = 'settled', actual_usd = ?, "
                     "cost_source = ?, settled_at = ? WHERE id = ?",
                     (str(amount), source, int(time.time()), reservation_id))
        result = dict(conn.execute("SELECT * FROM provider_spend WHERE id = ?", (reservation_id,)).fetchone())
        conn.execute("COMMIT")
        return result


def release_provider_spend(
    reservation: Reservation,
    *,
    path: Path = DEFAULT_LEDGER_PATH,
    now: int | None = None,
) -> None:
    timestamp = int(now if now is not None else time.time())
    ensure_ledger(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE provider_spend
            SET status = 'released', settled_at = ?
            WHERE id = ? AND status = 'reserved'
            """,
            (timestamp, reservation.id),
        )


def extract_usage(body: Mapping[str, Any]) -> Usage:
    usage = body.get("usage")
    if not isinstance(usage, Mapping):
        return Usage()
    return Usage(
        input_tokens=_optional_int(
            usage.get("input_tokens", usage.get("prompt_tokens"))
        ),
        output_tokens=_optional_int(
            usage.get("output_tokens", usage.get("completion_tokens"))
        ),
    )


def extract_provider_cost(
    provider: ProviderRef,
    *,
    body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    stream: bool = False,
    allow_usage_pricing: bool = True,
) -> Cost:
    headers = headers or {}
    if body is not None:
        usage = body.get("usage")
        if isinstance(usage, Mapping):
            reported = _decimal_or_none(usage.get("cost"))
            if reported is not None:
                return Cost(reported, "provider-reported", provider.id, provider.cost_class)
            if allow_usage_pricing:
                model = str(body.get("model") or "")
                if _has_owned_token_prices(provider, model):
                    owned_cost = _configured_usage_cost(provider, body)
                    return Cost(owned_cost, "price-table" if owned_cost is not None else "unknown", provider.id, provider.cost_class)
                sdk_model = _official_sdk_cost_model(provider, body)
                sdk_cost = _litellm_full_usage_cost(sdk_model, body, provider.price.get("usage_price_requirements")) if sdk_model else None
                if sdk_cost is not None:
                    return Cost(sdk_cost, "litellm", provider.id, provider.cost_class)
                return Cost(None, "unknown", provider.id, provider.cost_class)
    header_cost = _decimal_or_none(headers.get("x-litellm-response-cost"))
    if header_cost is not None and not stream and not _has_any_owned_token_prices(provider) and _official_sdk_cost_model(provider, body or {}):
        return Cost(header_cost, "litellm", provider.id, provider.cost_class)
    return Cost(None, "unknown", provider.id, provider.cost_class)


def resolve_provider_model(provider: ProviderRef, model: str) -> str:
    """Resolve registry model aliases using the same mapping as LiteLLM dispatch."""
    from gateway.litellm_substrate import litellm_provider_model_lookup
    registry = {"services": {provider.id: {
        "id": provider.id, "kind": "provider", "capabilities": ["openai"],
        "config": {**provider.config, "provider_id": provider.id,
                   "provider_type": "paid-remote", "api_key_env": provider.api_key_env},
    }}}
    facts = litellm_provider_model_lookup(registry).get(model)
    return str(facts.get("upstream_model") or model) if facts else model


def price_for_provider_model(provider: ProviderRef, model: str) -> Mapping[str, Any]:
    if not isinstance(provider.price.get("models"), Mapping) or not provider.price["models"]:
        return provider.price
    resolved = resolve_provider_model(provider, model)
    upstream_price = _price_for_model(provider.price, resolved)
    return _price_for_model(upstream_price, model)


def estimate_typesafe_cost(provider: ProviderRef, body: Mapping[str, Any]) -> Cost:
    estimated_tokens = _estimate_tokens_from_json(
        {"state": body.get("state", ""), "questions": body.get("questions", {})}
    )
    estimated = _cost_from_tokens(
        provider,
        model=str(body.get("model") or ""),
        input_tokens=estimated_tokens,
        output_tokens=0,
    )
    if estimated is None:
        return Cost(None, "missing-provider-price", provider.id, provider.cost_class)
    return Cost(estimated, "price-table", provider.id, provider.cost_class)


def estimate_chat_cost(provider: ProviderRef, body: Mapping[str, Any]) -> Cost:
    model = str(body.get("model") or "")
    owns_prices = _has_owned_token_prices(provider, model)
    price = price_for_provider_model(provider, model)
    input_rate = _decimal_or_none(price.get("input_usd_per_million_tokens", price.get("input_per_million_usd")))
    output_rate = _decimal_or_none(price.get("output_usd_per_million_tokens", price.get("output_per_million_usd")))
    if _has_unsupported_owned_rates(price):
        return Cost(None, "missing-provider-price", provider.id, provider.cost_class)
    if (owns_prices and (input_rate is None or (output_rate is None and not _looks_like_embeddings_request(body)))) or (not owns_prices and not _official_sdk_cost_model(provider, body)):
        return Cost(None, "missing-provider-price", provider.id, provider.cost_class)
    if _has_unbounded_media_payload(body):
        return Cost(None, "unknown", provider.id, provider.cost_class)
    input_tokens = _estimate_tokens_from_json(_input_estimation_payload(body))
    output_limit = _output_token_limit(body)
    if output_limit is None and _looks_like_embeddings_request(body):
        output_limit = 0
    if output_limit is None:
        return Cost(None, "unknown", provider.id, provider.cost_class)
    candidates = _completion_candidate_count(body)
    if candidates is None:
        return Cost(None, "unknown", provider.id, provider.cost_class)
    estimated = _cost_from_tokens(
        provider,
        model=str(body.get("model") or ""),
        input_tokens=input_tokens,
        output_tokens=output_limit * candidates,
    )
    estimate_source = "price-table"
    if estimated is None:
        sdk_model = _official_sdk_cost_model(provider, body) if not _has_owned_token_prices(provider, model) else None
        estimated = _litellm_full_usage_cost(sdk_model, {"model": sdk_model, "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_limit * candidates}}) if sdk_model else None
        if estimated is None:
            return Cost(None, "missing-provider-price", provider.id, provider.cost_class)
        estimate_source = "litellm"
    return Cost(estimated, estimate_source, provider.id, provider.cost_class)


def estimate_request_cost(
    provider: ProviderRef,
    body: Mapping[str, Any],
    *,
    protocol: str | None = None,
    intent: str | None = None,
) -> Cost:
    operation = str(protocol or intent or "").strip()
    if operation in {"typesafe-systemone", "decision.systemone", "systemone"}:
        return estimate_typesafe_cost(provider, body)
    return estimate_chat_cost(provider, body)


def _protocol_matches(requested: str, declared: tuple[str, ...]) -> bool:
    if requested in declared:
        return True
    if requested in {"openai-chat", "openai-compatible"}:
        return bool({"openai-chat", "openai-compatible"} & set(declared))
    return False


async def call_typesafe_systemone(
    provider: ProviderRef,
    profile: AppProfile,
    body: Mapping[str, Any],
    *,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
    admin_db_path: Path | None = None,
    paid_fallback: bool = False,
    timeout: float = 15.0,
    client: httpx.AsyncClient | None = None,
    request_id: str | None = None,
) -> tuple[dict[str, Any], Cost, Usage, Reservation]:
    require_remote_allowed(profile, provider, paid_fallback=paid_fallback)
    api_key = resolve_provider_api_key(provider, db_path=admin_db_path)
    if not api_key:
        raise ProviderKeyMissing(f"provider {provider.id!r} key is missing")
    reservation = reserve_provider_spend(
        profile,
        provider,
        estimate_typesafe_cost(provider, body),
        path=ledger_path,
        admin_db_path=admin_db_path,
        paid_fallback=paid_fallback,
        request_timeout_s=timeout,
    )
    identify_provider_spend(reservation, request_id=request_id, path=ledger_path)
    close_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await client.post(
            provider.base_url.rstrip('/') + ("/systemone" if provider.base_url.rstrip('/').endswith('/v1') else "/v1/systemone"),
            headers={"Authorization": f"Bearer {api_key}"},
            json=_vendor_typesafe_body(provider, body),
        )
        _raise_mapped_response_error(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ProviderError("provider returned a non-object JSON body")
        payload = _redact_provider_payload(payload, api_key)
        actual_cost = extract_provider_cost(provider, body=payload, headers=response.headers)
        actual_cost = settle_provider_spend(reservation, actual_cost, path=ledger_path)
        return payload, actual_cost, extract_usage(payload), reservation
    except httpx.TimeoutException as exc:
        raise ProviderTimeout("provider request timed out") from exc
    except httpx.HTTPError as exc:
        raise ProviderUpstreamError("remote provider request failed") from exc
    except (ValueError, UnicodeError) as exc:
        raise ProviderUpstreamError("remote provider returned invalid JSON") from exc
    except (ProviderAuthFailed, ProviderRateLimited):
        release_provider_spend(reservation, path=ledger_path)
        raise
    finally:
        # If a response was opaque or interrupted, its charge remains unknown.
        # Known settlements and proven unbilled rejections remain terminal.
        with sqlite3.connect(ledger_path) as conn:
            conn.execute("UPDATE provider_spend SET status = 'needs_reconciliation', settled_at = ? "
                         "WHERE id = ? AND status = 'reserved'",
                         (int(time.time()), reservation.id))
        if close_client:
            await client.aclose()


def _raise_mapped_response_error(response: httpx.Response) -> None:
    if response.status_code in {401, 403}:
        raise ProviderAuthFailed("remote provider rejected the operator key")
    if response.status_code == 429:
        raise ProviderRateLimited(
            "remote provider rate limited the request",
            retry_after=response.headers.get("retry-after"),
        )
    if response.status_code >= 400:
        status_code = response.status_code if response.status_code < 500 else 502
        raise ProviderUpstreamError(
            "remote provider request failed",
            status_code=status_code,
        )


def resolve_provider_api_key(provider: ProviderRef, *, db_path: Path | None = None) -> str:
    if provider.api_key_secret_id:
        try:
            return secret_store.get_provider_secret(
                provider.api_key_secret_id,
                db_path=db_path or admin_store.DB_PATH,
                expected_origin=secret_store.credential_origin_from_url(provider.base_url),
            )
        except Exception:
            return ""
    if provider.api_key_env:
        expected_origin_raw = str(provider.config.get("credential_origin") or "").strip()
        if not expected_origin_raw:
            return ""
        try:
            expected_origin = secret_store.credential_origin_from_url(expected_origin_raw)
            actual_origin = secret_store.credential_origin_from_url(provider.base_url)
        except Exception:
            return ""
        if actual_origin != expected_origin:
            return ""
        value = os.environ.get(provider.api_key_env, "")
        if value:
            return value
    return ""


def _vendor_typesafe_body(
    provider: ProviderRef, body: Mapping[str, Any]
) -> dict[str, Any]:
    out = dict(body)
    model = out.get("model")
    if isinstance(model, str):
        prefix, separator, rest = model.partition("/")
        if separator and canonical_provider_id(prefix) == provider.id:
            out["model"] = rest
    return out


def attach_cost_to_receipt(receipt: object, cost: Cost, usage: Usage | None = None) -> None:
    setattr(receipt, "remote", True)
    setattr(
        receipt,
        "cost",
        {
            "usd": str(cost.usd) if cost.usd is not None else None,
            "source": cost.source,
            "provider": cost.provider,
            "cost_class": cost.cost_class,
        },
    )
    if usage is not None:
        setattr(
            receipt,
            "usage",
            {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            },
        )


def _has_any_cap(
    profile: AppProfile,
    provider: ProviderRef,
    provider_admin: Mapping[str, Any],
) -> bool:
    remote = profile.remote
    return any(
        cap is not None
        for cap in (
            remote.max_usd_per_request,
            remote.budget_usd_per_day,
            remote.budget_usd_per_month,
            _provider_cap(provider, "daily"),
            _provider_cap(provider, "monthly"),
            _admin_provider_cap(provider_admin, provider.id, "daily"),
            _admin_provider_cap(provider_admin, provider.id, "monthly"),
            _admin_global_cap(provider_admin, "daily"),
            _admin_global_cap(provider_admin, "monthly"),
        )
    )


def _enforce_window_cap(
    conn: sqlite3.Connection,
    *,
    profile: AppProfile | None,
    provider: str | None,
    cap: Decimal | None,
    since: int,
    estimate: Decimal,
    label: str,
    provider_only: bool = False,
) -> None:
    if cap is None:
        return
    # Unknown completed charges must not age out of the admission barrier.
    # Only an actual settlement can establish how much budget remains.
    where = "status = 'needs_reconciliation'"
    params: list[Any] = []
    if profile is not None:
        where += " AND profile_id = ?"
        params.append(profile.id)
    if provider is not None:
        where += " AND lower(provider) = ?"
        params.append(canonical_provider_id(provider))
    if conn.execute(f"SELECT 1 FROM provider_spend WHERE {where} LIMIT 1", params).fetchone():
        raise ProviderNeedsReconciliation(
            f"{label} spend needs reconciliation; an owner must reconcile unknown charges before more paid requests"
        )
    provider_total = _window_total(
        conn,
        provider=provider,
        profile_id=profile.id if profile is not None else None,
        since=since,
    )
    if provider_only:
        if provider_total >= cap or provider_total + estimate > cap:
            raise BudgetExceeded(
                f"{label} provider cap for {provider!r} would exceed ${cap}"
            )
        return
    if provider_total >= cap or provider_total + estimate > cap:
        raise BudgetExceeded(f"{label} global cap would exceed ${cap}")


def _window_total(
    conn: sqlite3.Connection,
    *,
    provider: str | None,
    profile_id: str | None,
    since: int,
) -> Decimal:
    where = "created_at >= ? AND status IN ('reserved', 'settled', 'needs_reconciliation')"
    params: list[Any] = [since]
    if profile_id is not None:
        where += " AND profile_id = ?"
        params.append(profile_id)
    if provider is not None:
        where += " AND lower(provider) = ?"
        params.append(canonical_provider_id(provider))
    rows = conn.execute(
        f"SELECT estimated_usd, actual_usd FROM provider_spend WHERE {where}",
        params,
    ).fetchall()
    total = Decimal("0")
    for estimated, actual in rows:
        actual_amount = _decimal_or_none(actual)
        if actual_amount is not None:
            total += actual_amount
            continue
        total += _decimal_or_none(estimated) or Decimal("0")
    return total


def _month_start_epoch(timestamp: int) -> int:
    parts = time.gmtime(timestamp)
    return calendar.timegm((parts.tm_year, parts.tm_mon, 1, 0, 0, 0))


def _enforce_provider_concurrency(
    conn: sqlite3.Connection,
    provider_id: str,
    provider: ProviderRef,
) -> None:
    limit = provider_concurrency_limit(provider.config)
    current = conn.execute(
        "SELECT COUNT(*) FROM provider_spend WHERE lower(provider) = ? AND status = 'reserved'",
        (canonical_provider_id(provider_id),),
    ).fetchone()[0]
    if int(current or 0) >= limit:
        raise ProviderConcurrencyLimited(
            f"remote provider {provider_id!r} has reached its concurrency limit"
        )


def _provider_concurrency_limit(provider: ProviderRef) -> int:
    return provider_concurrency_limit(provider.config)


def _provider_cap(provider: ProviderRef, window: str) -> Decimal | None:
    caps = provider.config.get("spend_caps_usd")
    if not isinstance(caps, Mapping):
        return None
    return _cap_decimal_or_none(caps.get(window), where=f"provider {provider.id} {window} cap")


def _provider_admin_snapshot(admin_db_path: Path | None) -> Mapping[str, Any]:
    if admin_db_path is None:
        return {}
    try:
        record = admin_store.get_operator_setting("provider_admin", path=admin_db_path)
    except sqlite3.Error as exc:
        raise ProviderError("provider admin policy is unavailable") from exc
    if not record or not isinstance(record.get("value"), Mapping):
        return {}
    return record["value"]


def _effective_admin_db_path(
    admin_db_path: Path | None, ledger_path: Path
) -> Path | None:
    if admin_db_path is not None:
        return admin_db_path
    candidate = ledger_path.parent / "admin.db"
    return candidate if candidate.exists() else None


def _provider_admin_policy(
    provider_admin: Mapping[str, Any], provider_id: str
) -> Mapping[str, Any]:
    providers = provider_admin.get("providers")
    if not isinstance(providers, Mapping):
        return {}
    canonical_id = canonical_provider_id(provider_id)
    policy = providers.get(canonical_id)
    if policy is None:
        for raw_id, raw_policy in providers.items():
            if canonical_provider_id(raw_id) == canonical_id:
                policy = raw_policy
                break
    return policy if isinstance(policy, Mapping) else {}


def _enforce_allowed_plan(
    profile: AppProfile,
    provider: ProviderRef,
    provider_admin: Mapping[str, Any],
) -> None:
    provider_id = canonical_provider_id(provider.id)
    policy = _provider_admin_policy(provider_admin, provider_id)
    if "allowed_plans" not in policy:
        return
    raw = policy.get("allowed_plans")
    if raw is None:
        allowed: set[str] = set()
    elif isinstance(raw, str):
        allowed = {raw}
    elif isinstance(raw, (list, tuple, set)):
        allowed = {str(item) for item in raw}
    else:
        allowed = set()
    if profile.id not in allowed:
        raise RemoteProviderNotAllowed(
            f"remote provider {provider_id!r} is not allowed for access plan {profile.id!r}"
        )


def _admin_provider_cap(
    provider_admin: Mapping[str, Any], provider_id: str, window: str
) -> Decimal | None:
    policy = _provider_admin_policy(provider_admin, provider_id)
    caps = policy.get("caps")
    if not isinstance(caps, Mapping):
        return None
    return _cap_decimal_or_none(
        caps.get(f"{window}_usd"),
        where=f"provider_admin {provider_id} {window} cap",
    )


def _admin_global_cap(provider_admin: Mapping[str, Any], window: str) -> Decimal | None:
    caps = provider_admin.get("global_caps")
    if not isinstance(caps, Mapping):
        return None
    return _cap_decimal_or_none(
        caps.get(f"{window}_usd"),
        where=f"provider_admin global {window} cap",
    )


def _estimate_tokens_from_json(value: object) -> int:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    # Text can tokenize at one token per byte. Include framing overhead instead
    # of treating the average English characters/token ratio as a budget bound.
    return len(raw.encode("utf-8")) + 512


def _output_token_limit(body: Mapping[str, Any]) -> int | None:
    limits: list[int] = []
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        limit = _optional_int(body.get(key))
        if limit is not None and limit > 0:
            limits.append(limit)
    return max(limits) if limits else None


def _input_estimation_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    excluded = {"model", "max_tokens", "max_completion_tokens", "max_output_tokens"}
    return {key: value for key, value in body.items() if key not in excluded}


def _looks_like_embeddings_request(body: Mapping[str, Any]) -> bool:
    return "input" in body and "messages" not in body and "prompt" not in body


def _completion_candidate_count(body: Mapping[str, Any]) -> int | None:
    n = _positive_int_or_none(body.get("n"), default=1)
    best_of = _positive_int_or_none(body.get("best_of"), default=1)
    if n is None or best_of is None:
        return None
    batch_count = max(
        _prompt_batch_size(body.get("prompt")),
        _prompt_batch_size(body.get("input")),
        1,
    )
    return batch_count * max(n, best_of)


def _prompt_batch_size(value: object) -> int:
    if isinstance(value, list):
        if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
            return 1
        return max(1, len(value))
    return 1


def _positive_int_or_none(value: object, *, default: int) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _has_unbounded_media_payload(body: Mapping[str, Any]) -> bool:
    media_keys = {
        "image",
        "images",
        "audio",
        "file",
        "files",
        "video",
        "videos",
        "modalities",
        "image_url",
        "input_audio",
        "file_id",
    }
    def contains(value: Any) -> bool:
        if isinstance(value, Mapping):
            return bool(media_keys & set(value)) or value.get("type") in media_keys or any(contains(item) for item in value.values())
        if isinstance(value, list):
            return any(contains(item) for item in value)
        return False
    return contains(body)


def _cost_from_tokens(
    provider: ProviderRef,
    *,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> Decimal | None:
    price = price_for_provider_model(provider, model)
    if _has_unsupported_owned_rates(price):
        return None
    input_price = _decimal_or_none(price.get("input_usd_per_million_tokens", price.get("input_per_million_usd")))
    output_price = _decimal_or_none(price.get("output_usd_per_million_tokens", price.get("output_per_million_usd")))
    total = Decimal("0")
    if input_tokens is not None and input_tokens > 0:
        if input_price is None:
            return None
        total += Decimal(input_tokens) * input_price / Decimal(1_000_000)
    if output_tokens is not None and output_tokens > 0:
        if output_price is None:
            return None
        total += Decimal(output_tokens) * output_price / Decimal(1_000_000)
    return _quantize_usd(total)


def _has_owned_token_prices(provider: ProviderRef, model: str) -> bool:
    return _contains_owned_prices(price_for_provider_model(provider, model))


def _contains_owned_prices(price: Mapping[str, Any]) -> bool:
    return any(
        key.endswith(("_usd_per_million_tokens", "_per_million_usd", "_usd"))
        or key.startswith("usd_per_")
        for key in price if isinstance(key, str)
    )


def _has_any_owned_token_prices(provider: ProviderRef) -> bool:
    models = provider.price.get("models")
    return _contains_owned_prices(provider.price) or (
        isinstance(models, Mapping)
        and any(isinstance(price, Mapping) and _contains_owned_prices(price) for price in models.values())
    )


def _has_unsupported_owned_rates(price: Mapping[str, Any]) -> bool:
    supported = {"input_usd_per_million_tokens", "output_usd_per_million_tokens",
                 "input_per_million_usd", "output_per_million_usd",
                 "cache_read_input_usd_per_million_tokens"}
    return any(_contains_owned_prices({key: value}) for key, value in price.items() if key not in supported)


def _official_sdk_cost_model(provider: ProviderRef, body: Mapping[str, Any]) -> str | None:
    """Only the catalog can authorize SDK pricing for an exact official API."""
    from gateway.provider_catalog import load_provider_catalog
    from gateway.provider_listing import canonical_provider_base_url

    preset_id = provider.config.get("preset_id")
    if not isinstance(preset_id, str):
        return None
    try:
        preset = load_provider_catalog().get(preset_id)
        if preset is None or not preset.sdk_pricing:
            return None
        if canonical_provider_base_url(provider.base_url) != canonical_provider_base_url(preset.sdk_pricing["api_base"]):
            return None
    except (ValueError, OSError):
        return None
    model = _litellm_cost_model(provider, body)
    if not model:
        return None
    sdk_provider = preset.litellm_provider
    if "/" in model:
        prefix, _, _ = model.partition("/")
        return model if prefix == sdk_provider else None
    return f"{sdk_provider}/{model}"


def _usage_token_count(usage: Mapping[str, Any], *names: str) -> int | None:
    values = [usage[name] for name in names if usage.get(name) is not None]
    counts = [_optional_int(value) for value in values]
    if not counts or any(value is None for value in counts) or len(set(counts)) != 1:
        return None
    return counts[0]


def _configured_usage_cost(provider: ProviderRef, body: Mapping[str, Any]) -> Decimal | None:
    """Price complete text/cache-read usage using this provider's own rates.

    A new or unpriced billing dimension is an unknown bill, never permission
    to substitute another endpoint's SDK prices.
    """
    usage = body.get("usage")
    if not isinstance(usage, Mapping):
        return None
    choices = body.get("choices", [])
    if not isinstance(choices, list):
        return None
    for choice in choices:
        if not isinstance(choice, Mapping):
            return None
        message = choice.get("message")
        if isinstance(message, Mapping) and (message.get("annotations") or message.get("audio")):
            # Search and generated media can carry charges outside token usage.
            return None
    supported = {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens", "cost",
                 "prompt_tokens_details", "completion_tokens_details", "input_tokens_details", "output_tokens_details",
                 "cache_read_input_tokens", "cache_creation_input_tokens"}
    if set(usage) - supported:
        return None
    model = str(body.get("model") or "")
    price = price_for_provider_model(provider, model)
    if _has_unsupported_owned_rates(price):
        return None
    inputs = _usage_token_count(usage, "prompt_tokens", "input_tokens")
    outputs = _usage_token_count(usage, "completion_tokens", "output_tokens")
    if inputs is None or outputs is None:
        return None
    cached = 0
    for name, count in (("prompt_tokens_details", inputs), ("input_tokens_details", inputs),
                        ("completion_tokens_details", outputs), ("output_tokens_details", outputs)):
        details = usage.get(name)
        if details is None:
            continue
        if not isinstance(details, Mapping):
            return None
        for key, value in details.items():
            if value is None:
                continue
            units = _optional_int(value)
            if units is None or units > count:
                return None
            if key == "cached_tokens" and name in {"prompt_tokens_details", "input_tokens_details"}:
                if cached and units:
                    return None
                cached = units
            elif key == "text_tokens":
                continue
            elif key not in {"audio_tokens", "reasoning_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens"} or units:
                return None
    separate_cached = _optional_int(usage.get("cache_read_input_tokens", 0))
    creation = _optional_int(usage.get("cache_creation_input_tokens", 0))
    if separate_cached is None or creation is None or creation or (cached and separate_cached):
        return None
    if "total_tokens" in usage and _optional_int(usage["total_tokens"]) != inputs + outputs + separate_cached:
        return None
    total = _cost_from_tokens(provider, model=model, input_tokens=inputs - cached, output_tokens=outputs)
    if total is None:
        return None
    if cached or separate_cached:
        cache_rate = _decimal_or_none(price.get("cache_read_input_usd_per_million_tokens"))
        if cache_rate is None:
            return None
        total += Decimal(cached + separate_cached) * cache_rate / Decimal(1_000_000)
    return _quantize_usd(total)


def _litellm_full_usage_cost(
    model: str,
    body: Mapping[str, Any],
    requirements: object = None,
) -> Decimal | None:
    try:
        from litellm import ModelResponse, Usage, completion_cost, get_model_info  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        response = ModelResponse(**{**dict(body), "model": model})
        usage = body.get("usage")
        if isinstance(usage, Mapping):
            # Unknown usage extensions cannot establish a complete bill. The
            # SDK schema is the source of supported fields, not a vendor list.
            supported = set(Usage.model_fields) | {"input_tokens", "output_tokens"}
            if set(usage) - supported:
                return None
            full_usage = dict(usage)
            full_usage.setdefault("prompt_tokens", usage.get("input_tokens", 0))
            full_usage.setdefault("completion_tokens", usage.get("output_tokens", 0))
            full_usage.pop("input_tokens", None)
            full_usage.pop("output_tokens", None)
            response.usage = Usage(**full_usage)
            if _sdk_usage_has_unknown_fields(response.usage):
                return None
            model_info = get_model_info(model)
            if not _usage_price_requirements_met(
                usage,
                model_info if isinstance(model_info, Mapping) else {},
                requirements,
            ):
                return None
        value = completion_cost(completion_response=response, model=model)
    except Exception:
        return None
    return _decimal_or_none(value)


def _sdk_usage_has_unknown_fields(value: object) -> bool:
    if getattr(value, "model_extra", None):
        return True
    if isinstance(value, Mapping):
        return any(_sdk_usage_has_unknown_fields(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_sdk_usage_has_unknown_fields(child) for child in value)
    fields = getattr(type(value), "model_fields", {})
    return any(
        _sdk_usage_has_unknown_fields(getattr(value, name, None)) for name in fields
    )


def _usage_price_requirements_met(
    usage: Mapping[str, Any],
    model_info: Mapping[str, Any],
    requirements: object = None,
) -> bool:
    for requirement in _usage_price_requirements(requirements):
        units = _path_value(usage, requirement.get("usage_path"))
        amount = _optional_int(units)
        if amount is None or amount <= 0:
            continue
        if not any(_path_value(model_info, key) is not None for key in requirement.get("price_keys", ())):
            return False
    return True


def _usage_price_requirements(value: object = None) -> tuple[Mapping[str, Any], ...]:
    raw = value if isinstance(value, (list, tuple)) else DEFAULT_USAGE_PRICE_REQUIREMENTS
    out: list[Mapping[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            out.append(item)
    return tuple(out)


def _path_value(value: object, path: object) -> object:
    keys = path if isinstance(path, (list, tuple)) else (path,)
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _litellm_cost_model(provider: ProviderRef, body: Mapping[str, Any]) -> str:
    body_model = str(body.get("model") or "")
    model_price = _price_for_model(provider.price, body_model)
    model_cost_model = model_price.get("cost_model")
    if isinstance(model_cost_model, str) and model_cost_model.strip():
        return model_cost_model.strip()
    price_model = provider.price.get("cost_model")
    if isinstance(price_model, str) and price_model.strip():
        return price_model.strip()
    config_model = provider.config.get("cost_model", provider.config.get("litellm_model"))
    if isinstance(config_model, str) and config_model.strip():
        return config_model.strip()
    return body_model


def _price_for_model(price: Mapping[str, Any], model: str) -> Mapping[str, Any]:
    models = price.get("models")
    if isinstance(models, Mapping) and model:
        override = models.get(model)
        if isinstance(override, Mapping):
            merged = dict(price)
            merged.update(override)
            return merged
    return price


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    try:
        return _quantize_usd(amount)
    except InvalidOperation:
        return None


def _cap_decimal_or_none(value: object, *, where: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProviderError(f"{where} must be a non-negative finite decimal") from exc
    if not amount.is_finite() or amount < 0:
        raise ProviderError(f"{where} must be a non-negative finite decimal")
    return _quantize_usd(amount)


def _quantize_usd(value: Decimal) -> Decimal:
    return value.quantize(USD_QUANTUM)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return None


def _bool_from_value(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _redact_provider_payload(value: Any, secret: str) -> Any:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "<redacted>")
    if isinstance(value, list):
        return [_redact_provider_payload(item, secret) for item in value]
    if isinstance(value, dict):
        return {
            _redact_provider_payload(key, secret): _redact_provider_payload(item, secret)
            for key, item in value.items()
        }
    return value


async def call_openai_json(
    provider: ProviderRef,
    path: str,
    body: Mapping[str, Any],
    *,
    content: bytes | None = None,
    timeout: float = 15.0,
    client: httpx.AsyncClient | None = None,
    admin_db_path: Path | None = None,
) -> tuple[dict[str, Any], Cost, Usage, Mapping[str, str], bytes, int]:
    """Call an OpenAI-compatible provider JSON endpoint without LiteLLM.

    Admission, reservation and receipts stay with the caller because different
    OpenAI-shaped routes have different bounded-cost preconditions. ``content``
    lets opaque routes preserve the caller's original JSON bytes; when a
    response body contains the provider key, the returned bytes are replaced by
    redacted JSON.
    """

    api_key = resolve_provider_api_key(provider, db_path=admin_db_path)
    if not api_key:
        raise ProviderKeyMissing(f"provider {provider.id!r} key is missing")
    close_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await client.post(
            _provider_url(provider, path),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            content=content if content is not None else json.dumps(body, separators=(",", ":")).encode("utf-8"),
        )
        original_content = response.content
        parsed_json = True
        try:
            parsed = response.json()
        except ValueError:
            parsed_json = False
            parsed = None
        redacted = _redact_provider_payload(parsed, api_key) if parsed_json else None
        payload = redacted if isinstance(redacted, dict) else {}
        returned_content = original_content
        if parsed_json and redacted != parsed:
            returned_content = json.dumps(redacted, separators=(",", ":")).encode("utf-8")
        elif not parsed_json:
            returned_content = original_content.replace(api_key.encode("utf-8"), b"<redacted>")
        cost = extract_provider_cost(provider, body=payload, headers=response.headers, allow_usage_pricing=False)
        return payload, cost, extract_usage(payload), response.headers, returned_content, response.status_code
    except httpx.TimeoutException as exc:
        raise ProviderTimeout("provider request timed out") from exc
    except httpx.HTTPError as exc:
        raise ProviderUpstreamError("remote provider request failed") from exc
    finally:
        if close_client:
            await client.aclose()


def _provider_url(provider: ProviderRef, path: str) -> str:
    base = provider.base_url.rstrip("/")
    suffix = "/" + path.lstrip("/")
    if base.endswith("/v1") and suffix.startswith("/v1/"):
        suffix = suffix[3:]
    return base + suffix
