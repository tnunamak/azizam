"""Owner-only request activity, with stable pagination and safe receipt views."""
from __future__ import annotations

import json
import math
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from gateway import admin_store, receipt_store, sqlite_startup
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
                 person_id: str | None = Query(None, max_length=256),
                 source: str | None = Query(None, max_length=512),
                 before: str | None = Query(None, max_length=512)):
        denied = authorize(request)
        if denied is not None:
            return denied
        users = admin_store.list_users(path=admin_path)
        people = [{"id": row["id"], "label": row.get("display_name") or row.get("email") or "Gateway member"}
                  for row in users]
        labels = {person["id"]: person["label"] for person in people}
        apps = {row["id"]: row for row in admin_store.list_apps(path=admin_path)}
        sources = _sources(apps)
        clauses = ["1 = 1"]
        params: list = []
        if person_id is not None:
            owned = [id for id, app in apps.items() if app.get("owner_user_id") == person_id]
            if not owned:
                return {"records": [], "next_cursor": None, "people": people, "sources": sources}
            clauses.append(f"app_id IN ({','.join('?' for _ in owned)})")
            params.extend(owned)
        if source not in (None, "", "all"):
            if source == "local-network":
                clauses.append("app_id IS NULL")
            elif source.startswith("app:"):
                clauses.append("app_id = ?")
                params.append(source.removeprefix("app:"))
            else:
                return JSONResponse(status_code=400, content={"error": {
                    "code": "invalid_source", "message": "Source filter is invalid."}})
        if before:
            try:
                timestamp, id = json.loads(before)
                if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or not isinstance(id, str):
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                return JSONResponse(status_code=400, content={"error": {
                    "code": "invalid_cursor", "message": "Request cursor is invalid."}})
            clauses.append("(created_at < ? OR (created_at = ? AND request_id < ?))")
            params.extend([timestamp, timestamp, id])
        if not receipts_path.exists():
            return {"records": [], "next_cursor": None, "people": people, "sources": sources}
        with sqlite_startup.connect_readonly(receipts_path) as conn:
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
            source_info = _receipt_source(row, app)
            records.append({"receipt": row, "created_at": timestamp,
                            "person_label": labels.get(owner, "Unattributed request"),
                            "app_label": app.get("name") or source_info["label"],
                            "source": source_info})
        cursor = None
        if len(rows) > limit:
            last_request_id, last_created_at = rows[limit - 1]
            cursor = json.dumps([last_created_at, last_request_id])
        return {"records": records, "next_cursor": cursor, "people": people, "sources": sources}

    return router


def _sources(apps: dict[str, dict]) -> list[dict]:
    rows = [{"id": "local-network", "kind": "local-network", "label": "Local network · no key"}]
    for app_id, app in sorted(apps.items(), key=lambda item: str(item[1].get("name") or item[0])):
        rows.append({
            "id": f"app:{app_id}",
            "kind": "app",
            "label": str(app.get("name") or "App"),
            "app_id": app_id,
            "owner_user_id": app.get("owner_user_id"),
            "owner_service_account_id": app.get("owner_service_account_id"),
        })
    return rows


def _receipt_source(receipt: dict, app: dict) -> dict:
    app_id = receipt.get("app_id")
    if isinstance(app_id, str) and app_id:
        return {
            "id": f"app:{app_id}",
            "kind": "app",
            "label": str(app.get("name") or receipt.get("api_key_name") or "App"),
            "app_id": app_id,
        }
    return {
        "id": "local-network",
        "kind": "local-network",
        "label": str(receipt.get("source_label") or "Local network · no key"),
        "app_id": None,
        "client_hint": receipt.get("client_hint") if isinstance(receipt.get("client_hint"), str) else None,
    }
