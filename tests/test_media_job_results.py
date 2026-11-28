"""The job view lists a job's published results, and one JSON result is read on its own authenticated route (S2.5b).

The router is the real one, on a temporary jobs database. The identity is a header the test sets: the router's own
record authorizer and result reader come from the caller, as the gateway's do from authentication. The module under
test names no workflow, engine or modality: the result names and values are made up.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import media_jobs, result_bundle
from gateway.api import create_gateway_router
from gateway.discovery import WorkflowRegistry
from gateway.result_bundle import Reader, ResultLimits
from tests.support.registry_provider import provider_of

CANARY = "canary-value-7c1f"
LIMITS = ResultLimits()
VALUE = json.dumps({"answer": CANARY}).encode()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "media_jobs.db"
    media_jobs.init_db(path)
    return path


def make_job(db: Path, tag: str = "a", *, person: str = "person-1") -> str:
    job = media_jobs.create(
        media_jobs.MediaJob(intent="thing.make", request_id=f"req-{tag}", status=media_jobs.STATUS_RUNNING,
                            app_id="app-1", owner_key_hash=f"key-{tag}", maker_person_id=person),
        path=db, maker_from_request=False,
    )
    return job.job_id


def scope_of(db: Path, job_id: str) -> str:
    return media_jobs.get(job_id, audience="internal", path=db)["owner_scope"]


def stage_json(job_id: str, name: str = "answer", raw: bytes = VALUE):
    def stage(conn, attempt):
        result_bundle.stage_json_in(conn, job_id, attempt, name, raw, limits=LIMITS)
    return stage


def publish(db: Path, job_id: str, name: str = "answer") -> None:
    result_bundle.complete_job(job_id, stage_json(job_id, name), limits=LIMITS,
                               status=media_jobs.STATUS_SUCCEEDED, path=db)


def client(db: Path) -> TestClient:
    """A caller is whoever the ``x-person`` header names; a request with no header resolves no identity."""

    def authorizer(request, _kind, record) -> bool:
        return bool(request.headers.get("x-person")) and record.get("maker_person_id") == request.headers["x-person"]

    def reader(request):
        person = request.headers.get("x-person")
        if not person:
            return None
        # The App scope the job was stamped with: the test's one App.
        return Reader(owner_scope="app:app-1", person_id=person)

    app = FastAPI()
    app.include_router(create_gateway_router(
        provider_of(WorkflowRegistry()), media_job_store_path=db, record_authorizer=authorizer, result_reader_resolver=reader,
        audience_resolver=lambda _request: "client"))
    return TestClient(app)


def as_person(person: str = "person-1") -> dict[str, str]:
    return {"x-person": person}


def get_result(http: TestClient, job_id: str, name: str = "answer", person: str | None = "person-1"):
    return http.get(f"/gateway/v1/media/jobs/{job_id}/results/{name}", headers=as_person(person) if person else {})


# ------------------------------------------------------------------------------------------------- the job view

def test_a_published_json_result_is_listed_with_the_route_that_reads_it_and_never_its_value(db: Path):
    job_id = make_job(db)
    publish(db, job_id)
    body = client(db).get(f"/gateway/v1/media/jobs/{job_id}", headers=as_person()).json()
    assert body["results"] == {"answer": {"kind": "json", "count": 1,
                                          "href": f"/gateway/v1/media/jobs/{job_id}/results/answer"}}
    assert CANARY not in json.dumps(body)


def test_a_job_with_no_published_result_shows_no_results_field(db: Path):
    job_id = make_job(db)
    body = client(db).get(f"/gateway/v1/media/jobs/{job_id}", headers=as_person()).json()
    assert "results" not in body


def test_a_staged_result_of_a_failed_run_is_not_listed(db: Path):
    job_id = make_job(db)
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result_bundle.stage_json_in(conn, job_id, 1, "answer", VALUE, limits=LIMITS)
        conn.commit()
    media_jobs.update_status(job_id, media_jobs.STATUS_FAILED, error="boom", path=db)
    body = client(db).get(f"/gateway/v1/media/jobs/{job_id}", headers=as_person()).json()
    assert body["status"] == media_jobs.STATUS_FAILED
    assert "results" not in body


def test_an_asset_result_is_listed_with_its_count_and_no_href(db: Path):
    job_id = make_job(db)
    result_bundle.mirror_assets(
        job_id, [{"asset_id": "img_a", "media_type": "image/png", "size_bytes": 4}], limits=LIMITS, name="image",
        progress=100, path=db)
    body = client(db).get(f"/gateway/v1/media/jobs/{job_id}", headers=as_person()).json()
    assert body["results"] == {"image": {"kind": "assets", "count": 1}}


def test_the_job_list_carries_results_too(db: Path):
    job_id = make_job(db)
    publish(db, job_id)
    rows = client(db).get("/gateway/v1/media/jobs", headers=as_person()).json()["jobs"]
    assert rows[0]["results"]["answer"]["kind"] == "json"


# ------------------------------------------------------------------------------------------------- the read route

def test_the_maker_reads_the_published_value_as_json_and_nothing_caches_it(db: Path):
    job_id = make_job(db)
    publish(db, job_id)
    response = get_result(client(db), job_id)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.content == VALUE


def test_every_refusal_is_the_same_404(db: Path):
    job_id = make_job(db)
    publish(db, job_id)
    other = make_job(db, "b", person="person-2")
    http = client(db)
    refused = [
        get_result(http, "no-such-job"),                    # no such job
        get_result(http, job_id, "no-such-result"),          # no such result
        get_result(http, job_id, person="person-2"),         # another maker
        get_result(http, job_id, person=None),               # no identity
        get_result(http, other, person="person-2"),          # a job that published nothing
    ]
    assert {r.status_code for r in refused} == {404}
    assert len({r.text for r in refused}) == 1, "a caller cannot tell the refusals apart"
    assert CANARY not in "".join(r.text for r in refused)


def test_another_apps_reader_with_the_makers_person_is_refused(db: Path):
    job_id = make_job(db)
    publish(db, job_id)
    app = FastAPI()
    app.include_router(create_gateway_router(
        provider_of(WorkflowRegistry()), media_job_store_path=db, result_reader_resolver=lambda _r: Reader("app:app-2", "person-1"),
        audience_resolver=lambda _request: "client"))
    assert TestClient(app).get(f"/gateway/v1/media/jobs/{job_id}/results/answer").status_code == 404


def test_the_record_authorizer_decides_before_the_store_is_read(db: Path):
    job_id = make_job(db)
    publish(db, job_id)
    app = FastAPI()
    app.include_router(create_gateway_router(
        provider_of(WorkflowRegistry()), media_job_store_path=db, record_authorizer=lambda *_a: False,
        result_reader_resolver=lambda _r: Reader("app:app-1", "person-1"), audience_resolver=lambda _request: "client"))
    assert TestClient(app).get(f"/gateway/v1/media/jobs/{job_id}/results/answer").status_code == 404


def test_a_staged_value_of_a_failed_run_cannot_be_read(db: Path):
    job_id = make_job(db)
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result_bundle.stage_json_in(conn, job_id, 1, "answer", VALUE, limits=LIMITS)
        conn.commit()
    media_jobs.update_status(job_id, media_jobs.STATUS_FAILED, error="boom", path=db)
    response = get_result(client(db), job_id)
    assert response.status_code == 404 and CANARY not in response.text


def test_an_erased_value_is_gone_from_the_route_and_the_view(db: Path):
    job_id = make_job(db)
    publish(db, job_id)
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result_bundle.erase_in(conn, {scope_of(db, job_id)}, before=2**31)
        conn.commit()
    http = client(db)
    assert get_result(http, job_id).status_code == 404
    assert "results" not in http.get(f"/gateway/v1/media/jobs/{job_id}", headers=as_person()).json()


def test_an_asset_result_is_not_readable_as_json(db: Path):
    job_id = make_job(db)
    result_bundle.mirror_assets(
        job_id, [{"asset_id": "img_a", "media_type": "image/png", "size_bytes": 4}], limits=LIMITS, name="image",
        progress=100, path=db)
    assert get_result(client(db), job_id, "image").status_code == 404


def test_without_a_result_reader_the_route_answers_404(db: Path):
    job_id = make_job(db)
    publish(db, job_id)
    app = FastAPI()
    app.include_router(create_gateway_router(provider_of(WorkflowRegistry()), media_job_store_path=db,
                                             audience_resolver=lambda _request: "client"))
    assert TestClient(app).get(f"/gateway/v1/media/jobs/{job_id}/results/answer").status_code == 404


# ------------------------------------------------------------------------------------ a name is one path segment

@pytest.mark.parametrize("name", ["a/b", "/a", "a%2Fb", "..", ".", ".hidden", "a b", "a?b", "a#b", "", "x" * 129])
def test_a_name_that_is_not_one_path_segment_is_never_published(db: Path, name: str):
    job_id = make_job(db)
    with pytest.raises(result_bundle.ResultRefused) as refused:
        publish(db, job_id, name)
    assert refused.value.code == "bad_name"
    assert "results" not in client(db).get(f"/gateway/v1/media/jobs/{job_id}", headers=as_person()).json()


def test_a_published_result_is_read_back_by_the_href_the_view_advertises(db: Path):
    job_id = make_job(db)
    publish(db, job_id, "a.b_c-1")
    http = client(db)
    href = http.get(f"/gateway/v1/media/jobs/{job_id}", headers=as_person()).json()["results"]["a.b_c-1"]["href"]
    response = http.get(href, headers=as_person())
    assert response.status_code == 200 and response.content == VALUE


@pytest.mark.parametrize("name", ["a/b", "a%2Fb", "answer/extra", "%2e%2e/answer"])
def test_a_path_shaped_name_gets_the_same_refusal_as_a_missing_one(db: Path, name: str):
    job_id = make_job(db)
    publish(db, job_id)
    http = client(db)
    plain = get_result(http, job_id, "no-such-result")
    shaped = get_result(http, job_id, name)
    assert (shaped.status_code, shaped.text) == (plain.status_code, plain.text) == (404, plain.text)
