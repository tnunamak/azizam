"""A member's own choice for request history (GET and PUT /me/api/retention), session only.

This route is not in the slice 5 list. It is here because request history is on by default (D50): a member must be
able to turn it off. Tested against a router of its own with a stub session, so the session wiring of the whole app
is not needed.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import admin_store, media_jobs, request_history, request_history_api, retention


@pytest.fixture
def member(tmp_path, monkeypatch):
    admin = tmp_path / "admin.db"
    media = tmp_path / "media_jobs.db"
    admin_store.init_db(admin)
    media_jobs.init_db(media)
    person = admin_store.upsert_oidc_user("https://fixture.test", "choosy", email="choosy@example.test", path=admin)
    session = SimpleNamespace(subject=person["id"])
    state = {"session": session, "refuse": None}

    app = FastAPI()
    app.include_router(request_history_api.create_request_history_router(
        session_provider=lambda request: state["session"],
        mutation_validator=lambda request, session: state["refuse"],
        admin_path=lambda: admin,
        media_path=lambda: media,
    ))
    return SimpleNamespace(app=app, admin=admin, media=media, person=person["id"], state=state)


def _kept(media: Path, person: str) -> int:
    conn = sqlite3.connect(media)
    try:
        return conn.execute("SELECT COUNT(*) FROM kept_requests WHERE person_id=?", (person,)).fetchone()[0]
    finally:
        conn.close()


def _keep_one(member) -> None:
    request_history.insert({
        "record_id": request_history.new_record_id(), "request_id": None, "person_id": member.person, "app_id": None,
        "origin_kind": "session", "origin_id": None, "created_at": int(time.time()),
        "kept_until": int(time.time()) + 86_400, "parts": {"accepted": {"headers": {}, "body": {}}},
        "omissions": [], "policy_version": 1, "summary": {"route": "/v1/chat/completions"},
    }, path=member.media)


def test_a_new_member_sees_history_on_with_the_default_period_and_accepted_parts(member):
    body = TestClient(member.app).get("/me/api/retention").json()
    assert body["keep"] is True
    assert body["retention"] == "7_days"
    assert body["parts"] == ["accepted"]
    assert body["choices"] == ["1_day", "7_days", "30_days"]


def test_the_choice_names_the_parts_the_gateway_offers_so_a_screen_can_offer_them(member):
    body = TestClient(member.app).get("/me/api/retention").json()
    assert body["allowed_parts"] == ["accepted", "received"]


def test_turning_it_off_erases_the_kept_requests_and_refuses_new_ones(member):
    _keep_one(member)
    assert _kept(member.media, member.person) == 1
    response = TestClient(member.app).put("/me/api/retention", json={"keep": False, "retention": "7_days",
                                                                     "parts": ["accepted"]})
    assert response.status_code == 200, response.text
    assert response.json()["keep"] is False
    assert _kept(member.media, member.person) == 0
    decision = retention.decide(retention.REQUEST_HISTORY, person_id=member.person,
                                member=retention.member_choice(member.admin, member.person,
                                                               retention.GROUP_REQUEST_HISTORY),
                                household_policy=None, request_body=None)
    assert decision.keep is False and decision.reason == "member_disabled"


def test_turning_it_back_on_does_not_bring_back_what_was_erased(member):
    _keep_one(member)
    client = TestClient(member.app)
    client.put("/me/api/retention", json={"keep": False, "retention": "7_days", "parts": ["accepted"]})
    time.sleep(1.1)  # the erasure cutoff is a second; a request after it is a new record
    response = client.put("/me/api/retention", json={"keep": True, "retention": "7_days", "parts": ["accepted", "received"]})
    assert response.status_code == 200
    assert response.json()["parts"] == ["accepted", "received"]
    assert _kept(member.media, member.person) == 0


@pytest.mark.parametrize("payload", [
    {"keep": "yes", "retention": "7_days", "parts": ["accepted"]},
    {"keep": True, "retention": "1_hour", "parts": ["accepted"]},
    {"keep": True, "retention": "7_days", "parts": ["received"]},
    {"keep": True, "retention": "7_days", "parts": ["accepted", "sent"]},
    {"keep": True, "retention": "7_days", "parts": ["accepted", "accepted"]},
])
def test_an_invalid_choice_is_refused_with_400_and_changes_nothing(member, payload):
    client = TestClient(member.app)
    before = client.get("/me/api/retention").json()
    response = client.put("/me/api/retention", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_retention"
    assert client.get("/me/api/retention").json() == before


def test_no_session_is_unauthorized_and_a_failed_mutation_check_is_returned_unchanged(member):
    member.state["session"] = None
    assert TestClient(member.app).get("/me/api/retention").status_code == 401
    member.state["session"] = SimpleNamespace(subject=member.person)
    member.state["refuse"] = JSONResponse({"error": {"code": "user_action_header_required"}}, status_code=403)
    response = TestClient(member.app).put("/me/api/retention", json={"keep": False, "retention": "7_days",
                                                                     "parts": ["accepted"]})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "user_action_header_required"
