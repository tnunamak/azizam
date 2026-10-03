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
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import httpx

from gateway import admin_store
from gateway.policy import AppProfile, PolicyRequest, evaluate
from gateway.service_registry import canonical_provider_id, is_remote_provider_config

USD_QUANTUM = Decimal("0.000000001")
DEFAULT_PROVIDER_CONCURRENCY_LIMIT = 2
DEFAULT_LEDGER_PATH = Path(
    os.environ.get("AI_GATEWAY_PROVIDER_LEDGER_DB", "data/provider_spend.db")
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", canonical_provider_id(self.id))

    @property
    def configured(self) -> bool:
        return bool(self.api_key_env and os.environ.get(self.api_key_env))


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
    return ProviderRef(
        id=canonical_provider_id(config.get("provider_id") or raw.get("id") or provider_id),
        base_url=base_url,
        protocols=protocols,
        api_key_env=(
            str(config["api_key_env"])
            if isinstance(config.get("api_key_env"), str) and config.get("api_key_env")
            else None
        ),
        cost_class=str(config.get("cost_class") or config.get("provider_type") or "paid-remote"),
        price=config.get("price") if isinstance(config.get("price"), Mapping) else {},
        config=config,
        enabled=_bool_from_value(raw.get("enabled"), True),
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
                settled_at INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_spend_window "
            "ON provider_spend(provider, profile_id, created_at, status)"
        )


def reserve_provider_spend(
    profile: AppProfile,
    provider: ProviderRef,
    estimated_cost: Cost,
    *,
    path: Path = DEFAULT_LEDGER_PATH,
    admin_db_path: Path | None = None,
    now: int | None = None,
    paid_fallback: bool = False,
) -> Reservation:
    require_remote_allowed(profile, provider, paid_fallback=paid_fallback)
    provider_admin = _provider_admin_snapshot(
        _effective_admin_db_path(admin_db_path, path)
    )
    provider_id = canonical_provider_id(provider.id)
    _enforce_allowed_plan(profile, provider, provider_admin)
    estimate = estimated_cost.usd
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
                (provider, profile_id, status, estimated_usd, cost_source, created_at)
                VALUES (?, ?, 'reserved', ?, ?, ?)
                """,
                (
                    provider_id,
                    profile.id,
                    str(estimate),
                    estimated_cost.source,
                    timestamp,
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
) -> None:
    actual = None if actual_cost is None else actual_cost.usd
    if actual is None:
        return
    settled_amount = _quantize_usd(actual)
    timestamp = int(now if now is not None else time.time())
    ensure_ledger(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE provider_spend
            SET status = 'settled', actual_usd = ?, settled_at = ?
            WHERE id = ? AND status = 'reserved'
            """,
            (str(settled_amount), timestamp, reservation.id),
        )


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
) -> Cost:
    headers = headers or {}
    if body is not None:
        usage = body.get("usage")
        if isinstance(usage, Mapping):
            reported = _decimal_or_none(usage.get("cost"))
            if reported is not None:
                return Cost(reported, "provider-reported", provider.id, provider.cost_class)
    header_cost = _decimal_or_none(headers.get("x-litellm-response-cost"))
    if header_cost is not None and not stream:
        return Cost(header_cost, "litellm", provider.id, provider.cost_class)
    if body is not None and isinstance(body.get("usage"), Mapping):
        sdk_cost = _litellm_full_usage_cost(_litellm_cost_model(provider, body), body)
        if sdk_cost is not None:
            return Cost(sdk_cost, "litellm", provider.id, provider.cost_class)
    return Cost(None, "unknown", provider.id, provider.cost_class)


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
        return Cost(None, "unknown", provider.id, provider.cost_class)
    return Cost(estimated, "price-table", provider.id, provider.cost_class)


def estimate_chat_cost(provider: ProviderRef, body: Mapping[str, Any]) -> Cost:
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
    if estimated is None:
        return Cost(None, "unknown", provider.id, provider.cost_class)
    return Cost(estimated, "price-table", provider.id, provider.cost_class)


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
) -> tuple[dict[str, Any], Cost, Usage, Reservation]:
    require_remote_allowed(profile, provider, paid_fallback=paid_fallback)
    api_key = os.environ.get(provider.api_key_env or "")
    if not api_key:
        raise ProviderKeyMissing(f"provider {provider.id!r} key is missing")
    reservation = reserve_provider_spend(
        profile,
        provider,
        estimate_typesafe_cost(provider, body),
        path=ledger_path,
        admin_db_path=admin_db_path,
        paid_fallback=paid_fallback,
    )
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
        settle_provider_spend(reservation, actual_cost, path=ledger_path)
        return payload, actual_cost, extract_usage(payload), reservation
    except httpx.TimeoutException as exc:
        raise ProviderTimeout("provider request timed out") from exc
    except httpx.HTTPError as exc:
        raise ProviderUpstreamError("remote provider request failed") from exc
    except (ProviderAuthFailed, ProviderRateLimited):
        release_provider_spend(reservation, path=ledger_path)
        raise
    finally:
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
    where = "created_at >= ? AND status IN ('reserved', 'settled')"
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
    price = _price_for_model(provider.price, model)
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


def _litellm_full_usage_cost(model: str, body: Mapping[str, Any]) -> Decimal | None:
    try:
        from litellm import ModelResponse, Usage, completion_cost  # type: ignore[import-not-found]
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
            response.usage = Usage(**full_usage)
        value = completion_cost(completion_response=response, model=model)
    except Exception:
        return None
    return _decimal_or_none(value)


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
    return _quantize_usd(amount)


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
            key: _redact_provider_payload(item, secret)
            for key, item in value.items()
        }
    return value
