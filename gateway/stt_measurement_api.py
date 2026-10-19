"""Owner REST and MCP commands share one long-audio operation boundary."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse

from gateway import admin_store, egress
from gateway.engine_admin import EngineRouteError
from gateway.stt_measurement import Measurements
from gateway import stt_chunking
from pathlib import Path


def public_job(job: dict | None) -> dict | None:
    return {key: value for key, value in job.items() if key != "fingerprint"} if job else None


def owner_commands(service_id: str, job_id: str = "JOB_ID", revision: str = '"os-N"', settings: dict | None = None) -> dict[str, str]:
    base = '/admin/api/engines/' + quote(service_id, safe='')
    auth = '-H "Authorization: Bearer $AZIZAM_OWNER_KEY"'
    path = base + '/stt-measurements'
    def curl(method, url, body=None, match=False):
        command = f'curl -sS -X {method} "$AZIZAM_BASE{url}" {auth}'
        if match:
            command += ' -H ' + "'If-Match: " + revision + "'"
        if body is not None:
            command += ' -H "Content-Type: application/json" --data ' + "'" + json.dumps(body) + "'"
        return command
    commands = {"options": curl("GET", path), "start": curl("POST", path, {}),
            "progress": curl("GET", path + '/' + job_id), "cancel": curl("POST", path + '/' + job_id + '/cancel', {}),
            "apply": curl("POST", path + '/' + job_id + '/apply', {}, True)}
    if settings is not None:
        commands["configure"] = curl("PATCH", base + '/stt-settings', settings, True)
    return commands


async def operate(runner: Measurements, ctx: Any, service_id: str, action: str, *, body=None, job_id=None, revision=None) -> dict:
    if "owner" not in ctx.roles:
        raise EngineRouteError(403, "owner_required", "Only the owner can measure or change long-audio settings.")
    # Require the engine for every operation, including a historic job read.
    record, generation = await asyncio.to_thread(runner.record, service_id)
    if action == "options":
        options = await asyncio.to_thread(runner.options)
        try:
            execution = stt_chunking.SttExecutionConfig.from_service(service_id=service_id, service_config=record.get("config", {}), config_directory=Path.cwd())
        except stt_chunking.SttConfigurationError as error:
            defaults = stt_chunking.bundled_chunking_defaults()
            max_segment = defaults.get("max_segment_seconds")
            if isinstance(max_segment, bool) or not isinstance(max_segment, (int, float)):
                max_segment = defaults["target_seconds"] + 2 * defaults["search_seconds"]
            settings = {"target_seconds": defaults["target_seconds"],
                        "search_seconds": defaults["search_seconds"],
                        "max_segment_seconds": max_segment,
                        "native_long_audio": False}
            note = f"Saved STT settings need repair ({error}). Safe bundled values are shown; edit the invalid setting and save."
        else:
            settings = {k: getattr(execution, k) for k in ("target_seconds", "search_seconds", "max_segment_seconds", "native_long_audio")}
            note = execution.chunking_view()["note"]
        return {**options, "job": public_job(await asyncio.to_thread(runner.get, service_id)),
                "settings": settings, "settings_note": note,
                "revision": f'"os-{generation}"', "commands": owner_commands(service_id, revision=f'"os-{generation}"', settings=settings)}
    if action == "start":
        return {"job": public_job(await runner.start(ctx, service_id, body or {}))}
    if action in {"status", "cancel", "apply"}:
        if not isinstance(job_id, str) or not job_id.isalnum() or len(job_id) > 64:
            raise EngineRouteError(400, "invalid_measurement", "Supply a measurement job id.")
        job = await asyncio.to_thread(runner.get, service_id, job_id)
        if job is None:
            raise EngineRouteError(404, "measurement_not_found", "No measurement has that id for this engine.")
        if action == "status":
            return {"job": public_job(job), "commands": owner_commands(service_id, job_id, f'"os-{generation}"')}
        if action == "cancel":
            return {"job": public_job(await runner.cancel(service_id, job_id))}
    if action in {"configure", "apply"}:
        if not isinstance(revision, str) or not revision.startswith('"os-') or not revision.endswith('"') or not revision[4:-1].isascii() or not revision[4:-1].isdecimal():
            raise EngineRouteError(412, "generation_mismatch", 'Supply the current registry revision as If-Match: "os-N".')
        try:
            expected = int(revision[4:-1])
        except ValueError:
            raise EngineRouteError(412, "generation_mismatch", "Supply the current registry revision as If-Match: \"os-N\".") from None
        if action == "configure":
            return await asyncio.to_thread(runner.configure, ctx, service_id, body, expected)
        return await asyncio.to_thread(runner.apply, ctx, service_id, job_id, expected)
    raise EngineRouteError(400, "invalid_measurement", "Unknown long-audio operation.")


def register_routes(router, *, runner: Measurements, guard) -> None:
    async def handle(request: Request, service_id: str, action: str, job_id=None):
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            # Authorization precedes even body parsing and manifest/disk access.
            if "owner" not in ctx.roles:
                raise EngineRouteError(403, "owner_required", "Only the owner can measure or change long-audio settings.")
            body = None
            if action in {"start", "configure"}:
                try:
                    raw = await request.body()
                    body = json.loads(raw) if raw else {}
                except (ValueError, UnicodeDecodeError):
                    return egress.safe_error_response(400, "invalid_json", "Request body must be a valid JSON object.")
                if not isinstance(body, dict):
                    return egress.safe_error_response(400, "invalid_json", "Request body must be a valid JSON object.")
            result = await operate(runner, ctx, service_id, action, body=body, job_id=job_id, revision=request.headers.get("if-match"))
            return egress.plain_json_response(result, response_class="stt_measurement", status_code=202 if action == "start" else 200)
        except EngineRouteError as error:
            return error.as_response()
        except admin_store.OperatorSettingsGenerationConflict:
            return egress.safe_error_response(409, "generation_mismatch", "The registry changed; refresh and try again.")
        except PermissionError:
            return egress.safe_error_response(403, "owner_required", "Only the owner can change these settings.")

    @router.get("/engines/{service_id}/stt-measurements")
    async def options(service_id: str, request: Request):
        return await handle(request, service_id, "options")

    @router.post("/engines/{service_id}/stt-measurements")
    async def start(service_id: str, request: Request):
        return await handle(request, service_id, "start")

    @router.get("/engines/{service_id}/stt-measurements/{job_id}")
    async def progress(service_id: str, job_id: str, request: Request):
        return await handle(request, service_id, "status", job_id)

    @router.post("/engines/{service_id}/stt-measurements/{job_id}/cancel")
    async def cancel(service_id: str, job_id: str, request: Request):
        return await handle(request, service_id, "cancel", job_id)

    @router.post("/engines/{service_id}/stt-measurements/{job_id}/apply")
    async def apply(service_id: str, job_id: str, request: Request):
        return await handle(request, service_id, "apply", job_id)

    @router.patch("/engines/{service_id}/stt-settings")
    async def configure(service_id: str, request: Request):
        return await handle(request, service_id, "configure")
