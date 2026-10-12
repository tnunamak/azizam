"""Serve the exported web app at the root without touching any API route.

The committed page registry (``ui_pages.json``) is the page set. ``classify``
maps a request path to one of the export's exact shapes. ``UiRoute`` sits just
before the catch-all and fully matches only those shapes, plus GET and HEAD
with an explicit HTML ``Accept`` on a first segment that no route registers
(the HTML 404 page). It never matches partially, so it never takes part in
Starlette's 405 choice, and every registered route keeps its methods, statuses
and ``Allow`` headers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Match
from starlette.types import Receive, Scope, Send

from gateway.browser_routes import accepts_html
from gateway.ui_static import UIStaticFiles

REGISTRY_PATH = Path(__file__).with_name("ui_pages.json")
PAGES: dict[str, str] = json.loads(REGISTRY_PATH.read_text())["pages"]
ACCESS_CLASSES = frozenset({"public", "member", "admin"})
if set(PAGES.values()) - ACCESS_CLASSES:
    raise ValueError(f"{REGISTRY_PATH.name}: unknown access class")

ASSET_PREFIX = "/_next/static/"
NOT_BUILT = "The web app isn't built. Run the web build, then restart the gateway."


@dataclass(frozen=True)
class Page:
    name: str


@dataclass(frozen=True)
class PageSlashless:
    name: str


@dataclass(frozen=True)
class PagePayload:
    name: str


@dataclass(frozen=True)
class Asset:
    relative: str


UiPath = Page | PageSlashless | PagePayload | Asset


def _safe_segments(path: str) -> bool:
    segments = path.split("/")[1:]
    if segments and segments[-1] == "":
        segments = segments[:-1]
    return all(segment not in {"", ".", ".."} and "\\" not in segment and "\x00" not in segment
               for segment in segments)


def classify(path: str) -> UiPath | None:
    """Exact export shapes over the registry; ``path`` is percent-decoded."""
    if not path.startswith("/") or not _safe_segments(path):
        return None
    if path == "/":
        return Page("")
    if path == "/index.txt":
        return PagePayload("")
    if path.startswith(ASSET_PREFIX):
        relative = path[len("/"):]
        return Asset(relative) if not path.endswith("/") else None
    name, _, rest = path[1:].partition("/")
    if not name or PAGES.get(name) is None:
        return None
    if path == f"/{name}":
        return PageSlashless(name)
    if rest == "":
        return Page(name)
    if rest == "index.txt":
        return PagePayload(name)
    return None


def page_access(name: str) -> str | None:
    return PAGES.get(name)


def is_page_path(path: str) -> bool:
    return isinstance(classify(path), Page)


def page_file(name: str, payload: bool = False) -> str:
    leaf = "index.txt" if payload else "index.html"
    return f"{name}/{leaf}" if name else leaf


def first_segment(path: str) -> str:
    return path.lstrip("/").split("/", 1)[0]


def _no_cache(response: Response) -> Response:
    response.headers["cache-control"] = "no-cache"
    return response


def _method_not_allowed() -> Response:
    return JSONResponse(
        {"error": {"message": "Method not allowed.", "type": "invalid_request_error",
                   "code": "method_not_allowed"}},
        status_code=405, headers={"allow": "GET, HEAD"})


AdminGuard = Callable[[Request, str], Awaitable[Response | None]]


class UiRoute(BaseRoute):
    """The web app's pages, payloads and assets, and the HTML 404 page."""

    def __init__(self, directory: Path, admin_guard: AdminGuard,
                 registered_segments: frozenset[str] = frozenset()) -> None:
        self.directory = Path(directory)
        self.admin_guard = admin_guard
        self.registered_segments = registered_segments
        # html=False: a missing file is a plain 404, never 404.html.
        self.files = UIStaticFiles(directory=str(self.directory), html=False, check_dir=False)
        self.path = ""
        self.name = "web_ui"

    def _html_404_case(self, scope: Scope) -> bool:
        if scope["method"] not in {"GET", "HEAD"}:
            return False
        if first_segment(scope["path"]) in self.registered_segments:
            return False
        accept = ", ".join(value.decode("latin-1") for key, value in scope["headers"] if key == b"accept")
        return accepts_html(accept)

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] != "http":
            return Match.NONE, {}
        if classify(scope["path"]) is not None or self._html_404_case(scope):
            return Match.FULL, {}
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params):
        from starlette.routing import NoMatchFound

        raise NoMatchFound(name, path_params)

    def built(self) -> bool:
        return (self.directory / "index.html").is_file()

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = await self.respond(Request(scope, receive))
        await response(scope, receive, send)

    async def respond(self, request: Request) -> Response:
        path = request.url.path
        target = classify(path)
        if target is None:
            return await self._not_found_page(request)
        if request.method not in {"GET", "HEAD"}:
            return _method_not_allowed()
        if isinstance(target, PageSlashless):
            if not self.built():
                return _no_cache(PlainTextResponse(NOT_BUILT, status_code=503))
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"/{target.name}/{query}", status_code=308)
        if isinstance(target, Asset):
            return await self._file(request, target.relative)
        payload = isinstance(target, PagePayload)
        if PAGES[target.name] == "admin":
            # Before any file lookup, so a cached ETag can never skip the guard.
            denied = await self.admin_guard(request, target.name)
            if denied is not None:
                return denied
        if not self.built():
            if payload:
                return Response(status_code=404, headers={"cache-control": "no-cache"})
            return _no_cache(PlainTextResponse(NOT_BUILT, status_code=503))
        return await self._file(request, page_file(target.name, payload))

    async def _file(self, request: Request, relative: str) -> Response:
        try:
            return await self.files.get_response(relative, request.scope)
        except HTTPException as exc:
            return Response(status_code=exc.status_code, headers={"cache-control": "no-cache"})

    async def _not_found_page(self, request: Request) -> Response:
        if not self.built():
            return _no_cache(PlainTextResponse(NOT_BUILT, status_code=503))
        page = self.directory / "404.html"
        if not page.is_file():
            return Response(status_code=404, headers={"cache-control": "no-cache"})
        body = page.read_bytes()
        response = Response(b"" if request.method == "HEAD" else body, status_code=404,
                            media_type="text/html; charset=utf-8", headers={"cache-control": "no-cache"})
        response.headers["content-length"] = str(len(body))
        return response


def registered_first_segments(routes, *, exclude: tuple = ()) -> frozenset[str]:
    """First literal segment of every route; boot fails on a parameter there."""
    segments = set()
    for route in routes:
        if route in exclude:
            continue
        path = getattr(route, "path", None)
        if path is None:
            raise ValueError(f"route without a path: {route!r}")
        segment = first_segment(path)
        if "{" in segment:
            raise ValueError(f"route first segment must be literal: {path}")
        if segment:
            segments.add(segment)
    return frozenset(segments)
