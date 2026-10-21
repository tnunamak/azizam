"""Request content is private even when the caller has a permissive umask."""
import json
from pathlib import Path
from types import SimpleNamespace

import proxy
from gateway import admin_store, media_jobs
from gateway import local_owner, secret_store
from tests.test_private_modes import mode, permissive_umask  # noqa: F401
from tests.test_prompt_memory_lifecycle import request


def test_prompt_execution_spool_replaces_permissive_staging_privately(tmp_path, monkeypatch, permissive_umask):
    admin = tmp_path / "admin.db"
    admin_store.init_db(admin)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    spool = tmp_path / "cache" / "payloads"
    spool.mkdir(parents=True)
    stale = spool / "mj_private.json.tmp"
    stale.write_text("old")
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", spool)
    proxy._persist_image_media_job_payload("mj_private", body={"prompt": "fixture-private"},
                                           request=request(), auth_context=proxy.NO_AUTH_CONTEXT)
    path = spool / "mj_private.json"
    assert json.loads(path.read_text())["body"]["prompt"] == "fixture-private"
    assert mode(spool) == 0o700 and mode(path) == 0o600


def test_prompt_purge_queue_creates_private_identity_state(tmp_path, monkeypatch, permissive_umask):
    monkeypatch.setattr(media_jobs, "DB_PATH", tmp_path / "data" / "jobs.db")
    with proxy._prompt_purge_queue():
        pass
    path = proxy._prompt_migration_state_path()
    assert mode(path) == 0o600 and mode(path.parent) == 0o700


def test_peak_normalization_files_are_private_before_ffmpeg(tmp_path, monkeypatch, permissive_umask):
    observed = []

    def run(command, **_kwargs):
        source = Path(command[command.index("-i") + 1])
        observed.append(mode(source))
        if "volumedetect" in command:
            return SimpleNamespace(returncode=0, stderr="max_volume: -20.0 dB")
        target = Path(command[-1])
        observed.append(mode(target))
        target.write_bytes(b"normalized")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(proxy.subprocess, "run", run)
    result = proxy._peak_normalize_audio(b"fixture audio", "audio/wav")
    assert result == b"normalized"
    assert observed == [0o600, 0o600, 0o600]


def test_stt_replay_creates_a_private_spool_directory(tmp_path, permissive_umask):
    body = proxy._ReplayableBody(SimpleNamespace(scope={}, receive=None), tmp_path / "replay", 32, None)
    try:
        assert mode(tmp_path / "replay") == 0o700
    finally:
        body._file.close()


def test_standalone_secret_schema_and_key_create_private_identity_state(tmp_path, permissive_umask):
    database = tmp_path / "schema" / "admin.db"
    secret_store.ensure_provider_secret_schema(database)
    assert mode(database) == 0o600 and mode(database.parent) == 0o700
    key = tmp_path / "keys" / "nested" / "provider.key"
    secret_store.load_or_create_secret_key(key)
    assert mode(key) == 0o600 and mode(key.parent) == mode(key.parent.parent) == 0o700


def test_browser_signing_key_creates_private_parent_directories(tmp_path, permissive_umask):
    path = tmp_path / "identity" / "nested" / "session.key"
    local_owner.load_or_create_signing_key(path)
    assert mode(path) == 0o600 and mode(path.parent) == mode(path.parent.parent) == 0o700
