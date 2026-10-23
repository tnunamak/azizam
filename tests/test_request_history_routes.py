"""The maker's kept requests: list, read, delete and delete all, with expiry and the erasure fences (section 12).

docs/design/REQUEST_RETENTION.md sections 10 (surfaces), 12 (deletion, expiry, late writes), 16.1.4 (ownership before
pagination), 16.1.7 (late writes and erasure) and 16.1.8 (a delegated credential without the privacy permission gets
403 privacy_permission_required). The privacy permission itself is slice 5b, so no key or grant carries it here.
Routes run through the real app with a personal App key per member; rows are written through the one store.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import proxy
from gateway import admin_store, media_jobs, receipt_store, request_history, retention
from gateway.runtime_auth import hash_api_key
from tests.support import gateway as gateway_support
from tests.support.gateway import gateway_client, use_admin_db

NOW = int(time.time())


def _member(db: Path, subject: str) -> tuple[str, str]:
    """A person with a personal App and key of their own (section 4: only that key has a person)."""
    person = admin_store.upsert_oidc_user("https://fixture.test", subject, email=f"{subject}@example.test", path=db)
    app = admin_store.create_app(f"{subject} app", kind="personal", owner_user_id=person["id"],
                                 access_plan="person", path=db)
    key = f"opk_{subject.replace('-', '_')}_{app['id']}"
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"],
                                       hash=hash_api_key(key), prefix=key[:16], created_by=person["id"], path=db)
    return person["id"], key


def _record(person: str, *, created: int = NOW, kept_until: int | None = NOW + 86_400, app: str | None = None,
            origin: str = "app_key", credential: str | None = None, text: str = "a kept sentence") -> dict:
    return {
        "record_id": request_history.new_record_id(), "request_id": None, "person_id": person,
        "app_id": app, "origin_kind": origin, "origin_id": credential, "created_at": created,
        "kept_until": kept_until, "parts": {"accepted": {"headers": {}, "body": {"model": "m", "text": text}}},
        "omissions": [], "policy_version": 1,
        "summary": {"route": "/v1/chat/completions", "model": "m", "status": 200, "latency_ms": 7},
    }


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    db = use_admin_db(monkeypatch, tmp_path)
    media = tmp_path / "media_jobs.db"
    media_jobs.init_db(media)
    monkeypatch.setattr(media_jobs, "DB_PATH", media)
    receipt_store.init_db(receipt_store.DB_PATH)
    maker, maker_key = _member(db, "maker")
    other, other_key = _member(db, "other")
    return {"db": db, "media": media, "maker": maker, "maker_key": maker_key, "other": other,
            "other_key": other_key}


def _call(method: str, path: str, key: str | None = None, **kwargs):
    headers = {"authorization": f"Bearer {key}"} if key else {}
    async def run():
        async with gateway_client() as client:
            return await client.request(method, path, headers=headers, **kwargs)
    return asyncio.run(run())


def _store(gateway: dict, *records: dict) -> None:
    for record in records:
        request_history.insert(record, path=gateway["media"])


# ----------------------------------------------------------------------- list and read


def test_the_list_shows_only_the_makers_own_rows_newest_first_and_no_content(gateway):
    mine = [_record(gateway["maker"], created=NOW - index) for index in range(3)]
    _store(gateway, *mine, _record(gateway["other"], created=NOW + 5))
    response = _call("GET", "/gateway/v1/requests", gateway["maker_key"])
    assert response.status_code == 200, response.text
    items = response.json()["requests"]
    assert [item["record_id"] for item in items] == [record["record_id"] for record in mine]
    assert all("parts" not in item for item in items)
    assert items[0]["summary"]["route"] == "/v1/chat/completions"


def test_paging_runs_over_the_visible_rows_so_a_page_is_never_empty_because_of_other_members(gateway):
    mine = [_record(gateway["maker"], created=NOW - index) for index in range(5)]
    _store(gateway, *mine, *[_record(gateway["other"], created=NOW + index) for index in range(10)])
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        path = "/gateway/v1/requests?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = _call("GET", path, gateway["maker_key"]).json()
        assert page["requests"], "a page came back empty while the maker still has rows"
        seen.extend(item["record_id"] for item in page["requests"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    assert seen == [record["record_id"] for record in mine]


def test_read_one_returns_the_content_to_its_maker(gateway):
    record = _record(gateway["maker"], text="the sentence that was kept")
    _store(gateway, record)
    response = _call("GET", f"/gateway/v1/requests/{record['record_id']}", gateway["maker_key"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["record_id"] == record["record_id"]
    assert body["parts"]["accepted"]["body"]["text"] == "the sentence that was kept"


def test_another_members_row_reads_as_missing_with_the_same_answer(gateway):
    theirs = _record(gateway["other"])
    _store(gateway, theirs)
    other_reply = _call("GET", f"/gateway/v1/requests/{theirs['record_id']}", gateway["maker_key"])
    missing_reply = _call("GET", "/gateway/v1/requests/kr_does_not_exist", gateway["maker_key"])
    assert other_reply.status_code == missing_reply.status_code == 404
    assert other_reply.json()["error"]["code"] == missing_reply.json()["error"]["code"] == "not_found"


def test_an_expired_row_is_never_shown_even_before_maintenance_deletes_it(gateway):
    expired = _record(gateway["maker"], created=NOW - 100, kept_until=NOW - 1)
    _store(gateway, expired)
    assert _call("GET", f"/gateway/v1/requests/{expired['record_id']}", gateway["maker_key"]).status_code == 404
    assert _call("GET", "/gateway/v1/requests", gateway["maker_key"]).json()["requests"] == []


def test_the_owner_and_an_unauthenticated_caller_see_no_one_elses_history(gateway, monkeypatch):
    _store(gateway, _record(gateway["maker"]))
    monkeypatch.setitem(gateway_support._APP_KEY, "key", "")  # the client sends no default key
    assert _call("GET", "/gateway/v1/requests", None).status_code == 401
    assert _call("GET", "/gateway/v1/requests", gateway["other_key"]).json()["requests"] == []
    assert _call("GET", "/gateway/v1/requests", None).status_code == 401


# ----------------------------------------------------------------------- delete


def test_a_delegated_key_without_the_privacy_permission_cannot_delete_its_history(gateway):
    record = _record(gateway["maker"], app=None)
    _store(gateway, record)
    response = _call("DELETE", f"/gateway/v1/requests/{record['record_id']}", gateway["maker_key"])
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "privacy_permission_required"
    assert _call("GET", f"/gateway/v1/requests/{record['record_id']}", gateway["maker_key"]).status_code == 200


def test_delete_all_by_a_delegated_key_is_refused_the_same_way(gateway):
    _store(gateway, _record(gateway["maker"]))
    response = _call("DELETE", "/gateway/v1/requests", gateway["maker_key"])
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "privacy_permission_required"


def test_deleting_another_members_row_is_not_found_and_changes_nothing(gateway):
    theirs = _record(gateway["other"])
    _store(gateway, theirs)
    assert _call("DELETE", f"/gateway/v1/requests/{theirs['record_id']}", gateway["maker_key"]).status_code == 404
    assert _call("GET", f"/gateway/v1/requests/{theirs['record_id']}", gateway["other_key"]).status_code == 200


# ----------------------------------------------------------------------- erasure fences and expiry


def test_maintenance_deletes_expired_rows_and_keeps_the_rest(gateway):
    gone = _record(gateway["maker"], created=NOW - 100, kept_until=NOW - 1)
    kept = _record(gateway["maker"], created=NOW, kept_until=NOW + 1_000)
    _store(gateway, gone, kept)
    request_history.maintain(admin_path=gateway["db"], path=gateway["media"], now=NOW)
    ids = {row[0] for row in _rows(gateway["media"], "SELECT record_id FROM kept_requests")}
    assert ids == {kept["record_id"]}


def test_a_later_shorter_cap_shortens_existing_deadlines_and_a_longer_one_never_extends_them(gateway):
    record = _record(gateway["maker"], created=NOW, kept_until=NOW + 7 * 86_400)
    _store(gateway, record)
    request_history.tighten(3_600, person_id=gateway["maker"], path=gateway["media"], admin_path=gateway["db"])
    assert _rows(gateway["media"], "SELECT kept_until FROM kept_requests")[0][0] == NOW + 3_600
    request_history.tighten(7 * 86_400, person_id=gateway["maker"], path=gateway["media"], admin_path=gateway["db"])
    assert _rows(gateway["media"], "SELECT kept_until FROM kept_requests")[0][0] == NOW + 3_600


def test_delete_all_fences_a_request_that_started_before_it(gateway):
    """A request in flight while the maker deletes all history cannot write its record after the delete (16.1.7)."""
    started = NOW
    request_history.delete_all_for_person(gateway["maker"], admin_path=gateway["db"], path=gateway["media"], now=NOW + 1)
    decision = retention.decide(retention.REQUEST_HISTORY, person_id=gateway["maker"], member=None,
                                household_policy=None, request_body=None, now=started)
    calls: list = []
    outcomes: list = []
    kept = retention.write(retention.REQUEST_HISTORY, admin_path=gateway["db"], person_id=gateway["maker"],
                           created_at=started, decision=decision, commit=lambda deadline: calls.append(deadline),
                           on_result=outcomes.append)
    assert kept is False and calls == [] and outcomes[0].reason == "erasure_cutoff"


def test_turning_history_off_erases_the_group_and_refuses_new_writes_until_it_is_on_again(gateway):
    _store(gateway, _record(gateway["maker"]))
    request_history.set_member_choice(gateway["maker"], keep=False, retention_id="7_days", parts=["accepted"],
                                      admin_path=gateway["db"], path=gateway["media"], now=NOW + 1)
    assert _rows(gateway["media"], "SELECT COUNT(*) FROM kept_requests")[0][0] == 0
    decision = retention.decide(retention.REQUEST_HISTORY, person_id=gateway["maker"], member=None,
                                household_policy=None, request_body=None, now=NOW + 2)
    assert retention.write(retention.REQUEST_HISTORY, admin_path=gateway["db"], person_id=gateway["maker"],
                           created_at=NOW + 2, decision=decision, commit=lambda d: None) is False
    request_history.set_member_choice(gateway["maker"], keep=True, retention_id="7_days", parts=["accepted"],
                                      admin_path=gateway["db"], path=gateway["media"], now=NOW + 3)
    assert _rows(gateway["media"], "SELECT COUNT(*) FROM kept_requests")[0][0] == 0


def _rows(path: Path, sql: str) -> list[tuple]:
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        return [tuple(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()
