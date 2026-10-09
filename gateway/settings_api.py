"""Owner settings backed by the existing runtime database, never bootstrap writes."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import admin_store, lan_auth_policy, prompt_memory
from gateway.admin_api import AdminAuth, AdminContext, _require_admin

INSTANCE_SETTING_KEY = "instance_presentation"
PROMPT_MEMORY_SETTING_KEY = "prompt_memory"
PROMPT_MEMORY_DEFAULT = {
    "default_remember": True,
    "never_keep": False,
    "retention_cap": "never",
}


def instance_settings(path: Path, defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    value = {"name": "Azizam", "base_url": "", "theme_default": "system", **(defaults or {})}
    stored = admin_store.get_operator_setting(INSTANCE_SETTING_KEY, path=path)
    if stored:
        value.update(stored["value"])
    return value


def prompt_memory_settings(path: Path) -> dict[str, Any]:
    stored = admin_store.get_operator_setting(PROMPT_MEMORY_SETTING_KEY, path=path)
    value = dict(PROMPT_MEMORY_DEFAULT)
    if stored:
        value.update(_validate_prompt_memory(stored["value"]))
    return value


def _validate_prompt_memory(value: Mapping[str, Any]) -> dict[str, Any]:
    default_remember = value.get("default_remember", PROMPT_MEMORY_DEFAULT["default_remember"])
    never_keep = value.get("never_keep", PROMPT_MEMORY_DEFAULT["never_keep"])
    retention_cap = value.get("retention_cap", PROMPT_MEMORY_DEFAULT["retention_cap"])
    if not isinstance(default_remember, bool) or not isinstance(never_keep, bool):
        raise ValueError("prompt memory switches must be booleans")
    if retention_cap not in {"never", "3_months", "1_year"}:
        raise ValueError("retention_cap must be never, 3_months, or 1_year")
    return {
        "default_remember": default_remember,
        "never_keep": never_keep,
        "retention_cap": retention_cap,
    }


def _has_pending_purge(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).endswith("pending") and item not in (None, False, 0, ""):
                return True
            if _has_pending_purge(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_pending_purge(item) for item in value)
    return False


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
                           overrides_provider: Callable[[], Mapping[str, Any]] | None = None,
                           prompt_memory_path: Path | None = None,
                           on_prompt_memory_kill: Callable[[], Any] | None = None) -> APIRouter:
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
        record = admin_store.get_operator_setting(INSTANCE_SETTING_KEY, path=admin_path)
        value = {"name": "Azizam", "base_url": "", "theme_default": "system", **(instance_defaults() if instance_defaults else {})}
        if record:
            value.update(record["value"])
        return JSONResponse({"instance": value, "revision": admin_store.operator_setting_revision(record), "source": "admin.db", "apply_mode": "live"}, headers={"Cache-Control": "no-store"})

    def lan_placeholder_keys_response() -> JSONResponse:
        return JSONResponse({
            "lan_placeholder_keys": lan_auth_policy.lan_placeholder_keys(admin_path),
            "source": "admin.db",
            "apply_mode": "live",
        }, headers={"Cache-Control": "no-store"})

    def prompt_memory_response(*, extra: Mapping[str, Any] | None = None,
                               status_code: int = 200) -> JSONResponse:
        record = admin_store.get_operator_setting(PROMPT_MEMORY_SETTING_KEY, path=admin_path)
        value = dict(PROMPT_MEMORY_DEFAULT)
        if record:
            value.update(_validate_prompt_memory(record["value"]))
        return JSONResponse(
            {"prompt_memory": value, "revision": admin_store.operator_setting_revision(record),
             "source": "admin.db", "apply_mode": "live", **(extra or {})},
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/instance")
    def get_instance(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else instance_response()

    @router.get("/prompt-memory")
    def get_prompt_memory(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else prompt_memory_response()

    @router.put("/prompt-memory")
    async def put_prompt_memory(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await request.json()
            if not isinstance(body, Mapping) or set(body) != {"default_remember", "never_keep", "retention_cap"}:
                raise ValueError
            body = _validate_prompt_memory(body)
        except (ValueError, TypeError):
            return JSONResponse(
                {"error": {"code": "invalid_prompt_memory", "message": "Supply default_remember, never_keep and retention_cap."}},
                status_code=400,
            )
        revision = request.headers.get("if-match")
        if not revision:
            return JSONResponse({"error": {"code": "settings_revision_required", "message": "Reload settings and supply If-Match."}}, status_code=428)
        try:
            await asyncio.to_thread(
                admin_store.put_operator_setting_with_audit,
                PROMPT_MEMORY_SETTING_KEY, body, updated_by=ctx.subject, expected_revision=revision.strip('"'),
                audit_action="operator.prompt_memory.update", audit_actor_kind=ctx.actor_kind,
                audit_actor_id=ctx.audit_actor_id, audit_actor_label=ctx.actor_label,
                audit_target_kind="operator_setting", audit_target_id=PROMPT_MEMORY_SETTING_KEY,
                audit_metadata={"fields": sorted(body)}, path=admin_path,
            )
        except admin_store.SettingsConflictError:
            return JSONResponse({"error": {"code": "settings_conflict", "message": "Settings changed elsewhere; reload to see the latest"}}, status_code=409)
        purge_info: dict[str, Any] = {}
        purge_failed = False
        if body["never_keep"]:
            try:
                await asyncio.to_thread(prompt_memory.delete_all, path=prompt_memory_path)
            except Exception:
                purge_failed = True
            if on_prompt_memory_kill is not None:
                try:
                    result = await asyncio.to_thread(on_prompt_memory_kill)
                    if isinstance(result, Mapping):
                        purge_info.update(result)
                except Exception:
                    purge_failed = True
        else:
            try:
                await asyncio.to_thread(prompt_memory.tighten_retention, body["retention_cap"], path=prompt_memory_path)
            except Exception:
                purge_failed = True
        pending = purge_failed or _has_pending_purge(purge_info)
        if pending:
            purge_info["pending"] = True
            purge_info["retry_scheduled"] = True
            return prompt_memory_response(extra={"prompt_purge": purge_info}, status_code=202)
        if purge_info:
            return prompt_memory_response(extra={"prompt_purge": purge_info})
        return prompt_memory_response()

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
            if any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in body["base_url"]):
                raise ValueError("Base URL must not contain whitespace or control characters")
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
        revision = request.headers.get("if-match")
        if not revision:
            return JSONResponse({"error": {"code": "settings_revision_required", "message": "Reload settings and supply If-Match."}}, status_code=428)
        try:
            admin_store.put_operator_setting_with_audit(
                INSTANCE_SETTING_KEY, body, updated_by=ctx.subject, expected_revision=revision.strip('"'),
                audit_action="operator.instance.update", audit_actor_kind=ctx.actor_kind,
                audit_actor_id=ctx.audit_actor_id, audit_actor_label=ctx.actor_label,
                audit_target_kind="operator_setting", audit_target_id=INSTANCE_SETTING_KEY,
                audit_metadata={"fields": sorted(body)}, path=admin_path,
            )
        except admin_store.SettingsConflictError:
            return JSONResponse({"error": {"code": "settings_conflict", "message": "Settings changed elsewhere; reload to see the latest"}}, status_code=409)
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

    @router.get("/lan-placeholder-keys")
    def get_lan_placeholder_keys(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else lan_placeholder_keys_response()

    @router.put("/lan-placeholder-keys")
    async def put_lan_placeholder_keys(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {lan_auth_policy.LAN_PLACEHOLDER_KEYS_SETTING_KEY}:
                raise lan_auth_policy.LanPlaceholderKeysError("supply lan_placeholder_keys")
            values = lan_auth_policy.validate_lan_placeholder_keys(
                body[lan_auth_policy.LAN_PLACEHOLDER_KEYS_SETTING_KEY]
            )
        except (TypeError, ValueError):
            return JSONResponse({
                "error": {
                    "code": "invalid_lan_placeholder_keys",
                    "message": "Supply a list of 0-64 placeholder key strings.",
                }
            }, status_code=400)
        admin_store.put_operator_setting_with_audit(
            lan_auth_policy.LAN_PLACEHOLDER_KEYS_SETTING_KEY,
            {lan_auth_policy.LAN_PLACEHOLDER_KEYS_SETTING_KEY: values},
            updated_by=ctx.subject,
            audit_action="operator.lan_placeholder_keys.update",
            audit_actor_kind=ctx.actor_kind,
            audit_actor_id=ctx.audit_actor_id,
            audit_actor_label=ctx.actor_label,
            audit_target_kind="operator_setting",
            audit_target_id=lan_auth_policy.LAN_PLACEHOLDER_KEYS_SETTING_KEY,
            audit_metadata={"count": len(values)},
            path=admin_path,
        )
        return lan_placeholder_keys_response()

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
        snapshot["auth_policy"] = {
            "lan_placeholder_keys": lan_auth_policy.lan_placeholder_keys(admin_path),
        }
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
