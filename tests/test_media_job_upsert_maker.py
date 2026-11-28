"""The insert primitive stamps a job's maker the way ``create`` does (OV3 slice 1, round 6)."""
from __future__ import annotations

from gateway import media_jobs, request_source


def test_an_authenticated_upsert_stamps_a_new_job_with_its_maker(tmp_path):
    db = tmp_path / "jobs.db"
    media_jobs.init_db(db)
    with request_source.acting_as(app_id="app_test", person_id="usr_alice", credential_id="key:alice"):
        job = media_jobs.upsert(media_jobs.MediaJob(intent="image.generate", app_id="app_test"), path=db)

    stored = media_jobs.get(job.job_id, audience="internal", path=db)
    assert (stored["maker_person_id"], stored["maker_credential_id"]) == ("usr_alice", "key:alice")


def test_an_upsert_outside_a_request_stamps_no_maker(tmp_path):
    db = tmp_path / "jobs.db"
    media_jobs.init_db(db)
    job = media_jobs.upsert(media_jobs.MediaJob(intent="image.generate", app_id="app_test"), path=db)

    assert media_jobs.get(job.job_id, audience="internal", path=db)["maker_person_id"] is None


def test_an_upsert_keeps_the_maker_a_job_was_stamped_with(tmp_path):
    db = tmp_path / "jobs.db"
    media_jobs.init_db(db)
    with request_source.acting_as(app_id="app_test", person_id="usr_alice"):
        job = media_jobs.upsert(media_jobs.MediaJob(intent="image.generate", app_id="app_test"), path=db)
    with request_source.acting_as(app_id="app_test", person_id="usr_bob"):
        media_jobs.upsert(job, path=db)

    assert media_jobs.get(job.job_id, audience="internal", path=db)["maker_person_id"] == "usr_alice"


def test_an_indexed_upsert_keeps_the_maker_it_carries(tmp_path):
    db = tmp_path / "jobs.db"
    media_jobs.init_db(db)
    with request_source.acting_as(app_id="app_test", person_id="usr_bob"):
        job = media_jobs.upsert(
            media_jobs.MediaJob(intent="video.generate", app_id="app_test", maker_person_id="usr_carol"),
            path=db, maker_from_request=False,
        )

    assert media_jobs.get(job.job_id, audience="internal", path=db)["maker_person_id"] == "usr_carol"
