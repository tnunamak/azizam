"""A small engine for the Add engine browser journeys, started per spec on a lane's strict port.

/keyed/v1 lists one model only for the right key and refuses any other key
with 401; /empty/v1 answers with no models and no routes, so nothing is
recognised. Every request it gets is logged with the Authorization header
it carried (GET /fixture/seen), so a journey can prove at the gateway's
outbound boundary which key reached which address. The key is synthetic.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

GOOD_KEY = "sk-journey-gate-key"
_seen: list[dict[str, str]] = []


@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.url.path != "/health" and not request.url.path.startswith("/fixture/"):
        _seen.append({"method": request.method, "path": request.url.path,
                      "authorization": request.headers.get("authorization", "")})
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/fixture/seen")
async def seen() -> dict[str, list[dict[str, str]]]:
    return {"requests": list(_seen)}


@app.post("/fixture/forget")
async def forget() -> dict[str, int]:
    count = len(_seen)
    _seen.clear()
    return {"forgotten": count}


def _refused() -> JSONResponse:
    return JSONResponse({"error": {"message": "Invalid API key."}}, status_code=401)


@app.get("/keyed/v1/models")
async def keyed_models(request: Request):
    if request.headers.get("authorization") != f"Bearer {GOOD_KEY}":
        return _refused()
    return {"object": "list", "data": [{"id": "gate-chat", "object": "model", "owned_by": "gate"}]}


@app.post("/keyed/v1/chat/completions")
async def keyed_chat(request: Request):
    if request.headers.get("authorization") != f"Bearer {GOOD_KEY}":
        return _refused()
    return JSONResponse({"error": {"message": "'messages' is required"}}, status_code=400)


@app.get("/empty/v1/models")
async def empty_models():
    return {"object": "list", "data": []}
