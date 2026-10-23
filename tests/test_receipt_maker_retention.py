"""The maker of a receipt sees its full request-history retention entry on the member route (REQUEST_RETENTION.md 13.3).

The receipt names no person. The person who owns the receipt's App is the maker of its requests, so the detail route
gives the full entry to that person, and nobody else who can open the receipt sees more than kind and decision.
"""
from __future__ import annotations

import pytest

from gateway import admin_store, receipt_store
from gateway.user_runtime import UserPortalError, get_user_receipt
from gateway.admin_oidc import AdminSession
from tests.test_receipt_detail_context import CHAT_RECEIPT

ENTRY = {"kind": "request_history", "decision": "kept", "reason": "kept", "kept_until_ms": 1792300000000,
         "parts": ["accepted"], "limited_by": "member_choice"}


@pytest.fixture
def case(tmp_path):
    db, receipts = tmp_path / "admin.db", tmp_path / "receipts.db"
    admin_store.init_db(db)
    receipt_store.init_db(receipts)
    maker = admin_store.upsert_oidc_user("https://example.test", "maker", email="maker@example.test", path=db)
    other = admin_store.upsert_oidc_user("https://example.test", "other", email="other@example.test", path=db)
    app = admin_store.create_app("Maker app", kind="personal", owner_user_id=maker["id"], access_plan="person", path=db)
    receipt_store.record({**CHAT_RECEIPT, "request_id": "req_maker", "app_id": app["id"]}, path=receipts)
    assert receipt_store.update_retention("req_maker", ENTRY, path=receipts)
    session = lambda user: AdminSession(subject=user["id"], email=user["email"], role="admin", issued_at=1,
                                        expires_at=4_000_000_000)
    return {"db": db, "receipts": receipts, "maker": session(maker), "other": session(other)}


def test_the_maker_of_the_app_sees_the_full_retention_entry(case):
    receipt = get_user_receipt(case["maker"], "req_maker", path=case["db"], receipts_path=case["receipts"])
    assert receipt["retention"] == [ENTRY]


def test_someone_else_cannot_open_the_receipt_at_all(case):
    with pytest.raises(UserPortalError) as exc:
        get_user_receipt(case["other"], "req_maker", path=case["db"], receipts_path=case["receipts"])
    assert exc.value.status_code == 404
