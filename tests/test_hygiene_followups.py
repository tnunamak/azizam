"""Regression contracts for orphan cleanup, private paths and retryable outputs."""
import asyncio
import errno
import json
import os
from pathlib import Path

import pytest

import proxy
import firehose_metrics
from gateway import media_jobs, private_io, prompt_metadata_migration, output_availability
from tests.test_image_media_jobs import authenticated, _StubJsonRequest


@pytest.mark.parametrize("sweep", ["delete", "purge", "prune"])
def test_new_style_job_stages_are_swept(tmp_path, monkeypatch, sweep):
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path)
    monkeypatch.setattr(media_jobs, "get", lambda *a, **k: {"status": "succeeded"})
    orphan = tmp_path / ".mj_fixture.json.random.tmp"
    orphan.write_text("fixture")
    if sweep == "delete":
        proxy._delete_image_media_job_payload("mj_fixture")
    elif sweep == "purge":
        proxy._purge_prompt_payload_files()
    else:
        proxy._prune_orphaned_media_job_payloads()
    assert not orphan.exists()


def test_new_style_cached_media_stage_is_swept(tmp_path):
    cache = tmp_path / "images"
    cache.mkdir()
    orphan = cache / ("." + "a" * 64 + ".png.random.tmp")
    orphan.write_bytes(b"fixture")
    os.utime(orphan, (1, 1))
    prompt_metadata_migration.strip_cached_assets(cache, tmp_path / "migration.db")
    assert not orphan.exists()


def test_firehose_compaction_sweeps_killed_writer_stage(tmp_path):
    target = tmp_path / "key_activity.jsonl"
    target.write_text("")
    orphan = tmp_path / ".key_activity.jsonl.random.tmp"
    orphan.write_text("fixture")
    firehose_metrics.compact_log(path=target)
    assert not orphan.exists()


def test_private_content_write_accepts_symlinked_ancestor(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    target = alias / "new" / "private" / "content"
    private_io.atomic_write_text(target, "fixture")
    assert target.read_text() == "fixture"
    assert (target.stat().st_mode & 0o777) == 0o600
    assert (target.parent.stat().st_mode & 0o777) == 0o700


@pytest.mark.parametrize("dangling", [False, True])
def test_private_directory_refuses_symlinked_leaf(tmp_path, dangling):
    real = tmp_path / "real"
    if not dangling:
        real.mkdir(mode=0o755)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        private_io.ensure_private_dir(alias)
    if not dangling:
        assert (real.stat().st_mode & 0o777) == 0o755


@pytest.mark.parametrize("error,status,state", [(errno.ENOENT, 410, "missing"),
                                                 (errno.EACCES, 503, "unavailable"),
                                                 (errno.EIO, 503, "unavailable")])
def test_output_content_distinguishes_missing_and_retryable(tmp_path, monkeypatch, error, status, state):
    database = tmp_path / "jobs.db"
    media_jobs.init_db(database)
    monkeypatch.setattr(media_jobs, "DB_PATH", database)
    job = media_jobs.create(media_jobs.MediaJob(app_id="app_test", intent="image.generate",
        request_id="fixture", status="succeeded", progress=100,
        assets=[{"asset_id": "img_" + "f" * 64}]), path=database)
    def unavailable(_):
        raise OSError(error, "fixture")
    monkeypatch.setattr(proxy, "asset_cache_path", unavailable)
    with authenticated():
        response = asyncio.run(proxy.gateway_media_job_content(job.job_id, _StubJsonRequest({})))
    payload = json.loads(response.body)
    assert response.status_code == status
    assert payload["output_state"] == state
    assert payload["error"]["type"] == "server_error"
    if status == 503:
        assert int(response.headers["Retry-After"]) > 0


def test_projection_treats_stat_permission_error_as_retryable(tmp_path, monkeypatch):
    target = tmp_path / "asset"
    def denied(_):
        raise PermissionError(errno.EACCES, "fixture")
    monkeypatch.setattr(Path, "stat", denied)
    result = output_availability.project({"assets": [{"asset_id": "img_fixture"}]}, lambda _: target)
    assert result["output_state"] == "unavailable"
