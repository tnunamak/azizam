"""Owner settings backed by the existing runtime database, never bootstrap writes."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import admin_store, prompt_memory, receipt_store, retention, session_policy
from gateway.admin_api import AdminAuth, AdminContext, _require_admin
from gateway import egress
from gateway.projection import public_json

INSTANCE_SETTING_KEY = "instance_presentation"
# The prompts group's view of the household policy (gateway/retention.py): the resource the owner's screen edits.
PROMPT_MEMORY_SETTING_KEY = retention.PROMPTS_VIEW_KEY


def instance_settings(path: Path, defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    value = {"name": "Azizam", "base_url": "", "theme_default": "system", **(defaults or {})}
    stored = admin_store.get_operator_setting(INSTANCE_SETTING_KEY, path=path)
    if stored:
        value.update(stored["value"])
    return value


def prompt_memory_settings(path: Path) -> dict[str, Any]:
    stored = admin_store.get_operator_setting(retention.SETTING_KEY, path=path)
    return retention.validate_prompts_view(retention.household(stored["value"] if stored else None))


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


def create_settings_router(*, auth: AdminAuth, admin_path: Path,
                           config_provider: Callable[[], Mapping[str, Any]],
                           instance_defaults: Callable[[], Mapping[str, Any]] | None = None,
                           overrides_provider: Callable[[], Mapping[str, Any]] | None = None,
                           prompt_memory_path: Path | None = None,
                           receipts_path: Path | None = None,
                           on_prompt_memory_kill: Callable[[], Any] | None = None,
                           purge_status_provider: Callable[[], Mapping[str, Any]] | None = None) -> APIRouter:
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

    def prompt_memory_response(*, extra: Mapping[str, Any] | None = None,
                               status_code: int = 200) -> JSONResponse:
        record = admin_store.get_operator_setting(retention.SETTING_KEY, path=admin_path)
        value = retention.validate_prompts_view(retention.household(record["value"] if record else None))
        purge = dict(purge_status_provider() if purge_status_provider else {})
        if prompt_memory.erasure_pending("*", admin_path=admin_path):
            purge.update(pending=True, database_pending=True, retry_scheduled=True)
        extra = dict(extra or {})
        if purge.get("database_pending"):
            extra["prompt_purge"] = {**extra.get("prompt_purge", {}), **purge}
        return JSONResponse(
            {"prompt_memory": value, "revision": admin_store.operator_setting_revision(record),
             "source": "admin.db", "apply_mode": "live", "contract": retention.public_contract(),
             **({"prompt_purge": purge} if purge.get("pending") else {}), **(extra or {})},
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    receipt_path = receipts_path if receipts_path is not None else receipt_store.DB_PATH

    def receipts_response(**extra: Any) -> JSONResponse:
        record = admin_store.get_operator_setting(receipt_store.RETENTION_SETTING_KEY, path=admin_path)
        return JSONResponse({
            "retention_days": record["value"].get("retention_days") if record else None,
            "revision": admin_store.operator_setting_revision(record),
            "source": "admin.db", "apply_mode": "live", **extra,
        }, headers={"Cache-Control": "no-store"})

    @router.get("/receipts")
    def get_receipts(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else receipts_response()

    @router.get("/receipts/preview")
    def preview_receipts(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        raw = request.query_params.get("retention_days")
        if raw not in {"forever", "365", "90", "30"}:
            return JSONResponse({"error": {"code": "invalid_retention", "message": "Choose forever, 365, 90 or 30 days."}}, status_code=400)
        days = None if raw == "forever" else int(raw)
        return JSONResponse(receipt_store.retention_preview(days, path=receipt_path), headers={"Cache-Control": "no-store"})

    @router.put("/receipts")
    async def put_receipts(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"retention_days", "preview"}:
                raise ValueError
            days = body["retention_days"]
            if (days is not None and type(days) is not int) or days not in receipt_store.RETENTION_DAYS:
                raise ValueError
        except (ValueError, TypeError):
            return JSONResponse({"error": {"code": "invalid_retention", "message": "Supply retention_days (null, 365, 90 or 30) and preview."}}, status_code=400)
        revision = request.headers.get("if-match")
        if not revision:
            return JSONResponse({"error": {"code": "settings_revision_required", "message": "Reload settings and supply If-Match."}}, status_code=428)
        saved = False

        def save_setting():
            nonlocal saved
            admin_store.put_operator_setting_with_audit(
                receipt_store.RETENTION_SETTING_KEY, {"retention_days": days},
                updated_by=ctx.subject, expected_revision=revision.strip('"'),
                audit_action="operator.receipt_retention.update", audit_actor_kind=ctx.actor_kind,
                audit_actor_id=ctx.audit_actor_id, audit_actor_label=ctx.actor_label,
                audit_target_kind="operator_setting", audit_target_id=receipt_store.RETENTION_SETTING_KEY,
                audit_metadata={"retention_days": days, "preview": body["preview"] if days is not None else None},
                path=admin_path,
            )
            saved = True

        try:
            deleted = await asyncio.to_thread(receipt_store.save_retention, days, body["preview"], save_setting, path=receipt_path)
        except (admin_store.SettingsConflictError, receipt_store.RetentionPreviewChanged) as exc:
            return JSONResponse({"error": {"code": "settings_conflict", "message": str(exc)}}, status_code=409)
        except sqlite3.Error:
            if saved:
                response = receipts_response(cleanup_pending=True)
                response.status_code = 202
                return response
            return JSONResponse({"error": {"code": "receipt_retention_unavailable", "message": "The setting wasn’t saved. Try again."}}, status_code=503)
        return receipts_response(deleted_count=deleted)

    @router.get("/instance")
    def get_instance(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else instance_response()

    def sessions_response() -> JSONResponse:
        record = admin_store.get_operator_setting(session_policy.SESSION_POLICY_SETTING_KEY, path=admin_path)
        return JSONResponse({
            "sessions": session_policy.stored_setting(admin_path),
            "defaults": session_policy.DEFAULT_SETTING,
            "bounds": {key: list(bounds) for key, bounds in session_policy.BOUNDS.items()},
            "revision": admin_store.operator_setting_revision(record),
            "source": "admin.db", "apply_mode": "live",
        }, headers={"Cache-Control": "no-store"})

    @router.get("/sessions")
    def get_sessions(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else sessions_response()

    @router.put("/sessions")
    async def put_sessions(request: Request):
        """How long a sign-in lasts: idle timeout, absolute lifetime and the step-up window."""
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = session_policy.validate_setting(await request.json())
        except (ValueError, TypeError) as exc:
            message = str(exc) if isinstance(exc, session_policy.InvalidSessionPolicy) else "Supply the three session settings as whole numbers."
            return JSONResponse({"error": {"code": "invalid_sessions", "message": message}}, status_code=400)
        revision = request.headers.get("if-match")
        if not revision:
            return JSONResponse({"error": {"code": "settings_revision_required", "message": "Reload settings and supply If-Match."}}, status_code=428)
        try:
            await asyncio.to_thread(
                admin_store.put_operator_setting_with_audit,
                session_policy.SESSION_POLICY_SETTING_KEY, body, updated_by=ctx.subject, expected_revision=revision.strip('"'),
                audit_action="operator.sessions.update", audit_actor_kind=ctx.actor_kind,
                audit_actor_id=ctx.audit_actor_id, audit_actor_label=ctx.actor_label,
                audit_target_kind="operator_setting", audit_target_id=session_policy.SESSION_POLICY_SETTING_KEY,
                audit_metadata=body, path=admin_path,
            )
        except admin_store.SettingsConflictError:
            return JSONResponse({"error": {"code": "settings_conflict", "message": "Settings changed elsewhere; reload to see the latest"}}, status_code=409)
        return sessions_response()

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
            if not isinstance(body, Mapping) or set(body) != retention.PROMPTS_VIEW_FIELDS:
                raise ValueError
            body = retention.validate_prompts_view(body)
        except (ValueError, TypeError):
            return JSONResponse(
                {"error": {"code": "invalid_prompt_memory", "message": "Supply default_remember, never_keep and retention_cap."}},
                status_code=400,
            )
        revision = request.headers.get("if-match")
        if not revision:
            return JSONResponse({"error": {"code": "settings_revision_required", "message": "Reload settings and supply If-Match."}}, status_code=428)
        try:
            changed = await asyncio.to_thread(
                admin_store.put_operator_setting_with_audit,
                PROMPT_MEMORY_SETTING_KEY, body, updated_by=ctx.subject, expected_revision=revision.strip('"'),
                audit_action="operator.prompt_memory.update", audit_actor_kind=ctx.actor_kind,
                audit_actor_id=ctx.audit_actor_id, audit_actor_label=ctx.actor_label,
                audit_target_kind="operator_setting", audit_target_id=PROMPT_MEMORY_SETTING_KEY,
                audit_metadata={"fields": sorted(body)}, path=admin_path,
            )
        except admin_store.SettingsConflictError:
            return JSONResponse({"error": {"code": "settings_conflict", "message": "Settings changed elsewhere; reload to see the latest"}}, status_code=409)
        except Exception:
            return JSONResponse({"error": {"code": "prompt_erasure_unavailable",
                                "message": "The setting wasn’t saved. Try again."}}, status_code=503)
        purge_info: dict[str, Any] = {}
        purge_failed = False
        if not body["never_keep"]:
            try:
                await asyncio.to_thread(prompt_memory.tighten_retention, body["retention_cap"], path=prompt_memory_path)
            except Exception:
                return JSONResponse({"error": {"code": "prompt_retention_pending",
                    "message": "The setting was saved. Older prompt cleanup is pending and will be retried."}},
                    status_code=202, headers={"Cache-Control": "no-store"})
        if body["never_keep"]:
            try:
                await asyncio.to_thread(prompt_memory.prune_all_before, int(changed["updated_at"]), path=prompt_memory_path)
            except Exception:
                purge_failed = True
            if on_prompt_memory_kill is not None:
                try:
                    result = await asyncio.to_thread(on_prompt_memory_kill)
                    if isinstance(result, Mapping):
                        purge_info.update(result)
                except Exception:
                    purge_failed = True
        if body["never_keep"] and not purge_failed:
            try:
                await asyncio.to_thread(prompt_memory.complete_erasure, "*", int(changed["updated_at"]), admin_path=admin_path)
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
        return JSONResponse({"defaults": {"invite_access_plan": admin_store.invite_access_plan(path=admin_path)},
                             "source": "admin.db", "apply_mode": "live",
                             "aliases": public_json(config_provider().get("model_aliases", {})), "aliases_writable": False},
                            headers={"Cache-Control": "no-store"})

    @router.get("/defaults")
    def get_defaults(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else defaults_response()

    @router.put("/defaults")
    async def put_defaults(request: Request):
        """Set the plan preselected for new invitations (null: "Ask each time")."""
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"invite_access_plan"}:
                raise ValueError()
            plan_id = body["invite_access_plan"]
            if plan_id is not None and (not isinstance(plan_id, str) or admin_store.get_access_plan(plan_id, path=admin_path) is None):
                raise ValueError()
        except (ValueError, TypeError):
            return JSONResponse({"error": {"code": "invalid_defaults", "message": "Choose an existing access plan or null"}}, status_code=400)
        admin_store.put_operator_setting_with_audit(
            admin_store.INVITE_ACCESS_PLAN_SETTING_KEY, {"plan_id": plan_id}, updated_by=ctx.subject,
            audit_action="operator.invite_access_plan.update", audit_actor_kind=ctx.actor_kind,
            audit_actor_id=ctx.audit_actor_id, audit_actor_label=ctx.actor_label,
            audit_target_kind="operator_setting", audit_target_id=admin_store.INVITE_ACCESS_PLAN_SETTING_KEY,
            audit_metadata={"plan_id": plan_id}, path=admin_path,
        )
        return defaults_response()

    def effective() -> dict[str, Any]:
        snapshot = dict(config_provider())
        snapshot["instance"] = instance()
        snapshot["receipt_retention"] = {"retention_days": receipt_store.retention_days(admin_path)}
        snapshot["operator_overrides"] = dict(overrides_provider() if overrides_provider else {})
        return {"config": snapshot, "read_only": True, "redacted": True,
                "export_url": "/admin/api/settings/effective/export"}

    @router.get("/effective")
    def get_effective(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else egress.projected_response(effective(), headers={"Cache-Control": "no-store"})

    @router.get("/effective/export")
    def export_effective(request: Request):
        ctx = guard(request)
        return ctx if isinstance(ctx, JSONResponse) else egress.projected_response(effective(), headers={
            "Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="gateway-effective-config.json"'})

    return router
