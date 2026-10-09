"""Member prompt-memory settings API."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import admin_store
from gateway.admin_oidc import AdminSession

SessionProvider = Callable[[Request], AdminSession | None]
MutationValidator = Callable[[Request, AdminSession], JSONResponse | None]
HouseholdProvider = Callable[[], Mapping[str, Any]]

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

    def _settings_response(person_id: str, request: Request) -> JSONResponse:
        household = _household(request)
        member = _member_settings(person_id, path=admin_db_path, default_remember=household["default_remember"])
        effective = _effective(member, household)
        return JSONResponse(
            {
                "prompt_memory": {
                    "remember": member["remember"],
                    "retention": member["retention"],
                    "effective_remember": effective["remember"],
                    "effective_retention": effective["retention"],
                    "household": household,
                }
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/prompt-memory", include_in_schema=False)
    def get_prompt_memory(request: Request) -> JSONResponse:
        session = _session(request)
        return session if isinstance(session, JSONResponse) else _settings_response(str(session.subject), request)

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
        admin_store.put_prompt_memory_preference(
            str(session.subject),
            remember=remember,
            retention=str(retention),
            updated_by=str(session.subject),
            path=admin_db_path,
        )
        return _settings_response(str(session.subject), request)

    @router.delete("/prompt-memory/prompts", include_in_schema=False)
    def delete_prompt_memory(request: Request) -> JSONResponse:
        session = _session(request)
        if isinstance(session, JSONResponse):
            return session
        if mutation_validator is not None:
            error = mutation_validator(request, session)
            if error is not None:
                return error
        from gateway import prompt_memory

        kwargs = {"path": prompt_memory_path} if prompt_memory_path is not None else {}
        deleted = prompt_memory.delete_person(str(session.subject), **kwargs)
        return JSONResponse({"deleted_prompts": int(deleted)}, headers={"Cache-Control": "no-store"})

    return router


def _member_settings(person_id: str, *, path: Path, default_remember: bool) -> dict[str, Any]:
    record = admin_store.get_prompt_memory_preference(person_id, path=path)
    if record is None:
        return {**MEMBER_DEFAULT, "remember": default_remember}
    return {
        "remember": bool(record.get("remember")),
        "retention": str(record.get("retention") or MEMBER_DEFAULT["retention"]),
    }


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
