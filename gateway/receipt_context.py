"""Who, which app and when for one stored receipt.

The Activity list and the receipt detail both read this, so a receipt cannot
name its person in one view and show "not recorded" in the other. ``created_at``
is the receipts table column: it is not part of the receipt JSON, and a job
submission has no ``accepted_at_ms``.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

UNATTRIBUTED = "Unattributed request"


def person_label(user: Mapping[str, Any]) -> str:
    return user.get("display_name") or user.get("email") or "Gateway member"


def receipt_source(receipt: Mapping[str, Any], app: Mapping[str, Any]) -> dict[str, Any]:
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


def receipt_context(
    receipt: Mapping[str, Any],
    created_at: float,
    *,
    app: Mapping[str, Any],
    person_labels: Mapping[str, str],
) -> dict[str, Any]:
    """The fields every receipt view shows beside the receipt itself.

    ``app`` is the receipt's App row (``{}`` when it is gone) and
    ``person_labels`` maps a user id to its label.
    """
    source = receipt_source(receipt, app)
    return {
        "created_at": created_at,
        "person_label": person_labels.get(app.get("owner_user_id"), UNATTRIBUTED),
        "app_label": app.get("name") or source["label"],
        "source": source,
    }
