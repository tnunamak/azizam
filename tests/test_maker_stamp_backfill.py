"""The one-time maker backfill for media jobs written before schema 6 (OV3 slice 1).

A legacy job gets the maker its own creation-time evidence names: its own receipt, then the owner it was stored under.
Nothing else counts: a parent link, a client-supplied field, or the owner of a service App.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from gateway import admin_store, content_makers, maker_stamp_migration, me_media, media_jobs, receipt_store, record_ownership
from gateway.content_access import Viewer
from gateway.media_jobs import MediaJob


@pytest.fixture
def stores(tmp_path: Path):
    stores: dict[str, Any] = {"admin": tmp_path / "admin.db", "jobs": tmp_path / "media_jobs.db",
                              "receipts": tmp_path / "receipts.db"}
    admin_store.init_db(stores["admin"])
    media_jobs.init_db(stores["jobs"])
    receipt_store.init_db(stores["receipts"])
    people = {name: admin_store.upsert_oidc_user("issuer", f"{name}-sub", email=f"{name}@example.com",
                                                 path=stores["admin"]) for name in ("alice", "bob", "carol", "dave", "ops")}
    stores["people"] = {name: user["id"] for name, user in people.items()}
    stores["personal_app"] = admin_store.create_app("Alice", kind="personal", owner_user_id=people["alice"]["id"],
                                                    access_plan="person", path=stores["admin"])["id"]
    stores["service_app"] = admin_store.create_app("Desk", kind="service", owner_user_id=people["ops"]["id"],
                                                   access_plan="person", path=stores["admin"])["id"]
    stores["session_app"] = admin_store.ensure_session_app_for_user(
        people["alice"]["id"], name="Alice", path=stores["admin"])["id"]
    return stores


def _job(stores, job_id: str, *, request_id: str, app_id: str | None = None, owner: str | None = None,
         parent_request_id: str | None = None) -> str:
    """Store a job as the current code does. An App-keyed job's owner is the App principal, as the write stores it."""
    job = MediaJob(intent="image.generate", workflow="demo.image", backend="comfyui.local", request_id=request_id,
                   job_id=job_id, app_id=app_id, owner_key_hash=owner, parent_request_id=parent_request_id,
                   status=media_jobs.STATUS_SUCCEEDED, progress=100, metadata={"mode": "make"})
    media_jobs.create(job, path=stores["jobs"], maker_from_request=False)
    return job_id


def _make_legacy(stores, job_id: str) -> None:
    """Rewrite the row as a version 5 job: no maker keys in its JSON and NULL stamp columns."""
    with closing(sqlite3.connect(stores["jobs"])) as conn:
        conn.execute(
            "UPDATE media_jobs SET job_json = json_remove(job_json, '$.maker_person_id', '$.maker_credential_id', "
            "'$.maker_connection_id'), maker_person_id = NULL, maker_credential_id = NULL, "
            "maker_connection_id = NULL WHERE job_id = ?", (job_id,))
        conn.commit()


def _receipt(stores, request_id: str, *, job_id: str | None, app_id: str | None, maker: str | None) -> None:
    receipt_store.record({
        "request_id": request_id, "status": "succeeded", "intent": "image.generate", "job_id": job_id,
        "app_id": app_id, "maker_person_id": maker, "protocol": "azizam.make",
    }, path=stores["receipts"])


def _backfill(stores) -> dict[str, int] | None:
    return maker_stamp_migration.backfill(stores["jobs"], receipts_path=stores["receipts"], admin_path=stores["admin"])


def _row(stores, job_id: str) -> dict:
    job = media_jobs.get(job_id, path=stores["jobs"])
    assert job is not None
    return job


def _reads(stores, job_id: str, viewer: Viewer) -> bool:
    return record_ownership.owns_record("media_job", _row(stores, job_id), viewer=viewer,
                                        apps=content_makers.Apps(stores["admin"]))


def _session(stores, name: str) -> Viewer:
    return Viewer(person_id=stores["people"][name], auth_kind="session")


def test_a_legacy_personal_key_job_is_visible_to_its_maker_after_the_backfill(stores):
    _job(stores, "job_personal", request_id="req_personal", app_id=stores["personal_app"],
         owner=f"app:{stores['personal_app']}")
    _make_legacy(stores, "job_personal")
    assert not _reads(stores, "job_personal", _session(stores, "alice"))

    counts = _backfill(stores)

    assert counts == {"personal_app": 1}
    assert _row(stores, "job_personal")["maker_person_id"] == stores["people"]["alice"]
    assert _reads(stores, "job_personal", _session(stores, "alice"))
    assert not _reads(stores, "job_personal", _session(stores, "bob"))


def test_a_legacy_session_job_is_visible_to_the_signed_in_person_who_made_it(stores):
    _job(stores, "job_session", request_id="req_session", app_id=stores["session_app"],
         owner=f"app:{stores['session_app']}")
    _make_legacy(stores, "job_session")

    _backfill(stores)

    assert _row(stores, "job_session")["maker_person_id"] == stores["people"]["alice"]
    assert _reads(stores, "job_session", _session(stores, "alice"))
    assert not _reads(stores, "job_session", _session(stores, "ops"))


def test_a_legacy_personal_key_job_is_listed_in_its_makers_library_after_the_backfill(stores):
    # The Library lists the jobs its member scope selects, so the listing is the check a person sees.
    _job(stores, "job_listed", request_id="req_listed", app_id=stores["personal_app"],
         owner=f"app:{stores['personal_app']}")
    _make_legacy(stores, "job_listed")

    def listed(name: str) -> set[str]:
        scope = me_media._member_media_scope(stores["people"][name], stores["admin"])
        return {job["job_id"] for job, _receipt in me_media._scoped_jobs(
            scope, jobs_path=stores["jobs"], receipts_path=stores["receipts"])}

    assert "job_listed" not in listed("alice")
    _backfill(stores)
    assert "job_listed" in listed("alice")
    assert "job_listed" not in listed("bob")


def test_a_legacy_service_app_job_with_a_recorded_maker_goes_to_that_maker(stores):
    _job(stores, "job_svc", request_id="req_svc", app_id=stores["service_app"], owner=f"app:{stores['service_app']}")
    _receipt(stores, "req_svc", job_id="job_svc", app_id=stores["service_app"], maker=stores["people"]["carol"])
    _make_legacy(stores, "job_svc")

    _backfill(stores)

    assert _row(stores, "job_svc")["maker_person_id"] == stores["people"]["carol"]
    assert _reads(stores, "job_svc", _session(stores, "carol"))
    assert not _reads(stores, "job_svc", _session(stores, "ops"))


def test_a_legacy_service_app_job_with_no_maker_is_not_given_to_the_apps_owner(stores):
    _job(stores, "job_svc_none", request_id="req_svc_none", app_id=stores["service_app"],
         owner=f"app:{stores['service_app']}")
    _make_legacy(stores, "job_svc_none")

    counts = _backfill(stores)

    assert counts == {"credential_only": 1}
    assert _row(stores, "job_svc_none")["maker_person_id"] is None
    # The owner of a service App is never the maker. The App's own key reads the person-less row, as before.
    assert not _reads(stores, "job_svc_none", _session(stores, "ops"))
    app_key = Viewer(None, stores["service_app"], owner_keys=frozenset({f"app:{stores['service_app']}"}),
                     auth_kind="app_key", credential_id="cred:desk")
    assert _reads(stores, "job_svc_none", app_key)


def test_a_legacy_job_whose_parent_link_names_another_person_is_not_given_to_that_person(stores):
    _receipt(stores, "req_dave", job_id="job_dave", app_id=stores["service_app"], maker=stores["people"]["dave"])
    _job(stores, "job_child", request_id="req_child", app_id=stores["service_app"],
         owner=f"app:{stores['service_app']}", parent_request_id="req_dave")
    _make_legacy(stores, "job_child")

    _backfill(stores)

    assert _row(stores, "job_child")["maker_person_id"] is None
    assert not _reads(stores, "job_child", _session(stores, "dave"))


def test_a_receipt_that_names_another_job_does_not_lend_its_maker(stores):
    # A request id can repeat. The receipt under this job's request id belongs to another job, so it names no maker here.
    _job(stores, "job_reused", request_id="req_reused", app_id=stores["service_app"],
         owner=f"app:{stores['service_app']}")
    _receipt(stores, "req_reused", job_id="job_other", app_id=stores["service_app"], maker=stores["people"]["carol"])
    _make_legacy(stores, "job_reused")

    _backfill(stores)

    assert _row(stores, "job_reused")["maker_person_id"] is None
    assert not _reads(stores, "job_reused", _session(stores, "carol"))


def test_an_unresolvable_legacy_job_stays_unstamped_and_unread(stores):
    _job(stores, "job_unknown", request_id="req_unknown", owner="user:not-a-person")
    _make_legacy(stores, "job_unknown")

    counts = _backfill(stores)

    assert counts == {"unresolved": 1}
    row = _row(stores, "job_unknown")
    assert row["maker_person_id"] is None and row["maker_credential_id"] is None
    for name in ("alice", "ops"):
        assert not _reads(stores, "job_unknown", _session(stores, name))


def test_running_the_backfill_twice_changes_nothing(stores):
    _job(stores, "job_personal", request_id="req_p", app_id=stores["personal_app"], owner=f"app:{stores['personal_app']}")
    _job(stores, "job_svc", request_id="req_s", app_id=stores["service_app"], owner=f"app:{stores['service_app']}")
    _job(stores, "job_unknown", request_id="req_u", owner="user:not-a-person")
    for job_id in ("job_personal", "job_svc", "job_unknown"):
        _make_legacy(stores, job_id)
    _backfill(stores)
    with closing(sqlite3.connect(stores["jobs"])) as conn:
        first = conn.execute("SELECT job_id, job_json, maker_person_id, maker_credential_id, maker_connection_id "
                             "FROM media_jobs ORDER BY job_id").fetchall()

    assert _backfill(stores) is None
    # Even without the recorded pass, a second scan finds no legacy row and so writes nothing.
    with closing(sqlite3.connect(stores["jobs"])) as conn:
        conn.execute("DELETE FROM media_jobs_migrations")
        conn.commit()
    assert _backfill(stores) == {}
    with closing(sqlite3.connect(stores["jobs"])) as conn:
        second = conn.execute("SELECT job_id, job_json, maker_person_id, maker_credential_id, maker_connection_id "
                              "FROM media_jobs ORDER BY job_id").fetchall()
    assert second == first


def test_a_job_written_after_the_schema_upgrade_is_not_rewritten(stores):
    # Keys present with null are a decided stamp: the backfill must leave a job the current code wrote alone.
    _job(stores, "job_new", request_id="req_new", app_id=stores["personal_app"], owner=f"app:{stores['personal_app']}")

    counts = _backfill(stores)

    assert counts == {}
    assert _row(stores, "job_new")["maker_person_id"] is None
