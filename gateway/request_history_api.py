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


def _not_found() -> JSONResponse:
    return _error(404, "not_found", "No such request history record.", kind="not_found_error")


def _visible_now(row: dict[str, Any], cutoff: int, now: int) -> bool:
    """Not expired, and not erased by a fence written after it was created (section 12: reads check both)."""
    return (row["kept_until"] is None or int(row["kept_until"]) > now) and int(row["created_at"]) > cutoff


def _item(row: dict[str, Any]) -> dict[str, Any]:
    import json

    return {
        "record_id": row["record_id"], "request_id": row.get("request_id"), "kind": row["kind"],
        "created_at": int(row["created_at"]), "kept_until": row.get("kept_until"),
        "origin_kind": row.get("origin_kind"), "kept_parts": sorted(json.loads(row["parts_json"])),
        "summary": json.loads(row["summary_json"]),
    }


def _cursor(row: dict[str, Any]) -> str:
    return f"{int(row['created_at'])}:{row['record_id']}"


def _start_after(visible: list[dict[str, Any]], cursor: str | None) -> int | None:
    """The index after the cursor's row, or None when the cursor names no row of this list."""
    if not cursor:
        return 0
    for index, row in enumerate(visible):
        if _cursor(row) == cursor:
            return index + 1
    return None


def create_request_history_router(
    *,
    session_provider: Callable[[Request], Any],
    mutation_validator: Callable[[Request, Any], JSONResponse | None] | None,
    admin_path: Callable[[], Path],
    media_path: Callable[[], Path | None],
) -> APIRouter:
    router = APIRouter(tags=["requests"])

    def _viewer(request: Request) -> content_access.Viewer | JSONResponse:
        auth = request.scope.get("auth_context")
        if getattr(getattr(auth, "identity", None), "auth_kind", None) is None:
            return _error(401, "unauthorized", "An App key or an MCP grant is required.", kind="authentication_error")
        return content_access.viewer_from_auth(auth, owner_keys=())

    @router.get("/gateway/v1/requests")
    def list_requests(request: Request, limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                      cursor: str | None = Query(None)) -> Any:
        viewer = _viewer(request)
        if isinstance(viewer, JSONResponse):
            return viewer
        now = int(time.time())
        rows: list[dict[str, Any]] = []
        if viewer.person_id:
            after = retention.group_cutoff(admin_path(), viewer.person_id, GROUP)
            rows = [row for row in request_history.rows_for_person(viewer.person_id, after=after, now=now,
                                                                   path=media_path())]
        # Ownership first, then the page: a page is never short because another person's rows were cut from it.
        visible = content_access.visible_maker_rows(viewer, rows)
        start = _start_after(visible, cursor)
        if start is None:
            return _error(400, "invalid_cursor", "The cursor does not name a row of this list.")
        page = visible[start:start + limit]
        more = start + limit < len(visible)
        return {"requests": [_item(row) for row in page],
                "next_cursor": _cursor(page[-1]) if more and page else None}

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
    def delete_request(record_id: str, request: Request) -> Any:
        viewer = _viewer(request)
        if isinstance(viewer, JSONResponse):
            return viewer
        row = _readable(viewer, record_id)
        if row is None:
            return _not_found()
        if isinstance(content_access.decide(viewer, content_access.request_history_record(row), "delete"),
                      content_access.Deny):
            return _privacy_refused()
        request_history.delete_row(record_id, path=media_path())
        return Response(status_code=204)

    @router.delete("/gateway/v1/requests")
    def delete_all_requests(request: Request) -> Any:
        viewer = _viewer(request)
        if isinstance(viewer, JSONResponse):
            return viewer
        if not viewer.person_id:
            return {"deleted": 0}
        if isinstance(content_access.decide(viewer, content_access.RequestHistoryRecord(person_id=viewer.person_id),
                                            "delete"), content_access.Deny):
            return _privacy_refused()
        deleted = request_history.delete_all_for_person(viewer.person_id, admin_path=admin_path(),
                                                        path=media_path(), now=int(time.time()))
        return {"deleted": deleted}

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
    def get_my_retention(request: Request) -> Any:
        person = _session_person(request)
        if isinstance(person, JSONResponse):
            return person
        return _choice(person)

    @router.put("/me/api/retention", include_in_schema=False)
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
