"""Admin JSON API for paid remote providers.

The service registry owns provider identity and secret references.  This module
owns the UI-facing policy state for those providers: spend caps and which
Profiles may use them. Keys are read only for checks and custody validation;
API projections never return their values.
"""

from __future__ import annotations

from gateway.compat import get_env

import hmac
import math
import json
import hashlib
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

from gateway import admin_store, egress, receipt_store, service_registry
from gateway import provider_catalog, provider_configuration, provider_listing, secret_store
from gateway.admin_api import AdminAuthError, AdminContext
from gateway import providers as provider_core
from gateway.litellm_substrate import litellm_provider_model_lookup, secret_env_name_for_provider

PROVIDER_ADMIN_SETTING_KEY = "provider_admin"
PROVIDER_ADMIN_SCHEMA_VERSION = 1

MoneyUsageProvider = Callable[[], Mapping[str, Any]]
AdminAuth = Callable[[Request], AdminContext]


class ProviderAdminStore(Protocol):
    def get_service_registry(self) -> dict[str, Any]: ...

    def list_access_plans(self) -> list[dict[str, Any]]: ...

    def update_access_plan(
        self, *, plan_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None: ...

    def get_provider_admin_state(self) -> dict[str, Any]: ...

    def update_provider_admin_state(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]: ...

    def add_service(
        self, *, data: Mapping[str, Any], actor: AdminContext, expected_generation: int
    ) -> dict[str, Any]: ...

    def update_service(
        self, *, service_id: str, data: Mapping[str, Any], actor: AdminContext, expected_generation: int
    ) -> dict[str, Any]: ...

    def remove_service(
        self, *, service_id: str, actor: AdminContext, expected_generation: int
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
        return egress.safe_error_response(self.status_code, self.code, self.message)


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
    receipts_path: Path = receipt_store.DB_PATH,
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

    router = APIRouter(prefix=prefix, tags=["admin-providers"], route_class=egress.ManagementRoute)
    effective_spending_provider = spending_provider or (lambda: provider_ledger_spending_snapshot(path=ledger_path))

    def _guard(request: Request) -> AdminContext | JSONResponse:
        return _require_admin(auth, request)

    def _owner_guard(request: Request) -> AdminContext | JSONResponse:
        """Admit only the owner: every write here moves the owner's money or provider keys."""
        ctx = _require_admin(auth, request)
        if isinstance(ctx, AdminContext) and "owner" not in ctx.roles:
            return _RouteError(
                status.HTTP_403_FORBIDDEN, "owner_required", "Only the owner can change this.",
            ).as_response()
        return ctx

    @router.get("")
    def list_providers(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return egress.projected_response(
            status_code=status.HTTP_200_OK,
            content=_providers_payload(store, effective_spending_provider),
        )

    @router.get("/-/catalog")
    def list_catalog(request: Request) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            catalog = provider_catalog.load_provider_catalog()
        except ValueError as exc:
            return _RouteError(500, "provider_catalog_invalid", str(exc)).as_response()
        return egress.plain_json_response(response_class="provider_catalog",
            status_code=status.HTTP_200_OK,
            content={"providers": [preset.to_dict() for preset in catalog.values()]},
        )

    @router.post("")
    async def add_provider(request: Request) -> JSONResponse:
        ctx = _owner_guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            with secret_store.provider_mutation_lock(_admin_db_path(store)):
                service = provider_configuration.provider_service_from_body(body)
                expected_generation = _expected_registry_generation(store, body)
                provider_id = service_registry.canonical_provider_id(service["id"])
                if provider_id in (store.get_service_registry().get("services") or {}) or provider_id in _remote_providers(store):
                    raise _RouteError(status.HTTP_409_CONFLICT, "provider_exists", f"provider {provider_id!r} already exists")
                create_caps = _normalize_caps(body.get("caps") if isinstance(body.get("caps"), Mapping) else body)
                config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
                secret_id = str(config.get("api_key_secret_id") or "").strip()
                wrote_secret = False
                if secret_id:
                    secret_value = str(body.get("api_key") or "").strip()
                    if not secret_value:
                        raise _RouteError(status.HTTP_400_BAD_REQUEST, "missing_provider_key", "api_key is required when storing the provider key in the gateway")
                    secret_store.put_provider_secret(
                        secret_id,
                        secret_value,
                        db_path=_admin_db_path(store),
                        approved_origin=_provider_credential_origin(config),
                        session=ctx.browser_session,
                    )
                    wrote_secret = True
                try:
                    state = store.get_provider_admin_state()
                    provider_state = _provider_state_for_update(state, provider_id)
                    if create_caps:
                        provider_state["caps"] = create_caps
                    store.update_provider_admin_state(data=state, actor=ctx)
                    # A new provider is in no access plan until the owner adds it.
                    record = store.add_service(data=_provider_service_to_store_form(service), actor=ctx, expected_generation=expected_generation)
                except Exception:
                    if wrote_secret:
                        secret_store.delete_provider_secret(secret_id, db_path=_admin_db_path(store))
                    raise
                provider = _one_provider_payload(store, effective_spending_provider, provider_id)
        except _RouteError as exc:
            return exc.as_response()
        except admin_store.OperatorSettingsGenerationConflict:
            return _registry_conflict_response()
        except provider_configuration.ProviderConfigurationError as exc:
            return _RouteError(exc.status_code, exc.code, exc.message).as_response()
        except (ValueError, KeyError) as exc:
            return _RouteError(status.HTTP_400_BAD_REQUEST, "invalid_provider", str(exc)).as_response()
        return egress.projected_response(
            status_code=status.HTTP_201_CREATED,
            content={"provider": provider, "service_registry": record.get("value") or {}},
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
        return egress.projected_response(status_code=status.HTTP_200_OK, content={"provider": provider})

    @router.get("/{provider_id}/reconciliation")
    def list_provider_reconciliation(request: Request, provider_id: str) -> JSONResponse:
        ctx = _guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            rows = provider_core.list_provider_reconciliations(
                path=ledger_path,
                provider_id=provider_id,
            )
            if not rows:
                _require_provider(store, provider_id)
            # A charge belongs to an App; name it so the owner can tell which.
            names: dict[str, str | None] = {}
            for row in rows:
                app_id = row.get("app_id")
                if app_id and app_id not in names:
                    app = admin_store.get_app(str(app_id), path=_admin_db_path(store))
                    names[app_id] = app.get("name") if app else None
                row["app_name"] = names.get(app_id) if app_id else None
        except _RouteError as exc:
            return exc.as_response()
        except sqlite3.Error:
            return _RouteError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "provider_ledger_unavailable",
                "provider spend ledger is unavailable",
            ).as_response()
        return egress.plain_json_response(response_class="provider_reconciliation",
            status_code=status.HTTP_200_OK,
            content={"reconciliations": rows},
        )

    @router.post("/{provider_id}/reconciliation/{reservation_id}")
    async def reconcile_provider_reservation(
        request: Request,
        provider_id: str,
        reservation_id: int,
    ) -> JSONResponse:
        ctx = _owner_guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            row = provider_core.reconcile_provider_spend(
                reservation_id,
                body.get("actual_usd"),
                provider_id=provider_id,
                path=ledger_path,
            )
            _update_reconciled_receipt_cost(row, path=receipts_path)
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
        return egress.plain_json_response(response_class="provider_reconciliation",
            status_code=status.HTTP_200_OK,
            content={"reconciliation": row},
        )

    @router.put("/{provider_id}/key")
    async def set_provider_key(request: Request, provider_id: str) -> JSONResponse:
        ctx = _owner_guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            secret_value = str(body.get("api_key") or "").strip()
            if not secret_value:
                raise _RouteError(status.HTTP_400_BAD_REQUEST, "missing_provider_key", "api_key is required")
            with secret_store.provider_mutation_lock(_admin_db_path(store)):
                _service_id, service = _service_for_provider(store.get_service_registry(), provider_id)
                config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
                secret_id = str(config.get("api_key_secret_id") or "").strip()
                if not secret_id:
                    raise _RouteError(status.HTTP_409_CONFLICT, "env_key_provider", "This provider uses an environment-variable key. Switch it to gateway-stored keys before rotating in the UI.")
                status_row = secret_store.put_provider_secret(
                    secret_id,
                    secret_value,
                    db_path=_admin_db_path(store),
                    approved_origin=_provider_credential_origin(config),
                    session=ctx.browser_session,
                ).to_dict()
                _clear_provider_connection_state(store, provider_id, actor=ctx)
        except _RouteError as exc:
            return exc.as_response()
        except ValueError:
            return _RouteError(400, "invalid_provider_key", "Enter a provider key longer than four characters.").as_response()
        return egress.projected_response(status_code=status.HTTP_200_OK, content={"key_status": _secret_key_status(status_row)})

    @router.delete("/{provider_id}/key")
    def delete_provider_key(request: Request, provider_id: str) -> JSONResponse:
        ctx = _owner_guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            with secret_store.provider_mutation_lock(_admin_db_path(store)):
                _require_provider(store, provider_id)
                secret_id = _current_provider_secret_id(store, provider_id)
                if not secret_id:
                    raise _RouteError(status.HTTP_409_CONFLICT, "env_key_provider", "This provider uses an environment-variable key. Remove the environment reference from provider settings.")
                status_row = secret_store.delete_provider_secret(
                    secret_id, db_path=_admin_db_path(store), session=ctx.browser_session).to_dict()
                _clear_provider_connection_state(store, provider_id, actor=ctx)
        except _RouteError as exc:
            return exc.as_response()
        return egress.projected_response(status_code=status.HTTP_200_OK, content={"key_status": _secret_key_status(status_row)})

    @router.post("/-/test")
    async def test_unsaved_provider(request: Request) -> JSONResponse:
        ctx = _owner_guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            service = provider_configuration.provider_service_from_body(body)
            config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
            api_key = str(body.get("api_key") or "") if body.get("key_mode") != "env" else get_env(str(config.get("api_key_env") or ""), "")
            result = await provider_listing.probe_provider(config, api_key)
        except provider_configuration.ProviderConfigurationError as exc:
            return _RouteError(exc.status_code, exc.code, exc.message).as_response()
        except _RouteError as exc:
            return exc.as_response()
        except provider_listing.ProviderListingError as exc:
            return _RouteError(exc.status_code, exc.code, str(exc)).as_response()
        return egress.plain_json_response(response_class="provider_probe", status_code=status.HTTP_200_OK, content=result)

    @router.post("/{provider_id}/test")
    async def test_provider(request: Request, provider_id: str) -> JSONResponse:
        ctx = _owner_guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        provider = _one_provider_payload(store, effective_spending_provider, provider_id)
        if provider is None:
            return _RouteError(status.HTTP_404_NOT_FOUND, "provider_not_found", f"provider {provider_id!r} was not found").as_response()
        with secret_store.provider_mutation_lock(_admin_db_path(store)):
            _, service = _service_for_provider(store.get_service_registry(), provider_id)
            config = _provider_probe_config(service, provider_id)
            key_fingerprint = _provider_key_fingerprint(config, _admin_db_path(store))
        try:
            secret_id = str(config.get("api_key_secret_id") or "")
            if secret_id:
                try:
                    api_key = secret_store.get_provider_secret(
                        secret_id,
                        db_path=_admin_db_path(store),
                        expected_origin=_provider_credential_origin(config),
                    )
                except Exception:
                    raise provider_listing.ProviderListingError("The stored provider key is unavailable. Enter a replacement key.") from None
            else:
                api_key_env = str(config.get("api_key_env") or "")
                if api_key_env and not _provider_env_ref_origin_matches(config):
                    raise provider_listing.ProviderListingError("The provider key is approved for a different endpoint. Re-enter the key for this endpoint before running Gateway check.")
                api_key = get_env(api_key_env, "")
            result = await provider_listing.probe_provider(config, api_key)
        except provider_listing.ProviderListingError as exc:
            with secret_store.provider_mutation_lock(_admin_db_path(store)):
                if _saved_provider_probe_still_current(store, provider_id, key_fingerprint):
                    state = store.get_provider_admin_state()
                    _provider_state_for_update(state, provider_id)["connection"] = {"reachable": False, "checked_at": int(time.time())}
                    store.update_provider_admin_state(data=state, actor=ctx)
            return _RouteError(exc.status_code, exc.code, str(exc)).as_response()
        with secret_store.provider_mutation_lock(_admin_db_path(store)):
            if _saved_provider_probe_still_current(store, provider_id, key_fingerprint):
                state = store.get_provider_admin_state()
                _provider_state_for_update(state, provider_id)["connection"] = {"reachable": True, "checked_at": result["checked_at"]}
                store.update_provider_admin_state(data=state, actor=ctx)
        return egress.plain_json_response(response_class="provider_probe", status_code=status.HTTP_200_OK, content=result)

    @router.put("/{provider_id}/models")
    async def update_provider_models(request: Request, provider_id: str) -> JSONResponse:
        ctx = _owner_guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            with secret_store.provider_mutation_lock(_admin_db_path(store)):
                _require_provider(store, provider_id)
                registry = store.get_service_registry()
                service_id, service = _service_for_provider(registry, provider_id)
                merged = dict(service)
                config = dict(merged.get("config") if isinstance(merged.get("config"), Mapping) else {})
                config["models"] = provider_configuration.model_records_from_body(
                    body,
                    service_registry.canonical_provider_id(provider_id),
                )
                incoming_price = provider_configuration.price_config_from_body(
                    body,
                    service_registry.canonical_provider_id(provider_id),
                )
                merged_price = _merge_provider_price_config(
                    config.get("price") if isinstance(config.get("price"), Mapping) else {},
                    incoming_price,
                )
                if merged_price:
                    config["price"] = merged_price
                provider_configuration._validate_enabled_model_prices(config["models"], merged_price)
                config["model_policy_schema_version"] = service_registry.MODEL_POLICY_SCHEMA_VERSION
                merged["config"] = config
                record = store.update_service(
                    service_id=service_id,
                    data=_provider_service_to_store_form(merged),
                    actor=ctx,
                    expected_generation=_expected_registry_generation(store, body),
                )
                provider = _one_provider_payload(store, effective_spending_provider, provider_id)
        except _RouteError as exc:
            return exc.as_response()
        except provider_configuration.ProviderConfigurationError as exc:
            return _RouteError(exc.status_code, exc.code, exc.message).as_response()
        except admin_store.OperatorSettingsGenerationConflict:
            return _registry_conflict_response()
        except ValueError as exc:
            return _RouteError(status.HTTP_400_BAD_REQUEST, "invalid_provider", str(exc)).as_response()
        return egress.projected_response(status_code=status.HTTP_200_OK, content={"provider": provider, "service_registry": record.get("value") or {}})

    @router.delete("/{provider_id}")
    async def remove_provider(request: Request, provider_id: str) -> JSONResponse:
        ctx = _owner_guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body_or_empty(request)
            with secret_store.provider_mutation_lock(_admin_db_path(store)):
                registry = store.get_service_registry()
                service_id, service = _service_for_provider(registry, provider_id)
                _ensure_provider_has_no_pending_spend(provider_id, ledger_path)
                record = store.remove_service(
                    service_id=service_id,
                    actor=ctx,
                    expected_generation=_expected_registry_generation(store, body),
                )
                _drop_provider_from_plans(store, provider_id, actor=ctx)
                state = store.get_provider_admin_state()
                providers = state.get("providers") if isinstance(state.get("providers"), dict) else {}
                providers.pop(service_registry.canonical_provider_id(provider_id), None)
                state["providers"] = providers
                store.update_provider_admin_state(data=state, actor=ctx)
                config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
                secret_id = str(config.get("api_key_secret_id") or "").strip()
                if secret_id:
                    secret_store.delete_provider_secret(secret_id, db_path=_admin_db_path(store))
        except _RouteError as exc:
            return exc.as_response()
        except admin_store.OperatorSettingsGenerationConflict:
            return _registry_conflict_response()
        return egress.projected_response(status_code=status.HTTP_200_OK, content={"service_registry": record.get("value") or {}})


    @router.put("/{provider_id}/caps")
    async def set_provider_caps(request: Request, provider_id: str) -> JSONResponse:
        ctx = _owner_guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            with secret_store.provider_mutation_lock(_admin_db_path(store)):
                _require_provider(store, provider_id)
                state = store.get_provider_admin_state()
                provider_state = _provider_state_for_update(state, provider_id)
                provider_state["caps"] = _normalize_caps(body)
                store.update_provider_admin_state(data=state, actor=ctx)
                provider = _one_provider_payload(store, effective_spending_provider, provider_id)
        except _RouteError as exc:
            return exc.as_response()
        return egress.projected_response(status_code=status.HTTP_200_OK, content={"provider": provider})

    @router.delete("/{provider_id}/caps")
    def clear_provider_caps(request: Request, provider_id: str) -> JSONResponse:
        ctx = _owner_guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            with secret_store.provider_mutation_lock(_admin_db_path(store)):
                _require_provider(store, provider_id)
                state = store.get_provider_admin_state()
                provider_state = _provider_state_for_update(state, provider_id)
                provider_state.pop("caps", None)
                store.update_provider_admin_state(data=state, actor=ctx)
                provider = _one_provider_payload(store, effective_spending_provider, provider_id)
        except _RouteError as exc:
            return exc.as_response()
        return egress.projected_response(status_code=status.HTTP_200_OK, content={"provider": provider})

    @router.put("/-/global-caps")
    async def set_global_caps(request: Request) -> JSONResponse:
        ctx = _owner_guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await _read_json_body(request)
            with secret_store.provider_mutation_lock(_admin_db_path(store)):
                state = store.get_provider_admin_state()
                state["global_caps"] = _normalize_caps(body)
                store.update_provider_admin_state(data=state, actor=ctx)
        except _RouteError as exc:
            return exc.as_response()
        return egress.projected_response(
            status_code=status.HTTP_200_OK,
            content=_providers_payload(store, effective_spending_provider),
        )

    @router.delete("/-/global-caps")
    def clear_global_caps(request: Request) -> JSONResponse:
        ctx = _owner_guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        with secret_store.provider_mutation_lock(_admin_db_path(store)):
            state = store.get_provider_admin_state()
            state["global_caps"] = {}
            store.update_provider_admin_state(data=state, actor=ctx)
        return egress.projected_response(
            status_code=status.HTTP_200_OK,
            content=_providers_payload(store, effective_spending_provider),
        )

    return router



def _ensure_provider_has_no_pending_spend(provider_id: str, path: Path) -> None:
    counts = _provider_pending_spend_counts(provider_id, path)
    if not counts:
        return
    parts = []
    for status_name in ("reserved", "needs_reconciliation"):
        count = counts.get(status_name, 0)
        if count:
            parts.append(f"{count} {status_name}")
    detail = ", ".join(parts) or "pending"
    raise _RouteError(
        status.HTTP_409_CONFLICT,
        "provider_spend_pending",
        f"Provider {service_registry.canonical_provider_id(provider_id)!r} has {detail} charge(s). Reconcile or wait for pending provider charges before removing it.",
    )


def _provider_pending_spend_counts(provider_id: str, path: Path) -> dict[str, int]:
    try:
        path.stat()
    except FileNotFoundError:
        return {}
    except OSError:
        raise _RouteError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "provider_ledger_unavailable",
            "provider spend ledger is unavailable",
        ) from None
    try:
        provider_core.ensure_ledger(path)
        with closing(sqlite3.connect(path)) as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*)
                FROM provider_spend
                WHERE lower(provider) = ? AND status IN ('reserved', 'needs_reconciliation')
                GROUP BY status
                """,
                (service_registry.canonical_provider_id(provider_id),),
            ).fetchall()
    except sqlite3.Error:
        raise _RouteError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "provider_ledger_unavailable",
            "provider spend ledger is unavailable",
        ) from None
    return {str(row[0]): int(row[1] or 0) for row in rows}

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
    db_path = _admin_db_path(store)
    plans_by_provider = _access_plans_by_provider(store)
    providers = [
        _project_provider(
            provider_id,
            service,
            state,
            usage,
            spend_available,
            provider_models,
            db_path,
            plans_by_provider,
        )
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
        _admin_db_path(store),
        _access_plans_by_provider(store),
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
    db_path: Path,
    access_plans_by_provider: Mapping[str, list[dict[str, str]]],
) -> dict[str, Any]:
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    provider_state = _provider_state(state, provider_id)
    key_status = _project_key_status(provider_id, config, db_path)
    return {
        "id": provider_id,
        "name": str(service.get("name") or provider_id),
        "enabled": bool(service.get("enabled", True)),
        "reachable": (provider_state.get("connection") or {}).get("reachable"),
        "checked_at": (provider_state.get("connection") or {}).get("checked_at"),
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
        # Read-only: a plan's Paid providers decide who can use this provider.
        "access_plans": access_plans_by_provider.get(provider_id, []),
        "cap_guarantee": _cap_guarantee(),
        "concurrency": _project_concurrency(config),
        "recommendations": _project_recommendations(provider_state, config),
        "catalog": {
            "preset_id": str(config.get("preset_id") or ""),
            "protocol": str(config.get("protocol") or ""),
            "base_url": str(config.get("base_url") or ""),
            "docs_url": str(config.get("docs_url") or ""),
            "key_url": str(config.get("key_url") or ""),
            "billing_dimensions": list(config.get("billing_dimensions") or []),
        },
    }


def _provider_probe_config(service: Mapping[str, Any], provider_id: str) -> dict[str, Any]:
    config = dict(service.get("config") if isinstance(service.get("config"), Mapping) else {})
    config.setdefault("provider_id", service_registry.canonical_provider_id(provider_id))
    return config


def _provider_key_fingerprint(config: Mapping[str, Any], db_path: Path) -> tuple[Any, ...]:
    config_revision = hashlib.sha256(
        json.dumps(config, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    secret_id = str(config.get("api_key_secret_id") or "").strip()
    if secret_id:
        try:
            secret_revision = secret_store.provider_secret_revision(secret_id, db_path=db_path)
        except Exception:
            secret_revision = "unavailable"
        return ("secret", secret_id, secret_revision, config_revision)
    api_key_env = str(config.get("api_key_env") or "").strip()
    env_value = get_env(api_key_env) if api_key_env else None
    env_revision = hashlib.sha256(env_value.encode("utf-8")).hexdigest() if env_value is not None else "missing"
    return ("env", api_key_env, env_revision, config_revision)


def _saved_provider_probe_still_current(
    store: ProviderAdminStore, provider_id: str, key_fingerprint: tuple[Any, ...]
) -> bool:
    try:
        _service_id, service = _service_for_provider(store.get_service_registry(), provider_id)
    except _RouteError:
        return False
    config = _provider_probe_config(service, provider_id)
    return _provider_key_fingerprint(config, _admin_db_path(store)) == key_fingerprint


def _project_key_status(provider_id: str, config: Mapping[str, Any], db_path: Path) -> dict[str, Any]:
    api_key_env = str(config.get("api_key_env") or "").strip()
    if api_key_env:
        return {
            "state": "set" if get_env(api_key_env) and _provider_env_ref_origin_matches(config) else "missing",
            "source": "env",
            "api_key_env": api_key_env,
        }
    secret_id = str(config.get("api_key_secret_id") or "").strip()
    if secret_id:
        try:
            status_row = secret_store.provider_secret_status(secret_id, db_path=db_path).to_dict()
            if status_row.get("state") == "set":
                secret_store.get_provider_secret(
                    secret_id,
                    db_path=db_path,
                    expected_origin=_provider_credential_origin(config),
                )
        except Exception:
            status_row = {"state": "missing", "suffix": None, "updated_at": None}
        out = _secret_key_status(status_row)
        out["api_key_env"] = secret_env_name_for_provider(provider_id, secret_id)
        out["api_key_secret_id"] = secret_id
        return out
    return {"state": "missing", "source": "none", "api_key_env": None}


def _secret_key_status(status_row: Mapping[str, Any]) -> dict[str, Any]:
    state = str(status_row.get("state") or "missing")
    out: dict[str, Any] = {"state": state, "source": "secret_store"}
    if state == "set":
        # Nothing of the key is shown, not even its last characters (decision D26).
        out["display"] = "Set"
        out["updated_at"] = status_row.get("updated_at")
    return out


def _provider_secret_id(provider_id: str) -> str:
    return provider_configuration.provider_secret_id(provider_id)


def _current_provider_secret_id(store: ProviderAdminStore, provider_id: str) -> str:
    _service_id, service = _service_for_provider(store.get_service_registry(), provider_id)
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    return str(config.get("api_key_secret_id") or "").strip()


def _provider_credential_origin(config: Mapping[str, Any]) -> str:
    return secret_store.credential_origin_from_url(str(config.get("base_url") or ""))


def _provider_env_ref_origin_matches(config: Mapping[str, Any]) -> bool:
    expected_raw = str(config.get("credential_origin") or "").strip()
    if not expected_raw:
        return False
    try:
        expected = secret_store.credential_origin_from_url(expected_raw)
        actual = secret_store.credential_origin_from_url(str(config.get("base_url") or ""))
    except Exception:
        return False
    return actual == expected


def _admin_db_path(store: ProviderAdminStore) -> Path:
    return Path(getattr(store, "path", admin_store.DB_PATH))


def _expected_registry_generation(
    store: ProviderAdminStore, body: Mapping[str, Any]
) -> int:
    requested = provider_configuration.expected_generation(body)
    if not hasattr(store, "path"):
        return 0 if requested is None else requested
    try:
        record = admin_store.get_service_registry_with_generation(path=_admin_db_path(store))
    except sqlite3.Error:
        return 0 if requested is None else requested
    current = int(record["operator_settings_generation"]) if record is not None else 0
    if requested is None:
        return current
    if requested != current:
        raise admin_store.OperatorSettingsGenerationConflict(
            f"service_registry generation changed: expected {requested}, got {current}"
        )
    return requested


def _registry_conflict_response() -> JSONResponse:
    return egress.safe_error_response(
        status.HTTP_409_CONFLICT, "service_registry_conflict", "The service registry changed. Refresh and try again.")


def _merge_provider_price_config(
    existing: Mapping[str, Any], incoming: Mapping[str, Any]
) -> dict[str, Any]:
    merged = dict(existing)
    incoming_models = incoming.get("models") if isinstance(incoming.get("models"), Mapping) else {}
    if incoming_models:
        existing_models = dict(merged.get("models") if isinstance(merged.get("models"), Mapping) else {})
        for model, price in incoming_models.items():
            if not isinstance(price, Mapping):
                continue
            model_key = str(model)
            current = existing_models.get(model_key)
            if isinstance(current, Mapping):
                existing_models[model_key] = {**dict(current), **dict(price)}
            else:
                existing_models[model_key] = dict(price)
        merged["models"] = existing_models
    for key, value in incoming.items():
        if key == "models":
            continue
        merged[key] = value
    return merged


def _provider_model_summaries(registry: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    models: dict[str, list[dict[str, Any]]] = {}
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
    # Configured disabled models and non-chat protocols must remain editable.
    for service_id, service in (registry.get("services") or {}).items():
        if not isinstance(service, Mapping):
            continue
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        if not service_registry.is_remote_provider_config(config):
            continue
        provider_id = service_registry.canonical_provider_id(config.get("provider_id") or service_id)
        raw_models = config.get("models")
        if not isinstance(raw_models, list):
            continue
        rows = []
        prices = (config.get("price") or {}).get("models") or {}
        for raw in raw_models:
            if not isinstance(raw, Mapping):
                continue
            public = str(raw.get("model_group") or raw.get("id") or raw.get("upstream_model") or "")
            if not public:
                continue
            if not public.startswith(f"{provider_id}/"):
                public = f"{provider_id}/{public}"
            row = {"model": public, "upstream_model": str(raw.get("upstream_model") or raw.get("id") or public)}
            if config.get("preset_id") or "enabled" in raw or public in prices:
                row.update({"id": public, "name": str(raw.get("name") or public), "enabled": raw.get("enabled", True), "price": prices.get(public, {})})
            rows.append(row)
        models[provider_id] = rows
    return models


def _provider_service_to_store_form(service: Mapping[str, Any]) -> dict[str, Any]:
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    form: dict[str, Any] = {
        "id": service.get("id"),
        "name": service.get("name"),
        "kind": service.get("kind"),
        "enabled": service.get("enabled", True),
        "capabilities": service.get("capabilities") or [],
    }
    form.update(dict(config))
    return form


def _clear_provider_connection_state(
    store: ProviderAdminStore, provider_id: str, *, actor: AdminContext
) -> None:
    state = store.get_provider_admin_state()
    provider_state = _provider_state_for_update(state, provider_id)
    provider_state.pop("connection", None)
    store.update_provider_admin_state(data=state, actor=actor)


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


def _service_for_provider(registry: Mapping[str, Any], provider_id: str) -> tuple[str, Mapping[str, Any]]:
    normalized_id = service_registry.canonical_provider_id(provider_id)
    services = registry.get("services") if isinstance(registry, Mapping) else {}
    if isinstance(services, Mapping):
        for raw_id, service in services.items():
            if not isinstance(service, Mapping):
                continue
            config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
            candidate = service_registry.canonical_provider_id(config.get("provider_id") or service.get("id") or raw_id)
            if candidate == normalized_id:
                return str(raw_id), service
    raise _RouteError(
        status.HTTP_404_NOT_FOUND,
        "provider_not_found",
        f"provider {provider_id!r} was not found",
    )


def _require_provider(store: ProviderAdminStore, provider_id: str) -> None:
    if service_registry.canonical_provider_id(provider_id) not in _remote_providers(store):
        raise _RouteError(
            status.HTTP_404_NOT_FOUND,
            "provider_not_found",
            f"provider {provider_id!r} was not found",
        )


def _normalize_provider_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    policy = {"caps": _normalize_caps(value.get("caps"))}
    connection = value.get("connection")
    if isinstance(connection, Mapping) and isinstance(connection.get("reachable"), bool):
        policy["connection"] = {
            "reachable": connection["reachable"],
            "checked_at": connection.get("checked_at"),
        }
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


def _drop_provider_from_plans(
    store: ProviderAdminStore, provider_id: str, *, actor: AdminContext,
) -> None:
    """A removed provider leaves every plan's Paid providers."""
    provider = service_registry.canonical_provider_id(provider_id)
    for row in store.list_access_plans():
        doc = dict(row.get("plan") or {})
        paid = [item for item in doc.get("paid_providers") or [] if service_registry.canonical_provider_id(item) != provider]
        if paid != list(doc.get("paid_providers") or []):
            doc["paid_providers"] = paid
            store.update_access_plan(plan_id=str(row["id"]), data=doc, actor=actor)


def _access_plans_by_provider(store: ProviderAdminStore) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    for row in store.list_access_plans():
        for item in (row.get("plan") or {}).get("paid_providers") or []:
            out.setdefault(service_registry.canonical_provider_id(item), []).append(
                {"id": str(row["id"]), "name": str(row["name"])})
    return out


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
        store_path = getattr(store, "path", None)
        if store_path is None:
            return
        record_audit = lambda action, **fields: admin_store.record_audit(action, path=store_path, **fields)
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


def _update_reconciled_receipt_cost(row: Mapping[str, Any], *, path: Path) -> None:
    request_id = row.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return
    receipt_store.update_cost(
        request_id,
        {
            "usd": row.get("actual_usd"),
            "source": row.get("cost_source") or "owner-reported",
            "provider": row.get("provider"),
            "cost_class": "paid-remote",
            "reservation_id": row.get("id"),
            "reconciliation_status": "settled",
        },
        path=path,
    )


def _require_admin(auth: AdminAuth, request: Request) -> AdminContext | JSONResponse:
    try:
        ctx = auth(request)
    except AdminAuthError as exc:
        return _auth_error_response(exc)
    if not isinstance(ctx, AdminContext):
        return egress.safe_error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "admin_auth_invalid", "admin auth callable must return AdminContext")
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
            supplied = request.headers.get("x-azizam-csrf") or ""
            if not hmac.compare_digest(supplied, ctx.csrf_token):
                return _auth_error_response(
                    AdminAuthError(
                        "admin session mutation requires a valid CSRF token",
                        status_code=status.HTTP_403_FORBIDDEN,
                        code="admin_csrf_required",
                    )
                )
        elif request.headers.get("x-azizam-admin-action") != "1":
            return _auth_error_response(
                AdminAuthError(
                    "admin session mutation requires same-origin admin action header",
                    status_code=status.HTTP_403_FORBIDDEN,
                    code="admin_action_header_required",
                )
            )
    return ctx


def _auth_error_response(exc: AdminAuthError) -> JSONResponse:
    return egress.safe_error_response(exc.status_code, exc.code, exc.message)


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


async def _read_json_body_or_empty(request: Request) -> dict[str, Any]:
    return await _read_json_body(request)
