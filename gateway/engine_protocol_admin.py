"""Installed protocol choices for the engine editor, using the dispatch loader."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import egress
from gateway.engine_protocol import description


def installed_protocols() -> dict[str, Any]:
    """Expose validation metadata, never source contents or parser error values."""
    loaded = description.load_registry()
    user_root = description.user_directory()
    protocols = []
    for protocol_id, doc in sorted(loaded.items()):
        count = len(doc.operations)
        protocols.append({
            "id": protocol_id,
            "title": doc.title or protocol_id,
            "executor": doc.executor,
            "operations": sorted(doc.operations),
            "source": "user" if (user_root / f"{protocol_id}.yml").is_file() else "seed",
            "dispatch_supported": doc.executor == "http-v1",
            "validation": {"valid": True, "message": f"Valid description. {count} operation{'s' if count != 1 else ''}."},
        })
    return {"protocols": protocols, "problems": description.registry_problems()}


def register_engine_protocol_routes(router: APIRouter, *, guard: Callable[[Request], Any]) -> None:
    @router.get("/engine-protocols")
    async def engine_protocols(request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return egress.plain_json_response(await asyncio.to_thread(installed_protocols))
