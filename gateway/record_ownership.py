"""Authorize receipts and media jobs by immutable, trusted ownership.

Visibility is not authorization. Shared plans, names, client-supplied parent
receipt IDs, and asset references cannot prove who submitted a job. Historical
records without trusted App/key attribution fail closed for scoped callers.
"""
from collections.abc import Mapping
from typing import Any


def owns_record(kind: str, record: Mapping[str, Any], *, app_id: str | None,
                key_hash: str | None = None, unrestricted: bool = False) -> bool:
    if kind not in {"receipt", "media_job"}:
        return False
    if unrestricted:
        return True
    if record.get("app_id"):
        return bool(app_id and record["app_id"] == app_id)
    if record.get("owner_key_hash"):
        return bool(key_hash and record["owner_key_hash"] == key_hash)
    return False
