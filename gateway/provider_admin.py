"""Admin JSON API for paid remote providers.

The service registry owns provider identity and secret references.  This module
owns the UI-facing policy state for those providers: spend caps and which
Profiles may use them.  It never reads or returns a provider key value.
"""

from __future__ import annotations

import hmac
import math
import json
import os
import sqlite3
import calendar
import time
from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from gateway import admin_store, service_registry
from gateway.admin_api import AdminAuthError, AdminContext
from gateway import providers as provider_core
from gateway.litellm_substrate import litellm_provider_model_lookup

PROVIDER_ADMIN_SETTING_KEY = "provider_admin"
PROVIDER_ADMIN_SCHEMA_VERSION = 1

MoneyUsageProvider = Callable[[], Mapping[str, Any]]
AdminAuth = Callable[[Request], AdminContext]


class ProviderAdminStore(Protocol):
    def get_service_registry(self) -> dict[str, Any]: ...

    def list_profiles(self) -> list[dict[str, Any]]: ...

    def get_provider_admin_state(self) -> dict[str, Any]: ...

    def update_provider_admin_state(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ProviderAdminState:
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    global_caps: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROVIDER_ADMIN_SCHEMA_VERSION,
            "providers": self.providers,
            "global_caps": self.global_caps,
        }


class _RouteError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def as_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content={"error": {"code": self.code, "message": self.message}},
        )


def normalize_provider_admin_state(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    providers: dict[str, dict[str, Any]] = {}
    raw_providers = raw.get("providers")
    if isinstance(raw_providers, Mapping):
        for raw_id, raw_provider in raw_providers.items():
            provider_id = service_registry.canonical_provider_id(str(raw_id))
            if not provider_id or not isinstance(raw_provider, Mapping):
                continue
            providers[provider_id] = _normalize_provider_policy(raw_provider)
    return ProviderAdminState(
        providers=providers,
        global_caps=_normalize_caps(raw.get("global_caps")),
    ).to_dict()


def provider_admin_setting_from_store(path=admin_store.DB_PATH) -> dict[str, Any]:
    record = admin_store.get_operator_setting(PROVIDER_ADMIN_SETTING_KEY, path=path)
    return normalize_provider_admin_state(record["value"] if record else None)


def provider_admin_setting_to_store(
    value: Mapping[str, Any],
    *,
    actor: AdminContext,
    path=admin_store.DB_PATH,
) -> dict[str, Any]:
    normalized = normalize_provider_admin_state(value)
    record = admin_store.put_operator_setting_with_audit(
        PROVIDER_ADMIN_SETTING_KEY,
        normalized,
        updated_by=actor.subject,
        audit_action="operator.provider_admin.update",
        audit_actor_kind=actor.actor_kind,
        audit_actor_id=actor.audit_actor_id,
        audit_actor_label=actor.actor_label,
        audit_target_kind="operator_setting",
        audit_target_id=PROVIDER_ADMIN_SETTING_KEY,
        audit_metadata={
            "provider_count": len(normalized["providers"]),
            "global_cap_count": len(normalized["global_caps"]),
        },
        path=path,
    )
    return normalize_provider_admin_state(record["value"])


def create_provider_admin_router(
    store: ProviderAdminStore,
    *,
    auth: AdminAuth,
    spending_provider: MoneyUsageProvider | None = None,
    ledger_path: Path = provider_core.DEFAULT_LEDGER_PATH,
    prefix: str = "/admin/api/providers",
) -> APIRouter:
    """Build the provider admin router.

    Registration interface for the parent lane:

    ``app.include_router(create_provider_admin_router(ADMIN_STORE, auth=admin_auth))``

    The optional ``spending_provider`` returns non-secret usage data shaped as
    ``{"providers": {id: {"daily_usd": 1.2, "monthly_usd": 3.4}},
    "global": {"daily_usd": 1.2, "monthly_usd": 3.4}}``.
    When omitted, the router reads the provider ledger table if present.
    """

    router = APIRouter(prefix=prefix, tags=["admin-providers"])
    effective_spending_provider = spending_provider or (lambda: provider_ledger_spending_snapshot(path=ledger_path))

    def _guard(request: Request) -> AdminContext | JSONResponse:
        return _require_admin(auth, request)

    @router.get("")
    def list_providers(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_providers_payload(store, effective_spending_provider),
        )

    @router.get("/{provider_id}")
    def get_provider(request: Request, provider_id: str) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        provider = _one_provider_payload(store, effective_spending_provider, provider_id)
        if provider is None:
            return _RouteError(
                status.HTTP_404_NOT_FOUND,
                "provider_not_found",
                f"provider {provider_id!r} was not found",
            ).as_response()
        return JSONResponse(status_code=status.HTTP_200_OK, content={"provider": provider})

    @router.get("/{provider_id}/reconciliation")
    def list_provider_reconciliation(request: Request, provider_id: str) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            _require_provider(store, provider_id)
            rows = provider_core.list_provider_reconciliations(
                path=ledger_path,
                provider_id=provider_id,
            )
        except _RouteError as exc:
            return exc.as_response()
        except sqlite3.Error:
            return _RouteError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "provider_ledger_unavailable",
                "provider spend ledger is unavailable",
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"reconciliations": rows},
        )

    @router.post("/{provider_id}/reconciliation/{reservation_id}")
    async def reconcile_provider_reservation(
        request: Request,
        provider_id: str,
        reservation_id: int,
    ) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            _require_provider(store, provider_id)
            body = await _read_json_body(request)
            row = provider_core.reconcile_provider_spend(
                reservation_id,
                body.get("actual_usd"),
                provider_id=provider_id,
                path=ledger_path,
            )
            _audit_provider_reconciliation(store, ctx, provider_id, row)
        except _RouteError as exc:
            return exc.as_response()
        except ValueError as exc:
            return _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_provider_reconciliation",
                str(exc),
            ).as_response()
        except LookupError as exc:
            return _RouteError(
                status.HTTP_404_NOT_FOUND,
                "provider_reconciliation_not_found",
                str(exc),
            ).as_response()
        except sqlite3.Error:
            return _RouteError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "provider_ledger_unavailable",
                "provider spend ledger is unavailable",
            ).as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"reconciliation": row},
        )

    @router.put("/{provider_id}/caps")
    async def set_provider_caps(request: Request, provider_id: str) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            _require_provider(store, provider_id)
            body = await _read_json_body(request)
            state = store.get_provider_admin_state()
            provider_state = _provider_state_for_update(state, provider_id)
            provider_state["caps"] = _normalize_caps(body)
            store.update_provider_admin_state(data=state, actor=ctx)
            provider = _one_provider_payload(store, effective_spending_provider, provider_id)
        except _RouteError as exc:
            return exc.as_response()
        return JSONResponse(status_code=status.HTTP_200_OK, content={"provider": provider})

    @router.delete("/{provider_id}/caps")
    def clear_provider_caps(request: Request, provider_id: str) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            _require_provider(store, provider_id)
            state = store.get_provider_admin_state()
            provider_state = _provider_state_for_update(state, provider_id)
            provider_state.pop("caps", None)
            store.update_provider_admin_state(data=state, actor=ctx)
            provider = _one_provider_payload(store, effective_spending_provider, provider_id)
        except _RouteError as exc:
            return exc.as_response()
        return JSONResponse(status_code=status.HTTP_200_OK, content={"provider": provider})

    @router.put("/{provider_id}/allowed-plans")
    async def set_allowed_plans(request: Request, provider_id: str) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            _require_provider(store, provider_id)
            body = await _read_json_body(request)
            allowed = _normalize_allowed_plans(body, _profile_ids(store))
            state = store.get_provider_admin_state()
            provider_state = _provider_state_for_update(state, provider_id)
            provider_state["allowed_plans"] = allowed
            store.update_provider_admin_state(data=state, actor=ctx)
            provider = _one_provider_payload(store, effective_spending_provider, provider_id)
        except _RouteError as exc:
            return exc.as_response()
        return JSONResponse(status_code=status.HTTP_200_OK, content={"provider": provider})

    @router.put("/-/global-caps")
    async def set_global_caps(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            state = store.get_provider_admin_state()
            state["global_caps"] = _normalize_caps(body)
            store.update_provider_admin_state(data=state, actor=ctx)
        except _RouteError as exc:
            return exc.as_response()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_providers_payload(store, effective_spending_provider),
        )

    @router.delete("/-/global-caps")
    def clear_global_caps(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        state = store.get_provider_admin_state()
        state["global_caps"] = {}
        store.update_provider_admin_state(data=state, actor=ctx)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_providers_payload(store, effective_spending_provider),
        )

    return router


def provider_ledger_spending_snapshot(
    *, path: Path | None = None, now: int | None = None
) -> dict[str, Any]:
    """Read provider/global spend totals from the shared provider ledger.

    This is intentionally a read-only projection over the execution lane's
    `provider_spend` table.  Cap enforcement belongs in the reservation
    transaction that writes that ledger; this function only feeds the admin UI.
    """

    if path is None:
        try:
            from gateway.providers import DEFAULT_LEDGER_PATH
        except Exception:  # pragma: no cover - providers lane may not be present yet
            return _spend_unavailable("ledger_module_unavailable")
        path = DEFAULT_LEDGER_PATH
    if not path.exists():
        return _spend_unavailable("ledger_missing")
    timestamp = int(now if now is not None else time.time())
    day_start = timestamp - 24 * 60 * 60
    month_start = _month_start_epoch(timestamp)
    since = min(day_start, month_start)
    try:
        with closing(sqlite3.connect(path)) as conn:
            rows = conn.execute(
                """
                SELECT provider, created_at, estimated_usd, actual_usd, status
                FROM provider_spend
                WHERE (status IN ('reserved', 'settled') AND created_at >= ?) OR status = 'needs_reconciliation'
                """,
                (since,),
            ).fetchall()
    except sqlite3.Error:
        return _spend_unavailable("ledger_unavailable")
    providers: dict[str, dict[str, Decimal]] = {}
    global_daily = Decimal("0")
    global_monthly = Decimal("0")
    pending: dict[str, int] = {}
    for provider, created_at, estimated, actual, _status in rows:
        provider_id = service_registry.canonical_provider_id(provider)
        if _status == "needs_reconciliation":
            pending[provider_id] = pending.get(provider_id, 0) + 1
        amount = _ledger_amount(actual, estimated)
        provider_totals = providers.setdefault(
            provider_id, {"daily_usd": Decimal("0"), "monthly_usd": Decimal("0")}
        )
        if int(created_at) >= month_start:
            provider_totals["monthly_usd"] += amount
            global_monthly += amount
        if int(created_at) >= day_start:
            provider_totals["daily_usd"] += amount
            global_daily += amount
    return {
        "available": True,
        "providers": {
            provider: {
                "daily_usd": float(totals["daily_usd"]),
                "monthly_usd": float(totals["monthly_usd"]),
                "pending_reconciliation_count": pending.get(provider, 0),
            }
            for provider, totals in providers.items()
        },
        "global": {
            "daily_usd": float(global_daily),
            "monthly_usd": float(global_monthly),
            "pending_reconciliation_count": sum(pending.values()),
        },
    }


def _providers_payload(
    store: ProviderAdminStore,
    spending_provider: MoneyUsageProvider | None,
) -> dict[str, Any]:
    state = normalize_provider_admin_state(store.get_provider_admin_state())
    usage = _usage_snapshot(spending_provider)
    spend_available = usage.get("available", True) is not False
    registry = store.get_service_registry()
    provider_models = _provider_model_summaries(registry)
    providers = [
        _project_provider(provider_id, service, state, usage, spend_available, provider_models)
        for provider_id, service in _remote_providers(store).items()
    ]
    providers.sort(key=lambda row: row["id"])
    return {
        "providers": providers,
        "global": {
            "caps": state["global_caps"],
            "spend": _normalize_spend(
                usage.get("global"),
                available=spend_available,
                error=usage.get("error"),
            ),
        },
        "available_plans": _plan_summaries(store),
    }


def _one_provider_payload(
    store: ProviderAdminStore,
    spending_provider: MoneyUsageProvider | None,
    provider_id: str,
) -> dict[str, Any] | None:
    providers = _remote_providers(store)
    normalized_id = service_registry.canonical_provider_id(provider_id)
    service = providers.get(normalized_id)
    if service is None:
        return None
    state = normalize_provider_admin_state(store.get_provider_admin_state())
    usage = _usage_snapshot(spending_provider)
    return _project_provider(
        normalized_id,
        service,
        state,
        usage,
        usage.get("available", True) is not False,
        _provider_model_summaries(store.get_service_registry()),
    )


def _remote_providers(store: ProviderAdminStore) -> dict[str, Mapping[str, Any]]:
    registry = store.get_service_registry()
    services = registry.get("services") if isinstance(registry, Mapping) else {}
    out: dict[str, Mapping[str, Any]] = {}
    if not isinstance(services, Mapping):
        return out
    for raw_id, raw_service in services.items():
        if not isinstance(raw_service, Mapping):
            continue
        config = raw_service.get("config")
        if isinstance(config, Mapping) and service_registry.is_remote_provider_config(config):
            provider_ref = provider_core.provider_lookup(
                {"services": {raw_id: raw_service}},
                str(raw_service.get("id") or raw_id),
            )
            provider_id = service_registry.canonical_provider_id(
                provider_ref.id if provider_ref is not None else str(config.get("provider_id") or raw_service.get("id") or raw_id)
            )
            if provider_id:
                out[provider_id] = raw_service
    return out


def _project_provider(
    provider_id: str,
    service: Mapping[str, Any],
    state: Mapping[str, Any],
    usage: Mapping[str, Any],
    spend_available: bool,
    provider_models: Mapping[str, list[dict[str, str]]],
) -> dict[str, Any]:
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    provider_state = _provider_state(state, provider_id)
    api_key_env = str(config.get("api_key_env") or "").strip()
    key_set = bool(api_key_env and os.environ.get(api_key_env))
    key_status = {
        "state": "set" if key_set else "missing",
        "api_key_env": api_key_env or None,
    }
    return {
        "id": provider_id,
        "name": str(service.get("name") or provider_id),
        "enabled": bool(service.get("enabled", True)),
        "provider_type": str(config.get("provider_type") or config.get("cost_class") or "remote"),
        "models": list(provider_models.get(provider_id, [])),
        "key_status": key_status,
        "spend": _normalize_spend(
            (usage.get("providers") or {}).get(provider_id)
            if isinstance(usage.get("providers"), Mapping)
            else None,
            available=spend_available,
            error=usage.get("error"),
        ),
        "caps": _normalize_caps(provider_state.get("caps")),
        "allowed_plans": (
            _normalize_string_list(provider_state.get("allowed_plans"))
            if "allowed_plans" in provider_state
            else None
        ),
        "cap_guarantee": _cap_guarantee(),
        "concurrency": _project_concurrency(config),
        "recommendations": _project_recommendations(provider_state, config),
    }


def _provider_model_summaries(registry: Mapping[str, Any]) -> dict[str, list[dict[str, str]]]:
    models: dict[str, list[dict[str, str]]] = {}
    try:
        lookup = litellm_provider_model_lookup(registry)
    except ValueError:
        return models
    for public_name, facts in sorted(lookup.items()):
        if not facts.get("remote"):
            continue
        provider_id = service_registry.canonical_provider_id(str(facts.get("provider_id") or ""))
        if not provider_id:
            continue
        row = {"model": str(public_name)}
        upstream = str(facts.get("upstream_model") or "").strip()
        if upstream:
            row["upstream_model"] = upstream
        models.setdefault(provider_id, []).append(row)
    return models


def _provider_state(state: Mapping[str, Any], provider_id: str) -> dict[str, Any]:
    providers = state.get("providers")
    if not isinstance(providers, Mapping):
        return {}
    raw = providers.get(provider_id)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _provider_state_for_update(state: dict[str, Any], provider_id: str) -> dict[str, Any]:
    normalized = normalize_provider_admin_state(state)
    state.clear()
    state.update(normalized)
    providers = state.setdefault("providers", {})
    provider_state = providers.setdefault(service_registry.canonical_provider_id(provider_id), {})
    if not isinstance(provider_state, dict):
        provider_state = {}
        providers[service_registry.canonical_provider_id(provider_id)] = provider_state
    return provider_state


def _require_provider(store: ProviderAdminStore, provider_id: str) -> None:
    if service_registry.canonical_provider_id(provider_id) not in _remote_providers(store):
        raise _RouteError(
            status.HTTP_404_NOT_FOUND,
            "provider_not_found",
            f"provider {provider_id!r} was not found",
        )


def _normalize_provider_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    policy = {"caps": _normalize_caps(value.get("caps"))}
    if "allowed_plans" in value:
        policy["allowed_plans"] = _normalize_string_list(value.get("allowed_plans"))
    if "recommendations" in value:
        policy["recommendations"] = _normalize_recommendations(value.get("recommendations"))
    return policy


DEFAULT_PROVIDER_RECOMMENDATIONS = [
    {
        "id": "provider_key_credit_limit",
        "message": "Set the provider key credit limit equal to gateway cap when supported",
    }
]


def _cap_guarantee() -> dict[str, str]:
    return {
        "scope": "gateway_reservation",
        "guarantee": "A cap can be exceeded by at most the actual cost of requests already in flight when it was reached.",
        "note": "Gateway provider and global caps are enforced at reservation time.",
    }


def _project_concurrency(config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "max_in_flight": provider_core.provider_concurrency_limit(config),
        "source": "config" if "concurrency_limit" in config else "default",
    }


def _project_recommendations(
    provider_state: Mapping[str, Any], config: Mapping[str, Any]
) -> list[dict[str, str]]:
    for source in (provider_state, config):
        if "recommendations" in source:
            return _normalize_recommendations(source.get("recommendations"))
        recommendation = source.get("provider_credit_limit_recommendation")
        if isinstance(recommendation, str) and recommendation.strip():
            return [{"id": "provider_credit_limit", "message": recommendation.strip()}]
        if isinstance(recommendation, Mapping):
            normalized = _normalize_recommendations([recommendation])
            if normalized:
                return normalized
    return [dict(row) for row in DEFAULT_PROVIDER_RECOMMENDATIONS]


def _normalize_recommendations(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            message = item.strip()
            if message:
                out.append({"id": f"recommendation_{index + 1}", "message": message})
            continue
        if not isinstance(item, Mapping):
            continue
        message = str(item.get("message") or item.get("recommendation") or "").strip()
        if not message:
            continue
        row = {
            "id": str(item.get("id") or f"recommendation_{index + 1}").strip(),
            "message": message,
        }
        url = str(item.get("url") or "").strip()
        if url:
            row["url"] = url
        out.append(row)
    return out


def _normalize_caps(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, float] = {}
    for key in ("daily_usd", "monthly_usd"):
        if key not in value or value[key] in (None, ""):
            continue
        try:
            amount = float(value[key])
        except (TypeError, ValueError) as exc:
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_spend_cap",
                f"{key} must be a non-negative number",
            ) from exc
        if isinstance(value[key], bool) or not math.isfinite(amount) or amount < 0:
            raise _RouteError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_spend_cap",
                f"{key} must be a non-negative number",
            )
        out[key] = amount
    return out


def _spend_unavailable(error: str) -> dict[str, Any]:
    return {"available": False, "error": error, "providers": {}, "global": {}}


def _normalize_spend(
    value: Any, *, available: bool = True, error: Any = None
) -> dict[str, float | str | None]:
    if not available:
        out: dict[str, float | str | None] = {
            "daily_usd": None,
            "monthly_usd": None,
            "status": "unknown",
        }
        if isinstance(error, str) and error:
            out["error"] = error
        return out
    if not isinstance(value, Mapping):
        return {"daily_usd": 0.0, "monthly_usd": 0.0, "status": "known"}
    return {
        "daily_usd": _coerce_non_negative_float(value.get("daily_usd")),
        "monthly_usd": _coerce_non_negative_float(value.get("monthly_usd")),
        "status": "needs_reconciliation" if value.get("pending_reconciliation_count") else "known",
        "pending_reconciliation_count": value.get("pending_reconciliation_count", 0),
    }


def _coerce_non_negative_float(value: Any) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    return amount if amount >= 0 else 0.0


def _ledger_amount(actual: Any, estimated: Any) -> Decimal:
    raw = actual if actual not in (None, "") else estimated
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return amount if amount >= 0 else Decimal("0")


def _month_start_epoch(timestamp: int) -> int:
    current = time.gmtime(timestamp)
    return calendar.timegm((current.tm_year, current.tm_mon, 1, 0, 0, 0))


def _normalize_allowed_plans(
    body: Mapping[str, Any], profile_aliases: Mapping[str, str]
) -> list[str]:
    raw = body.get("allowed_plans")
    if raw is None:
        raw = body.get("plans")
    plans = _normalize_string_list(raw)
    unknown = sorted(plan for plan in plans if plan not in profile_aliases)
    if unknown:
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "unknown_access_plan",
            f"unknown access plan(s): {unknown}",
        )
    return sorted({profile_aliases[plan] for plan in plans})


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_provider_policy",
            "expected a string list",
        )
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _profile_ids(store: ProviderAdminStore) -> dict[str, str]:
    ids: dict[str, str] = {}
    for profile in store.list_profiles():
        if not isinstance(profile, Mapping):
            continue
        row_id = str(profile.get("id") or "").strip()
        policy = profile.get("profile")
        policy_id = ""
        if isinstance(policy, Mapping):
            policy_id = str(policy.get("id") or "").strip()
        canonical = policy_id or row_id
        if canonical:
            ids[canonical] = canonical
        if row_id:
            ids[row_id] = canonical or row_id
    return ids


def _plan_summaries(store: ProviderAdminStore) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for profile in store.list_profiles():
        if not isinstance(profile, Mapping):
            continue
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id:
            continue
        policy = profile.get("profile")
        policy_id = ""
        if isinstance(policy, Mapping):
            policy_id = str(policy.get("id") or "").strip()
        rows.append({
            "id": policy_id or profile_id,
            "record_id": profile_id,
            "name": str(profile.get("name") or profile.get("label") or profile_id),
        })
    rows.sort(key=lambda row: row["id"])
    return rows


def _usage_snapshot(spending_provider: MoneyUsageProvider | None) -> Mapping[str, Any]:
    if spending_provider is None:
        return {}
    snapshot = spending_provider()
    return snapshot if isinstance(snapshot, Mapping) else {}


def _audit_provider_reconciliation(
    store: ProviderAdminStore,
    ctx: AdminContext,
    provider_id: str,
    row: Mapping[str, Any],
) -> None:
    record_audit = getattr(store, "record_audit", None)
    if not callable(record_audit):
        return
    record_audit(
        "operator.provider_reconciliation.update",
        actor_kind=ctx.actor_kind,
        actor_id=ctx.audit_actor_id,
        actor_label=ctx.actor_label,
        target_kind="provider_spend",
        target_id=str(row.get("id") or ""),
        metadata={
            "provider_id": service_registry.canonical_provider_id(provider_id),
            "actual_usd": row.get("actual_usd"),
            "cost_source": row.get("cost_source"),
        },
    )


def _require_admin(auth: AdminAuth, request: Request) -> AdminContext | JSONResponse:
    try:
        ctx = auth(request)
    except AdminAuthError as exc:
        return _auth_error_response(exc)
    if not isinstance(ctx, AdminContext):
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": {"code": "admin_auth_invalid", "message": "admin auth callable must return AdminContext"}},
        )
    if not ctx.is_admin:
        return _auth_error_response(
            AdminAuthError(
                "admin role required",
                status_code=status.HTTP_403_FORBIDDEN,
                code="admin_forbidden",
            )
        )
    if ctx.auth_method == "session" and request.method not in {"GET", "HEAD", "OPTIONS"}:
        if ctx.csrf_token:
            supplied = request.headers.get("x-ai-gateway-csrf") or ""
            if not hmac.compare_digest(supplied, ctx.csrf_token):
                return _auth_error_response(
                    AdminAuthError(
                        "admin session mutation requires a valid CSRF token",
                        status_code=status.HTTP_403_FORBIDDEN,
                        code="admin_csrf_required",
                    )
                )
        elif request.headers.get("x-ai-gateway-admin-action") != "1":
            return _auth_error_response(
                AdminAuthError(
                    "admin session mutation requires same-origin admin action header",
                    status_code=status.HTTP_403_FORBIDDEN,
                    code="admin_action_header_required",
                )
            )
    return ctx


def _auth_error_response(exc: AdminAuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


async def _read_json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_json",
            f"request body is not valid JSON: {exc.msg}",
        ) from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise _RouteError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_json",
            "request body must be a JSON object",
        )
    return payload
