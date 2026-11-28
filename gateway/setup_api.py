"""Public, secret-free setup capabilities for the static setup presentation."""

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def build_setup_api_router(
    state_provider: Callable[[Request], Mapping[str, Any]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/setup/api/state", include_in_schema=False)
    async def setup_state(request: Request) -> JSONResponse:
        state = state_provider(request)
        # This public contract cannot expose sessions, secrets, or user records.
        return JSONResponse(
            {
                "owner_exists": bool(state.get("owner_exists")),
                "oidc_enabled": bool(state.get("oidc_enabled")),
                "can_recover": bool(state.get("can_recover")),
            },
            headers={"Cache-Control": "no-store"},
        )

    return router
