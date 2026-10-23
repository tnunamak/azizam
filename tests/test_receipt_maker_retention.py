"""A receipt's full request-history entry goes only to the person the receipt names as its maker.

The maker is the person a request ran as. The gateway writes that person onto the receipt when the receipt is created,
from the request's own authentication. An App's owner is not the maker of the requests that come through the App, so
owning the App never shows the full entry. A receipt with no maker shows its kind and decision to everyone, its App's
owner included (REQUEST_RETENTION.md 13.3, DECISIONS.md D51).
"""
from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

import proxy
from gateway import admin_store, receipt_store
from gateway.admin_oidc import AdminSession
from gateway.user_runtime import UserPortalError, get_user_receipt
from tests.support.fake_upstream import StreamingASGITransport, fake_upstreams  # noqa: F401 - fixture
from tests.test_receipt_detail_context import CHAT_RECEIPT
from tests.test_request_history_e2e_matrix import MODEL, _receipts, _run, SHAPES, world  # noqa: F401 - fixture

ENTRY = {"kind": "request_history", "decision": "kept", "reason": "kept", "kept_until_ms": 1792300000000,
         "parts": ["accepted"], "limited_by": "member_choice"}
SUMMARY = [{"kind": "request_history", "decision": "kept"}]
TRUSTED_ADDRESS = "203.0.113.7"


class Case:
    def __init__(self, db, receipts, people, personal, shared):
        self.db, self.receipts, self.people = db, receipts, people
        self.personal, self.shared = personal, shared

    def store(self, request_id, app, *, maker=None):
        receipt = {**CHAT_RECEIPT, "request_id": request_id, "app_id": app["id"], "retention": [ENTRY]}
        if maker is not None:
            receipt["maker_person_id"] = self.people[maker]["id"]
        receipt_store.record(receipt, path=self.receipts)

    def session(self, name):
        user = self.people[name]
        return AdminSession(subject=user["id"], email=user["email"], role="none", issued_at=1, expires_at=4_000_000_000)

    def open_as(self, name, request_id):
        """The receipt as the signed-in person sees it on the member route."""
        receipt = get_user_receipt(self.session(name), request_id, path=self.db, receipts_path=self.receipts)
        return receipt["retention"]

    def read_as(self, name, request_id):
        """The receipt as the receipt store hands it to a viewer; None reads with no viewer at all."""
        viewer = None if name is None else self.people[name]["id"]
        found = receipt_store.get(request_id, audience="client", viewer_person_id=viewer, path=self.receipts)
        assert found is not None, f"no receipt {request_id}"
        return found["retention"]


@pytest.fixture
def case(tmp_path):
    db, receipts = tmp_path / "admin.db", tmp_path / "receipts.db"
    admin_store.init_db(db)
    receipt_store.init_db(receipts)
    people = {name: admin_store.upsert_oidc_user("https://example.test", name, email=f"{name}@example.test", path=db)
              for name in ("maker", "member", "owner")}
    personal = admin_store.create_app("Maker app", kind="personal", owner_user_id=people["maker"]["id"],
                                      access_plan="person", path=db)
    shared = admin_store.create_app("Shared app", kind="service", owner_user_id=people["owner"]["id"],
                                    access_plan="person", path=db)
    return Case(db, receipts, people, personal, shared)


def test_the_maker_sees_the_full_retention_entry(case):
    case.store("req_mine", case.personal, maker="maker")
    assert case.open_as("maker", "req_mine") == [ENTRY]


def test_an_app_owner_who_is_not_the_maker_sees_only_kind_and_decision(case):
    case.store("req_member", case.shared, maker="member")
    assert case.open_as("owner", "req_member") == SUMMARY


def test_the_member_who_made_a_request_on_a_shared_app_reads_its_full_entry(case):
    case.store("req_member", case.shared, maker="member")
    assert case.read_as("member", "req_member") == [ENTRY]


def test_a_receipt_with_no_maker_shows_only_kind_and_decision_to_its_app_owner(case):
    case.store("req_orphan", case.personal)
    assert case.open_as("maker", "req_orphan") == SUMMARY


@pytest.mark.parametrize("viewer", ["maker", "member", "owner", None])
def test_a_person_less_receipt_shows_full_detail_to_nobody(case, viewer):
    case.store("req_nobody", case.shared)
    assert case.read_as(viewer, "req_nobody") == SUMMARY


def test_someone_else_cannot_open_the_receipt_at_all(case):
    case.store("req_maker", case.personal, maker="maker")
    with pytest.raises(UserPortalError) as exc:
        case.open_as("member", "req_maker")
    assert exc.value.status_code == 404


def test_a_chat_request_writes_the_person_it_ran_as_onto_its_receipt(world):
    _run(world, next(shape for shape in SHAPES if shape.name == "chat_sse"))
    mine = [receipt for receipt in _receipts(world) if receipt.get("app_id") == world.app]
    assert mine, "the chat request wrote no receipt for its App"
    assert all(receipt.get("maker_person_id") == world.person for receipt in mine)


async def _keyless_chat(address: str) -> httpx.Response:
    """A chat request with no App key: the gateway admits it only because the owner trusted this address."""
    transport = StreamingASGITransport(proxy.app, client=(address, 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        return await client.post("/v1/chat/completions", json={
            "model": MODEL, "messages": [{"role": "user", "content": "sent from a trusted address"}],
        })


def test_a_trusted_address_request_names_no_maker_and_its_app_owner_sees_only_kind_and_decision(world):
    with sqlite3.connect(world.db) as conn:
        app_id, owner_id = conn.execute("SELECT id, owner_user_id FROM apps WHERE kind='service'").fetchone()
    admin_store.add_trusted_address(app_id, TRUSTED_ADDRESS, max_entries=32, path=world.db, created_by=owner_id)
    response = asyncio.run(_keyless_chat(TRUSTED_ADDRESS))
    assert response.status_code == 200, response.text
    mine = [receipt for receipt in _receipts(world) if receipt.get("app_id") == app_id]
    assert mine, "the trusted-address request wrote no receipt for its App"
    assert all("maker_person_id" not in receipt for receipt in mine)
    session = AdminSession(subject=owner_id, email="fixture@example.test", role="none", issued_at=1,
                           expires_at=4_000_000_000)
    entry = get_user_receipt(session, mine[0]["request_id"], path=world.db, receipts_path=world.receipts)["retention"]
    assert entry == [{"kind": "request_history", "decision": "not_kept"}]
