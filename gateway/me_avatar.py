"""The signed-in person's avatar: the choice, and the picture it shows, served from here.

``GET/PUT /me/api/profile/avatar`` read and change the choice. ``GET /me/api/profile/avatar/picture`` serves
the picture the person shows: the one the gateway made for them, or the sign-in picture the provider shared.
The browser never loads the provider's URL: this module fetches it on the server (no cookies, no credentials,
https only, bounded in size and time), checks that it is a small image, keeps it for a while and answers from
that copy. A failed fetch answers 404, and the page shows the palette initial instead.

A person may also have the gateway make a picture of them. ``GET .../avatar/generator`` says whether an image
route exists, ``POST .../avatar/generate`` starts a make job, ``POST .../avatar/generated`` keeps one of the
person's own results, and ``DELETE .../avatar/generated`` removes it (gateway/avatar_generation.py).

A person with no sign-in picture and no chosen avatar also gets one picture made for them, once:
``POST .../avatar/default`` starts it on a signed-in page load that finds it due. Only one starts at a time, and
none while other picture work is busy. A load that finds either, or a start refused for capacity, starts nothing
and uses no attempt, so a later load tries again. ``GET`` and ``POST`` keep the picture once its job has stopped,
so the page sees it on its next read. Once the attempt is used, the default is never repeated.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from gateway import admin_store, avatar_generation, avatars, media_fetch
from gateway.admin_oidc import AdminSession
from gateway.avatars import image_type
from gateway.public_failure import PublicFailure

SessionProvider = Callable[[Request], AdminSession | None]
MutationValidator = Callable[[Request, AdminSession], JSONResponse | None]
PictureFetcher = Callable[[str], Awaitable[media_fetch.FetchedMedia]]
log = logging.getLogger(__name__)
# The make job states after which it makes no more pictures.
JOB_STOPPED = frozenset({"succeeded", "failed", "canceled", "cancelled"})
JOB_SUCCEEDED = "succeeded"
# A start refused with one of these statuses is busy work or no capacity: a later load tries again.
RETRY_LATER_STATUS = frozenset({429, 503})

MAX_PICTURE_BYTES = 1024 * 1024
FETCH_TIMEOUT_SECONDS = 8.0
MAX_REDIRECTS = 2
KEEP_SECONDS = 6 * 3600
KEEP_FAILURE_SECONDS = 300
MAX_KEPT_BYTES = 32 * 1024 * 1024
MAX_KEPT_ENTRIES = 256
ADMIN_ROLES = frozenset({"owner", "superadmin", "admin"})

ALLOWED_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


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


@dataclass(frozen=True)
class AvatarGeneration:
    """What the app supplies so the gateway can make a picture of a person: the route, the job and the Library."""

    # The model id of the image route the person may use right now, or None when there is none.
    model_for: Callable[[str], str | None]
    # Start the person's make job for the prompt and return the job as the make page shows it.
    start: Callable[[Request, str, str, str], Mapping[str, Any]]
    # The file of one of the person's own Library pictures, or None when it is not theirs or not a picture.
    own_picture_file: Callable[[str, str], Path | None]
    # The person's own make job: {"status": ..., "asset_ids": [...]}, or None when it is not theirs or not found.
    job_state: Callable[[str, str], Mapping[str, Any] | None] = lambda _person_id, _job_id: None
    # Whether other picture work is queued or running here. The automatic default waits for it.
    busy: Callable[[], bool] = lambda: False


class _DefaultSlot:
    """Lets one automatic default start at a time, and only while no other picture work is busy.

    ``reserve`` checks ``busy`` and takes the slot in one synchronous step on the event loop, so two loads cannot both
    pass it. The slot is held across the awaits that follow, until the job has started or been refused. A started job
    counts as busy as soon as it is scheduled, so once the slot is free, a second default must still wait.
    """

    def __init__(self, busy: Callable[[], bool]) -> None:
        self._busy = busy
        self._held = False

    def reserve(self) -> bool:
        if self._held or self._busy():
            return False
        self._held = True
        return True

    def release(self) -> None:
        self._held = False


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _picture(data: bytes, content_type: str) -> Response:
    # The copy is kept on the server, per person and URL. The browser keeps nothing: whether it may
    # show this picture depends on the session asking, and a session can end or lose its rights.
    return Response(content=data, media_type=content_type, headers={
        "Cache-Control": "no-store",
        "Content-Disposition": 'inline; filename="avatar"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cross-Origin-Resource-Policy": "same-origin",
    })


def _own_picture_bytes(generation: AvatarGeneration, person_id: str, asset_id: str) -> bytes | None:
    """The bytes of one of the person's own Library pictures, or None when it is not theirs or cannot be read."""
    source = generation.own_picture_file(person_id, asset_id)
    if source is None:
        return None
    try:
        return source.read_bytes()
    except OSError:
        return None


def create_me_avatar_router(
    *,
    session_provider: SessionProvider,
    mutation_validator: MutationValidator | None = None,
    admin_db_path: Path | Callable[[], Path] = admin_store.DB_PATH,
    picture_fetcher: PictureFetcher = fetch_picture,
    cache: PictureCache | None = None,
    generation: AvatarGeneration | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/me/api/profile", tags=["me"])
    kept = cache if cache is not None else PictureCache()
    default_slot = _DefaultSlot(generation.busy if generation is not None else (lambda: False))

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

    async def _mutation_error(request: Request, session: AdminSession) -> JSONResponse | None:
        if mutation_validator is None:
            return None
        error = await asyncio.to_thread(mutation_validator, request, session)
        if error is not None:
            error.headers["Cache-Control"] = "no-store"
        return error

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

    async def _settle_default(person_id: str) -> None:
        """Once the default's job has stopped, keep the picture it made (if any) and forget the job."""
        record = await asyncio.to_thread(avatars.get, person_id, path=db())
        job_id = record["default_job_id"]
        if job_id is None:
            return
        job = None
        if generation is not None:
            job = await asyncio.to_thread(generation.job_state, person_id, job_id)
        if job is not None and job.get("status") not in JOB_STOPPED:
            return
        data = None
        if job is not None and job.get("status") == JOB_SUCCEEDED and job.get("asset_ids") and generation is not None:
            data = await asyncio.to_thread(_own_picture_bytes, generation, person_id, str(job["asset_ids"][0]))
        await asyncio.to_thread(avatars.finish_default, person_id, job_id, data, path=db())

    async def _fail_default(person_id: str, reason: str) -> None:
        """The default's start failed for good: the attempt is used up, so the picture is never retried."""
        log.warning("automatic avatar for a person did not start: %s", reason)
        await asyncio.to_thread(avatars.claim_default, person_id, None, path=db())

    async def _start_default(request: Request, person_id: str) -> None:
        """Start the person's default picture: once, at low priority, and only when an image route exists.

        The slot is reserved before anything is awaited, so loads that arrive together cannot all start one. A load
        that finds picture work busy, another default starting, no route, or a capacity refusal starts nothing and
        records no attempt: the default is still due on a later load. The attempt is recorded once a job has started,
        or when the start fails for good.
        """
        if generation is None or not default_slot.reserve():
            return
        try:
            if not await asyncio.to_thread(avatars.default_due, person_id, path=db()):
                return
            model = await asyncio.to_thread(generation.model_for, person_id)
            if model is None:
                return
            prompt = avatar_generation.build_prompt(avatar_generation.new_seed())
            try:
                job = generation.start(request, person_id, model, prompt)
            except PublicFailure as exc:
                if exc.status_code in RETRY_LATER_STATUS:
                    return  # Busy or at capacity: a later load starts it, and the attempt is kept.
                await _fail_default(person_id, exc.code)
                return
            except Exception as exc:  # noqa: BLE001 - the page shows the palette; the attempt is used up
                await _fail_default(person_id, type(exc).__name__)
                return
            if not await asyncio.to_thread(avatars.claim_default, person_id, str(job["job_id"]), path=db()):
                log.info("automatic avatar job started after the person chose an avatar: its picture is not kept")
        finally:
            default_slot.release()

    @router.get("/avatar", include_in_schema=False)
    async def get_avatar(request: Request) -> JSONResponse:
        session = await _session(request)
        if isinstance(session, JSONResponse):
            return session
        person_id = str(session.subject)
        await _settle_default(person_id)
        record = await asyncio.to_thread(avatars.get, person_id, path=db())
        return JSONResponse(avatars.public_view(person_id, record, viewer_id=person_id),
                            headers={"Cache-Control": "no-store"})

    @router.put("/avatar", include_in_schema=False)
    async def put_avatar(request: Request) -> JSONResponse:
        session = await _session(request)
        if isinstance(session, JSONResponse):
            return session
        error = await _mutation_error(request, session)
        if error is not None:
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
                if person_id != viewer_id and record["choice"] not in avatars.SHOWN_TO_OTHERS:
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
        # ``kind`` asks for one of the person's own pictures, whichever avatar they show now: the picker previews
        # both beside the palette. Without it, the picture shown is the one the person chose.
        requested = request.query_params.get("kind")
        if requested is not None and (requested not in {avatars.SHOWN_GENERATED, avatars.SHOWN_SIGN_IN}
                                      or person_id != viewer_id):
            return missing

        async def current() -> dict | None:
            # The record right now. Others see a picture only when the person chose it.
            record = await asyncio.to_thread(avatars.get, person_id, path=db())
            if requested is None and person_id != viewer_id and record["choice"] not in avatars.SHOWN_TO_OTHERS:
                return None
            return record

        record = await current()
        if record is None:
            return missing
        kind = avatars.shown_kind(record) if requested is None else requested
        if kind is None or not avatars.has_picture(kind, record):
            return missing
        if kind == avatars.SHOWN_GENERATED:
            data = await asyncio.to_thread(avatars.read_generated, person_id, path=db())
            # The choice or the copy may have changed while it was read: answer as a request made now would.
            again = await current()
            if (again is None or again["generated_at"] != record["generated_at"]
                    or not avatars.has_picture(kind, again)
                    or (requested is None and avatars.shown_kind(again) != kind)):
                return missing
            content_type = image_type(data) if data is not None else None
            return _picture(data, content_type) if data is not None and content_type else missing
        url = record["picture_url"]
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
            again = await current()
            if again is None or again["picture_url"] != url or (requested is None and avatars.shown_kind(again) != kind):
                return missing
        if entry.data is None or entry.content_type is None:
            return missing
        return _picture(entry.data, entry.content_type)

    @router.get("/avatar/generator", include_in_schema=False)
    async def get_generator(request: Request) -> JSONResponse:
        """Whether this gateway has an image route the person may use to make their avatar. The page shows the
        button only then."""
        session = await _session(request)
        if isinstance(session, JSONResponse):
            return session
        available = False
        if generation is not None:
            available = await asyncio.to_thread(generation.model_for, str(session.subject)) is not None
        return JSONResponse({"available": available}, headers={"Cache-Control": "no-store"})

    @router.post("/avatar/generate", include_in_schema=False)
    async def post_generate(request: Request) -> JSONResponse:
        """Start a make job for a new attempt. Each call is a new seed, so "Try another" is the same call."""
        session = await _session(request)
        if isinstance(session, JSONResponse):
            return session
        error = await _mutation_error(request, session)
        if error is not None:
            return error
        person_id = str(session.subject)
        if generation is None:
            return _error("generator_unavailable", "This gateway has no picture route to make an avatar with.", 503)
        model = await asyncio.to_thread(generation.model_for, person_id)
        if model is None:
            return _error("generator_unavailable", "This gateway has no picture route to make an avatar with.", 503)
        prompt = avatar_generation.build_prompt(avatar_generation.new_seed())
        try:
            # The make runtime schedules the job on the running loop, so the job starts on this loop, as a make
            # page request does. A worker thread has no loop, and would fail after leaving the job queued.
            job = generation.start(request, person_id, model, prompt)
        except PublicFailure as exc:
            return JSONResponse(exc.body(), status_code=exc.status_code, headers={"Cache-Control": "no-store"})
        return JSONResponse({"job": job}, status_code=202, headers={"Cache-Control": "no-store"})

    @router.post("/avatar/default", include_in_schema=False)
    async def post_default(request: Request) -> JSONResponse:
        """The person's automatic picture: made once, on their first signed-in page load where the avatar shows."""
        session = await _session(request)
        if isinstance(session, JSONResponse):
            return session
        error = await _mutation_error(request, session)
        if error is not None:
            return error
        person_id = str(session.subject)
        await _settle_default(person_id)
        await _start_default(request, person_id)
        record = await asyncio.to_thread(avatars.get, person_id, path=db())
        return JSONResponse(avatars.public_view(person_id, record, viewer_id=person_id),
                            headers={"Cache-Control": "no-store"})

    @router.post("/avatar/generated", include_in_schema=False)
    async def post_generated(request: Request) -> JSONResponse:
        """Keep one of the person's own results as their avatar. Only a picture in their own Library is taken."""
        session = await _session(request)
        if isinstance(session, JSONResponse):
            return session
        error = await _mutation_error(request, session)
        if error is not None:
            return error
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, Mapping) or set(body) != {"asset_id"} or not isinstance(body["asset_id"], str):
            return _error("invalid_generated_picture", "Choose one of your pictures.", 400)
        person_id = str(session.subject)
        asset_id = body["asset_id"]
        source = None
        if generation is not None and asset_id.startswith("img_"):
            source = await asyncio.to_thread(generation.own_picture_file, person_id, asset_id)
        if source is None:
            return _error("generated_not_found", "That picture is not in your Library.", 404)
        try:
            data = await asyncio.to_thread(source.read_bytes)
        except OSError:
            return _error("generated_not_found", "That picture is not in your Library.", 404)
        try:
            record = await asyncio.to_thread(avatars.store_generated, person_id, data, path=db())
        except avatars.AvatarError as exc:
            return _error(exc.code, exc.message, exc.status)
        return JSONResponse(avatars.public_view(person_id, record, viewer_id=person_id),
                            headers={"Cache-Control": "no-store"})

    @router.delete("/avatar/generated", include_in_schema=False)
    async def delete_generated(request: Request) -> JSONResponse:
        """Remove the picture the gateway made. The person shows their sign-in picture, or the palette."""
        session = await _session(request)
        if isinstance(session, JSONResponse):
            return session
        error = await _mutation_error(request, session)
        if error is not None:
            return error
        person_id = str(session.subject)
        record = await asyncio.to_thread(avatars.clear_generated, person_id, path=db())
        return JSONResponse(avatars.public_view(person_id, record, viewer_id=person_id),
                            headers={"Cache-Control": "no-store"})

    return router
