"""Ownership decisions for gateway receipts and asynchronous media records.

Visibility is not authorization: two Apps on the same plan remain distinct.
Labels and shared profiles never establish ownership. Unattributed historical
records are unavailable to scoped callers, even when a label happens to match.
"""
from collections.abc import Callable, Mapping
from typing import Any


def owns_record(kind: str, record: Mapping[str, Any], *, app_id: str | None,
                receipt_reader: Callable, asset_authorizer: Callable,
                unrestricted: bool = False) -> bool:
    if unrestricted:
        return True
    if kind == "receipt":
        return bool(app_id and record.get("app_id") == app_id)
    if kind != "media_job":
        return False
    owner = record.get("app_id") or (record.get("metadata") or {}).get("owner_app_id")
    if owner:
        return bool(app_id and owner == app_id)
    for request_id in (record.get("request_id"), record.get("parent_request_id")):
        if request_id:
            receipt = receipt_reader(request_id)
            if receipt and receipt.get("app_id"):
                return bool(app_id and receipt["app_id"] == app_id)
    assets = record.get("assets") or []
    return bool(assets and all(isinstance(asset, Mapping) and asset.get("asset_id")
                               and asset_authorizer(asset["asset_id"]) for asset in assets))
