"""Member prompt-memory settings API."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import admin_store, prompt_memory
from gateway.admin_oidc import AdminSession

SessionProvider = Callable[[Request], AdminSession | None]
MutationValidator = Callable[[Request, AdminSession], JSONResponse | None]
HouseholdProvider = Callable[[], Mapping[str, Any]]
PersonPromptPurger = Callable[[str], Any]
PurgeStatusProvider = Callable[[str], Mapping[str, Any] | None]

MEMBER_DEFAULT = {"remember": True, "retention": "never"}
HOUSEHOLD_DEFAULT = {"default_remember": True, "never_keep": False, "retention_cap": "never"}
RETENTION_ORDER = {"3_months": 0, "1_year": 1, "never": 2}


def create_me_prompt_memory_router(
    *,
    session_provider: SessionProvider,
    mutation_validator: MutationValidator | None = None,
    household_provider: HouseholdProvider | None = None,
    admin_db_path: Path = admin_store.DB_PATH,
    prompt_memory_path: Path | None = None,
    on_person_prompt_purge: PersonPromptPurger | None = None,
    on_person_memory_off: Callable[[str, int], Any] | None = None,
    purge_status_provider: PurgeStatusProvider | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/me/api", tags=["me"])

    def _session(request: Request) -> AdminSession | JSONResponse:
        session = session_provider(request)
        if session is None:
            return JSONResponse({"error": {"code": "unauthorized", "message": "Sign in required"}}, status_code=401)
        return session

    def _household(request: Request) -> dict[str, Any]:
        if household_provider is not None:
            return _household_policy(household_provider())
        return _household_policy(getattr(request.app.state, "prompt_memory_household", None))

    def _settings_response(person_id: str, request: Request, *, extra: Mapping[str, Any] | None = None,
                           status_code: int = 200) -> JSONResponse:
        household = _household(request)
        member = _member_settings(person_id, path=admin_db_path)
        effective = _effective(member, household)
        extra = dict(extra or {})
        if prompt_memory.erasure_pending(person_id, admin_path=admin_db_path):
            extra["prompt_purge"] = {**extra.get("prompt_purge", {}), "pending": True,
                                     "database_pending": True, "retry_scheduled": True}
        return JSONResponse(
            {
                "prompt_memory": {
                    "remember": member["remember"],
                    "retention": member["retention"],
                    "effective_remember": effective["remember"],
                    "effective_retention": effective["retention"],
                    "household": household,
                },
                **(extra or {}),
            },
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/prompt-memory", include_in_schema=False)
    def get_prompt_memory(request: Request) -> JSONResponse:
        session = _session(request)
        if isinstance(session, JSONResponse):
            return session
        person_id = str(session.subject)
        # Queued file cleanup stays visible until it finishes.
        status = purge_status_provider(person_id) if purge_status_provider is not None else None
        if isinstance(status, Mapping) and _has_pending_purge(status):
            return _settings_response(person_id, request, extra={"prompt_purge": dict(status)})
        return _settings_response(person_id, request)

    @router.put("/prompt-memory", include_in_schema=False)
    async def put_prompt_memory(request: Request) -> JSONResponse:
        session = _session(request)
        if isinstance(session, JSONResponse):
            return session
        if mutation_validator is not None:
            error = mutation_validator(request, session)
            if error is not None:
                return error
        try:
            body = await request.json()
            if not isinstance(body, Mapping):
                raise ValueError
            remember = body.get("remember")
            retention = body.get("retention")
            if not isinstance(remember, bool) or retention not in {"never", "3_months", "1_year"}:
                raise ValueError
        except (ValueError, TypeError):
            return JSONResponse(
                {"error": {"code": "invalid_prompt_memory_settings", "message": "Supply remember and retention."}},
                status_code=400,
            )
        from gateway import prompt_memory

        person_id = str(session.subject)
        try:
            changed = await asyncio.to_thread(
                admin_store.put_prompt_memory_preference,
                person_id, remember=remember, retention=str(retention),
                updated_by=person_id, path=admin_db_path,
            )
        except Exception:
            return JSONResponse({"error": {"code": "prompt_erasure_unavailable",
                                "message": "The setting wasn’t saved. Try again."}}, status_code=503)
        purge_info: dict[str, Any] = {}
        purge_failed = False
        if remember:
            try:
                await asyncio.to_thread(prompt_memory.tighten_retention, str(retention),
                                        person_id=person_id, path=prompt_memory_path)
            except Exception:
                return JSONResponse({"error": {"code": "prompt_retention_pending",
                    "message": "The setting was saved. Older prompt cleanup is pending and will be retried."}},
                    status_code=503, headers={"Cache-Control": "no-store"})
        if not remember:
            cutoff = int(changed["updated_at"])
            kwargs = {"path": prompt_memory_path} if prompt_memory_path is not None else {}
            try:
                await asyncio.to_thread(prompt_memory.prune_person_before, person_id, cutoff, **kwargs)
            except Exception:
                purge_failed = True
            if on_person_memory_off is not None or on_person_prompt_purge is not None:
                try:
                    if on_person_memory_off is not None:
                        result = await asyncio.to_thread(on_person_memory_off, person_id, cutoff)
                    else:
                        result = await asyncio.to_thread(on_person_prompt_purge, person_id)
                    if isinstance(result, Mapping):
                        purge_info.update(result)
                except Exception:
                    purge_failed = True
        if not remember and not purge_failed:
            try:
                await asyncio.to_thread(prompt_memory.complete_erasure, person_id, cutoff, admin_path=admin_db_path)
            except Exception:
                purge_failed = True
        if purge_failed or _has_pending_purge(purge_info):
            purge_info["pending"] = True
            purge_info["retry_scheduled"] = True
            return _settings_response(
                person_id, request, extra={"prompt_purge": purge_info}, status_code=202,
            )
        if purge_info:
            return _settings_response(person_id, request, extra={"prompt_purge": purge_info})
        return _settings_response(str(session.subject), request)

    @router.delete("/prompt-memory/prompts", include_in_schema=False)
    async def delete_prompt_memory(request: Request) -> JSONResponse:
        session = _session(request)
        if isinstance(session, JSONResponse):
            return session
        if mutation_validator is not None:
            error = mutation_validator(request, session)
            if error is not None:
                return error
        from gateway import prompt_memory

        kwargs = {"path": prompt_memory_path} if prompt_memory_path is not None else {}
        person_id = str(session.subject)
        purge_info: dict[str, Any] = {}
        purge_failed = False
        try:
            cutoff = await asyncio.to_thread(prompt_memory.mark_person_erasure, person_id, admin_path=admin_db_path)
        except Exception:
            return JSONResponse({"error": {"code": "prompt_erasure_unavailable",
                "message": "Deletion could not be started. Try again."}, "deleted_prompts": 0,
                "prompt_purge": {"pending": True, "retry_scheduled": False, "retry_required": True}},
                status_code=503, headers={"Cache-Control": "no-store"})
        try:
            deleted = await asyncio.to_thread(prompt_memory.prune_person_before, person_id, cutoff, **kwargs)
        except Exception:
            deleted = 0
            purge_failed = True
        if on_person_prompt_purge is not None:
            try:
                result = await asyncio.to_thread(on_person_prompt_purge, person_id)
                if isinstance(result, Mapping):
                    purge_info.update(result)
            except Exception:
                purge_failed = True
        pending = purge_failed or _has_pending_purge(purge_info)
        if pending:
            purge_info["pending"] = True
            purge_info["retry_scheduled"] = True
            if purge_failed:
                purge_info["database_pending"] = True
        else:
            try:
                await asyncio.to_thread(prompt_memory.complete_erasure, person_id, cutoff, admin_path=admin_db_path)
            except Exception:
                pending = True
                purge_info.update(pending=True, database_pending=True, retry_scheduled=True)
        return JSONResponse(
            {"deleted_prompts": int(deleted), **({"prompt_purge": purge_info} if purge_info else {})},
            status_code=202 if pending else 200,
            headers={"Cache-Control": "no-store"},
        )

    return router


def _member_settings(person_id: str, *, path: Path) -> dict[str, Any]:
    record = admin_store.ensure_prompt_memory_preference(
        person_id, path=path,
    )
    return {
        "remember": bool(record.get("remember")),
        "retention": str(record.get("retention") or MEMBER_DEFAULT["retention"]),
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


def _household_policy(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    return {
        "default_remember": bool(source.get("default_remember", HOUSEHOLD_DEFAULT["default_remember"])),
        "never_keep": bool(source.get("never_keep", HOUSEHOLD_DEFAULT["never_keep"])),
        "retention_cap": str(source.get("retention_cap") or HOUSEHOLD_DEFAULT["retention_cap"]),
    }


def _effective(member: Mapping[str, Any], household: Mapping[str, Any]) -> dict[str, Any]:
    remember = bool(member.get("remember", True)) and not bool(household.get("never_keep"))
    retention = _shorter_retention(
        str(member.get("retention") or MEMBER_DEFAULT["retention"]),
        str(household.get("retention_cap") or HOUSEHOLD_DEFAULT["retention_cap"]),
    )
    return {"remember": remember, "retention": retention}


def _shorter_retention(left: str, right: str) -> str:
    left = left if left in RETENTION_ORDER else MEMBER_DEFAULT["retention"]
    right = right if right in RETENTION_ORDER else HOUSEHOLD_DEFAULT["retention_cap"]
    return left if RETENTION_ORDER[left] <= RETENTION_ORDER[right] else right
