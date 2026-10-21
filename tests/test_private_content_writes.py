from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sqlite3
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from gateway import (
    prompt_memory, stt_measurement, stt_samples, video_inputs,
    video_job_records, voice_audio,
)
from gateway.stt_samples import Licence, Rights, SampleSpec, Source
from tests.support.fake_upstream import HandlerTransport


@pytest.fixture
def permissive_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 160)


def test_stt_request_spool_ancestors_are_private(tmp_path, permissive_umask):
    from gateway import stt_chunking
    from tests.test_engine_protocol_adapter import _stt_execution

    spool = tmp_path / "spool" / "nested"
    workspace = stt_chunking.create_request_workspace(_stt_execution(spool_dir=spool))
    try:
        assert mode(workspace) == mode(spool) == mode(spool.parent) == 0o700
    finally:
        stt_chunking.remove_request_workspace(workspace)


def test_measurement_database_and_workspace_lock_are_private(tmp_path, monkeypatch, permissive_umask):
    class Store:
        path = tmp_path / "data" / "admin.db"

    runner = stt_measurement.Measurements(Store(), None)
    monkeypatch.setattr(runner, "_scratch_directory", lambda: tmp_path / "scratch")
    with runner._db():
        pass
    lock = runner.path.parent / "stt-measurement-work.lock"
    assert mode(runner.path.parent) == 0o700
    assert mode(runner.path) == mode(lock) == 0o600


def test_measurement_does_not_change_existing_database_mode(tmp_path, monkeypatch, permissive_umask):
    class Store:
        path = tmp_path / "data" / "admin.db"

    runner = stt_measurement.Measurements(Store(), None)
    runner.path.parent.mkdir()
    with sqlite3.connect(runner.path):
        pass
    os.chmod(runner.path, 0o644)
    monkeypatch.setattr(runner, "_scratch_directory", lambda: tmp_path / "scratch")
    with runner._db():
        pass
    assert mode(runner.path) == 0o644


def test_measurement_pcm_scratch_is_private(tmp_path, monkeypatch, permissive_umask):
    class Store:
        path = tmp_path / "data" / "admin.db"
        record = {"id": "speech", "capabilities": ["audio.transcriptions"]}

        def get_service_registry_snapshot(self):
            return {"services": {"speech": self.record}}, 1

    async def transcribe(*_args):
        return "recognized words"

    runner = stt_measurement.Measurements(Store(), transcribe)
    monkeypatch.setattr(runner, "_scratch_directory", lambda: tmp_path / "scratch")
    audio = tmp_path / "input.wav"
    write_wav(audio)
    record = Store.record
    job = {"id": "job", "service_id": "speech", "fingerprint": stt_measurement.fingerprint(record), "model": "m", "completed": 0}
    seen = {}

    def frame_reader(path):
        seen["path"] = path
        seen["file_mode"] = mode(path)
        seen["dir_mode"] = mode(path.parent)
        return "quietest-frame"

    monkeypatch.setattr(stt_measurement.stt_chunking, "quietest_frame_reader", frame_reader)
    monkeypatch.setattr(stt_measurement.stt_chunking, "plan_quietest_frame_segments", lambda **_kwargs: [(0.0, 0.01)])
    asyncio.run(runner._transcription(None, job, audio, {
        "enabled": True, "target_seconds": 10, "search_seconds": 1, "max_segment_seconds": 10,
    }))
    assert seen["file_mode"] == 0o600
    assert seen["dir_mode"] == 0o700
    assert not seen["path"].exists()


def test_measurement_wave_slice_creates_private_output(tmp_path, permissive_umask):
    source = tmp_path / "source.wav"
    write_wav(source)
    target = tmp_path / "slices" / "piece.wav"
    with wave.open(str(source), "rb") as audio:
        stt_measurement._wave_slice(audio, target, 0, 0.005)
    assert mode(target.parent) == 0o700
    assert mode(target) == 0o600


def test_measurement_prepare_writes_private_audio_and_reference(tmp_path, monkeypatch, permissive_umask):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "prepared.wav"
    write_wav(source)
    os.chmod(source, 0o600)
    monkeypatch.setattr(stt_measurement, "_arm_parent_death", lambda _pid: None)
    monkeypatch.setattr(stt_measurement.sys, "argv", [
        "gateway.stt_measurement", "prepare", "sample", str(workspace), "1024", str(os.getpid()),
    ])
    monkeypatch.setattr(stt_samples, "load_manifest", lambda: [SimpleNamespace(id="sample")])
    monkeypatch.setattr(stt_samples, "fetch_sample", lambda *_args, **_kwargs: SimpleNamespace(
        audio_path=source, words=[("word", 0.0, 0.1)],
    ))
    stt_measurement._prepare_main()
    assert mode(workspace) == 0o700
    assert mode(workspace / "audio.wav") == mode(workspace / "reference.json") == 0o600


def test_voice_decode_source_is_private_even_with_permissive_umask(monkeypatch, permissive_umask):
    seen = {}

    def fake_run(command, **_kwargs):
        source = Path(command[command.index("-i") + 1])
        seen["file_mode"] = mode(source)
        seen["dir_mode"] = mode(source.parent)
        return SimpleNamespace(returncode=0, stdout=b"\x00\x00")

    monkeypatch.setattr(voice_audio.subprocess, "run", fake_run)
    voice_audio.decode_pcm(b"upload", max_seconds=1)
    assert seen == {"file_mode": 0o600, "dir_mode": 0o700}


def test_video_probe_source_and_workspace_are_private(monkeypatch, tmp_path, permissive_umask):
    seen = {}
    monkeypatch.setattr(video_inputs.private_tmp, "inside", lambda _path: True)
    monkeypatch.setattr(video_inputs.shutil, "which", lambda _name: "ffprobe")

    def fake_run(command, **_kwargs):
        source = Path(command[-1])
        seen["file_mode"] = mode(source)
        seen["temp_dir_mode"] = mode(source.parent)
        if "-count_frames" in command:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [{
                "avg_frame_rate": "24/1", "nb_read_frames": "24", "duration": "1", "width": 32, "height": 32,
            }]}).encode())
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(video_inputs.subprocess, "run", fake_run)
    result = video_inputs.probe_video(b"video", workdir=tmp_path / "private-work")
    assert result is not None
    assert mode(tmp_path / "private-work") == 0o700
    assert seen == {"file_mode": 0o600, "temp_dir_mode": 0o700}


def test_stt_sample_download_and_conversion_outputs_are_private(monkeypatch, tmp_path, permissive_umask):
    content = b"public sample fixture"
    source = Source(
        name="audio", url="https://samples.test/audio", sha256=hashlib.sha256(content).hexdigest(),
        bytes=len(content), audio_rights=Rights("stated", "fixture"),
        transcript_rights=Rights("not-applicable", "fixture"), provenance="fixture",
        human_transcribed_evidence="fixture",
    )
    spec = SampleSpec(
        id="sample", corpus="fixture", title="fixture", licence=Licence("test", "", "", ""),
        duration_seconds=1, speakers=1, timing="word", sources=(source,), recipe_kind="ami-words", recipe_params={},
    )
    client = httpx.Client(transport=HandlerTransport(lambda _request: httpx.Response(200, content=content)))
    cached = stt_samples.fetch_sources(spec, tmp_path / "sample-cache", client_factory=lambda: client)
    cached_file = cached["audio"]
    assert mode(cached_file.parent) == 0o700
    assert mode(cached_file) == 0o600

    input_file = tmp_path / "input.wav"
    write_wav(input_file)
    monkeypatch.setattr(stt_samples.shutil, "which", lambda _name: "ffmpeg")
    seen = {}

    def fake_run(command, **_kwargs):
        output = Path(command[-1])
        seen["stage_mode"] = mode(output)
        seen["dir_mode"] = mode(output.parent)
        write_wav(output)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(stt_samples.subprocess, "run", fake_run)
    target = tmp_path / "converted" / "audio.wav"
    stt_samples.convert_audio(input_file, target)
    assert seen == {"stage_mode": 0o600, "dir_mode": 0o700}
    assert mode(target) == 0o600


def test_chunked_stt_upload_spool_is_private(monkeypatch, tmp_path, permissive_umask, fake_upstreams):
    from gateway.engine_protocol.chunked_stt import ChunkedSttAdapter, SttUpload
    from tests.test_engine_protocol_adapter import _stt_execution

    workspace = tmp_path / "spool" / "request"
    workspace.mkdir(parents=True)
    seen = {}

    class Lease:
        workspace = None

        @staticmethod
        def release():
            pass

    def reply(_request):
        path = workspace / "upload-0.part"
        seen["file_mode"] = mode(path)
        seen["dir_mode"] = mode(path.parent)
        return httpx.Response(200, json={"text": "ok"})

    fake_upstreams.router.add_transport("unused", HandlerTransport(reply))

    monkeypatch.setattr("gateway.engine_protocol.chunked_stt.stt_chunking.uncut_upload_needs_decode", lambda _config: True)
    monkeypatch.setattr("gateway.engine_protocol.chunked_stt.stt_chunking.GLOBAL_STT_RESOURCE_GATE.acquire", lambda **_kwargs: Lease())
    monkeypatch.setattr("gateway.engine_protocol.chunked_stt.stt_chunking.create_request_workspace", lambda _config: workspace)
    monkeypatch.setattr("gateway.engine_protocol.chunked_stt.stt_chunking.guard_uncut_uploads", lambda *_args, **_kwargs: asyncio.sleep(0))
    monkeypatch.setattr("gateway.engine_protocol.chunked_stt.stt_chunking.remove_request_workspace", lambda _path: None)
    result = SttUpload(
        op="stt.transcribe", body={}, parts={"file": ("clip.wav", b"private audio", "audio/wav")},
        execution=_stt_execution(spool_dir=str(workspace.parent)),
    )
    asyncio.run(ChunkedSttAdapter._post_once("http://unused", httpx.Timeout(1), result))
    assert seen == {"file_mode": 0o600, "dir_mode": 0o700}


def test_chunked_stt_ffmpeg_output_exists_private_before_execution(monkeypatch, tmp_path, permissive_umask):
    from gateway import stt_chunking
    from gateway.engine_protocol.chunked_stt import _RunWire
    from tests.test_engine_protocol_adapter import _stt_execution

    spool = tmp_path / "spool"
    workspace = spool / "request"
    workspace.mkdir(parents=True)
    source = workspace / "upload.bin"
    source.write_bytes(b"audio")
    upload = SimpleNamespace(file=source.open("rb"))
    execution = _stt_execution(spool_dir=str(spool))
    cleanup = stt_chunking.SttCleanupOwner()
    run = SimpleNamespace(
        execution=execution, workspace=workspace, cleanup=cleanup, resource_lease=None,
        cancellation_waiters=(), deadline_at=9999999999, on_cancel=lambda: None,
    )
    seen = {}

    async def fake_command(command, **_kwargs):
        output = Path(command[-1])
        seen["exists"] = output.exists()
        seen["mode"] = mode(output)
        write_wav(output)

    monkeypatch.setattr(stt_chunking, "run_media_command", fake_command)
    result = asyncio.run(_RunWire("test", None, "unused", run).prepared_wav(upload))
    upload.file.close()
    assert seen == {"exists": True, "mode": 0o600}
    assert mode(result) == 0o600


def test_prompt_memory_db_fallback_and_markers_are_private(tmp_path, permissive_umask, monkeypatch):
    db = tmp_path / "prompt-state" / "nested" / "prompts.db"
    conn = prompt_memory._connect(db)
    conn.close()
    assert mode(db.parent) == mode(db.parent.parent) == 0o700
    assert mode(db) == 0o600

    folder = tmp_path / "media"
    folder.mkdir()
    replace_path = folder / prompt_memory.MEDIA_FOLDER_MARKER
    original_replace = os.replace
    scratch_modes = []

    def inspect_replace(source, target):
        scratch_modes.append(mode(Path(source)))
        original_replace(source, target)

    monkeypatch.setattr(prompt_memory.os, "replace", inspect_replace)
    prompt_memory._write_media_marker(folder, replace=True)
    assert scratch_modes == [0o600]
    assert mode(folder) == 0o700
    assert mode(replace_path) == 0o600

    adopt = tmp_path / "adopt"
    adopt.mkdir()
    prompt_memory._write_media_marker(adopt, replace=False)
    assert mode(adopt) == 0o700
    assert mode(adopt / prompt_memory.MEDIA_FOLDER_MARKER) == 0o600


def test_video_job_record_fallback_precreates_private_database(tmp_path, permissive_umask):
    db = tmp_path / "video-state" / "nested" / "jobs.db"
    conn = video_job_records._connect(db)
    conn.close()
    assert mode(db.parent) == mode(db.parent.parent) == 0o700
    assert mode(db) == 0o600
