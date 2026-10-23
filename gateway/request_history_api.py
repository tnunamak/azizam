"""The maker's request history: list, read, delete and delete all, and the member's own choice for it.

Routes (docs/design/REQUEST_RETENTION.md section 10):

* ``/gateway/v1/requests``: the maker's kept requests, for a personal App key or an MCP grant (and a browser
  session, where the gateway accepts one). Every row is checked by ``content_access`` before a page is cut, so a
  caller never gets another person's row, and an owner or administrator gets none.
* ``/me/api/retention``: the member's choice for request history (session only, as the other member settings).

Deleting needs the privacy permission (D52, section 10). No key or grant carries it in this slice, so a delegated
credential gets 403 ``privacy_permission_required`` and keeps what it has.
"""
from __future__ import annotations

import functools
import inspect
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from gateway import content_access, request_history, retention
from gateway.public_failure import PublicFailure

GROUP = retention.GROUP_REQUEST_HISTORY
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _error(status: int, code: str, message: str, *, kind: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind, "code": code}}, status_code=status)


def _retry_required() -> JSONResponse:
    """A read or write the store's lock would not admit. Nothing was half done, so the same request can be sent again."""
    return JSONResponse(
        {"error": {"message": "The store is busy. Try the request again.", "type": "server_error",
                   "code": "retry_required"}},
        status_code=503, headers={"Retry-After": "1", "Cache-Control": "no-store"})


def _retry_when_busy(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Map a store lock that stays busy to 503 retry_required, so a busy store is never a 500 (REQUEST_RETENTION.md 12)."""
    if inspect.iscoroutinefunction(handler):
        @functools.wraps(handler)
        async def wrapped_async(*args: Any, **kwargs: Any) -> Any:
            try:
                return await handler(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                return _retry_required()

        return wrapped_async

    @functools.wraps(handler)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return handler(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            return _retry_required()

    return wrapped


def _not_found() -> JSONResponse:
    return _error(404, "not_found", "No such request history record.", kind="not_found_error")


def _visible_now(row: dict[str, Any], cutoff: int, now: int) -> bool:
    """Not expired, and not erased by a fence written after it was created (section 12: reads check both)."""
    return (row["kept_until"] is None or int(row["kept_until"]) > now) and int(row["created_at"]) > cutoff


#: Rows read per page of the database, before the viewer's own rows are cut from them.
_BATCH = 100


def _item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": row["record_id"], "request_id": row.get("request_id"), "kind": row["kind"],
        "created_at": int(row["created_at"]), "kept_until": row.get("kept_until"),
        "origin_kind": row.get("origin_kind"), "kept_parts": json.loads(row["parts_kept"]),
        "summary": json.loads(row["summary_json"]),
    }


def _cursor(row: dict[str, Any]) -> str:
    return f"{int(row['created_at'])}:{row['record_id']}"


def _cursor_position(cursor: str | None) -> tuple[int, str] | None:
    """The position a cursor names, as the list gives cursors out. A malformed cursor raises ValueError."""
    if not cursor:
        return None
    created, separator, record_id = cursor.partition(":")
    if not separator or not record_id or not created.isdigit():
        raise ValueError(cursor)
    return int(created), record_id


#: The newest pages of the person's history a Home read may scan for chats, so a history of other requests stays bounded.
_CHAT_SCAN_PAGES = 3


def _is_chat(row: dict[str, Any]) -> bool:
    summary = json.loads(row["summary_json"])
    return str(summary.get("route") or "").endswith("/chat/completions")


def recent_chats(person_id: str, *, admin_path: Path, media_path: Path | None, limit: int,
                 now: int | None = None) -> list[dict[str, Any]]:
    """The person's own kept chats, newest first, each titled by its first user message.

    A chat appears only while history keeps its body, so a conversation the person forgot or that expired is not
    listed. Rows are read by the same ownership rule as the list route, and the title is read now, not stored.
    """
    viewer = content_access.Viewer(person_id=person_id, auth_kind="session")
    now = int(time.time()) if now is None else now
    after = retention.group_cutoff(admin_path, person_id, GROUP)
    chats: list[dict[str, Any]] = []
    before: tuple[int, str] | None = None
    for _ in range(_CHAT_SCAN_PAGES):
        batch = request_history.page_for_person(person_id, after=after, now=now, before=before, limit=_BATCH,
                                                path=media_path)
        for row in batch:
            if len(chats) == limit:
                break
            if not _is_chat(row):
                continue
            if isinstance(content_access.decide(viewer, content_access.request_history_record(row), "read"),
                          content_access.Own):
                chats.append(row)
        if len(chats) == limit or len(batch) < _BATCH:
            break
        before = (int(batch[-1]["created_at"]), str(batch[-1]["record_id"]))
    bodies = request_history.parts_json_for([str(row["record_id"]) for row in chats], path=media_path)
    titled = []
    for row in chats:
        title = request_history.first_user_text(bodies.get(str(row["record_id"])))
        if title is not None:
            titled.append({"request_id": row.get("request_id"), "record_id": row["record_id"],
                           "created_at": int(row["created_at"]), "title": title})
    return titled


def create_request_history_router(
    *,
    session_provider: Callable[[Request], Any],
    mutation_validator: Callable[[Request, Any], JSONResponse | None] | None,
    admin_path: Callable[[], Path],
    media_path: Callable[[], Path | None],
) -> APIRouter:
    router = APIRouter(tags=["requests"])

    def _viewer(request: Request) -> content_access.Viewer | JSONResponse:
        """The caller's view of the maker's rows: a delegated credential's own view, or the browser session's person."""
        auth = request.scope.get("auth_context")
        if getattr(getattr(auth, "identity", None), "auth_kind", None) is not None:
            return content_access.viewer_from_auth(auth, owner_keys=())
        session = session_provider(request)
        if session is None:
            return _error(401, "unauthorized", "An App key, an MCP grant or a sign-in is required.",
                          kind="authentication_error")
        return content_access.Viewer(person_id=str(session.subject), auth_kind="session")

    def _session_write_refusal(request: Request, viewer: content_access.Viewer) -> JSONResponse | None:
        """A write from a browser session needs the user-action header and CSRF token, as every session write does."""
        if viewer.auth_kind != "session" or mutation_validator is None:
            return None
        return mutation_validator(request, session_provider(request))

    @router.get("/gateway/v1/requests")
    @_retry_when_busy
    def list_requests(request: Request, limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                      cursor: str | None = Query(None)) -> Any:
        viewer = _viewer(request)
        if isinstance(viewer, JSONResponse):
            return viewer
        try:
            before = _cursor_position(cursor)
        except ValueError:
            return _error(400, "invalid_cursor", "The cursor is not one this list gives out.")
        if not viewer.person_id:
            return {"requests": [], "next_cursor": None}
        after = retention.group_cutoff(admin_path(), viewer.person_id, GROUP)
        now = int(time.time())
        # Pages are cut in SQL from metadata. The viewer's own rows are cut from each batch, and batches are read until
        # the page is full, so a page is never short because other people's rows were in the batch.
        page: list[dict[str, Any]] = []
        next_cursor: str | None = None
        while next_cursor is None:
            batch = request_history.page_for_person(viewer.person_id, after=after, now=now, before=before,
                                                    limit=_BATCH, path=media_path())
            for row in batch:
                if not isinstance(content_access.decide(viewer, content_access.request_history_record(row), "read"),
                                  content_access.Own):
                    continue
                if len(page) == limit:
                    next_cursor = _cursor(page[-1])
                    break
                page.append(row)
            if len(batch) < _BATCH:
                break
            before = (int(batch[-1]["created_at"]), str(batch[-1]["record_id"]))
        return {"requests": [_item(row) for row in page], "next_cursor": next_cursor}

    def _readable(viewer: content_access.Viewer, record_id: str) -> dict[str, Any] | None:
        row = request_history.get_row(record_id, path=media_path())
        if row is None:
            return None
        cutoff = retention.group_cutoff(admin_path(), str(row["person_id"]), GROUP)
        if not _visible_now(row, cutoff, int(time.time())):
            return None
        access = content_access.decide(viewer, content_access.request_history_record(row), "read")
        return row if isinstance(access, content_access.Own) else None

    @router.get("/gateway/v1/requests/{record_id}")
    @_retry_when_busy
    def read_request(record_id: str, request: Request) -> Any:
        viewer = _viewer(request)
        if isinstance(viewer, JSONResponse):
            return viewer
        row = _readable(viewer, record_id)
        if row is None:
            return _not_found()
        try:
            content = content_access.maker_content(viewer, row)
        except PublicFailure:
            return _not_found()
        return {"record_id": content.record_id, "kind": content.kind, "created_at": content.created_at,
                "kept_until": content.kept_until, "origin_kind": content.origin_kind,
                "request_id": content.request_id, "parts": content.parts, "omissions": list(content.omissions),
                "summary": content.summary}

    @router.delete("/gateway/v1/requests/{record_id}")
    @_retry_when_busy
    def delete_request(record_id: str, request: Request) -> Any:
        viewer = _viewer(request)
        if isinstance(viewer, JSONResponse):
            return viewer
        refusal = _session_write_refusal(request, viewer)
        if refusal is not None:
            return refusal
        row = _readable(viewer, record_id)
        if row is None:
            return _not_found()
        if isinstance(content_access.decide(viewer, content_access.request_history_record(row), "delete"),
                      content_access.Deny):
            return _privacy_refused()
        request_history.delete_row(record_id, path=media_path())
        return Response(status_code=204)

    @router.delete("/gateway/v1/requests")
    @_retry_when_busy
    def delete_all_requests(request: Request) -> Any:
        viewer = _viewer(request)
        if isinstance(viewer, JSONResponse):
            return viewer
        refusal = _session_write_refusal(request, viewer)
        if refusal is not None:
            return refusal
        if not viewer.person_id:
            return {"deleted": 0}
        if isinstance(content_access.decide(viewer, content_access.RequestHistoryRecord(person_id=viewer.person_id),
                                            "delete"), content_access.Deny):
            return _privacy_refused()
        result = request_history.delete_all_for_person(viewer.person_id, admin_path=admin_path(),
                                                       path=media_path(), now=int(time.time()))
        if result["pending"]:
            # The records are hidden now, and maintenance deletes them (REQUEST_RETENTION.md 12).
            return JSONResponse({"deleted": 0, "pending": True}, status_code=202, headers={"Cache-Control": "no-store"})
        return {"deleted": result["deleted"]}

    def _privacy_refused() -> JSONResponse:
        return _error(403, "privacy_permission_required",
                      "Deleting request history needs the privacy permission, which only the person can grant.",
                      kind="permission_error")

    def _choice(person_id: str) -> dict[str, Any]:
        member = retention.member_choice(admin_path(), person_id, GROUP) or {}
        defaults = retention.bundled().member_defaults(retention.REQUEST_HISTORY)
        return {
            "keep": bool(member.get("remember", defaults["remember"])),
            "retention": member.get("retention", defaults["retention"]),
            "parts": member.get("parts", list(retention.bundled().parts(retention.REQUEST_HISTORY))),
            "choices": list(retention.bundled().data["groups"][GROUP]["choices"]),
        }

    def _session_person(request: Request) -> str | JSONResponse:
        session = session_provider(request)
        if session is None:
            return _error(401, "unauthorized", "Sign in required.", kind="authentication_error")
        return str(session.subject)

    @router.get("/me/api/retention", include_in_schema=False)
    @_retry_when_busy
    def get_my_retention(request: Request) -> Any:
        person = _session_person(request)
        if isinstance(person, JSONResponse):
            return person
        return _choice(person)

    @router.put("/me/api/retention", include_in_schema=False)
    @_retry_when_busy
    async def put_my_retention(request: Request) -> Any:
        session = session_provider(request)
        if session is None:
            return _error(401, "unauthorized", "Sign in required.", kind="authentication_error")
        if mutation_validator is not None:
            refusal = mutation_validator(request, session)
            if refusal is not None:
                return refusal
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "invalid_request", "The body must be a JSON object.")
        if not isinstance(body, dict):
            return _error(400, "invalid_request", "The body must be a JSON object.")
        keep, choice, parts = body.get("keep"), body.get("retention"), body.get("parts")
        if not isinstance(keep, bool):
            return _error(400, "invalid_retention", "keep must be true or false.")
        if choice not in retention.bundled().data["groups"][GROUP]["choices"]:
            return _error(400, "invalid_retention", "retention must be one of the offered periods.")
        allowed = retention.bundled().data["kinds"][retention.REQUEST_HISTORY]["parts"]["allowed"]
        if (not isinstance(parts, list) or "accepted" not in parts
                or any(part not in allowed for part in parts) or len(set(parts)) != len(parts)):
            return _error(400, "invalid_retention", "parts must include accepted and only offered parts.")
        person = str(session.subject)
        request_history.set_member_choice(person, keep=keep, retention_id=choice, parts=parts,
                                          admin_path=admin_path(), path=media_path(), now=int(time.time()))
        return _choice(person)

    return router
