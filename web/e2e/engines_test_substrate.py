from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import litellm
from fastapi import FastAPI, HTTPException, Request


app = FastAPI()

STRICT_MODEL = "strict-chat"
_router: litellm.Router | None = None
_accepted_models: list[str] = []


def _own_port(request: Request) -> int:
    host = request.headers.get("host") or ""
    try:
        return int(host.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        server = request.scope.get("server")
        if isinstance(server, tuple) and len(server) >= 2:
            return int(server[1])
    raise HTTPException(status_code=400, detail="Could not determine fixture port.")


def _reject_external_api_bases(model_list: list[dict[str, Any]], port: int) -> None:
    allowed_hosts = {"127.0.0.1", "localhost"}
    for row in model_list:
        if not isinstance(row, dict):
            raise HTTPException(status_code=400, detail="model_list rows must be objects.")
        params = row.get("litellm_params")
        if not isinstance(params, dict):
            raise HTTPException(
                status_code=400,
                detail="model_list rows must include litellm_params.",
            )
        api_base = str(params.get("api_base") or "").strip()
        parsed = urlparse(api_base)
        if parsed.scheme not in {"http", "https"}:
            raise HTTPException(status_code=400, detail="api_base must be absolute.")
        if parsed.hostname not in allowed_hosts or parsed.port != port:
            raise HTTPException(
                status_code=400,
                detail="api_base must point at this fixture's loopback port.",
            )
        if parsed.path.rstrip("/") != "/v1":
            raise HTTPException(
                status_code=400,
                detail="api_base must point at this fixture's /v1 backend.",
            )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def strict_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": STRICT_MODEL, "object": "model", "owned_by": "test"}],
    }


@app.post("/v1/chat/completions")
async def strict_chat(request: Request) -> dict[str, Any]:
    body = await request.json()
    model = str(body.get("model") or "")
    _accepted_models.append(model)
    if model != STRICT_MODEL:
        raise HTTPException(
            status_code=404,
            detail={"code": "model_not_found", "message": "Unknown model"},
        )
    return {
        "id": "chatcmpl-strict",
        "object": "chat.completion",
        "created": 1,
        "model": STRICT_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Strict model mapping works.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@app.post("/test/configure")
async def configure(request: Request) -> dict[str, Any]:
    global _router
    body = await request.json()
    model_list = body.get("model_list")
    if not isinstance(model_list, list):
        raise HTTPException(status_code=400, detail="model_list must be a list.")
    _reject_external_api_bases(model_list, _own_port(request))
    _router = litellm.Router(
        model_list=model_list,
        num_retries=0,
        allowed_fails=0,
        set_verbose=False,
    )
    return {
        "configured": True,
        "models": [
            str(row.get("model_name"))
            for row in model_list
            if isinstance(row, dict) and row.get("model_name")
        ],
    }


@app.get("/substrate/v1/models")
async def substrate_models() -> dict[str, Any]:
    if _router is None:
        raise HTTPException(status_code=503, detail="Router is not configured.")
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "owned_by": "test-substrate"}
            for name in sorted(_router.model_names)
        ],
    }


@app.post("/substrate/v1/chat/completions")
async def substrate_chat(request: Request) -> dict[str, Any]:
    if _router is None:
        raise HTTPException(status_code=503, detail="Router is not configured.")
    body = await request.json()
    result = await _router.acompletion(**body)
    return result.model_dump()


@app.get("/test/observations")
async def observations() -> dict[str, list[str]]:
    return {"accepted_models": list(_accepted_models)}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", os.environ.get("UI_E2E_SUBSTRATE_PORT", "18186")))
    if port < 18000 or port > 18999:
        raise SystemExit("Fixture port must be between 18000 and 18999.")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
