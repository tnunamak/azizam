"""Owner and administrator REST views of retention facts.

Reads use only local catalog and SQLite data. Editing a list, including an
empty list, creates an owner override; upgrades never silently restore seeds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import admin_store, retention, retention_facts, service_registry


def _service_id(registry: Mapping[str, Any], target_type: str, target_id: str) -> str | None:
    services = registry.get("services") or {}
    if target_type == "engine":
        row = services.get(target_id)
        config = row.get("config") or {} if isinstance(row, Mapping) else {}
        return target_id if isinstance(row, Mapping) and not service_registry.is_remote_provider_config(config) else None
    for service_id, row in services.items():
        if not isinstance(row, Mapping):
            continue
        config = row.get("config") or {}
        if service_registry.is_remote_provider_config(config) and (service_id == target_id or config.get("provider_id") == target_id):
            return str(service_id)
    return None


def register_routes(
    router: APIRouter, *, store: Any,
    guard: Callable[[Request], Any], read_json: Callable[[Request], Any],
) -> None:
    def view(service_id: str | None = None) -> dict[str, Any]:
        registry = store.get_service_registry()
        if service_id is not None and service_id not in (registry.get("services") or {}):
            raise KeyError(service_id)
        return retention_facts.ledger(
            registry,
            kinds=retention.bundled().data["kinds"],
            overrides=admin_store.retention_fact_overrides(path=store.path),
            service_id=service_id,
        )

    def reply(data: dict[str, Any], code: int = 200) -> JSONResponse:
        return JSONResponse({"ledger": data}, status_code=code, headers={"Cache-Control": "no-store"})

    @router.get("/settings/retention/ledger")
    def retention_ledger(request: Request) -> JSONResponse:
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return reply(view())

    @router.get("/engines/{service_id}/retention/ledger")
    def engine_ledger(service_id: str, request: Request) -> JSONResponse:
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        registry = store.get_service_registry()
        actual = _service_id(registry, "engine", service_id)
        if actual is None:
            return JSONResponse({"error": {"code": "engine_not_found", "message": "Engine not found"}}, status_code=404)
        return reply(view(actual))

    def get_fact_override(target_type: str, target_id: str, request: Request) -> JSONResponse:
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        kind = {"engines": "engine", "providers": "provider", "systems": "system"}[target_type]
        actual = (target_id if target_id in retention.bundled().data.get("system_facts", {}) else None)
        if kind != "system":
            actual = _service_id(store.get_service_registry(), kind, target_id)
        if actual is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        overrides = admin_store.retention_fact_overrides(path=store.path)
        return JSONResponse({"retention_facts": overrides.get((kind, actual)),
                             "overridden": (kind, actual) in overrides},
                            headers={"Cache-Control": "no-store"})

    async def put_fact_override(target_type: str, target_id: str, request: Request) -> JSONResponse:
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if "owner" not in ctx.roles:
            return JSONResponse({"error": {"code": "owner_required", "message": "Only the owner may edit facts"}}, status_code=403)
        kind = {"engines": "engine", "providers": "provider", "systems": "system"}[target_type]
        actual = (target_id if target_id in retention.bundled().data.get("system_facts", {}) else None)
        if kind != "system":
            actual = _service_id(store.get_service_registry(), kind, target_id)
        if actual is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        body = await read_json(request)
        if not isinstance(body, Mapping) or set(body) != {"retention_facts"}:
            return JSONResponse({"error": {"code": "invalid_facts", "message": "Send retention_facts only"}}, status_code=400)
        try:
            admin_store.put_retention_fact_override(
                kind, actual, body["retention_facts"], updated_by=ctx.audit_actor_id, path=store.path,
            )
        except ValueError as exc:
            return JSONResponse({"error": {"code": "invalid_facts", "message": str(exc)}}, status_code=400)
        return reply(view(None if kind == "system" else actual))

    @router.get("/engines/{service_id}/retention/facts")
    def get_engine_facts(service_id: str, request: Request) -> JSONResponse:
        return get_fact_override("engines", service_id, request)

    @router.put("/engines/{service_id}/retention/facts")
    async def put_engine_facts(service_id: str, request: Request) -> JSONResponse:
        return await put_fact_override("engines", service_id, request)

    @router.get("/providers/{provider_id}/retention/facts")
    def get_provider_facts(provider_id: str, request: Request) -> JSONResponse:
        return get_fact_override("providers", provider_id, request)

    @router.put("/providers/{provider_id}/retention/facts")
    async def put_provider_facts(provider_id: str, request: Request) -> JSONResponse:
        return await put_fact_override("providers", provider_id, request)

    @router.get("/settings/retention/systems/{system_id}/facts")
    def get_system_facts(system_id: str, request: Request) -> JSONResponse:
        return get_fact_override("systems", system_id, request)

    @router.put("/settings/retention/systems/{system_id}/facts")
    async def put_system_facts(system_id: str, request: Request) -> JSONResponse:
        return await put_fact_override("systems", system_id, request)
