"""Authorize receipts and media jobs by their maker (D51, D52).

Visibility is not authorization. Owning an App does not make its receipts or jobs the owner's. A row is
read by the person who made it, or by its App's own credential when it has no person. Historical records
without trusted attribution fail closed for delegated callers.
"""
from collections.abc import Mapping
from typing import Any

from gateway.content_access import ContentOrigin, JobRecord, Own, ReceiptRecord, Viewer, decide
from gateway.content_makers import Apps, row_makers


def _job_readable(job: Mapping[str, Any], *, viewer: Viewer) -> bool:
    """A job is read by the maker stamped on it at creation (D51). Its creating receipt and its parent link name no
    maker, so a job whose request id was released for replay keeps its maker, and a client's parent link adds none."""
    app_id = str(job.get("app_id") or "") or None
    connection = job.get("maker_connection_id")
    origin: ContentOrigin | None = None
    if isinstance(connection, str) and connection:
        origin = ContentOrigin("mcp_grant", connection_id=connection)
    elif viewer.auth_kind == "app_key" and app_id and app_id == viewer.app_id:
        # Jobs record no credential of a key. A key on an App is the only writer of that App's rows through its own
        # API, and a session never runs under a key App, so App attribution stands for the creating credential here.
        # Rows of another App, and rows of another maker, still deny.
        origin = ContentOrigin("app_key", credential_id=viewer.credential_id or app_id)
    row = JobRecord(
        person_id=str(job.get("maker_person_id") or "") or None, app_id=app_id, origin=origin,
        owner_key_hash=str(job.get("owner_key_hash") or "") or None,
        credential_id=str(job.get("maker_credential_id") or "") or None,
    )
    return isinstance(decide(viewer, row, "read"), Own)


def owns_record(kind: str, record: Mapping[str, Any], *, viewer: Viewer, apps: Apps) -> bool:
    """Whether ``viewer`` may read this stored receipt or media job, by its maker."""
    if kind == "media_job":
        return _job_readable(record, viewer=viewer)
    if kind != "receipt":
        return False
    app_id = str(record.get("app_id") or "") or None
    makers = row_makers(
        explicit=[str(record.get("maker_person_id") or "")],
        app_id=app_id,
        owner_keys=[str(record.get("owner_key_hash") or "")],
        apps=apps,
    )
    connection = record.get("connection_id")
    origin: ContentOrigin | None = None
    if isinstance(connection, str) and connection:
        origin = ContentOrigin("mcp_grant", connection_id=connection)
    elif viewer.auth_kind == "app_key" and app_id and app_id == viewer.app_id:
        # Receipts record no credential. A key on an App is the only writer of that App's rows through its own API,
        # and a session never runs under a key App, so App attribution stands for the creating credential here.
        # Rows of another App, and rows of another maker, still deny.
        origin = ContentOrigin("app_key", credential_id=viewer.credential_id or app_id)
    person_id = makers[0] if len(makers) == 1 else None
    owner_key_hash = str(record.get("owner_key_hash") or "") or None
    row = ReceiptRecord(person_id=person_id, app_id=app_id, origin=origin, owner_key_hash=owner_key_hash)
    return isinstance(decide(viewer, row, "read"), Own)
