"""Authorize receipts and media jobs by immutable, trusted ownership.

Visibility is not authorization. Shared plans, names, client-supplied parent
receipt IDs, and asset references cannot prove who submitted a job. Historical
records without trusted App/key attribution fail closed for scoped callers.
"""
from collections.abc import Mapping
from typing import Any


from gateway.content_access import JobRecord, Own, ReceiptRecord, Viewer, decide


def owns_record(kind: str, record: Mapping[str, Any], *, app_id: str | None,
                key_hash: str | None = None, owner_keys: tuple[str, ...] = ()) -> bool:
    if kind not in {"receipt", "media_job"}:
        return False
    # An explicit App is authoritative; a key field cannot override it.
    if record.get("app_id"):
        principals = frozenset({f"app:{record['app_id']}"})
        readable = frozenset({f"app:{app_id}"}) if app_id and record["app_id"] == app_id else frozenset()
    else:
        owner = record.get("owner_key_hash")
        principals = frozenset({owner}) if isinstance(owner, str) and owner else frozenset()
        readable = frozenset((*owner_keys, *([key_hash] if key_hash else [])))
    content = ReceiptRecord(principals) if kind == "receipt" else JobRecord(principals)
    return isinstance(decide(Viewer(app_id=app_id, owner_keys=readable), content, "read"), Own)
