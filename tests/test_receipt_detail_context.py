"""The receipt detail answers who, when and what the same way the Activity list does.

HTTP tests use the production app and per-test stores. The list route and the
detail route read one function, so a receipt cannot show a person or a time in
one view and "not recorded" in the other.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, receipt_store
from gateway.activity_api import create_activity_router
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.admin_oidc import AdminSession

JOB_CREATED_AT = 1792291546

# The shape of a stored job-submission receipt: no accepted_at_ms, no model,
# only a workflow, and no created_at inside the JSON (it is a database column).
JOB_RECEIPT = {
    "request_id": "req_job", "status": "ok", "intent": "image.generate",
    "protocol": "openai.images", "path": "/gateway/v1/media/jobs/image-generations",
    "accepted_at_ms": None, "finished_at_ms": JOB_CREATED_AT * 1000 + 998,
    "requested_model": None, "resolved_model": None,
    "requested_workflow": "flux2-klein-4b",
    "resolved_workflow": "image.generate.flux2-klein-4b",
    "metadata": {"private_note": "never expose this"},
}
CHAT_RECEIPT = {
    "request_id": "req_chat", "status": "succeeded", "intent": "chat.completions",
    "requested_model": "fast", "resolved_model": "demo-model", "latency_ms": 321.5,
    "metadata": {"private_note": "never expose this"},
}


@pytest.fixture
def detail(monkeypatch, tmp_path):
    admin_path, receipts_path = tmp_path / "admin.db", tmp_path / "receipts.db"
    for module, path in ((admin_store, admin_path), (receipt_store, receipts_path)):
        module.init_db(path)
        connect = module._conn
        monkeypatch.setattr(module, "_conn", lambda _path, fn=connect, db=path: fn(db))
        monkeypatch.setattr(module, "DB_PATH", path)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin_path)

    def person(name, **fields):
        return admin_store.upsert_oidc_user("https://people.test", name, path=admin_path, **fields)

    alice = person("alice", email="alice@example.test", display_name="Alice Example")
    bob = person("bob", email="bob@example.test", display_name="Bob Example")
    owner = person("owner", email="owner@example.test", display_name="The Owner")
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=owner["id"], path=admin_path)
    alice_app = admin_store.create_app("Desktop", kind="personal", owner_user_id=alice["id"],
                                       access_plan="person", path=admin_path)
    bob_app = admin_store.create_app("Phone", kind="personal", owner_user_id=bob["id"],
                                     access_plan="person", path=admin_path)
    sessions = {
        name: AdminSession(subject=user["id"], email=user["email"], role="none",
                           issued_at=1, expires_at=4_000_000_000)
        for name, user in (("alice", alice), ("bob", bob), ("owner", owner))
    }
    monkeypatch.setattr(proxy, "_oidc_session_from_request", lambda request: sessions.get(
        request.headers.get("x-test-member", "")))

    def store(request_id, receipt, app_id, created_at):
        receipt_store.record({**receipt, "request_id": request_id, "app_id": app_id},
                             created_at=created_at, path=receipts_path)
        return request_id

    activity = FastAPI()
    activity.include_router(create_activity_router(
        auth=lambda _request: AdminContext(subject="tester", roles=frozenset({"owner"})),
        admin_path=admin_path, receipts_path=receipts_path))
    client = TestClient(proxy.app)

    def get(request_id, viewer):
        return client.get(f"/me/api/receipts/{request_id}", headers={"X-Test-Member": viewer})

    def listed(request_id):
        rows = TestClient(activity).get("/admin/api/activity/requests").json()["records"]
        return next(row for row in rows if row["receipt"]["request_id"] == request_id)

    class Case:
        pass
    case = Case()
    case.alice, case.bob, case.alice_app, case.bob_app = alice, bob, alice_app, bob_app
    case.store, case.get, case.listed = store, get, listed
    return case


def test_job_submission_detail_names_person_app_workflow_and_time(detail):
    detail.store("req_job", JOB_RECEIPT, detail.alice_app["id"], JOB_CREATED_AT)
    response = detail.get("req_job", "owner")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["person_label"] == "Alice Example"
    assert body["app_label"] == "Desktop"
    assert body["created_at"] == JOB_CREATED_AT
    assert body["resolved_workflow"] == "image.generate.flux2-klein-4b"
    assert body["requested_workflow"] == "flux2-klein-4b"
    assert "never expose this" not in response.text


def test_chat_detail_names_person_model_and_time(detail):
    detail.store("req_chat", CHAT_RECEIPT, detail.bob_app["id"], 1792300000)
    body = detail.get("req_chat", "owner").json()
    assert (body["person_label"], body["app_label"]) == ("Bob Example", "Phone")
    assert body["created_at"] == 1792300000
    assert (body["requested_model"], body["resolved_model"]) == ("fast", "demo-model")


@pytest.mark.parametrize("app_id", ["app_deleted_long_ago", None])
def test_receipt_without_an_owner_is_unattributed_not_blank(detail, app_id):
    detail.store("req_orphan", CHAT_RECEIPT, app_id, 1792300001)
    body = detail.get("req_orphan", "owner").json()
    assert body["person_label"] == "Unattributed request"
    assert body["created_at"] == 1792300001
    assert body["resolved_model"] == "demo-model"


@pytest.mark.parametrize("request_id,receipt,app", [
    ("req_job", JOB_RECEIPT, "alice_app"), ("req_chat", CHAT_RECEIPT, "bob_app"),
    ("req_orphan", CHAT_RECEIPT, None),
])
def test_detail_and_list_agree_on_person_app_and_time(detail, request_id, receipt, app):
    app_id = getattr(detail, app)["id"] if app else None
    detail.store(request_id, receipt, app_id, 1792300002)
    row, body = detail.listed(request_id), detail.get(request_id, "owner").json()
    assert body["person_label"] == row["person_label"]
    assert body["app_label"] == row["app_label"]
    assert body["created_at"] == row["created_at"]
    assert body["source"] == row["source"]


def test_member_sees_only_their_own_receipts_with_their_own_name(detail):
    detail.store("req_mine", CHAT_RECEIPT, detail.alice_app["id"], 1792300003)
    detail.store("req_theirs", CHAT_RECEIPT, detail.bob_app["id"], 1792300004)
    detail.store("req_nobody", CHAT_RECEIPT, None, 1792300005)
    mine = detail.get("req_mine", "alice")
    assert mine.status_code == 200
    assert mine.json()["person_label"] == "Alice Example"
    assert mine.json()["created_at"] == 1792300003
    for hidden in ("req_theirs", "req_nobody"):
        denied = detail.get(hidden, "alice")
        assert denied.status_code == 404
        assert "Bob Example" not in denied.text
        assert json.loads(denied.text)["error"]["code"] == "receipt_not_found"
