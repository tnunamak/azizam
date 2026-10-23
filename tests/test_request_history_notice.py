"""The one-time request history notice for a member who predates it (REQUEST_RETENTION.md section 12, item 3; D50).

A member who predates request history has a history row with the switch off that nobody changed. The schema 17
upgrade wrote that row with no last writer (``retention_migration.migrate_group_keys``). That member is told once.
Turning the switch on or off answers the notice, and so does dismissing it. A new member starts with history on, so
is never told, and a member who turned it off is not told either.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import admin_store, media_jobs, request_history_api, retention


class Harness:
    def __init__(self, root) -> None:
        self.admin = root / "admin.db"
        self.media = root / "media_jobs.db"
        admin_store.init_db(self.admin)
        media_jobs.init_db(self.media)
        self.session = None
        self.refuse = None
        app = FastAPI()
        app.include_router(request_history_api.create_request_history_router(
            session_provider=lambda request: self.session,
            mutation_validator=lambda request, session: self.refuse,
            admin_path=lambda: self.admin,
            media_path=lambda: self.media,
        ))
        self.client = TestClient(app)

    def person(self, subject: str) -> str:
        return admin_store.upsert_oidc_user("https://fixture.test", subject, email=f"{subject}@example.test",
                                            path=self.admin)["id"]

    def sign_in(self, person_id: str) -> None:
        self.session = SimpleNamespace(subject=person_id)

    def predate(self, person_id: str) -> None:
        """The row the schema 17 upgrade gives an account that existed before request history: off, and no last writer."""
        conn = sqlite3.connect(self.admin)
        try:
            conn.execute(f"UPDATE {retention.MEMBER_TABLE} SET remember=0, updated_by=NULL WHERE user_id=? AND grp=?",
                         (person_id, retention.GROUP_REQUEST_HISTORY))
            conn.commit()
        finally:
            conn.close()


@pytest.fixture
def h(tmp_path) -> Harness:
    return Harness(tmp_path)


def test_a_member_who_predates_history_is_told_the_notice_is_due(h):
    alice = h.person("alice")
    h.predate(alice)
    h.sign_in(alice)
    body = h.client.get("/me/api/retention").json()
    assert body["keep"] is False
    assert body["notice_due"] is True


def test_a_new_member_is_never_told(h):
    bo = h.person("bo")
    h.sign_in(bo)
    body = h.client.get("/me/api/retention").json()
    assert body["keep"] is True
    assert body["notice_due"] is False


def test_a_member_who_turned_history_off_is_not_told(h):
    cy = h.person("cy")
    h.sign_in(cy)
    assert h.client.put("/me/api/retention", json={"keep": False, "retention": "7_days",
                                                   "parts": ["accepted"]}).status_code == 200
    assert h.client.get("/me/api/retention").json()["notice_due"] is False


def test_turning_history_on_answers_the_notice(h):
    dee = h.person("dee")
    h.predate(dee)
    h.sign_in(dee)
    response = h.client.put("/me/api/retention", json={"keep": True, "retention": "7_days",
                                                       "parts": ["accepted"]})
    assert response.status_code == 200
    assert response.json()["notice_due"] is False
    assert h.client.get("/me/api/retention").json()["notice_due"] is False


def test_dismissing_the_notice_hides_it_and_leaves_the_choice_as_it_was(h):
    eve = h.person("eve")
    h.predate(eve)
    h.sign_in(eve)
    response = h.client.post("/me/api/retention/notice-seen")
    assert response.status_code == 200
    assert response.json() == {"notice_due": False}
    body = h.client.get("/me/api/retention").json()
    assert body["keep"] is False
    assert body["notice_due"] is False


def test_dismissing_twice_is_harmless(h):
    fay = h.person("fay")
    h.predate(fay)
    h.sign_in(fay)
    assert h.client.post("/me/api/retention/notice-seen").status_code == 200
    assert h.client.post("/me/api/retention/notice-seen").status_code == 200
    assert h.client.get("/me/api/retention").json()["notice_due"] is False


def test_dismissing_is_for_one_person_only(h):
    gus = h.person("gus")
    hal = h.person("hal")
    h.predate(gus)
    h.predate(hal)
    h.sign_in(gus)
    h.client.post("/me/api/retention/notice-seen")
    h.sign_in(hal)
    assert h.client.get("/me/api/retention").json()["notice_due"] is True


def test_the_dismissal_needs_a_session(h):
    ida = h.person("ida")
    h.predate(ida)
    h.sign_in(ida)
    h.session = None
    response = h.client.post("/me/api/retention/notice-seen")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    h.sign_in(ida)
    assert h.client.get("/me/api/retention").json()["notice_due"] is True


def test_the_dismissal_runs_the_same_write_checks_as_a_choice(h):
    jo = h.person("jo")
    h.predate(jo)
    h.sign_in(jo)
    h.refuse = JSONResponse({"error": {"code": "user_action_header_required"}}, status_code=403)
    response = h.client.post("/me/api/retention/notice-seen")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "user_action_header_required"
    h.refuse = None
    assert h.client.get("/me/api/retention").json()["notice_due"] is True


def test_the_choice_says_when_the_owner_has_turned_history_off_for_everyone(h):
    """The household kill switch is off for everyone, whatever a member chose. The member's own choice is unchanged."""
    ann = h.person("ann")
    h.sign_in(ann)
    assert h.client.get("/me/api/retention").json()["household_off"] is False
    admin_store.put_operator_setting("retention", {"never_keep": True}, path=h.admin)
    body = h.client.get("/me/api/retention").json()
    assert body["household_off"] is True
    assert body["keep"] is True
