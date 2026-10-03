"""Owner-only request activity, with stable pagination and safe receipt views."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from gateway import admin_store, receipt_store
from gateway.admin_api import AdminAuth, _require_admin


def create_activity_router(
    *, auth: AdminAuth, admin_path: Path = admin_store.DB_PATH,
    receipts_path: Path = receipt_store.DB_PATH,
) -> APIRouter:
    router = APIRouter(prefix="/admin/api/activity", tags=["Activity"])

    def authorize(request: Request):
        ctx = _require_admin(auth, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if "owner" not in ctx.roles:
            return JSONResponse(status_code=403, content={"error": {
                "code": "owner_required", "message": "Only the gateway owner can view all requests."}})
        return None

    @router.get("/requests")
    def requests(request: Request, limit: int = Query(50, ge=1, le=100),
                 person_id: str | None = None, before: str | None = None):
        denied = authorize(request)
        if denied is not None:
            return denied
        users = admin_store.list_users(path=admin_path)
        people = [{"id": row["id"], "label": row.get("display_name") or row.get("email") or "Gateway member"}
                  for row in users]
        labels = {person["id"]: person["label"] for person in people}
        apps = {row["id"]: row for row in admin_store.list_apps(path=admin_path)}
        clauses = ["parent_request_id IS NULL"]
        params: list = []
        if person_id is not None:
            owned = [id for id, app in apps.items() if app.get("owner_user_id") == person_id]
            if not owned:
                return {"records": [], "next_cursor": None, "people": people}
            clauses.append(f"app_id IN ({','.join('?' for _ in owned)})")
            params.extend(owned)
        if before:
            try:
                timestamp, id = json.loads(before)
                if type(timestamp) is not int or not isinstance(id, str):
                    raise ValueError
            except (ValueError, TypeError):
                return JSONResponse(status_code=400, content={"error": {
                    "code": "invalid_cursor", "message": "Request cursor is invalid."}})
            clauses.append("(created_at < ? OR (created_at = ? AND request_id < ?))")
            params.extend([timestamp, timestamp, id])
        if not receipts_path.exists():
            return {"records": [], "next_cursor": None, "people": people}
        with sqlite3.connect(receipts_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT request_id, created_at FROM receipts WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC, request_id DESC LIMIT ?", [*params, limit + 1]).fetchall()
        records = []
        for id, timestamp in rows[:limit]:
            row = receipt_store.get(id, audience="admin", path=receipts_path)
            if row is None:
                continue
            app = apps.get(row.get("app_id"), {})
            owner = app.get("owner_user_id")
            records.append({"receipt": row, "created_at": timestamp,
                            "person_label": labels.get(owner, "Unattributed request"),
                            "app_label": app.get("name") or "Unknown app"})
        cursor = json.dumps(rows[limit - 1]) if len(rows) > limit else None
        return {"records": records, "next_cursor": cursor, "people": people}

    return router
