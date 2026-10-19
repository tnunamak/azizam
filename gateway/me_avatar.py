"""The signed-in person's avatar: the choice, and the sign-in picture served from here.

``GET/PUT /me/api/profile/avatar`` read and change the choice. ``GET /me/api/profile/avatar/picture``
serves the picture the sign-in provider shared. The browser never loads the provider's URL: this
module fetches it on the server (no cookies, no credentials, https only, bounded in size and time),
checks that it is a small image, keeps it for a while and answers from that copy. A failed fetch
answers 404, and the page shows the generated avatar instead.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from gateway import admin_store, avatars, media_fetch
from gateway.admin_oidc import AdminSession

SessionProvider = Callable[[Request], AdminSession | None]
MutationValidator = Callable[[Request, AdminSession], JSONResponse | None]
PictureFetcher = Callable[[str], Awaitable[media_fetch.FetchedMedia]]

MAX_PICTURE_BYTES = 1024 * 1024
FETCH_TIMEOUT_SECONDS = 8.0
MAX_REDIRECTS = 2
KEEP_SECONDS = 6 * 3600
KEEP_FAILURE_SECONDS = 300
MAX_KEPT_BYTES = 32 * 1024 * 1024
MAX_KEPT_ENTRIES = 256
ADMIN_ROLES = frozenset({"owner", "superadmin", "admin"})

ALLOWED_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def image_type(data: bytes) -> str | None:
    """The image type the bytes start as, from the file signature; None if it is not an allowed image."""
    for prefix, content_type in _SIGNATURES:
        if data.startswith(prefix):
            return content_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def allowed_picture(media: media_fetch.FetchedMedia) -> tuple[bytes, str]:
    """The bytes and type to serve, or MediaFetchError. The header and the bytes must agree."""
    declared = media.content_type.split(";", 1)[0].strip().lower()
    if declared == "image/jpg":
        declared = "image/jpeg"
    if declared not in ALLOWED_TYPES:
        raise media_fetch.MediaFetchError("the sign-in picture is not a PNG, JPEG, WebP or GIF image")
    if len(media.data) > MAX_PICTURE_BYTES:
        raise media_fetch.MediaFetchError("the sign-in picture is larger than 1 MB")
    if image_type(media.data) != declared:
        raise media_fetch.MediaFetchError("the sign-in picture is not the image type it says")
    return media.data, declared


async def fetch_picture(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> media_fetch.FetchedMedia:
    """Fetch a provider's picture: https on port 443 only, public addresses only on every hop.

    ``public_only`` ignores the operator's AZIZAM_MEDIA_FETCH_ALLOW_PRIVATE_HOSTS, which is meant for
    media inputs, not for an address a sign-in provider (or a redirect) names.
    """
    return await media_fetch.safe_fetch_media(
        url, limit=MAX_PICTURE_BYTES, max_redirects=MAX_REDIRECTS, schemes=frozenset({"https"}),
        timeout=FETCH_TIMEOUT_SECONDS, accept="image/png,image/jpeg,image/webp,image/gif",
        public_only=True, ports=frozenset({443}), transport=transport,
    )


@dataclass
class _Kept:
    expires_at: float
    data: bytes | None
    content_type: str | None


class PictureCache:
    """A small in-memory copy per (person, URL): a good picture for hours, a failure for minutes."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: OrderedDict[tuple[str, str], _Kept] = OrderedDict()
        self._bytes = 0

    def get(self, key: tuple[str, str]) -> _Kept | None:
        item = self._items.get(key)
        if item is None:
            return None
        if item.expires_at <= self._clock():
            self._drop(key)
            return None
        self._items.move_to_end(key)
        return item

    def put(self, key: tuple[str, str], data: bytes | None, content_type: str | None) -> None:
        self._drop(key)
        ttl = KEEP_SECONDS if data is not None else KEEP_FAILURE_SECONDS
        self._items[key] = _Kept(self._clock() + ttl, data, content_type)
        self._bytes += len(data or b"")
        while self._items and (len(self._items) > MAX_KEPT_ENTRIES or self._bytes > MAX_KEPT_BYTES):
            self._drop(next(iter(self._items)))

    def _drop(self, key: tuple[str, str]) -> None:
        item = self._items.pop(key, None)
        if item is not None:
            self._bytes -= len(item.data or b"")


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def create_me_avatar_router(
    *,
    session_provider: SessionProvider,
    mutation_validator: MutationValidator | None = None,
    admin_db_path: Path | Callable[[], Path] = admin_store.DB_PATH,
    picture_fetcher: PictureFetcher = fetch_picture,
    cache: PictureCache | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/me/api/profile", tags=["me"])
    kept = cache if cache is not None else PictureCache()

    def db() -> Path:
        # A callable follows the gateway's store when it moves (a reload, a test).
        return admin_db_path() if callable(admin_db_path) else admin_db_path

    async def _session(request: Request) -> AdminSession | JSONResponse:
        # The session lookup reads the store; keep it off the event loop.
        session = await asyncio.to_thread(session_provider, request)
        if session is None:
            return JSONResponse({"error": {"code": "unauthorized", "message": "Sign in required"}}, status_code=401,
                                headers={"Cache-Control": "no-store"})
        return session

    def _is_admin(person_id: str) -> bool:
        return bool(ADMIN_ROLES & set(admin_store.effective_admin_roles("user", person_id, path=db())))

    inflight: dict[tuple[str, str], asyncio.Future] = {}

    async def _fetch_and_keep(key: tuple[str, str], url: str) -> _Kept:
        try:
            data, content_type = allowed_picture(await picture_fetcher(url))
        except Exception:  # noqa: BLE001 - a provider that is down, slow or wrong shows the generated mark
            kept.put(key, None, None)
            return _Kept(0.0, None, None)
        kept.put(key, data, content_type)
        return _Kept(0.0, data, content_type)

    @router.get("/avatar", include_in_schema=False)
    async def get_avatar(request: Request) -> JSONResponse:
        session = await _session(request)
        if isinstance(session, JSONResponse):
            return session
        person_id = str(session.subject)
        record = await asyncio.to_thread(avatars.get, person_id, path=db())
        return JSONResponse(avatars.public_view(person_id, record, viewer_id=person_id),
                            headers={"Cache-Control": "no-store"})

    @router.put("/avatar", include_in_schema=False)
    async def put_avatar(request: Request) -> JSONResponse:
        session = await _session(request)
        if isinstance(session, JSONResponse):
            return session
        if mutation_validator is not None:
            error = await asyncio.to_thread(mutation_validator, request, session)
            if error is not None:
                error.headers["Cache-Control"] = "no-store"
                return error
        try:
            body = await request.json()
        except ValueError:
            body = None
        # Only the word. A body that carries anything else (a URL, say) is refused, not ignored.
        if not isinstance(body, Mapping) or set(body) != {"choice"}:
            return _error("invalid_avatar_choice", "Choose a generated avatar or your sign-in picture.", 400)
        person_id = str(session.subject)
        try:
            record = await asyncio.to_thread(avatars.set_choice, person_id, body["choice"], path=db())
        except avatars.AvatarError as exc:
            return _error(exc.code, exc.message, exc.status)
        return JSONResponse(avatars.public_view(person_id, record, viewer_id=person_id),
                            headers={"Cache-Control": "no-store"})

    @router.get("/avatars", include_in_schema=False)
    async def list_avatars(request: Request) -> JSONResponse:
        """The choices the viewer may see: their own, and everyone's for an owner or admin."""
        session = await _session(request)
        if isinstance(session, JSONResponse):
            return session
        viewer_id = str(session.subject)

        def read() -> dict[str, dict]:
            everyone = _is_admin(viewer_id)
            records = avatars.list_stored(path=db()) if everyone else {
                viewer_id: avatars.get(viewer_id, path=db())}
            out = {}
            for person_id, record in records.items():
                view = avatars.public_view(person_id, record, viewer_id=viewer_id)
                if person_id != viewer_id and record["choice"] != avatars.PICTURE:
                    view["picture_url"] = None  # a picture nobody was shown stays private
                out[person_id] = {"choice": view["choice"], "picture_url": view["picture_url"]}
            return out

        return JSONResponse({"people": await asyncio.to_thread(read)}, headers={"Cache-Control": "no-store"})

    @router.get("/avatar/picture", include_in_schema=False)
    async def get_picture(request: Request) -> Response:
        session = await _session(request)
        if isinstance(session, JSONResponse):
            return session
        viewer_id = str(session.subject)
        person_id = request.query_params.get("person") or viewer_id
        missing = _error("avatar_picture_unavailable", "No picture to show.", 404)
        if person_id != viewer_id and not await asyncio.to_thread(_is_admin, viewer_id):
            return missing

        async def shown_url() -> str | None:
            # The picture to show right now: others see it only when the person chose it.
            record = await asyncio.to_thread(avatars.get, person_id, path=db())
            url = record["picture_url"]
            if not url or (person_id != viewer_id and record["choice"] != avatars.PICTURE):
                return None
            return url

        url = await shown_url()
        if url is None:
            return missing
        key = (person_id, url)
        entry = kept.get(key)
        if entry is None:
            # One fetch per key at a time: requests that arrive while it runs wait for its answer.
            task = inflight.get(key)
            if task is None:
                task = inflight[key] = asyncio.ensure_future(_fetch_and_keep(key, url))
                task.add_done_callback(lambda _done, key=key: inflight.pop(key, None))
            entry = await asyncio.shield(task)
            # The choice or the picture may have changed while we waited: answer as a request made now would.
            if await shown_url() != url:
                return missing
        if entry.data is None or entry.content_type is None:
            return missing
        # The copy is kept on the server, per person and URL. The browser keeps nothing: whether it may
        # show this picture depends on the session asking, and a session can end or lose its rights.
        return Response(content=entry.data, media_type=entry.content_type, headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="avatar"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cross-Origin-Resource-Policy": "same-origin",
        })

    return router
