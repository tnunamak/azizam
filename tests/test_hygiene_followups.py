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


@pytest.mark.parametrize("sweep", ["delete", "purge", "prune", "prune_missing_history"])
def test_new_style_job_stages_are_swept(tmp_path, monkeypatch, sweep):
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path)
    monkeypatch.setattr(media_jobs, "get", lambda *a, **k: None if sweep == "prune_missing_history" else {"status": "succeeded"})
    orphan = tmp_path / ".mj_fixture.json.random.tmp"
    orphan.write_text("fixture")
    os.utime(orphan, (1, 1))
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
    os.utime(orphan, (1, 1))
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
                                                 (errno.ENOTDIR, 410, "missing"),
                                                 (errno.ENAMETOOLONG, 410, "missing"),
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


def test_stored_identity_extras_remove_prompt_echoes_only():
    from tests.test_identity_jobs import result
    from gateway import identity_api, identity_scene
    scene = result()
    scene.prompt_used = "PRIVATE_PROMPT_FIXTURE"
    scene.people = [{"label": "PRIVATE_PROMPT_FIXTURE", "photo": 0, "reason": "age-match"}]
    scene.warnings = [{"code": "profile_result", "message": "echo PRIVATE_PROMPT_FIXTURE"},
                      {"code": "pose_rewritten", "param": "prompt", "message": "Choose a clearer pose."}]
    stored = identity_api.stored_scene_extras(scene, identity_scene.SceneRequest(photos=[], prompt="PRIVATE_PROMPT_FIXTURE"))
    assert "PRIVATE_PROMPT_FIXTURE" not in json.dumps(stored)
    assert stored["people"] == [{"person_index": 0, "photo": 0, "reason": "age-match"}]
    assert stored["warnings"][1]["message"] == "Choose a clearer pose."
    assert stored["likeness"] == scene.report
    assert stored["refinements"] == [{"person": 1, "accepted": True}]


def test_stored_identity_labels_use_indexes_without_changing_maker_response():
    from tests.test_identity_jobs import result
    from gateway import identity_api, identity_scene
    scene = result()
    label = "PRIVATE_LABEL_FIXTURE"
    scene.people = [{"label": label, "photo": 0}]
    scene.report["people"] = [{"label": label, "match": {"score": 0.8}}]
    scene.attempts = [{"scores": {label: 0.8}}]
    scene.refinements = [{"person": label, "others_before": {label: 0.7}, "others_after": {label: 0.8}}]
    scene.warnings = [{"param": label, "message": f"photo 1 was not used for {label}"}]
    immediate = json.dumps(identity_api.scene_extras(scene))
    stored = identity_api.stored_scene_extras(scene, identity_scene.SceneRequest(photos=[], prompt="scene"))
    assert label not in json.dumps(stored)
    assert stored["attempts"][0]["scores"] == {"0": 0.8}
    assert stored["refinements"][0]["person"] == 0
    assert stored["refinements"][0]["others_before"] == {"0": 0.7}
    assert stored["refinements"][0]["others_after"] == {"0": 0.8}
    assert stored["likeness"]["people"][0]["person_index"] == 0
    assert json.dumps(identity_api.scene_extras(scene)) == immediate
    assert label in immediate


@pytest.mark.parametrize("short", ["a", "in", "the"])
def test_short_identity_descriptions_do_not_erase_warnings(short):
    from tests.test_identity_jobs import result
    from gateway import identity_api, identity_scene
    scene = result()
    scene.warnings = [{"code": "pose", "message": "Choose another angle in the image."}]
    req = identity_scene.SceneRequest(photos=[], prompt="scene", people=[identity_scene.PersonSpec(description=short)])
    assert identity_api.stored_scene_extras(scene, req)["warnings"] == scene.warnings


@pytest.mark.parametrize("sweep", ["delete", "purge", "prune"])
def test_young_payload_stage_survives_sweep(tmp_path, monkeypatch, sweep):
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path)
    monkeypatch.setattr(media_jobs, "get", lambda *a, **k: {"status": "succeeded"})
    stage = tmp_path / ".mj_fixture.json.random.tmp"
    stage.write_text("fixture")
    if sweep == "delete":
        proxy._delete_image_media_job_payload("mj_fixture")
    elif sweep == "purge":
        proxy._purge_prompt_payload_files()
    else:
        proxy._prune_orphaned_media_job_payloads()
    assert stage.exists()


def test_young_compaction_stage_survives_sweep(tmp_path, capsys):
    target = tmp_path / "key_activity.jsonl"
    target.write_text("")
    stage = tmp_path / ".key_activity.jsonl.random.tmp"
    stage.write_text("fixture")
    firehose_metrics.compact_log(path=target)
    assert stage.exists()
    assert " startup_replay_skipped=" in capsys.readouterr().out


def test_compaction_log_names_startup_replay_count(tmp_path, capsys):
    firehose_metrics.compact_log(path=tmp_path / "absent.jsonl")
    log = capsys.readouterr().out
    assert " startup_replay_skipped=" in log
    assert " replay_skipped=" not in log


def test_metadata_stage_killed_mid_replace_is_aged_then_swept(tmp_path, monkeypatch):
    import signal
    import subprocess
    import sys
    child = subprocess.run([sys.executable, "-c", """
import os, signal, sys
from pathlib import Path
import proxy
proxy.IMAGE_CACHE_DIR = Path(sys.argv[1])
def killed_replace(*args):
    os.kill(os.getpid(), signal.SIGKILL)
os.replace = killed_replace
proxy._write_asset_metadata('img_' + 'a' * 64, {'fixture': True})
""", str(tmp_path)], capture_output=True, timeout=30)
    assert child.returncode == -signal.SIGKILL
    stages = list(tmp_path.glob(".*.meta.json.*.tmp"))
    assert len(stages) == 1
    prompt_metadata_migration.strip_cached_assets(tmp_path, tmp_path / "migration.db")
    assert stages[0].exists()
    os.utime(stages[0], (1, 1))
    prompt_metadata_migration.strip_cached_assets(tmp_path, tmp_path / "migration.db")
    assert not stages[0].exists()
