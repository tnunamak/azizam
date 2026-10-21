"""Missing retained bytes must be visible without changing historical job facts."""
import json
import sqlite3
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import media_jobs
from gateway.api import create_gateway_router
from tests.test_gateway_api import _build_registry


def test_job_api_reports_missing_outputs_and_does_not_issue_broken_links(tmp_path):
    present = tmp_path / "present.png"
    present.write_bytes(b"fixture")
    job = {"job_id": "mj_fixture", "status": "succeeded", "intent": "image.generate", "assets": [
        {"asset_id": "img_" + "a" * 64, "url": "https://fixture.invalid/present"},
        {"asset_id": "img_" + "b" * 64, "url": "https://fixture.invalid/missing",
         "asset_url": "https://fixture.invalid/missing", "content_url": "https://fixture.invalid/missing"},
    ]}
    app = FastAPI()
    app.include_router(create_gateway_router(
        _build_registry(), media_job_getter=lambda *_a, **_k: job,
        media_job_lister=lambda **_k: [job],
        asset_path_resolver=lambda aid: present if aid.endswith("a" * 64) else tmp_path / "gone.png"))
    with TestClient(app) as client:
        detail = client.get("/gateway/v1/media/jobs/mj_fixture").json()
        listed = client.get("/gateway/v1/media/jobs").json()["jobs"][0]
    for view in (detail, listed):
        assert view["status"] == "succeeded"
        assert view["output_state"] == "partial"
        assert view["assets"][0] == job["assets"][0]
        missing = view["assets"][1]
        assert missing["available"] is False and missing["availability"] == "missing"
        assert not any(missing.get(key) for key in ("url", "asset_url", "content_url"))
    assert "available" not in job["assets"][0], "projection mutated the stored record"


def test_consistency_command_counts_by_kind_without_content_names_or_database_writes(tmp_path):
    database = tmp_path / "jobs.db"
    cache = tmp_path / "images"
    cache.mkdir()
    media_jobs.init_db(database)
    for number, kind in enumerate(("img", "aud", "vid")):
        media_jobs.create(media_jobs.MediaJob(
            job_id=f"mj_{number}", request_id=f"req_{number}", intent="image.generate", status="succeeded",
            assets=[{"asset_id": kind + "_" + str(number) * 64}]), path=database)
    # Arbitrary retained suffixes count; sidecars and staging files do not.
    (cache / ("0" * 64 + ".gif")).write_bytes(b"fixture")
    (cache / ("1" * 64 + ".meta.json")).write_text("{}")
    (cache / ("2" * 64 + ".mp4.tmp")).write_bytes(b"fixture")
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir() if p.is_file()}
    result = subprocess.run([sys.executable, "scripts/check-missing-outputs", "--database", str(database),
                             "--media-dir", str(cache)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["missing_outputs"] == {"image": {"jobs": 0, "outputs": 0},
                                         "audio": {"jobs": 1, "outputs": 1},
                                         "video": {"jobs": 1, "outputs": 1}}
    assert all(token not in result.stdout for token in ("mj_", "req_", "0" * 64, "1" * 64, "2" * 64))
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir() if p.is_file()}
    assert before == after


def test_missing_output_projection_is_used_by_person_jobs(tmp_path):
    from gateway.me_make import _client_view
    job = {"job_id": "mj_fixture", "intent": "image.generate", "status": "succeeded", "assets": [
        {"asset_id": "img_" + "a" * 64, "url": "https://fixture.invalid/missing"}]}
    result = _client_view(job, asset_path_resolver=lambda _aid: tmp_path / "missing.png")
    assert result["output_state"] == "missing"
    assert result["assets"][0]["availability"] == "missing"
    assert not result["assets"][0].get("url")
