"""``/showcase``: a static, discovery-driven demo page served by the gateway.

The page (``showcase/`` in the repo) is plain HTML, CSS and JavaScript with no
build step and no CDN. It builds its cards at load time from the gateway's own
discovery endpoints, so a new workflow appears without a code change.

Curated examples are real outputs produced by ``scripts/build-showcase-examples``
and stored under the gateway data directory (``data/showcase/examples``, not in
git). Each example is one directory holding ``example.json`` and its media.

Access rules:

* The static page, its assets and the example files are public. They hold no
  secrets and no private data; the example script uses only benign prompts.
* ``GET /showcase/api/status`` goes through the normal gateway authentication,
  like every other data route, because it reports backend state.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

SHOWCASE_PREFIX = "/showcase"
_PUBLIC_API_PREFIX = f"{SHOWCASE_PREFIX}/api/"

# media-src and img-src need blob: for results the Try-it panels fetch with an
# API key and play locally. Everything else stays at the gateway baseline.
SHOWCASE_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob: data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)

_STATIC_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_EXAMPLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}

StatusProvider = Callable[[], Awaitable[Mapping[str, Any]]]


def is_public_showcase_path(path: str) -> bool:
    """True for the page, its assets and example files (not ``/showcase/api``)."""
    if path == SHOWCASE_PREFIX:
        return True
    if not path.startswith(SHOWCASE_PREFIX + "/"):
        return False
    return not path.startswith(_PUBLIC_API_PREFIX)


def load_examples(examples_dir: Path) -> list[dict[str, Any]]:
    """Read every ``<id>/example.json``; skip unreadable or malformed ones."""
    out: list[dict[str, Any]] = []
    if not examples_dir.is_dir():
        return out
    for child in sorted(examples_dir.iterdir()):
        meta = child / "example.json"
        if not child.is_dir() or not _EXAMPLE_ID.match(child.name) or not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        data["id"] = child.name
        data["base"] = f"{SHOWCASE_PREFIX}/examples/{child.name}/"
        out.append(data)
    return out


def _file_response(path: Path, *, cache: str) -> Response:
    media_type = _MEDIA_TYPES.get(path.suffix.lower())
    if path.suffix.lower() == ".html":
        media_type = "text/html; charset=utf-8"
    elif path.suffix.lower() == ".js":
        media_type = "text/javascript; charset=utf-8"
    elif path.suffix.lower() == ".css":
        media_type = "text/css; charset=utf-8"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": cache, "Content-Security-Policy": SHOWCASE_CSP},
    )


def build_showcase_router(
    *,
    static_dir: Path,
    examples_dir: Path,
    authenticate: Callable[[Request, str], Any] | None = None,
    status_provider: StatusProvider | None = None,
    gallery_ui_url: str | None = None,
) -> APIRouter:
    """Build the router. ``authenticate`` follows ``proxy.authenticate_request``:
    it returns a ``Response`` on failure and anything else on success."""
    router = APIRouter()

    @router.get(SHOWCASE_PREFIX, include_in_schema=False)
    async def showcase_index() -> Response:
        if gallery_ui_url:
            return RedirectResponse(gallery_ui_url, status_code=307)
        index = static_dir / "index.html"
        if not index.is_file():
            return JSONResponse({"error": {"code": "showcase_missing"}}, status_code=404)
        return _file_response(index, cache="no-cache")

    @router.get(SHOWCASE_PREFIX + "/", include_in_schema=False)
    async def showcase_index_slash() -> Response:
        return RedirectResponse(SHOWCASE_PREFIX, status_code=307)

    @router.get(SHOWCASE_PREFIX + "/static/{name}", include_in_schema=False)
    async def showcase_static(name: str) -> Response:
        path = static_dir / name
        if not _STATIC_NAME.match(name) or name == "index.html" or not path.is_file():
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        return _file_response(path, cache="no-cache")

    @router.get(SHOWCASE_PREFIX + "/examples/index.json", include_in_schema=False)
    async def showcase_examples_index() -> Response:
        return JSONResponse(
            {"object": "list", "data": load_examples(examples_dir)},
            headers={"Cache-Control": "no-cache"},
        )

    @router.get(SHOWCASE_PREFIX + "/examples/{example_id}/{name}", include_in_schema=False)
    async def showcase_example_file(example_id: str, name: str) -> Response:
        path = examples_dir / example_id / name
        if (
            not _EXAMPLE_ID.match(example_id)
            or not _STATIC_NAME.match(name)
            or not path.is_file()
        ):
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        return _file_response(path, cache="public, max-age=300")

    @router.get(SHOWCASE_PREFIX + "/api/status", include_in_schema=False)
    async def showcase_status(request: Request) -> Response:
        if authenticate is not None:
            denied = authenticate(request, request.url.path)
            if isinstance(denied, Response):
                return denied
        if status_provider is None:
            return JSONResponse({"engines": [], "comfyui": None, "llm": None})
        return JSONResponse(await status_provider(), headers={"Cache-Control": "no-store"})

    return router
