"""The receipt detail answers who, when and what the same way the Activity list does.

HTTP tests use the production app and per-test stores. The list route and the
detail route read one function, so a receipt cannot show a person or a time in
one view and "not recorded" in the other.
"""
from __future__ import annotations

import json
import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, receipt_store
from gateway.activity_api import create_activity_router
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.admin_oidc import AdminSession
from gateway.user_runtime import build_user_activity_snapshot
from gateway.user_runtime import mint_user_app_key

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
    case.owner_session, case.admin_path, case.receipts_path = sessions["owner"], admin_path, receipts_path
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


@pytest.mark.parametrize("entry", [
    {"kind": "conversation", "decision": "kept", "reason": "kept",
     "kept_until_ms": 1792300000000, "parts": ["accepted", "received"],
     "limited_by": "member_choice"},
    {"kind": "conversation", "decision": "not_kept", "reason": "member_disabled"},
])
def test_retention_detail_is_visible_to_maker_but_not_to_owner_or_admin(detail, entry):
    receipt = {**CHAT_RECEIPT, "maker_person_id": detail.alice["id"], "retention": [entry]}
    detail.store("req_private_retention", receipt, detail.alice_app["id"], 1792300000)
    maker = detail.get("req_private_retention", "alice").json()["retention"]
    owner = detail.get("req_private_retention", "owner").json()["retention"]
    admin = detail.listed("req_private_retention")["receipt"]["retention"]
    home = build_user_activity_snapshot(
        detail.owner_session, path=detail.admin_path, receipts_path=detail.receipts_path,
    )["receipts"][0]["retention"]
    assert maker == receipt["retention"]
    assert owner == admin == home == [{"kind": entry["kind"], "decision": entry["decision"]}]


def test_shared_app_receipt_route_masks_other_makers_retention(detail):
    from fastapi.testclient import TestClient

    entry = {"kind": "conversation", "decision": "kept", "reason": "kept",
             "kept_until_ms": 1792300000000, "parts": ["accepted"], "limited_by": "member_choice"}
    detail.store("req_shared_app", {**CHAT_RECEIPT, "maker_person_id": detail.bob["id"],
                                    "retention": [entry]}, detail.alice_app["id"], 1792300000)
    detail.store("req_own_app", {**CHAT_RECEIPT, "maker_person_id": detail.alice["id"],
                                 "retention": [entry]}, detail.alice_app["id"], 1792300001)
    alice_session = AdminSession(subject=detail.alice["id"], role="none", email=None,
                                 issued_at=1, expires_at=4_000_000_000)
    key = mint_user_app_key(alice_session, detail.alice_app["id"], path=detail.admin_path)["api_key"]
    client = TestClient(proxy.app)
    headers = {"Authorization": f"Bearer {key}"}
    single = client.get("/gateway/v1/receipts/req_shared_app", headers=headers)
    listing = client.get("/gateway/v1/receipts", headers=headers)
    assert single.status_code == listing.status_code == 200
    assert single.json()["retention"] == [{"kind": "conversation", "decision": "kept"}]
    assert next(row for row in listing.json()["receipts"] if row["request_id"] == "req_shared_app")["retention"] == [
        {"kind": "conversation", "decision": "kept"}]
    own = client.get("/gateway/v1/receipts/req_own_app", headers=headers)
    assert own.status_code == 200
    assert own.json()["retention"] == [entry]
    assert next(row for row in listing.json()["receipts"] if row["request_id"] == "req_own_app")["retention"] == [entry]


def test_every_non_internal_receipt_store_read_call_passes_viewer():
    root = Path(__file__).resolve().parent.parent
    calls = []
    for path in [root / "proxy.py", *(root / "gateway").rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id not in {"receipt_store", "_receipt_store"}:
                continue
            if node.func.attr not in {"get", "get_many", "get_with_created_at", "recent"}:
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            audience = keywords.get("audience")
            if isinstance(audience, ast.Constant) and audience.value == "internal":
                continue
            calls.append(f"{path.relative_to(root)}:{node.lineno}")
            assert "viewer_person_id" in keywords, f"receipt read lacks viewer: {calls[-1]}"
    assert len(calls) >= 7, calls


@pytest.mark.parametrize("read", ["get", "get_many", "get_with_created_at", "recent"])
@pytest.mark.parametrize("audience", ["client", "admin"])
def test_receipt_store_reads_deny_retention_detail_without_maker_match(tmp_path, read, audience):
    path = tmp_path / "receipts.db"
    receipt_store.init_db(path)
    entry = {"kind": "conversation", "decision": "kept", "reason": "kept",
             "kept_until_ms": 1792300000000, "parts": ["accepted"], "limited_by": "member_choice"}
    receipt_store.record({**CHAT_RECEIPT, "request_id": "req_access", "maker_person_id": "person-b",
                          "retention": [entry]}, path=path)

    def fetch(viewer):
        kwargs = {"audience": audience, "path": path}
        if viewer is not None:
            kwargs["viewer_person_id"] = viewer
        if read == "get":
            return receipt_store.get("req_access", **kwargs)
        if read == "get_many":
            return receipt_store.get_many(["req_access"], **kwargs)["req_access"]
        if read == "get_with_created_at":
            return receipt_store.get_with_created_at("req_access", **kwargs)[0]
        return receipt_store.recent(**kwargs)[0]

    summary = [{"kind": "conversation", "decision": "kept"}]
    assert fetch(None)["retention"] == summary
    assert fetch("person-a")["retention"] == summary
    assert fetch("person-b")["retention"] == [entry]
    assert receipt_store.get("req_access", audience="internal", path=path)["retention"] == [entry]
