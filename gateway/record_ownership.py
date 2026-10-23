"""Authorize receipts and media jobs by their maker (D51, D52).

Visibility is not authorization. Owning an App does not make its receipts or jobs the owner's. A row is
read by the person who made it, or by its App's own credential when it has no person. Historical records
without trusted attribution fail closed for delegated callers.
"""
from collections.abc import Mapping
from typing import Any

from gateway import receipt_store
from gateway.content_access import ContentOrigin, JobRecord, Own, ReceiptRecord, Viewer, decide
from gateway.content_makers import Apps, job_receipt_ids, row_makers


def _creating_receipt(job: Mapping[str, Any]) -> Mapping[str, Any] | None:
    ids = job_receipt_ids(job)
    if not ids:
        return None
    receipts = receipt_store.get_many(ids, audience="internal")
    return next((receipts[key] for key in ids if key in receipts), None)


def owns_record(kind: str, record: Mapping[str, Any], *, viewer: Viewer, apps: Apps) -> bool:
    """Whether ``viewer`` may read this stored receipt or media job. A job takes its creating receipt's maker."""
    if kind not in {"receipt", "media_job"}:
        return False
    receipt = _creating_receipt(record) if kind == "media_job" else None
    source = receipt if kind == "media_job" else record
    app_id = str(record.get("app_id") or (receipt or {}).get("app_id") or "") or None
    makers = row_makers(
        explicit=[str((source or {}).get("maker_person_id") or "")],
        app_id=app_id,
        owner_keys=[str(record.get("owner_key_hash") or ""), str((receipt or {}).get("owner_key_hash") or "")],
        apps=apps,
    )
    connection = (receipt or record).get("connection_id")
    origin: ContentOrigin | None = None
    if isinstance(connection, str) and connection:
        origin = ContentOrigin("mcp_grant", connection_id=connection)
    elif viewer.auth_kind == "app_key" and app_id and app_id == viewer.app_id:
        # Receipts and jobs record no credential. A key on an App is the only writer of that App's rows
        # through its own API, and a session never runs under a key App, so App attribution stands for
        # the creating credential here. Rows of another App, and rows of another maker, still deny.
        origin = ContentOrigin("app_key", credential_id=viewer.credential_id or app_id)
    person_id = makers[0] if len(makers) == 1 else None
    row = (ReceiptRecord if kind == "receipt" else JobRecord)(person_id=person_id, app_id=app_id, origin=origin)
    return isinstance(decide(viewer, row, "read"), Own)
