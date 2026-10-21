"""Authorize receipts and media jobs by immutable, trusted ownership.

Visibility is not authorization. Shared plans, names, client-supplied parent
receipt IDs, and asset references cannot prove who submitted a job. Historical
records without trusted App/key attribution fail closed for scoped callers.
"""
from collections.abc import Mapping
from typing import Any
from functools import lru_cache

from gateway.content_access import Viewer, decide_own, viewer_from_auth
from gateway.runtime_auth import RuntimeIdentity


@lru_cache(maxsize=256)
def _scope_viewers(app_id: str | None, key_hash: str | None, owner_keys: tuple[str, ...]) -> tuple[Viewer, Viewer]:
    """Cache only trusted scope inputs, never content rows or policy decisions."""
    # These inputs carry only a trusted principal scope, not person authority.
    identity = RuntimeIdentity(name="", key_hash=key_hash or "", app_id=app_id)
    return (viewer_from_auth(identity, owner_keys=(f"app:{app_id}",) if app_id else ()),
            viewer_from_auth(identity, owner_keys=(*owner_keys, *([key_hash] if key_hash else []))))


def owns_record(kind: str, record: Mapping[str, Any], *, app_id: str | None,
                key_hash: str | None = None, owner_keys: tuple[str, ...] = ()) -> bool:
    if kind not in {"receipt", "media_job"}:
        return False
    app_viewer, key_viewer = _scope_viewers(app_id, key_hash, tuple(owner_keys))
    # An explicit App is authoritative; a key field cannot override it.
    if record.get("app_id"):
        principals = (f"app:{record['app_id']}",)
        if record["app_id"] != app_id:
            return False
        viewer = app_viewer
    else:
        owner = record.get("owner_key_hash")
        principals = (owner,) if isinstance(owner, str) and owner else ()
        viewer = key_viewer
    return decide_own(viewer, principals)
