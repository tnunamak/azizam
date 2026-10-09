"""Member prompt-memory settings API."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.admin_oidc import AdminSession

SessionProvider = Callable[[Request], AdminSession | None]
HouseholdProvider = Callable[[], Mapping[str, Any]]


def create_me_prompt_memory_router(
    *,
    session_provider: SessionProvider,
    household_provider: HouseholdProvider | None = None,
    prompt_memory_path: Path | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/me/api", tags=["me"])

    def _service():
        return importlib.import_module("gateway.prompt_memory")

    def _person(request: Request) -> str | JSONResponse:
        session = session_provider(request)
        if session is None:
            return JSONResponse({"error": {"code": "unauthorized", "message": "Sign in required"}}, status_code=401)
        return str(session.subject)

    def _household(request: Request) -> dict[str, Any]:
        if household_provider is not None:
            return dict(household_provider())
        return dict(getattr(request.app.state, "prompt_memory_household", None) or {})

    def _settings_response(person_id: str, request: Request) -> JSONResponse:
        service = _service()
        kwargs = {"path": prompt_memory_path} if prompt_memory_path is not None else {}
        member = service.get_member(person_id, **kwargs)
        household = service.household_default(_household(request))
        effective = service.effective(member, household)
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
        person = _person(request)
        return person if isinstance(person, JSONResponse) else _settings_response(person, request)

    @router.put("/prompt-memory", include_in_schema=False)
    async def put_prompt_memory(request: Request) -> JSONResponse:
        person = _person(request)
        if isinstance(person, JSONResponse):
            return person
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
        service = _service()
        kwargs = {"path": prompt_memory_path} if prompt_memory_path is not None else {}
        service.put_member(person, remember=remember, retention=retention, **kwargs)
        return _settings_response(person, request)

    @router.delete("/prompt-memory/prompts", include_in_schema=False)
    def delete_prompt_memory(request: Request) -> JSONResponse:
        person = _person(request)
        if isinstance(person, JSONResponse):
            return person
        service = _service()
        kwargs = {"path": prompt_memory_path} if prompt_memory_path is not None else {}
        deleted = service.delete_person(person, **kwargs)
        return JSONResponse({"deleted_prompts": int(deleted)}, headers={"Cache-Control": "no-store"})

    return router
