"""Owner settings backed by the existing runtime database, never bootstrap writes."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import admin_store
from gateway.admin_api import AdminAuth, AdminContext, _require_admin

INSTANCE_SETTING_KEY = "instance_presentation"


def instance_settings(path: Path, defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    value = {"name": "AI Gateway", "base_url": "", "theme_default": "system", **(defaults or {})}
    stored = admin_store.get_operator_setting(INSTANCE_SETTING_KEY, path=path)
    if stored:
        value.update(stored["value"])
    return value


def redact_config(value: Any) -> Any:
    """Redact nested credentials and credential-bearing URLs in runtime data."""
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if re.search(
                r"secret|password|credential|key|token|authorization|cookie|headers", str(key), re.I
            ) else redact_config(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_config(item) for item in value]
    if isinstance(value, str):
        if re.search(r"\b(?:opk_|oak_|sk-)[A-Za-z0-9_-]+|^Bearer\s|^data:", value, re.I):
            return "<redacted>"
        if "://" in value:
            try:
                url = urlsplit(value)
                if url.username or url.password or url.query or url.fragment:
                    host = url.hostname or ""
                    if ":" in host:
                        host = f"[{host}]"
                    return f"{url.scheme}://{host}" + (f":{url.port}" if url.port else "") + url.path
            except ValueError:
                return "<redacted>"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "<not serializable>"


def create_settings_router(*, auth: AdminAuth, admin_path: Path,
                           config_provider: Callable[[], Mapping[str, Any]],
                           instance_defaults: Callable[[], Mapping[str, Any]] | None = None,
                           overrides_provider: Callable[[], Mapping[str, Any]] | None = None) -> APIRouter:
    router = APIRouter(prefix="/admin/api/settings", tags=["admin"])

    def guard(request: Request) -> AdminContext | JSONResponse:
        ctx = _require_admin(auth, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if "owner" not in ctx.roles:
            return JSONResponse({"error": {"code": "owner_required", "message": "Owner role required"}}, status_code=403)
        return ctx

    def instance() -> dict[str, Any]:
        return instance_settings(admin_path, instance_defaults() if instance_defaults else None)

    def instance_response() -> JSONResponse:
        return JSONResponse({"instance": instance(), "source": "admin.db", "apply_mode": "live"}, headers={"Cache-Control": "no-store"})

    @router.get("/instance")
    def get_instance(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else instance_response()

    @router.put("/instance")
    async def put_instance(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"name", "base_url", "theme_default"}:
                raise ValueError("Supply name, base_url and theme_default")
            if not all(isinstance(v, str) for v in body.values()):
                raise ValueError("Instance fields must be text")
            body = {k: v.strip() for k, v in body.items()}
            if not body["name"] or len(body["name"]) > 100:
                raise ValueError("Name must contain 1–100 characters")
            if body["theme_default"] not in {"system", "light", "dark"}:
                raise ValueError("Choose system, light or dark theme")
            if body["base_url"]:
                url = urlsplit(body["base_url"])
                if (url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password
                        or url.query or url.fragment or url.path not in {"", "/"} or len(body["base_url"]) > 2048):
                    raise ValueError("Base URL must be an HTTP(S) origin without credentials, path, query or fragment")
                _ = url.port
                body["base_url"] = body["base_url"].rstrip("/")
        except (ValueError, TypeError):
            return JSONResponse({"error": {"code": "invalid_instance", "message": "Supply a name, a valid HTTP(S) base URL and system/light/dark theme"}}, status_code=400)
        admin_store.put_operator_setting_with_audit(
            INSTANCE_SETTING_KEY, body, updated_by=ctx.subject,
            audit_action="operator.instance.update", audit_actor_kind=ctx.actor_kind,
            audit_actor_id=ctx.audit_actor_id, audit_actor_label=ctx.actor_label,
            audit_target_kind="operator_setting", audit_target_id=INSTANCE_SETTING_KEY,
            audit_metadata={"fields": sorted(body)}, path=admin_path,
        )
        return instance_response()

    def defaults_response() -> JSONResponse:
        mappings = admin_store.list_runtime_profile_mappings(path=admin_path)
        universal = next((m for m in mappings if m["source_kind"] == "all_users"), None)
        return JSONResponse({"defaults": {"profile_id": universal["profile_id"] if universal else None},
                             "source": "admin.db", "apply_mode": "live",
                             "aliases": redact_config(config_provider().get("model_aliases", {})), "aliases_writable": False},
                            headers={"Cache-Control": "no-store"})

    @router.get("/defaults")
    def get_defaults(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else defaults_response()

    @router.put("/defaults")
    async def put_defaults(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"profile_id"}:
                raise ValueError()
            profile_id = body["profile_id"]
            if profile_id is not None and (not isinstance(profile_id, str) or not profile_id):
                raise ValueError()
            admin_store.replace_default_runtime_profile_mapping(profile_id=profile_id, actor=ctx, path=admin_path)
        except (ValueError, TypeError):
            return JSONResponse({"error": {"code": "invalid_defaults", "message": "Choose an existing access plan or null"}}, status_code=400)
        return defaults_response()

    def effective() -> dict[str, Any]:
        snapshot = dict(config_provider())
        snapshot["instance"] = instance()
        snapshot["operator_overrides"] = dict(overrides_provider() if overrides_provider else {})
        snapshot["runtime_profile_mappings"] = admin_store.list_runtime_profile_mappings(path=admin_path)
        return {"config": redact_config(snapshot), "read_only": True, "redacted": True,
                "export_url": "/admin/api/settings/effective/export"}

    @router.get("/effective")
    def get_effective(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else JSONResponse(effective(), headers={"Cache-Control": "no-store"})

    @router.get("/effective/export")
    def export_effective(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else JSONResponse(effective(), headers={
            "Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="gateway-effective-config.json"'})

    return router
